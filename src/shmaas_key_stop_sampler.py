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
HEADERS = {
    "Content-Type": "application/json",
    "X-Saic-CityCode": "310100",
    "User-Agent": "shanghai-bus-operations/1.0",
}

SERVICE_HINT_KEYWORDS = ("终到", "终点", "目的地", "区间", "短线", "短途", "折返")
SERVICE_HINT_KEY_PARTS = (
    "destination", "terminal", "endstop", "end_stop", "short", "section",
    "service_type", "servicetype", "trip_type", "triptype",
)
SAMPLE_STRIDE = 5


def extract_service_hints(payload) -> list[dict]:
    hints: list[dict] = []

    def visit(value, path=""):
        if isinstance(value, dict):
            for key, item in value.items():
                child = f"{path}.{key}" if path else str(key)
                visit(item, child)
            return
        if isinstance(value, list):
            for idx, item in enumerate(value):
                visit(item, f"{path}[{idx}]")
            return
        if value is None:
            return
        text = str(value).strip()
        lowered_path = path.lower().replace("-", "_")
        if text and (
            any(part in lowered_path for part in SERVICE_HINT_KEY_PARTS)
            or any(keyword in path or keyword in text for keyword in SERVICE_HINT_KEYWORDS)
        ):
            hints.append({"field": path, "value": text})

    visit(payload)
    seen = set()
    out = []
    for hint in hints:
        marker = (hint["field"], hint["value"])
        if marker not in seen:
            seen.add(marker)
            out.append(hint)
    return out


def normalize_plate(value):
    if not value:
        return None
    return re.sub(r"(无障碍|低地板|新能源|空调)$", "", str(value).strip()).strip()


def sampling_phase(now: datetime) -> int:
    # Rotate the spatial sample on every 10-minute slot. Five phases cover every
    # stop without increasing the normal number of requests per run.
    slot = (now.hour * 60 + now.minute) // 10
    return slot % SAMPLE_STRIDE


def sample_indices(stop_count: int, phase: int) -> list[int]:
    if stop_count <= 0:
        return []
    indices = set(range(phase, stop_count, SAMPLE_STRIDE))
    # Terminals are always observed so departures/terminal ETA evidence remains
    # continuous while intermediate stops rotate.
    indices.add(0)
    indices.add(stop_count - 1)
    return sorted(indices)


def post(path: str, payload: dict, timeout: int = 12, retries: int = 2):
    last = None
    for attempt in range(retries + 1):
        req = urllib.request.Request(
            BASE + path,
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers=HEADERS,
            method="POST",
        )
        started = time.time()
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                data = json.loads(resp.read().decode("utf-8"))
            if data.get("errCode") != 0:
                raise RuntimeError(f"API errCode={data.get('errCode')} errMsg={data.get('errMsg')}")
            return data.get("data") or {}, round((time.time() - started) * 1000)
        except Exception as exc:
            last = exc
            if attempt < retries:
                time.sleep(1.5 * (2 ** attempt))
    raise last


