from __future__ import annotations

import argparse
import csv
import json
from datetime import datetime
from pathlib import Path

# Rule v4 keeps the safe trajectory normalization from v3, but short-turn labeling
# now requires the dedicated reverse watcher: after the first missed main sample,
# start probing at mirrored reverse position +3 stops every 5 minutes and confirm
# after >=5 stops of reverse progress.
import analyze_daily_rule4 as rule4
import export_daily_operations as tripops

core = rule4.core
ROOT = Path(__file__).resolve().parents[1]
ROLLING_ROUTES = ("浦东78路", "浦东35路")
CSV_FIELDS = [
    "线路", "车牌号", "发车时间", "发车站", "终点站", "班次类型",
    "区间/异常说明", "最后可靠采集站点", "本车首次观测",
    "截至时间", "临时结果", "判定规则版本",
]


def latest_cutoff_by_route(snapshots):
    latest = {}
    for snap in snapshots:
        route = snap.get("route")
        dt = core.event_time(snap)
        if route not in ROLLING_ROUTES or dt is None:
            continue
        if route not in latest or dt > latest[route]:
            latest[route] = dt
    return latest


def write_csv(path, rows, cutoff):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=CSV_FIELDS)
        writer.writeheader()
        for row in rows:
            writer.writerow({
                "线路": row.get("线路", ""),
                "车牌号": row.get("车牌号", ""),
                "发车时间": row.get("发车时间", ""),
                "发车站": row.get("发车站", ""),
                "终点站": row.get("终点站", ""),
                "班次类型": row.get("班次类型", ""),
                "区间/异常说明": row.get("区间/异常说明", ""),
                "最后可靠采集站点": row.get("最后可靠采集站点", ""),
                "本车首次观测": row.get("本车首次观测", ""),
                "截至时间": cutoff.strftime("%Y-%m-%d %H:%M:%S") if cutoff else "",
                "临时结果": "是",
                "判定规则版本": rule4.RULE_VERSION,
            })


def main():
    parser = argparse.ArgumentParser(description="Generate provisional daytime short-turn results from current SHMAAS JSONL.")
    parser.add_argument("--date", help="Shanghai service date YYYY-MM-DD; defaults to current Shanghai date")
    args = parser.parse_args()

    now_cst = datetime.now(core.TZ)
    date = args.date or now_cst.strftime("%Y-%m-%d")
    snapshots, source_files = core.load_snapshots(date)
    route_info, events, dispatches, first_seen = core.collect_evidence(snapshots)
    # Use the same trip-isolated classifier as the full operations export.\n    # This guarantees rolling counts retain every plate + departure + direction.\n    op_rows, _, _, _ = tripops.build(date, snapshots)\n    rows = [\n        {\n            "线路": r.get("线路", ""), "车牌号": r.get("车牌号", ""),\n            "方向": r.get("方向", ""), "发车时间": r.get("发车时间", ""),\n            "发车站": r.get("始发站", ""), "终点站": r.get("终点站", ""),\n            "班次类型": r.get("班次类型", ""),\n            "区间/异常说明": r.get("班次类型判定依据", ""),\n            "最后可靠采集站点": "", "本车首次观测": r.get("本车首次观测", ""),\n        }\n        for r in op_rows\n    ]
    cutoffs = latest_cutoff_by_route(snapshots)

    out_dir = ROOT / "data" / "rolling"
    out_dir.mkdir(parents=True, exist_ok=True)
    summary = {
        "date_cst": date,
        "generated_at_cst": now_cst.isoformat(timespec="seconds"),
        "provisional": True,
        "counting_unit": "trip",
        "trip_key": ["plate", "departure", "direction"],
        "analysis_rule_version": rule4.RULE_VERSION,
        "semantics": "Trip-level rolling classification uses the same trip-isolated short-turn classifier as the full operations export: plate + departure + direction is one trip; reliable terminal ETA protects full trips; reverse physical progress confirms short turns.",
        "sources": source_files,
        "routes": {},
    }

    for route in ROLLING_ROUTES:
        candidates = [
            row for row in rows
            if row.get("线路") == route and row.get("班次类型") == "疑似区间车"
        ]
        candidates.sort(key=lambda row: (row.get("发车时间", ""), row.get("车牌号", "")))
        cutoff = cutoffs.get(route)
        csv_path = out_dir / f"{date}-{route}-疑似区间车.csv"
        write_csv(csv_path, candidates, cutoff)
        summary["routes"][route] = {
            "cutoff_cst": cutoff.isoformat(timespec="seconds") if cutoff else None,
            "candidate_count": len(candidates),
            "trip_count": len(candidates),
            "vehicle_count": len({row.get("车牌号", "") for row in candidates if row.get("车牌号")}),
            "candidates": [
                {
                    "plate": row.get("车牌号", ""),
                    "direction": row.get("方向", ""),
                    "departure": row.get("发车时间", ""),
                    "origin": row.get("发车站", ""),
                    "destination": row.get("终点站", ""),
                    "reason": row.get("区间/异常说明", ""),
                    "last_reliable_stop": row.get("最后可靠采集站点", ""),
                }
                for row in candidates
            ],
        }
        print(f"rolling {route}: {len(candidates)} short-turn candidate(s) -> {csv_path.relative_to(ROOT)}")

    summary_path = out_dir / f"{date}-short-turn-summary.json"
    with summary_path.open("w", encoding="utf-8") as f:
        json.dump(summary, f, ensure_ascii=False, indent=2)
        f.write("\n")
    print(f"summary -> {summary_path.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
