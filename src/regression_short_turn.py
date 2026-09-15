from __future__ import annotations

import argparse
from datetime import datetime
from zoneinfo import ZoneInfo

from extract_physical_trajectory import extract

TZ = ZoneInfo("Asia/Shanghai")
CASES = [
    {"date":"2026-09-15","route":"浦东35路","plate":"沪A11062A","departure":"06:36","direction":0,"stop_count":64,"expected":"疑似区间车"},
    {"date":"2026-09-15","route":"浦东35路","plate":"沪A11062A","departure":"09:24","direction":0,"stop_count":64,"expected":"疑似区间车"},
    {"date":"2026-09-11","route":"浦东78路","plate":"沪A55207D","departure":"19:58","direction":0,"stop_count":40,"expected":"全程车"},
]


def dt(date, hhmm): return datetime.fromisoformat(f"{date}T{hhmm}:00+08:00").astimezone(TZ)

def split_trip(points, case, horizon_min=240):
    start=dt(case["date"],case["departure"]); end=start.timestamp()+horizon_min*60
    return [p for p in points if start.timestamp() <= datetime.fromisoformat(p.time).timestamp() <= end]

def evaluate(case):
    trip=split_trip(extract(case["date"],case["route"],case["plate"]),case)
    same=[p for p in trip if p.direction==case["direction"]]
    if not same: return "无证据", "无同向物理点"
    # Critical: candidate anchor is the last source point BEFORE reverse tracking starts,
    # not a later same-direction point belonging to another trip of the same vehicle.
    reverse_all=[p for p in trip if p.direction!=case["direction"]]
    if not reverse_all: return "全程车", "未见反向物理轨迹"
    first_reverse=min(reverse_all,key=lambda p:p.time)
    prior_same=[p for p in same if p.time < first_reverse.time]
    if not prior_same: return "无证据", "反向出现前无同向物理点"
    anchor=max(prior_same,key=lambda p:p.time)
    fraction=(anchor.physical_seq-1)/(case["stop_count"]-1)
    reverse=sorted([p for p in reverse_all if p.time>anchor.time],key=lambda p:p.time)
    gain=max((p.physical_seq for p in reverse),default=0)-reverse[0].physical_seq if reverse else 0
    if fraction < .80 and gain >= 2:
        return "疑似区间车", f"同向消失点seq{anchor.physical_seq}({fraction:.0%})；反向seq{reverse[0].physical_seq}->{max(p.physical_seq for p in reverse)}，推进{gain}站"
    return "全程车", f"同向消失点{fraction:.0%}；反向推进{gain}站，未同时满足<80%和>=2站"

def summarize(case):
    result,note=evaluate(case)
    ok = result == case["expected"]
    print(f"CASE {case['date']} {case['route']} {case['plate']} {case['departure']} expected={case['expected']} result={result} {'PASS' if ok else 'FAIL'}")
    print(f"  {note}")
    if not ok: raise AssertionError(f"regression failed: {case} -> {result}: {note}")

def main():
    ap=argparse.ArgumentParser(); ap.add_argument("--case",choices=["all","11062-0636","11062-0924","55207-1958"],default="all"); args=ap.parse_args()
    chosen=CASES if args.case=="all" else [CASES[{"11062-0636":0,"11062-0924":1,"55207-1958":2}[args.case]]]
    for case in chosen: summarize(case)

if __name__=="__main__": main()
