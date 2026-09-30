#!/usr/bin/env python3
"""Check actual sampling coverage near short-turn dispatch and estimated arrival."""
import csv,json,re
from collections import defaultdict
from datetime import datetime,timedelta
from pathlib import Path
DATE="2026-09-29";ROUTE="浦东35路"
def dt(x):
 try:return datetime.fromisoformat(DATE+"T"+x+":00+08:00")
 except:return None
samples=defaultdict(set)
for line in Path(f"data/shmaas/{DATE}-{ROUTE}.jsonl").open(encoding="utf-8-sig"):
 try:s=json.loads(line)
 except ValueError:continue
 if not s.get("success"):continue
 t=datetime.fromisoformat(s["sample_time_cst"])
 for v in s.get("vehicles",[]):
  for o in v.get("observations",[]):
   if o.get("role") not in ("current","next"):continue
   try:d=str(int(o["direction"]));pos=max(1,int(o["stop_seq"])-int(o["remaining_stops"]))
   except (KeyError,ValueError,TypeError):continue
   samples[(v.get("plate"),d)].add((t,pos))
with Path(f"data/export/{DATE}-{ROUTE}.csv").open(encoding="utf-8-sig",newline="") as f:rows=list(csv.DictReader(f))
results=[]
for r in rows:
 if r.get("班次类型")!="疑似区间车":continue
 dep=dt(r.get("发车时间",""));arr=dt(r.get("区间站到达时间",""))
 match=re.search(r"seq(\d+)",r.get("区间站",""));seq=int(match.group(1)) if match else None
 points=sorted(samples.get((r["车牌号"],r["方向"]),set()))
 start=[(t,p) for t,p in points if dep and dep-timedelta(minutes=5)<=t<=dep+timedelta(minutes=15)]
 end=[(t,p) for t,p in points if arr and arr-timedelta(minutes=15)<=t<=arr+timedelta(minutes=5)]
 near_start=sorted(start,key=lambda x:(abs((x[0]-dep).total_seconds()),x[1]))[:1]
 near_end=sorted(end,key=lambda x:(abs((x[0]-arr).total_seconds()),-x[1]))[:1]
 def show(x):return {"sample":x[0].isoformat(),"physical_seq":x[1],"offset_minutes":round((x[0]-(dep if x in near_start else arr)).total_seconds()/60,1)} if x else None
 start_point=near_start[0] if near_start else None;end_point=near_end[0] if near_end else None
 flags=[]
 if not start_point:flags.append("NO_START_OBSERVATION_WITHIN_WINDOW")
 elif start_point[1]>5:flags.append("START_ALREADY_BEYOND_SEQ5")
 if not end_point:flags.append("NO_END_OBSERVATION_WITHIN_WINDOW")
 elif seq is not None and abs(end_point[1]-seq)>5:flags.append("END_POSITION_MORE_THAN_5_STOPS_FROM_TURN")
 if not arr:flags.append("NO_TURN_ARRIVAL_ESTIMATE")
 results.append({"plate":r["车牌号"],"departure":r["发车时间"],"turn_arrival":r.get("区间站到达时间"),"turn_seq":seq,"start_observation":{"time":start_point[0].isoformat(),"seq":start_point[1],"offset_minutes":round((start_point[0]-dep).total_seconds()/60,1)} if start_point else None,"end_observation":{"time":end_point[0].isoformat(),"seq":end_point[1],"offset_minutes":round((end_point[0]-arr).total_seconds()/60,1)} if end_point else None,"flags":flags,"note":"Observations near planned dispatch/estimated turn are diagnostic only, not proof of exact departure or arrival."})
out=Path(f"data/export/{DATE}-{ROUTE}-endpoint-validation.json")
out.write_text(json.dumps({"date":DATE,"route":ROUTE,"checks":results},ensure_ascii=False,indent=2)+"\n",encoding="utf-8")
print(json.dumps({"checks":len(results),"flagged":sum(bool(x["flags"]) for x in results)},ensure_ascii=False))
