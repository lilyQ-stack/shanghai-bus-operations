from __future__ import annotations

import re
from collections import defaultdict

import analyze_daily as core


def parse_eta_minutes(value):
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip()
    if not text:
        return None
    if any(token in text for token in ("即将到站", "即将进站", "已到站", "到站")):
        return 0.0
    try:
        return float(text)
    except ValueError:
        pass
    match = re.search(r"(\d+(?:\.\d+)?)\s*分钟", text)
    if match:
        return float(match.group(1))
    return None


def parse_distance(value):
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip().replace(",", "")
    if not text:
        return None
    try:
        return float(text)
    except ValueError:
        pass
    match = re.search(r"(\d+(?:\.\d+)?)", text)
    return float(match.group(1)) if match else None


def collect_evidence(snapshots):
    route_info = defaultdict(dict)
    events = defaultdict(list)
    dispatches = defaultdict(set)
    first_seen = {}

    for snap in snapshots:
        route = snap.get("route")
        dt = core.event_time(snap)
        if not route or not dt:
            continue
        sample_min = dt.hour * 60 + dt.minute + dt.second / 60

        for direction in snap.get("directions", []):
            d = direction.get("direction")
            if d is None:
                continue
            route_info[(route, d)] = {
                "start": direction.get("start_stop", ""),
                "end": direction.get("end_stop", ""),
                "stop_count": direction.get("stop_count") or 0,
            }

        for vehicle in snap.get("vehicles", []):
            plate = vehicle.get("plate")
            if not plate:
                continue
            first_seen[(route, plate)] = min(first_seen.get((route, plate), dt), dt)
            for obs in vehicle.get("observations", []):
                d = obs.get("direction")
                if d is None:
                    continue
                role = obs.get("role") or ""
                dispatch = core.parse_hm(obs.get("dispatch_time"))
                if role == "dispatch" and dispatch is not None:
                    dispatches[(route, plate, d)].add(dispatch)

                # Historical sampler records used arrive_time/distance; newer records may
                # also expose normalized eta_min/distance_m. Support both so old days can
                # be re-analysed correctly.
                eta = parse_eta_minutes(obs.get("eta_min", obs.get("arrive_time")))
                distance = parse_distance(obs.get("distance_m", obs.get("distance")))
                seq = obs.get("stop_seq")
                try:
                    seq = int(seq) if seq is not None else None
                except Exception:
                    seq = None

                events[(route, plate, d)].append({
                    "time": sample_min,
                    "dt": dt,
                    "role": role,
                    "stop_seq": seq,
                    "stop_name": obs.get("stop_name", ""),
                    "eta": eta,
                    "distance": distance,
                    "service_hints": obs.get("service_hints") or [],
                })

    for key in events:
        events[key].sort(key=lambda x: x["time"])
    return route_info, events, dispatches, first_seen


def derive_min_full_runtimes(route_info, events, dispatches):
    """Build short-turn baseline only from actual terminal-stop ETA evidence.

    Entering the last 15% of a route is useful for service classification, but it
    is not proof that the vehicle has completed the route. In particular, never
    substitute the sample timestamp for a missing ETA when building the minimum
    full-trip runtime.
    """
    candidates = defaultdict(list)
    for (route, plate, d), deps in dispatches.items():
        terminal = (route_info.get((route, d), {}) or {}).get("end") or ""
        if not terminal:
            continue
        same = events.get((route, plate, d), [])
        dep_list = sorted(deps)
        for i, dep in enumerate(dep_list):
            upper = dep_list[i + 1] if i + 1 < len(dep_list) else dep + 240
            for ev in same:
                if not (dep <= ev["time"] < upper):
                    continue
                if ev.get("stop_name") != terminal:
                    continue
                eta = ev.get("eta")
                if eta is None:
                    continue
                runtime = ev["time"] + eta - dep
                if 30 <= runtime <= 240:
                    candidates[(route, d)].append(runtime)
    return {key: min(values) for key, values in candidates.items() if values}


# Patch the core module before entering its normal CLI/main flow. This keeps the
# export format stable while correcting evidence normalization and full-trip baselines.
core.collect_evidence = collect_evidence
core.derive_min_full_runtimes = derive_min_full_runtimes

if __name__ == "__main__":
    core.main()
