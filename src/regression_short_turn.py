from __future__ import annotations

import argparse
from datetime import datetime
from zoneinfo import ZoneInfo

from extract_physical_trajectory import extract

TZ = ZoneInfo("Asia/Shanghai")
# User-verified / historically audited ground truths.  These expectations never
# feed the classifier; they only make the regression fail if behavior drifts.
CASES = [
    {"id":"11062-0636","date":"2026-09-15","route":"浦东35路","plate":"沪A11062A","departure":"06:36","direction":0,"stop_count":64,"expected":"疑似区间车"},
    {"id":"11062-0924","date":"2026-09-15","route":"浦东35路","plate":"沪A11062A","departure":"09:24","direction":0,"stop_count":64,"expected":"疑似区间车"},
    {"id":"35172-1825","date":"2026-09-11","route":"浦东78路","plate":"沪A35172D","departure":"18:25","direction":0,"stop_count":40,"expected":"全程车"},
    {"id":"36809-1905","date":"2026-09-11","route":"浦东78路","plate":"沪A36809D","departure":"19:05","direction":0,"stop_count":40,"expected":"全程车"},
    {"id":"55207-1958","date":"2026-09-11","route":"浦东78路","plate":"沪A55207D","departure":"19:58","direction":0,"stop_count":40,"expected":"全程车"},
    {"id":"56123-1725","date":"2026-09-11","route":"浦东78路","plate":"沪A56123D","departure":"17:25","direction":0,"stop_count":40,"expected":"全程车"},
    {"id":"57022-1930","date":"2026-09-11","route":"浦东78路","plate":"沪A57022D","departure":"19:30","direction":0,"stop_count":40,"expected":"全程车"},
    {"id":"57902-1710","date":"2026-09-11","route":"浦东78路","plate":"沪A57902D","departure":"17:10","direction":0,"stop_count":40,"expected":"全程车"},
    {"id":"59117-1755","date":"2026-09-11","route":"浦东78路","plate":"沪A59117D","departure":"17:55","direction":0,"stop_count":40,"expected":"全程车"},
]

def dt(date,hhmm): return datetime.fromisoformat(f"{date}T{hhmm}:00+08:00").astimezone(TZ)
def split_trip(points,case,horizon_min=240):
    start=dt(case['date'],case['departure']); end=start.timestamp()+horizon_min*60
    return [p for p in points if start.timestamp()<=datetime.fromisoformat(p.time).timestamp()<=end]

def evaluate(case):
    trip=split_trip(extract(case['date'],case['route'],case['plate']),case)
    same=[p for p in trip if p.direction==case['direction']]
    if not same:return '无证据','无同向物理点'
    reverse_all=[p for p in trip if p.direction!=case['direction']]
    if not reverse_all:return '全程车','未见反向物理轨迹'
    first_reverse=min(reverse_all,key=lambda p:p.time)
    prior_same=[p for p in same if p.time<first_reverse.time]
    if not prior_same:return '无证据','反向出现前无同向物理点'
    anchor=max(prior_same,key=lambda p:p.time)
    fraction=(anchor.physical_seq-1)/(case['stop_count']-1)
    reverse=sorted([p for p in reverse_all if p.time>anchor.time],key=lambda p:p.time)
    gain=max((p.physical_seq for p in reverse),default=0)-reverse[0].physical_seq if reverse else 0
    if fraction<.80 and gain>=2:return '疑似区间车',f"同向消失点seq{anchor.physical_seq}({fraction:.0%})；反向seq{reverse[0].physical_seq}->{max(p.physical_seq for p in reverse)}，推进{gain}站"
    return '全程车',f"同向消失点{fraction:.0%}；反向推进{gain}站，未同时满足<80%和>=2站"

def summarize(case):
    result,note=evaluate(case); ok=result==case['expected']
    print(f"CASE {case['id']} {case['date']} {case['route']} {case['plate']} {case['departure']} expected={case['expected']} result={result} {'PASS' if ok else 'FAIL'}")
    print(' ',note)
    if not ok:raise AssertionError(f"regression failed: {case['id']} -> {result}: {note}")

def main():
    ap=argparse.ArgumentParser(); ap.add_argument('--case',default='all',choices=['all']+[c['id'] for c in CASES]); a=ap.parse_args()
    chosen=CASES if a.case=='all' else [next(c for c in CASES if c['id']==a.case)]
    for c in chosen:summarize(c)
if __name__=='__main__':main()
