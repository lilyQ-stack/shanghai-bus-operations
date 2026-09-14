from __future__ import annotations

import argparse
import json
import statistics
from datetime import datetime
from pathlib import Path

import shmaas_key_stop_sampler as api

ROUTE = "浦东35路"
CST = api.CST
MISS_COUNT = 3
START_OFFSET = 3
CONFIRM_GAIN = 5
TERMINAL_GUARD = 0.85


def load_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    rows = []
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            continue
        rows.append(rec)
    return rows


def vehicle_positions(snapshot: dict, plate: str, direction: int) -> list[float]:
    vals = []
    for vehicle in snapshot.get("vehicles") or []:
        if vehicle.get("plate") != plate:
            continue
        for obs in vehicle.get("observations") or []:
            if obs.get("direction") != direction or obs.get("role") not in ("current", "next"):
                continue
            try:
                target = int(obs.get("stop_seq"))
                remaining = int(float(obs.get("remaining_stops")))
            except (TypeError, ValueError):
                continue
            vals.append(float(max(1, target - remaining)))
    return vals


def plates(snapshot: dict) -> set[str]:
    return {
        v.get("plate") for v in snapshot.get("vehicles") or []
        if v.get("plate")
    }


def stop_counts(snapshot: dict) -> dict[int, int]:
    result = {}
    for d in snapshot.get("directions") or []:
        direction = d.get("direction")
        if direction in (0, 1):
            result[direction] = int(d.get("stop_count") or 0)
    return result


def dispatch_before(snapshots: list[dict], plate: str, direction: int, cutoff_dt: datetime):
    cutoff_min = cutoff_dt.hour * 60 + cutoff_dt.minute
    values = set()
    for snap in snapshots:
        try:
            dt = datetime.fromisoformat(snap.get("sample_time_cst"))
        except Exception:
            continue
        if dt > cutoff_dt:
            break
        for vehicle in snap.get("vehicles") or []:
            if vehicle.get("plate") != plate:
                continue
            for obs in vehicle.get("observations") or []:
                if obs.get("direction") != direction or obs.get("role") != "dispatch":
                    continue
                text = obs.get("dispatch_time")
                try:
                    h, m = map(int, str(text).split(":"))
                except Exception:
                    continue
                minute = h * 60 + m
                if minute <= cutoff_min:
                    values.add((minute, f"{h:02d}:{m:02d}"))
    return max(values)[1] if values else None


def find_active_watches(snapshots: list[dict]) -> list[dict]:
    snapshots = [s for s in snapshots if s.get("success") and s.get("route") == ROUTE]
    snapshots.sort(key=lambda s: s.get("sample_time_cst") or "")
    if len(snapshots) < MISS_COUNT + 1:
        return []
    counts = stop_counts(snapshots[-1])
    all_plates = sorted(set().union(*(plates(s) for s in snapshots)))
    watches = []

    for plate in all_plates:
        for direction in (0, 1):
            last_idx = None
            last_seq = None
            for idx, snap in enumerate(snapshots):
                pos = vehicle_positions(snap, plate, direction)
                if pos:
                    med = statistics.median(pos)
                    last_idx, last_seq = idx, med
            if last_idx is None or last_seq is None:
                continue
            if len(snapshots) - last_idx - 1 < MISS_COUNT:
                continue
            # Exactly the new trigger: the first three main samples after the last
            # same-direction observation contain no same-direction movement evidence.
            next_three = snapshots[last_idx + 1:last_idx + 1 + MISS_COUNT]
            if any(vehicle_positions(s, plate, direction) for s in next_three):
                continue
            count = counts.get(direction) or 0
            if count < 2:
                continue
            fraction = (last_seq - 1) / (count - 1)
            if fraction >= TERMINAL_GUARD:
                continue
            try:
                last_dt = datetime.fromisoformat(snapshots[last_idx]["sample_time_cst"])
            except Exception:
                continue
            departure = dispatch_before(snapshots, plate, direction, last_dt)
            watches.append({
                "plate": plate,
                "source_direction": direction,
                "last_source_seq": round(last_seq, 2),
                "last_source_time": last_dt.isoformat(timespec="seconds"),
                "departure": departure,
                "missing_main_samples": len(snapshots) - last_idx - 1,
            })
    return watches


def query_route_stops():
    search, _ = api.post("/traffic/v2/querytrafficline", {"keywords": ROUTE, "type": 0, "pageNo": 1, "pageSize": 20})
    candidates = []
    for item in search.get("trafficStop") or []:
        candidates.extend(x for x in (item.get("stopLineInfo") or []) if x.get("lineName") == ROUTE)
        info = item.get("lineInfo") or {}
        if info.get("lineName") == ROUTE:
            candidates.append(info)
    if not candidates:
        raise RuntimeError(f"{ROUTE} not found")
    line_id = str(candidates[0]["lineId"])
    result = {}
    for direction in (0, 1):
        line, _ = api.post("/traffic/v1/querybusline", {"lineId": line_id, "lineName": ROUTE, "direction": direction})
        bus_line = line.get("busLine") or {}
        stops = [s for s in (bus_line.get("stop") or []) if s.get("stopId") and s.get("stopName")]
        for seq, stop in enumerate(stops, 1):
            stop["seq"] = seq
        result[direction] = stops
    return result


