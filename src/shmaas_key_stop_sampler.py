from __future__ import annotations

import argparse
import json
import re
import time
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

BASE = "https://api.shmaas.net"
CST = timezone(timedelta(hours=8))
HEADERS = {"Content-Type": "application/json", "X-Saic-CityCode": "310100", "User-Agent": "shanghai-bus-operations/1.0"}
SERVICE_HINT_KEYWORDS = ("终到", "终点", "目的地", "区间", "短线", "短途", "折返")
SERVICE_HINT_KEY_PARTS = ("destination", "terminal", "endstop", "end_stop", "short", "section", "service_type", "servicetype", "trip_type", "triptype")
SAMPLE_STRIDE = 5
FIXED_BACKBONE_ROUTE = "浦东35路"
FIXED_BACKBONE_STRIDE = 4
FOLLOWUP_OFFSETS = (2, 4, 6)
MAX_WATCH_VEHICLES = 5


def extract_service_hints(payload) -> list[dict]:
    hints = []
    def visit(value, path=""):
        if isinstance(value, dict):
            for key, item in value.items(): visit(item, f"{path}.{key}" if path else str(key))
            return
        if isinstance(value, list):
            for idx, item in enumerate(value): visit(item, f"{path}[{idx}]")
            return
        if value is None: return
        text = str(value).strip(); lowered_path = path.lower().replace("-", "_")
        if text and (any(part in lowered_path for part in SERVICE_HINT_KEY_PARTS) or any(keyword in path or keyword in text for keyword in SERVICE_HINT_KEYWORDS)):
            hints.append({"field": path, "value": text})
    visit(payload)
    seen = set(); out = []
    for hint in hints:
        marker = (hint["field"], hint["value"])
        if marker not in seen: seen.add(marker); out.append(hint)
    return out


def normalize_plate(value):
    if not value: return None
    return re.sub(r"(无障碍|低地板|新能源|空调)$", "", str(value).strip()).strip()


