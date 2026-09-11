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


def parse_remaining_stops(value):
    if value is None or value == "":
        return None
    try:
        return int(float(value))
    except (TypeError, ValueError):
        match = re.search(r"\d+", str(value))
        return int(match.group(0)) if match else None


def fmt_hm(value):
    if value is None:
        return ""
    value = int(round(float(value))) % (24 * 60)
    return f"{value // 60:02d}:{value % 60:02d}"


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
                    # A dispatch entry is only a planned departure. It must never be
                    # reintroduced into current/next trajectory evidence.
                    continue

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
                remaining = parse_remaining_stops(obs.get("remaining_stops"))
                vehicle_seq_est = None
                if seq is not None and remaining is not None:
                    # stop_seq identifies the queried stop, not the vehicle itself.
                    # Subtracting remaining_stops gives a rough vehicle position on the
                    # directional stop sequence. This is retained for future strict
                    # trajectory validation; raw stop_seq alone must not be treated as a
                    # physical vehicle position.
                    vehicle_seq_est = max(1, seq - remaining)

                events[(route, plate, d)].append({
                    "time": sample_min,
                    "dt": dt,
                    "role": role,
                    "stop_seq": seq,
                    "stop_name": obs.get("stop_name", ""),
                    "eta": eta,
                    "distance": distance,
                    "remaining_stops": remaining,
                    "vehicle_seq_est": vehicle_seq_est,
                    "service_hints": obs.get("service_hints") or [],
                })

    for key in events:
        events[key].sort(key=lambda x: x["time"])
    return route_info, events, dispatches, first_seen


def derive_min_full_runtimes(route_info, events, dispatches):
    """Build a full-trip baseline only from actual terminal-stop ETA evidence."""
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


def trajectory_classification(route, plate, d, dep, next_dep, route_info, events, min_full):
    """Classify conservatively from stop-centric SHMAAS ETA observations.

    current/next means the vehicle is predicted for a queried stop; it is not a
    GPS position and must not by itself prove that the vehicle has reversed.
    Until a strict position-series validator is added, inferred reverse-direction
    observations may downgrade confidence but may not create a short-turn label.
    """
    info = route_info.get((route, d), {})
    terminal = info.get("end") or ""
    same = [e for e in events.get((route, plate, d), []) if dep <= e["time"] < next_dep]
    opposite = [
        e for e in events.get((route, plate, 1 - d), [])
        if dep + core.MIN_REVERSE_ELAPSED_MIN <= e["time"] < next_dep
    ]
    if not same:
        return "运行异常待查", "发车后缺少同向到站预测证据", "", ""

    hint_type, hint_text = core.explicit_service_hint(same)
    if hint_type:
        last = same[-1]
        return hint_type, f"实时服务提示：{hint_text}", last.get("stop_name", ""), ""

    last = max(same, key=lambda e: e["time"])
    terminal_events = [
        e for e in same
        if e.get("stop_name") == terminal and e.get("eta") is not None
    ]
    terminal_sample_times = sorted({round(e["time"], 3) for e in terminal_events})
    close_terminal = [
        e for e in terminal_events
        if e.get("eta") is not None
        and (e["eta"] <= 5 or (e.get("distance") is not None and e["distance"] <= 1000))
    ]

    # Repeated terminal ETA observations mean SHMAAS continues to project this
    # plate through to the route terminal. That is stronger evidence of a full
    # trip than a stray appearance in the opposite-direction ETA list.
    if len(terminal_sample_times) >= 2 or close_terminal:
        best = terminal_events[-1]
        predicted = best["time"] + best["eta"]
        reason = (
            "同向终点ETA在多个采样时点持续出现；按全程运行处理"
            if len(terminal_sample_times) >= 2
            else "已取得接近终点的同向ETA证据；按全程运行处理"
        )
        return "全程车", reason, last.get("stop_name", ""), fmt_hm(predicted)

    if opposite:
        return (
            "运行异常待查",
            "反方向current/next属于站点到站预测，未形成可验证的实际折返轨迹；不判区间车",
            last.get("stop_name", ""),
            "",
        )

    if terminal_events:
        best = terminal_events[-1]
        return (
            "运行中待确认",
            "已出现单次同向终点ETA，等待更多采样确认全程运行",
            last.get("stop_name", ""),
            fmt_hm(best["time"] + best["eta"]),
        )

    return "运行中待确认", "尚未取得连续终点ETA或可靠折返轨迹证据", last.get("stop_name", ""), ""


# Patch the core module before entering its normal CLI/main flow. This keeps the
# export format stable while correcting evidence normalization and conservative
# service classification.
core.fmt_hm = fmt_hm
core.collect_evidence = collect_evidence
core.derive_min_full_runtimes = derive_min_full_runtimes
core.trajectory_classification = trajectory_classification

if __name__ == "__main__":
    core.main()