def watch_key(watch: dict) -> str:
    return f"{watch['plate']}|{watch['source_direction']}|{watch.get('departure') or watch['last_source_time']}"


def prior_progress(records: list[dict]) -> dict[str, dict]:
    progress = {}
    for rec in records:
        for item in rec.get("watches") or []:
            key = item.get("watch_key")
            if not key:
                continue
            p = progress.setdefault(key, {"max_reverse_seq": None, "confirmed": False})
            seq = item.get("reverse_seq_est")
            if seq is not None:
                p["max_reverse_seq"] = max(p["max_reverse_seq"] or seq, seq)
            p["confirmed"] = p["confirmed"] or bool(item.get("confirmed"))
    return progress


def query_target(stop: dict, direction: int, plate: str):
    data, _ = api.post("/traffic/v1/getbusstoparrivedetails", {
        "lineName": ROUTE,
        "stopName": stop["stopName"],
        "stopId": str(stop["stopId"]),
        "direction": direction,
    })
    arrive = data.get("stopArriveInfo") or {}
    candidates = [
        ("current", arrive.get("currentLicensePlate"), arrive.get("currentBusStopCount")),
        ("next", arrive.get("nextLicensePlate"), arrive.get("nextBusStopCount")),
    ]
    for role, raw_plate, remaining in candidates:
        if api.normalize_plate(raw_plate) != plate:
            continue
        try:
            remaining = int(float(remaining))
        except (TypeError, ValueError):
            remaining = None
        reverse_seq = max(1, stop["seq"] - remaining) if remaining is not None else None
        return role, reverse_seq
    return None, None


def run(date: str, root: Path):
    source = root / "data" / "shmaas" / f"{date}-{ROUTE}.jsonl"
    track_path = root / "data" / "shmaas" / f"{date}-{ROUTE}-reverse-watch.jsonl"
    snapshots = load_jsonl(source)
    prior = load_jsonl(track_path)
    active = find_active_watches(snapshots)
    progress = prior_progress(prior)
    stops = query_route_stops() if active else None
    now = datetime.now(CST)
    result = {
        "sample_time_cst": now.isoformat(timespec="seconds"),
        "date_cst": date,
        "route": ROUTE,
        "rule_version": "3miss-reverse+3-gain5-v1",
        "watches": [],
    }

    counts = stop_counts(snapshots[-1]) if snapshots else {}
    for watch in active:
        key = watch_key(watch)
        prev = progress.get(key, {})
        if prev.get("confirmed"):
            continue
        sd = watch["source_direction"]
        rd = 1 - sd
        source_count = counts.get(sd) or 0
        reverse_stops = stops[rd]
        if source_count < 2 or len(reverse_stops) < 2:
            continue
        p = (watch["last_source_seq"] - 1) / (source_count - 1)
        mirror_seq = round((1 - p) * (len(reverse_stops) - 1)) + 1
        start_seq = min(len(reverse_stops), mirror_seq + START_OFFSET)
        max_prior = prev.get("max_reverse_seq")
        target_seq = start_seq if max_prior is None else min(len(reverse_stops), max(start_seq, int(max_prior) + START_OFFSET))
        stop = reverse_stops[target_seq - 1]
        role, reverse_seq = query_target(stop, rd, watch["plate"])
        gain = (reverse_seq - mirror_seq) if reverse_seq is not None else None
        confirmed = gain is not None and gain >= CONFIRM_GAIN
        result["watches"].append({
            **watch,
            "watch_key": key,
            "reverse_direction": rd,
            "mirror_seq": mirror_seq,
            "target_seq": target_seq,
            "target_stop": stop["stopName"],
            "matched_role": role,
            "reverse_seq_est": reverse_seq,
            "reverse_gain_stops": round(gain, 2) if gain is not None else None,
            "confirmed": confirmed,
        })

    track_path.parent.mkdir(parents=True, exist_ok=True)
    with track_path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(result, ensure_ascii=False, separators=(",", ":")) + "\n")
    print(json.dumps(result, ensure_ascii=False))
    return result


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--date")
    parser.add_argument("--root", default=str(Path(__file__).resolve().parents[1]))
    args = parser.parse_args()
    root = Path(args.root)
    date = args.date or datetime.now(CST).strftime("%Y-%m-%d")
    run(date, root)


if __name__ == "__main__":
    main()