def sampling_phase(now: datetime) -> int:
    return ((now.hour * 60 + now.minute) // 10) % SAMPLE_STRIDE


def sample_indices(stop_count: int, phase: int, fixed: bool = False) -> list[int]:
    if stop_count <= 0: return []
    indices = set(range(0 if fixed else phase, stop_count, FIXED_BACKBONE_STRIDE if fixed else SAMPLE_STRIDE))
    indices.add(0); indices.add(stop_count - 1)
    return sorted(indices)


def post(path: str, payload: dict, timeout: int = 12, retries: int = 2):
    last = None
    for attempt in range(retries + 1):
        req = urllib.request.Request(BASE + path, data=json.dumps(payload, ensure_ascii=False).encode("utf-8"), headers=HEADERS, method="POST")
        started = time.time()
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp: data = json.loads(resp.read().decode("utf-8"))
            if data.get("errCode") != 0: raise RuntimeError(f"API errCode={data.get('errCode')} errMsg={data.get('errMsg')}")
            return data.get("data") or {}, round((time.time() - started) * 1000)
        except Exception as exc:
            last = exc
            if attempt < retries: time.sleep(1.5 * (2 ** attempt))
    raise last


def observation_max_seq(snapshot: dict, plate: str, direction: int):
    for vehicle in snapshot.get("vehicles") or []:
        if vehicle.get("plate") != plate: continue
        seqs = [int(o["stop_seq"]) for o in vehicle.get("observations") or [] if o.get("direction") == direction and o.get("role") in ("current", "next") and o.get("stop_seq") is not None]
        return max(seqs) if seqs else None
    return None


def plates_by_direction(snapshot: dict) -> dict[int, set[str]]:
    result = {0: set(), 1: set()}
    for vehicle in snapshot.get("vehicles") or []:
        plate = vehicle.get("plate")
        if not plate: continue
        for obs in vehicle.get("observations") or []:
            if obs.get("role") not in ("current", "next"): continue
            direction = obs.get("direction")
            if direction in result: result[direction].add(plate)
    return result


def build_watch_candidates(history: list[dict], current: dict, stop_counts: dict[int, int]) -> list[dict]:
    if len(history) < 2: return []
    current_plates = plates_by_direction(current); latest = history[-1]; previous = history[-2]; candidates = []
    for vehicle in latest.get("vehicles") or []:
        plate = vehicle.get("plate")
        if not plate: continue
        for direction in (0, 1):
            seq_latest = observation_max_seq(latest, plate, direction); seq_previous = observation_max_seq(previous, plate, direction)
            if seq_latest is None or seq_previous is None: continue
            count = stop_counts.get(direction) or 0
            if count < 2 or seq_latest >= max(2, int(count * 0.85)): continue
            if seq_latest + 2 < seq_previous: continue
            if plate in current_plates[direction] or plate in current_plates[1 - direction]: continue
            candidates.append({"plate": plate, "source_direction": direction, "last_source_seq": seq_latest, "previous_source_seq": seq_previous})
    candidates.sort(key=lambda x: x["last_source_seq"], reverse=True)
    return candidates[:MAX_WATCH_VEHICLES]


def collect(route: str, history: list[dict] | None = None) -> dict:
    history = history or []; now = datetime.now(CST); phase = sampling_phase(now); fixed_backbone = route == FIXED_BACKBONE_ROUTE
    out = {"sample_time_cst": now.isoformat(timespec="seconds"), "date_cst": now.date().isoformat(), "source": BASE, "route": route,
           "strategy": "fixed_backbone_plus_reverse_followup" if fixed_backbone else "rotating_every_5th_stop_plus_terminals",
           "sampling_phase": None if fixed_backbone else phase, "sampling_stride": FIXED_BACKBONE_STRIDE if fixed_backbone else SAMPLE_STRIDE,
           "success": False, "requests": [], "directions": [], "vehicles": {}, "errors": [], "tracking_errors": [], "watch_candidates": [], "tracking_probes": []}
    search, ms = post("/traffic/v2/querytrafficline", {"keywords": route, "type": 0, "pageNo": 1, "pageSize": 20}); out["requests"].append({"endpoint": "querytrafficline", "elapsed_ms": ms})
    candidates = []
    for item in search.get("trafficStop") or []:
        candidates.extend(x for x in (item.get("stopLineInfo") or []) if x.get("lineName") == route)
        info = item.get("lineInfo") or {}
        if info.get("lineName") == route: candidates.append(info)
    if not candidates: raise RuntimeError(f"{route} not found")
    line_id = str(candidates[0]["lineId"]); out["line_id"] = line_id

    def add_vehicle(raw_plate, direction, role, stop, fields):
        plate = normalize_plate(raw_plate)
        if not plate: return
        rec = out["vehicles"].setdefault(plate, {"plate": plate, "raw_values": [], "directions": [], "roles": [], "observations": [], "dispatch_times": []})
        if str(raw_plate) not in rec["raw_values"]: rec["raw_values"].append(str(raw_plate))
        if direction not in rec["directions"]: rec["directions"].append(direction)
        if role not in rec["roles"]: rec["roles"].append(role)
        rec["observations"].append({"direction": direction, "role": role, "stop_seq": stop["seq"], "stop_id": str(stop["stopId"]), "stop_name": stop["stopName"], **fields})
        dispatch_time = fields.get("dispatch_time")
        if dispatch_time and dispatch_time not in rec["dispatch_times"]: rec["dispatch_times"].append(dispatch_time)

    stops_by_direction = {}; sampled_indices_by_direction = {}
    def sample_stop(direction: int, stop: dict, probe_type: str, fatal: bool):
        try:
            eta, ms = post("/traffic/v1/getbusstoparrivedetails", {"lineName": route, "stopName": stop["stopName"], "stopId": str(stop["stopId"]), "direction": direction})
            out["requests"].append({"endpoint": "getbusstoparrivedetails", "direction": direction, "stop_id": str(stop["stopId"]), "probe_type": probe_type, "elapsed_ms": ms})
            arrive = eta.get("stopArriveInfo") or {}; schedule = eta.get("dispatchCarSchedule") or {}; common = {"probe_type": probe_type}
            add_vehicle(arrive.get("currentLicensePlate"), direction, "current", stop, {"arrive_time": arrive.get("currentBusArriveTime"), "distance": arrive.get("currentBusDistance"), "remaining_stops": arrive.get("currentBusStopCount"), "service_hints": extract_service_hints(arrive), **common})
            add_vehicle(arrive.get("nextLicensePlate"), direction, "next", stop, {"arrive_time": arrive.get("nextBusArriveTime"), "distance": arrive.get("nextBusDistance"), "remaining_stops": arrive.get("nextBusStopCount"), "service_hints": extract_service_hints(arrive), **common})
            for car in schedule.get("dispatchCars") or []:
                add_vehicle(car.get("vehicle"), direction, "dispatch", stop, {"dispatch_time": car.get("time"), "dispatch_countdown": car.get("countdown"), "service_hints": extract_service_hints(car), **common})
            return True, None
        except Exception as exc:
            err = {"direction": direction, "stop_id": str(stop.get("stopId")), "stop_name": stop.get("stopName"), "probe_type": probe_type, "error": repr(exc)}
            (out["errors"] if fatal else out["tracking_errors"]).append(err); return False, err
        finally: time.sleep(0.12)

    for direction in (0, 1):
        line, ms = post("/traffic/v1/querybusline", {"lineId": line_id, "lineName": route, "direction": direction}); out["requests"].append({"endpoint": "querybusline", "direction": direction, "elapsed_ms": ms})
        bus_line = line.get("busLine") or {}; stops = [s for s in (bus_line.get("stop") or []) if s.get("stopId") and s.get("stopName")]
        for seq, stop in enumerate(stops, 1): stop["seq"] = seq
        stops_by_direction[direction] = stops; indices = sample_indices(len(stops), phase, fixed=fixed_backbone); sampled_indices_by_direction[direction] = set(indices)
        d_out = {"direction": direction, "start_stop": bus_line.get("upStartStop"), "end_stop": bus_line.get("upEndStop"), "stop_count": len(stops), "sampled_stop_count": len(indices), "sampling_phase": None if fixed_backbone else phase, "sampled_stops": []}
        for idx in indices:
            stop = stops[idx]; kind = "backbone" if fixed_backbone else "rotating"; ok, err = sample_stop(direction, stop, kind, True)
            d_out["sampled_stops"].append({"seq": stop["seq"], "stop_id": str(stop["stopId"]), "stop_name": stop["stopName"], "probe_type": kind, "ok": True} if ok else {**err, "seq": stop["seq"], "ok": False})
        out["directions"].append(d_out)

    if fixed_backbone:
        stop_counts = {d: len(stops_by_direction[d]) for d in (0, 1)}; watches = build_watch_candidates(history, out, stop_counts); out["watch_candidates"] = watches
        for watch in watches:
            sd = watch["source_direction"]; rd = 1 - sd; source_count = stop_counts[sd]; reverse_stops = stops_by_direction[rd]
            if source_count < 2 or len(reverse_stops) < 2: continue
            p = (watch["last_source_seq"] - 1) / (source_count - 1); mirror_seq = round((1 - p) * (len(reverse_stops) - 1)) + 1
            for offset in FOLLOWUP_OFFSETS:
                target_seq = min(len(reverse_stops), mirror_seq + offset); idx = target_seq - 1
                probe = {"plate": watch["plate"], "source_direction": sd, "last_source_seq": watch["last_source_seq"], "reverse_direction": rd, "mirror_seq": mirror_seq, "target_seq": target_seq, "offset": offset}
                if idx in sampled_indices_by_direction[rd]: probe["reused_backbone"] = True
                else:
                    ok, err = sample_stop(rd, reverse_stops[idx], "reverse_followup", False); probe["ok"] = ok
                    if err: probe["error"] = err["error"]
                out["tracking_probes"].append(probe)
                reverse_seq = observation_max_seq({"vehicles": list(out["vehicles"].values())}, watch["plate"], rd)
                if reverse_seq is not None and reverse_seq >= mirror_seq: probe["matched_reverse_plate"] = True; break

    vehicles = list(out["vehicles"].values())
    for vehicle in vehicles: vehicle["directions"].sort(); vehicle["roles"].sort(); vehicle["dispatch_times"].sort()
    out["vehicles"] = sorted(vehicles, key=lambda x: x["plate"]); out["unique_vehicle_count"] = len(vehicles); out["request_count"] = len(out["requests"]); out["failed_sample_count"] = len(out["errors"])
    timings = [r["elapsed_ms"] for r in out["requests"] if r.get("elapsed_ms") is not None]; out["avg_request_elapsed_ms"] = round(sum(timings) / len(timings)) if timings else None; out["max_request_elapsed_ms"] = max(timings) if timings else None; out["success"] = not out["errors"]
    return out


def load_history(out_dir: Path, route: str, date_cst: str, limit: int = 3) -> list[dict]:
    path = out_dir / f"{date_cst}-{route.replace('/', '_')}.jsonl"
    if not path.exists(): return []
    rows = []
    for line in path.read_text(encoding="utf-8").splitlines()[-limit:]:
        try: rec = json.loads(line)
        except json.JSONDecodeError: continue
        if rec.get("success") and rec.get("route") == route: rows.append(rec)
    return rows


def main() -> int:
    parser = argparse.ArgumentParser(); parser.add_argument("--route", required=True); parser.add_argument("--output-dir", default="data/shmaas"); args = parser.parse_args()
    out_dir = Path(args.output_dir); out_dir.mkdir(parents=True, exist_ok=True); today = datetime.now(CST).date().isoformat(); history = load_history(out_dir, args.route, today); result = collect(args.route, history=history)
    safe_route = args.route.replace("/", "_"); jsonl_path = out_dir / f"{result['date_cst']}-{safe_route}.jsonl"; latest_path = out_dir / f"latest-{safe_route}.json"
    line = json.dumps(result, ensure_ascii=False, separators=(",", ":"))
    with jsonl_path.open("a", encoding="utf-8") as f: f.write(line + "\n")
    latest_path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    phase_text = result["sampling_phase"] if result["sampling_phase"] is not None else "fixed"
    print(f"route={args.route} time={result['sample_time_cst']} phase={phase_text} vehicles={result['unique_vehicle_count']} requests={result['request_count']} watches={len(result.get('watch_candidates') or [])} followups={len(result.get('tracking_probes') or [])} failures={result['failed_sample_count']}")
    if result["errors"]: raise RuntimeError(f"{len(result['errors'])} key-stop samples failed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
