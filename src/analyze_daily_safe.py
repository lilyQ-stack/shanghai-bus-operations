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
            key = (route, d)
            info = route_info[key]
            info["start"] = direction.get("start_stop", "") or info.get("start", "")
            info["end"] = direction.get("end_stop", "") or info.get("end", "")
            info["stop_count"] = direction.get("stop_count") or info.get("stop_count", 0)
            stop_names = info.setdefault("stop_names", {})
            for stop in direction.get("sampled_stops", []):
                try:
                    seq = int(stop.get("seq"))
                except (TypeError, ValueError):
                    continue
                name = stop.get("stop_name") or ""
                if name:
                    stop_names[seq] = name

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
                    # stop_seq is the queried target stop, not the vehicle position.
                    # remaining_stops tells how many stops remain before that target, so
                    # their difference reconstructs the vehicle's approximate sequence.
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


def median_value(values):
    values = sorted(values)
    if not values:
        return None
    n = len(values)
    if n % 2:
        return float(values[n // 2])
    return (values[n // 2 - 1] + values[n // 2]) / 2.0


def trajectory_points(trip_events):
    """Collapse stop-centric ETA rows into one reconstructed vehicle position per sample."""
    grouped = defaultdict(list)
    for ev in trip_events:
        seq = ev.get("vehicle_seq_est")
        if seq is not None:
            grouped[round(ev["time"], 3)].append(float(seq))

    points = []
    for sample_time, seqs in sorted(grouped.items()):
        # A healthy SHMAAS snapshot usually gives the same reconstructed position from
        # every queried downstream stop. Allow tiny disagreement but reject incoherent rows.
        if max(seqs) - min(seqs) > 2:
            continue
        points.append({"time": sample_time, "seq": median_value(seqs), "samples": len(seqs)})
    return points


def progressing(points, min_points=3, min_gain=4):
    if len(points) < min_points:
        return False
    if points[-1]["seq"] - points[0]["seq"] < min_gain:
        return False
    drops = sum(1 for a, b in zip(points, points[1:]) if b["seq"] + 1 < a["seq"])
    return drops <= 1


def stop_name_for_seq(route_info, route, d, seq):
    info = route_info.get((route, d), {}) or {}
    names = info.get("stop_names") or {}
    if not names or seq is None:
        return ""
    target = int(round(seq))
    if target in names:
        return names[target]
    nearest = min(names, key=lambda x: abs(x - target))
    return names.get(nearest, "")


def physical_short_turn_evidence(route, plate, d, dep, next_dep, route_info, events):
    """Return strong mid-route turnback evidence from reconstructed physical positions.

    The key protection against false positives is temporal ordering: reverse-direction ETA
    rows are ignored until after the final same-direction physical point. A short turn needs
    a progressing same-direction trajectory, then a progressing reverse trajectory over at
    least three distinct samples, with both trajectories meeting in roughly the same place.
    """
    same_events = [e for e in events.get((route, plate, d), []) if dep <= e["time"] < next_dep]
    same_points = trajectory_points(same_events)
    if not progressing(same_points):
        return None

    same_count = (route_info.get((route, d), {}) or {}).get("stop_count") or 0
    opp_count = (route_info.get((route, 1 - d), {}) or {}).get("stop_count") or 0
    if same_count < 2 or opp_count < 2:
        return None

    last_same = same_points[-1]
    same_fraction = (last_same["seq"] - 1) / (same_count - 1)
    # A vehicle already in the last 15% is much more likely to be a normal terminal turn.
    if same_fraction >= 0.85:
        return None

    opposite_events = [
        e for e in events.get((route, plate, 1 - d), [])
        if last_same["time"] < e["time"] < next_dep
    ]
    opposite_points = trajectory_points(opposite_events)
    if not progressing(opposite_points):
        return None

    first_reverse = opposite_points[0]
    gap = first_reverse["time"] - last_same["time"]
    if gap < 0 or gap > 30:
        return None

    # Convert reverse-direction sequence back onto the original direction's 0..1 axis.
    reverse_fraction_on_original = 1 - (first_reverse["seq"] - 1) / (opp_count - 1)
    spatial_gap = abs(same_fraction - reverse_fraction_on_original)
    if spatial_gap > 0.12:
        return None

    same_name = stop_name_for_seq(route_info, route, d, last_same["seq"])
    reverse_name = stop_name_for_seq(route_info, route, 1 - d, first_reverse["seq"])
    return {
        "same_last": last_same,
        "reverse_first": first_reverse,
        "reverse_points": opposite_points,
        "gap_min": gap,
        "spatial_gap": spatial_gap,
        "same_name": same_name,
        "reverse_name": reverse_name,
    }


def trajectory_classification(route, plate, d, dep, next_dep, route_info, events, min_full):
    """Classify using terminal evidence plus reconstructed physical trajectory continuity."""
    info = route_info.get((route, d), {})
    terminal = info.get("end") or ""
    same = [e for e in events.get((route, plate, d), []) if dep <= e["time"] < next_dep]
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

    # Repeated terminal ETA observations are strong full-trip evidence. Keep this check
    # ahead of turnback inference; it specifically protects through-running trips such as
    # a last departure that appears early in the opposite-direction prediction list.
    if len(terminal_sample_times) >= 2 or close_terminal:
        best = terminal_events[-1]
        predicted = best["time"] + best["eta"]
        reason = (
            "同向终点ETA在多个采样时点持续出现；按全程运行处理"
            if len(terminal_sample_times) >= 2
            else "已取得接近终点的同向ETA证据；按全程运行处理"
        )
        physical_points = trajectory_points(same)
        last_name = (
            stop_name_for_seq(route_info, route, d, physical_points[-1]["seq"])
            if physical_points else last.get("stop_name", "")
        )
        return "全程车", reason, last_name, fmt_hm(predicted)

    turn = physical_short_turn_evidence(route, plate, d, dep, next_dep, route_info, events)
    if turn:
        last_name = turn["same_name"] or last.get("stop_name", "")
        reverse_name = turn["reverse_name"] or "附近"
        reverse_count = len(turn["reverse_points"])
        reason = (
            f"同向实际位置连续推进至{last_name or '线路中段'}附近后停止；"
            f"约{turn['gap_min']:.0f}分钟后在{reverse_name}附近形成反向连续轨迹"
            f"（{reverse_count}个采样点），空间衔接符合中途折返"
        )
        return "疑似区间车", reason, last_name, ""

    opposite = [
        e for e in events.get((route, plate, 1 - d), [])
        if dep + core.MIN_REVERSE_ELAPSED_MIN <= e["time"] < next_dep
    ]
    if opposite:
        physical_points = trajectory_points(same)
        last_name = (
            stop_name_for_seq(route_info, route, d, physical_points[-1]["seq"])
            if physical_points else last.get("stop_name", "")
        )
        return (
            "运行异常待查",
            "出现反方向到站预测，但未满足连续位置、时间及空间衔接三项折返证据；不判区间车",
            last_name,
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

    physical_points = trajectory_points(same)
    last_name = (
        stop_name_for_seq(route_info, route, d, physical_points[-1]["seq"])
        if physical_points else last.get("stop_name", "")
    )
    return "运行中待确认", "尚未取得连续终点ETA或可靠折返轨迹证据", last_name, ""


# Patch the core module before entering its normal CLI/main flow. This keeps the
# export format stable while correcting evidence normalization and service classification.
core.fmt_hm = fmt_hm
core.collect_evidence = collect_evidence
core.derive_min_full_runtimes = derive_min_full_runtimes
core.trajectory_classification = trajectory_classification

if __name__ == "__main__":
    core.main()
