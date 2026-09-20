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
import analyze_daily_operations as operations

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
    # Use the operations-level classifier for rolling counts so every distinct\n    # plate + departure + direction is retained. Rule4 remains the guard/version\n    # layer, but candidate enumeration must be trip-level rather than plate-level.\n    rows = operations.build_rows(date, route_info, events, dispatches, first_seen)
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
        "semantics": "Pudong35 short-turn labels start reverse tracking after the first missed main sample; probes run every 5 minutes from mirrored reverse position +3 stops and confirm after at least 5 stops of reverse progress. Explicit official short-turn service hints remain authoritative.",
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
