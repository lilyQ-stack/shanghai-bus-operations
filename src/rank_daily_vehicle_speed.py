from __future__ import annotations

import argparse
import csv
import json
import re
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
TZ = ZoneInfo("Asia/Shanghai")


def read_csv(path: Path) -> list[dict]:
    with path.open(encoding="utf-8-sig", newline="") as f:
        return list(csv.DictReader(f))


def write_csv(path: Path, rows: list[dict], fields: list[str]) -> None:
    with path.open("w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        w.writerows({k: r.get(k, "") for k in fields} for r in rows)


def parse_duration(value: str) -> float | None:
    m = re.search(r"(\d+(?:\.\d+)?)\s*分钟", str(value or ""))
    return float(m.group(1)) if m else None


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--date")
    args = ap.parse_args()
    date = args.date or datetime.now(TZ).date().isoformat()
    export = ROOT / "data" / "export"
    combined = export / f"{date}-operations.csv"
    if not combined.exists():
        print("No operations export; skip daily vehicle speed ranking")
        return 0

    rows = read_csv(combined)
    if not rows:
        return 0

    route_vehicles = defaultdict(set)
    eligible_durations = defaultdict(list)
    actual_trip_counts = defaultdict(int)

    for row in rows:
        route = row.get("线路", "")
        plate = row.get("车牌号", "")
        if not route or not plate:
            continue
        route_vehicles[route].add(plate)
        actual_trip_counts[(route, plate)] += 1

        if row.get("班次类型") != "全程车" or row.get("参与车速排名") != "是":
            continue
        duration = parse_duration(row.get("全程时间", ""))
        if duration is None or duration <= 0:
            continue
        eligible_durations[(route, plate)].append(duration)

    averages = {}
    eligible_counts = {}
    ranks = {}

    for route, plates in route_vehicles.items():
        sortable = []
        for plate in sorted(plates):
            vals = eligible_durations.get((route, plate), [])
            if not vals:
                continue
            avg = sum(vals) / len(vals)
            averages[(route, plate)] = avg
            eligible_counts[(route, plate)] = len(vals)
            sortable.append((avg, plate))

        sortable.sort(key=lambda x: (x[0], x[1]))
        last_avg = None
        last_rank = 0
        for idx, (avg, plate) in enumerate(sortable, 1):
            if last_avg is None or abs(avg - last_avg) > 1e-9:
                last_rank = idx
                last_avg = avg
            ranks[(route, plate)] = last_rank

    for row in rows:
        route = row.get("线路", "")
        plate = row.get("车牌号", "")
        total = len(route_vehicles.get(route, set()))
        key = (route, plate)
        row["当日实际运营次数"] = str(actual_trip_counts.get(key, 0))
        row["当日计入排名班次"] = str(eligible_counts.get(key, 0))
        if key in averages:
            row["当日平均全程时间（分钟）"] = f"{averages[key]:.1f}"
            row["当日车速排名"] = f"{ranks[key]}/{total}"
        else:
            row["当日平均全程时间（分钟）"] = ""
            row["当日车速排名"] = f"—/{total}" if total else ""

    base_fields = list(rows[0].keys())
    extras = ["当日实际运营次数", "当日计入排名班次", "当日平均全程时间（分钟）", "当日车速排名"]
    fields = [f for f in base_fields if f not in extras] + extras
    write_csv(combined, rows, fields)

    by_route = defaultdict(list)
    for row in rows:
        by_route[row.get("线路", "")].append(row)
    for route, route_rows in by_route.items():
        route_path = export / f"{date}-{route.replace('/', '_')}.csv"
        write_csv(route_path, route_rows, [f for f in fields if f != "线路"])

    meta_path = export / f"{date}-operations-meta.json"
    if meta_path.exists():
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        meta["daily_vehicle_speed_ranking_rule"] = (
            "Within each route, rank each vehicle by its arithmetic mean full-trip duration across that day's A/B/C rank-eligible full-route trips. Lower mean duration ranks faster. Each vehicle is calculated only from its own trips. Short turns and D-grade arrivals are excluded. Display format is rank/total vehicles observed on that route that day. Physical km/h remains blank until reliable route distance is available."
        )
        meta["daily_vehicle_ranked_counts"] = {
            route: sum(1 for plate in plates if (route, plate) in ranks)
            for route, plates in route_vehicles.items()
        }
        meta_path.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")

    print(f"Ranked {len(ranks)} vehicles across {len(route_vehicles)} routes")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
