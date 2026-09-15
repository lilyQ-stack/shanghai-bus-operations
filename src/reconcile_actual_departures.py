from __future__ import annotations

import csv
import json
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
TZ = ZoneInfo("Asia/Shanghai")
MATCH_WINDOW_MIN = 45
TERMINAL_TURNAROUND_MIN = 2
EXTRA_FIELDS = ["计划发车时间", "发车时间依据"]


def parse_dt(date: str, hm: str):
    try:
        return datetime.fromisoformat(f"{date}T{hm}:00+08:00").astimezone(TZ)
    except Exception:
        return None


def read_csv(path: Path):
    with path.open(encoding="utf-8-sig", newline="") as f:
        return list(csv.DictReader(f))


def write_csv(path: Path, rows: list[dict], fields: list[str]):
    with path.open("w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        w.writerows({k: r.get(k, "") for k in fields} for r in rows)


def recalc_duration(row: dict, date: str):
    dep = parse_dt(date, row.get("发车时间", ""))
    arr = parse_dt(date, row.get("到达时间", "")) if row.get("到达时间") not in {"", "待确认"} else None
    if not dep or not arr or arr < dep:
        return
    row["全程时间"] = f"{round((arr - dep).total_seconds() / 60)}分钟"


def main() -> int:
    date = datetime.now(TZ).date().isoformat()
    export = ROOT / "data" / "export"
    combined = export / f"{date}-operations.csv"
    if not combined.exists():
        print("No operations export; skip actual departure reconciliation")
        return 0

    rows = read_csv(combined)
    if not rows:
        return 0
    fields = list(rows[0].keys())
    for field in EXTRA_FIELDS:
        if field not in fields:
            fields.append(field)
    for row in rows:
        row.setdefault("计划发车时间", "")
        row.setdefault("发车时间依据", "计划发车")

    by_vehicle = defaultdict(list)
    for row in rows:
        dep = parse_dt(date, row.get("发车时间", ""))
        if dep:
            by_vehicle[(row.get("线路", ""), row.get("车牌号", ""))].append((dep, row))
    for key in by_vehicle:
        by_vehicle[key].sort(key=lambda x: x[0])

    overridden = 0
    invalid_plans = 0
    terminal_plus_two_count = 0
    for _, trips in by_vehicle.items():
        for i, (planned_dt, row) in enumerate(trips):
            row["计划发车时间"] = row.get("计划发车时间") or row.get("发车时间", "")
            if i == 0:
                continue

            previous = None
            for j in range(i - 1, -1, -1):
                prev_dt, candidate = trips[j]
                if candidate.get("方向", "") != row.get("方向", ""):
                    previous = (prev_dt, candidate)
                    break
            if not previous:
                continue
            _, prev = previous

            actual_turn_hm = str(prev.get("区间站折返发车时间") or "").strip()
            short_turn_departure = parse_dt(date, actual_turn_hm) if actual_turn_hm else None

            arrival_hm = str(prev.get("到达时间") or "").strip()
            prev_arrival = parse_dt(date, arrival_hm) if arrival_hm not in {"", "待确认"} else None

            impossible_by_arrival = bool(prev_arrival and prev_arrival > planned_dt)
            impossible_by_short_turn = bool(short_turn_departure and short_turn_departure > planned_dt)
            if not (impossible_by_arrival or impossible_by_short_turn):
                continue

            actual_turn = None
            basis = ""
            if impossible_by_arrival and prev_arrival:
                actual_turn = prev_arrival + timedelta(minutes=TERMINAL_TURNAROUND_MIN)
                basis = "终点确认到达+2分钟（覆盖计划时间）"
                terminal_plus_two_count += 1
            elif short_turn_departure:
                actual_turn = short_turn_departure
                basis = "实际区间折返发车（覆盖计划时间）"

            if actual_turn:
                gap = abs((actual_turn - planned_dt).total_seconds()) / 60
                if gap <= MATCH_WINDOW_MIN:
                    original = row.get("发车时间", "")
                    row["计划发车时间"] = original
                    row["发车时间"] = actual_turn.astimezone(TZ).strftime("%H:%M")
                    row["发车时间依据"] = basis
                    recalc_duration(row, date)
                    overridden += 1
                    continue

            row["发车时间依据"] = "计划时间失效，实际折返发车待确认"
            invalid_plans += 1

    rows.sort(key=lambda r: (r.get("线路", ""), r.get("发车时间", ""), r.get("车牌号", "")))
    write_csv(combined, rows, fields)

    per_route = defaultdict(list)
    for row in rows:
        per_route[row.get("线路", "")].append(row)
    route_fields = [f for f in fields if f != "线路"]
    for route, route_rows in per_route.items():
        write_csv(export / f"{date}-{route.replace('/', '_')}.csv", route_rows, route_fields)

    meta_path = export / f"{date}-operations-meta.json"
    if meta_path.exists():
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        meta["actual_departure_override_count"] = overridden
        meta["invalid_planned_departure_count"] = invalid_plans
        meta["terminal_arrival_plus_two_count"] = terminal_plus_two_count
        meta["departure_priority_rule"] = (
            "If a vehicle reaches the previous trip terminal after its next planned departure time, the planned time is invalid and actual departure is set to confirmed terminal arrival plus 2 minutes. "
            "Known short-turn departures continue to use their independently inferred actual turnaround departure. Full-trip duration is recalculated after any departure override."
        )
        meta.pop("terminal_turnaround_inferred_count", None)
        meta_path.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")

    print(
        f"Reconciled actual departures: {overridden} plan(s) overridden; "
        f"{invalid_plans} impossible plan(s) awaiting actual departure; "
        f"{terminal_plus_two_count} terminal late-arrival departure(s) set to arrival+2 minutes"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