def collect(route: str) -> dict:
    now = datetime.now(CST)
    phase = sampling_phase(now)
    out = {
        "sample_time_cst": now.isoformat(timespec="seconds"),
        "date_cst": now.date().isoformat(),
        "source": BASE,
        "route": route,
        "strategy": "rotating_every_5th_stop_plus_terminals",
        "sampling_phase": phase,
        "sampling_stride": SAMPLE_STRIDE,
        "success": False,
        "requests": [],
        "directions": [],
        "vehicles": {},
        "errors": [],
    }

    search, ms = post("/traffic/v2/querytrafficline", {
        "keywords": route,
        "type": 0,
        "pageNo": 1,
        "pageSize": 20,
    })
    out["requests"].append({"endpoint": "querytrafficline", "elapsed_ms": ms})

    candidates = []
    for item in search.get("trafficStop") or []:
        candidates.extend(x for x in (item.get("stopLineInfo") or []) if x.get("lineName") == route)
        info = item.get("lineInfo") or {}
        if info.get("lineName") == route:
            candidates.append(info)
    if not candidates:
        raise RuntimeError(f"{route} not found")

    line_id = str(candidates[0]["lineId"])
    out["line_id"] = line_id

    def add_vehicle(raw_plate, direction, role, stop, fields):
        plate = normalize_plate(raw_plate)
        if not plate:
            return
        rec = out["vehicles"].setdefault(plate, {
            "plate": plate,
            "raw_values": [],
            "directions": [],
            "roles": [],
            "observations": [],
            "dispatch_times": [],
        })
        if str(raw_plate) not in rec["raw_values"]:
            rec["raw_values"].append(str(raw_plate))
        if direction not in rec["directions"]:
            rec["directions"].append(direction)
        if role not in rec["roles"]:
            rec["roles"].append(role)
        rec["observations"].append({
            "direction": direction,
            "role": role,
            "stop_seq": stop["seq"],
            "stop_id": str(stop["stopId"]),
            "stop_name": stop["stopName"],
            **fields,
        })
        dispatch_time = fields.get("dispatch_time")
        if dispatch_time and dispatch_time not in rec["dispatch_times"]:
            rec["dispatch_times"].append(dispatch_time)

    for direction in (0, 1):
        line, ms = post("/traffic/v1/querybusline", {
            "lineId": line_id,
            "lineName": route,
            "direction": direction,
        })
        out["requests"].append({"endpoint": "querybusline", "direction": direction, "elapsed_ms": ms})
        bus_line = line.get("busLine") or {}
        stops = [s for s in (bus_line.get("stop") or []) if s.get("stopId") and s.get("stopName")]
        for seq, stop in enumerate(stops, 1):
            stop["seq"] = seq

        indices = sample_indices(len(stops), phase)
        d_out = {
            "direction": direction,
            "start_stop": bus_line.get("upStartStop"),
            "end_stop": bus_line.get("upEndStop"),
            "stop_count": len(stops),
            "sampled_stop_count": len(indices),
            "sampling_phase": phase,
            "sampled_stops": [],
        }

        for idx in indices:
            stop = stops[idx]
            try:
                eta, ms = post("/traffic/v1/getbusstoparrivedetails", {
                    "lineName": route,
                    "stopName": stop["stopName"],
                    "stopId": str(stop["stopId"]),
                    "direction": direction,
                })
                out["requests"].append({
                    "endpoint": "getbusstoparrivedetails",
                    "direction": direction,
                    "stop_id": str(stop["stopId"]),
                    "elapsed_ms": ms,
                })
                arrive = eta.get("stopArriveInfo") or {}
                schedule = eta.get("dispatchCarSchedule") or {}
                add_vehicle(arrive.get("currentLicensePlate"), direction, "current", stop, {
                    "arrive_time": arrive.get("currentBusArriveTime"),
                    "distance": arrive.get("currentBusDistance"),
                    "remaining_stops": arrive.get("currentBusStopCount"),
                    "service_hints": extract_service_hints(arrive),
                })
                add_vehicle(arrive.get("nextLicensePlate"), direction, "next", stop, {
                    "arrive_time": arrive.get("nextBusArriveTime"),
                    "distance": arrive.get("nextBusDistance"),
                    "remaining_stops": arrive.get("nextBusStopCount"),
                    "service_hints": extract_service_hints(arrive),
                })
                for car in schedule.get("dispatchCars") or []:
                    add_vehicle(car.get("vehicle"), direction, "dispatch", stop, {
                        "dispatch_time": car.get("time"),
                        "dispatch_countdown": car.get("countdown"),
                        "service_hints": extract_service_hints(car),
                    })
                d_out["sampled_stops"].append({
                    "seq": stop["seq"],
                    "stop_id": str(stop["stopId"]),
                    "stop_name": stop["stopName"],
                    "ok": True,
                })
            except Exception as exc:
                err = {
                    "direction": direction,
                    "stop_id": str(stop.get("stopId")),
                    "stop_name": stop.get("stopName"),
                    "error": repr(exc),
                }
                out["errors"].append(err)
                d_out["sampled_stops"].append({**err, "seq": stop["seq"], "ok": False})
            time.sleep(0.12)

        out["directions"].append(d_out)

    vehicles = list(out["vehicles"].values())
    for vehicle in vehicles:
        vehicle["directions"].sort()
        vehicle["roles"].sort()
        vehicle["dispatch_times"].sort()
    out["vehicles"] = sorted(vehicles, key=lambda x: x["plate"])
    out["unique_vehicle_count"] = len(vehicles)
    out["request_count"] = len(out["requests"])
    out["failed_sample_count"] = len(out["errors"])
    timings = [r["elapsed_ms"] for r in out["requests"] if r.get("elapsed_ms") is not None]
    out["avg_request_elapsed_ms"] = round(sum(timings) / len(timings)) if timings else None
    out["max_request_elapsed_ms"] = max(timings) if timings else None
    out["success"] = not out["errors"]
    return out


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--route", required=True)
    parser.add_argument("--output-dir", default="data/shmaas")
    args = parser.parse_args()

    result = collect(args.route)
    out_dir = Path(args.output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    safe_route = args.route.replace("/", "_")
    jsonl_path = out_dir / f"{result['date_cst']}-{safe_route}.jsonl"
    latest_path = out_dir / f"latest-{safe_route}.json"

    line = json.dumps(result, ensure_ascii=False, separators=(",", ":"))
    with jsonl_path.open("a", encoding="utf-8") as f:
        f.write(line + "\n")
    latest_path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")

    print(
        f"route={args.route} time={result['sample_time_cst']} phase={result['sampling_phase']} "
        f"vehicles={result['unique_vehicle_count']} requests={result['request_count']} "
        f"failures={result['failed_sample_count']}"
    )
    if result["errors"]:
        raise RuntimeError(f"{len(result['errors'])} key-stop samples failed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
