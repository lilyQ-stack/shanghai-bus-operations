from __future__ import annotations

import json
from pathlib import Path

import analyze_daily_safe as v3

core = v3.core
ROOT = Path(__file__).resolve().parents[1]
RULE_VERSION = "3miss-reverse+3-gain5-v1"


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


def trajectory_classification(route, plate, d, dep, next_dep, route_info, events, min_full):
    same = [e for e in events.get((route, plate, d), []) if dep <= e["time"] < next_dep]

    # Keep explicit official service hints authoritative.
    if same:
        hint_type, hint_text = core.explicit_service_hint(same)
        if hint_type:
            last = same[-1]
            return hint_type, f"实时服务提示：{hint_text}", last.get("stop_name", ""), ""

    external = confirmed_external_turn(route, plate, d, dep, events)
    if external:
        last_seq = external.get("last_source_seq")
        last_name = v3.stop_name_for_seq(route_info, route, d, last_seq)
        gain = external.get("reverse_gain_stops")
        missing = external.get("missing_main_samples")
        target = external.get("target_stop") or "反向追踪站"
        reason = (
            f"同向连续{missing or 3}次主采样未再发现该车后启动定向反向追踪；"
            f"从最后位置反向+3站开始每5分钟采集，在{target}方向确认反向推进{gain:.0f}站，"
            f"达到≥5站区间车判据"
        )
        return "疑似区间车", reason, last_name, ""

    result = v3.trajectory_classification(route, plate, d, dep, next_dep, route_info, events, min_full)

    # Rule v4: legacy reconstructed reverse trajectories may raise suspicion, but they
    # are no longer sufficient by themselves to label a short turn. This removes the
    # former 30-minute/time-gap and 12%-spatial-gap decision path from final labeling.
    if result[0] == "疑似区间车" and not str(result[1]).startswith("实时服务提示："):
        return (
            "运行异常待查",
            "旧轨迹模型发现疑似折返信号，但新规则要求：同向连续3次未采到后启动5分钟定向反向追踪，并确认反向推进≥5站；当前尚未取得该确认",
            result[2],
            "",
        )
    return result


core.trajectory_classification = trajectory_classification

if __name__ == "__main__":
    core.main()
