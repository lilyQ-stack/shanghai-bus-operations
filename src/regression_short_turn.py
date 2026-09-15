from __future__ import annotations

import argparse
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from extract_physical_trajectory import extract
from temporal_feasibility import DirectionBound, impossible_partial_transition

TZ = ZoneInfo("Asia/Shanghai")

# User-confirmed truth samples. This runner never manufactures a result from the label;
# it reports whether raw evidence supports the expected class.
CASES = [
    {"date":"2026-09-15","route":"浦东35路","plate":"沪A11062A","departure":"06:36","direction":0,"expected":"疑似区间车"},
    {"date":"2026-09-15","route":"浦东35路","plate":"沪A11062A","departure":"09:24","direction":0,"expected":"强疑似区间车"},
    {"date":"2026-09-11","route":"浦东78路","plate":"沪A55207D","departure":"19:58","direction":0,"expected":"全程车"},
]


def dt(date, hhmm):
    return datetime.fromisoformat(f"{date}T{hhmm}:00+08:00").astimezone(TZ)


def split_trip(points, date, departure, direction, horizon_min=240):
    start = dt(date, departure); end = start.timestamp() + horizon_min * 60
    return [p for p in points if start.timestamp() <= datetime.fromisoformat(p.time).timestamp() <= end]


def summarize(case):
    points = extract(case["date"], case["route"], case["plate"])
    trip = split_trip(points, case["date"], case["departure"], case["direction"])
    same = [p for p in trip if p.direction == case["direction"]]
    reverse = [p for p in trip if p.direction != case["direction"]]
    print(f"CASE {case['date']} {case['route']} {case['plate']} {case['departure']} expected={case['expected']}")
    print(f"  physical_points={len(trip)} same={len(same)} reverse={len(reverse)}")
    if same:
        print(f"  same: first={same[0].time} seq={same[0].physical_seq}; last={same[-1].time} seq={same[-1].physical_seq}")
    if reverse:
        print(f"  reverse: first={reverse[0].time} seq={reverse[0].physical_seq}; last={reverse[-1].time} seq={reverse[-1].physical_seq}")
    # Raw-only conservative reversal diagnostic. Temporal feasibility is evaluated by
    # the production classifier after history-derived bounds are available.
    if same and reverse:
        candidates = [(s, r) for s in same for r in reverse if datetime.fromisoformat(r.time) > datetime.fromisoformat(s.time)]
        if candidates:
            s, r = min(candidates, key=lambda x: datetime.fromisoformat(x[1].time) - datetime.fromisoformat(x[0].time))
            gap = (datetime.fromisoformat(r.time)-datetime.fromisoformat(s.time)).total_seconds()/60
            print(f"  closest_same_to_reverse: {s.time} seq={s.physical_seq} -> {r.time} seq={r.physical_seq}, gap={gap:.1f}m")
    print()


def main():
    ap = argparse.ArgumentParser(); ap.add_argument("--case", choices=["all","11062-0636","11062-0924","55207-1958"], default="all")
    args = ap.parse_args()
    chosen = CASES if args.case == "all" else [CASES[{"11062-0636":0,"11062-0924":1,"55207-1958":2}[args.case]]]
    for case in chosen:
        summarize(case)

if __name__ == "__main__": main()
