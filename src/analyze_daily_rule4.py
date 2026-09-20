from __future__ import annotations

import json
from pathlib import Path

import analyze_daily_safe as v3

core = v3.core
ROOT = Path(__file__).resolve().parents[1]
RULE_VERSION = "dual-evidence-short-turn-v4-terminal-zone"


def load_reverse_confirmations(date: str) -> list[dict]:
    path = ROOT / "data" / "shmaas" / f"{date}-浦东35路-reverse-watch.jsonl"
    if not path.exists():
        return []
    items = []
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            continue
        for item in rec.get("watches") or []:
            if item.get("confirmed"):
                items.append(item)
    return items


def confirmed_external_turn(route, plate, direction, dep, events):
    if route != "浦东35路":
        return None
    same = events.get((route, plate, direction), [])
    dt = next((e.get("dt") for e in same if e.get("dt") is not None), None)
    if dt is None:
        return None
    date = dt.strftime("%Y-%m-%d")
    departure = v3.fmt_hm(dep)
    matches = [
        item for item in load_reverse_confirmations(date)
        if item.get("plate") == plate
        and item.get("source_direction") == direction
        and (not item.get("departure") or item.get("departure") == departure)
    ]
    if not matches:
        return None
    return max(matches, key=lambda x: x.get("reverse_gain_stops") or 0)


def reliable_terminal_eta(route, d, dep, next_dep, route_info, events, plate):
    """Strong full-trip guard learned from the proven private pipeline.

    Repeated terminal ETA observations, or one ETA very close to the terminal,
    override generic opposite-direction trajectory noise. This protects normal
    terminal turnarounds such as the known Pudong78 last-bus case.
    """
    info = route_info.get((route, d)) or {}
    terminal_seq = info.get("stop_count")
    terminal_name = info.get("end_stop") or ""
    same = [e for e in events.get((route, plate, d), []) if dep <= e["time"] < next_dep]
    hits = []
    for e in same:
        seq = e.get("stop_seq")
        name = e.get("stop_name") or ""
        eta = e.get("eta_min")
        if eta is None:
            continue
        if (terminal_seq and seq == terminal_seq) or (terminal_name and name == terminal_name):
            hits.append(e)
    if len(hits) >= 2:
        return True
    if any((e.get("eta_min") or 999) <= 5 for e in hits):
        return True
    return False


def near_terminal_zone(route, d, last_stop_name, route_info, protected_stops=6):
    """Treat a main-sampler break near the scheduled terminal as normal turnaround.

    This guard applies only to generic reconstructed-trajectory evidence. Explicit
    service hints and independently confirmed reverse-watch evidence remain valid.
    """
    info = route_info.get((route, d)) or {}
    stop_count = info.get("stop_count")
    if not stop_count or not last_stop_name:
        return False
    for seq in range(max(1, stop_count - protected_stops + 1), stop_count + 1):
        if v3.stop_name_for_seq(route_info, route, d, seq) == last_stop_name:
            return True
    return False


def trajectory_classification(route, plate, d, dep, next_dep, route_info, events, min_full):
    same = [e for e in events.get((route, plate, d), []) if dep <= e["time"] < next_dep]

    if same:
        hint_type, hint_text = core.explicit_service_hint(same)
        if hint_type:
            last = same[-1]
            return hint_type, f"实时服务提示：{hint_text}", last.get("stop_name", ""), ""

    # Full-trip evidence has priority over generic reverse noise. This is the
    # key protection against recreating the historical 55207 false positive.
    terminal_guard = reliable_terminal_eta(route, d, dep, next_dep, route_info, events, plate)

    external = confirmed_external_turn(route, plate, d, dep, events)
    if external and not terminal_guard:
        last_seq = external.get("last_source_seq")
        last_name = v3.stop_name_for_seq(route_info, route, d, last_seq)
        gain = external.get("reverse_gain_stops") or 0
        target = external.get("target_stop") or "反向追踪站"
        reason = (
            f"同向出现后下一次主采样即未再发现该车，立即启动定向反向追踪；"
            f"从最后位置反向+3站开始追踪，在{target}方向确认反向推进{gain:.0f}站，"
            f"达到≥5站区间车判据"
        )
        return "疑似区间车", reason, last_name, ""

    result = v3.trajectory_classification(route, plate, d, dep, next_dep, route_info, events, min_full)

    # Dual-evidence policy: the dedicated watcher is the preferred proof, but
    # a complete reconstructed physical trajectory from the main sampler is
    # also valid evidence when the watcher missed the event. v3 only emits a
    # short-turn here after same-direction progression, a mid-route stop, and
    # continuous spatially connected reverse progression. Do not accept it if
    # strong terminal ETA evidence says the vehicle actually ran full route.
    if result[0] == "疑似区间车" and not str(result[1]).startswith("实时服务提示："):
        last_stop = result[2]
        terminal_zone = near_terminal_zone(route, d, last_stop, route_info)
        if terminal_guard or terminal_zone:
            return (
                "全程车",
                ("已取得可靠同向终点ETA证据；" if terminal_guard else "最后可靠轨迹已进入终点保护区（末6站）；") + "反向信息按正常终点折返/采样噪声处理，不判区间车",
                result[2],
                result[3],
            )
        return (
            "疑似区间车",
            "主采样物理轨迹已形成完整折返证据（同向连续推进→中途停止→空间衔接的反向连续推进）；虽未取得独立reverse-watch确认，仍按双证据规则判定疑似区间车",
            result[2],
            result[3],
        )
    return result


core.trajectory_classification = trajectory_classification

if __name__ == "__main__":
    core.main()
