#!/usr/bin/env python3
"""Conservative segment-matched short-turn ranking, never fabricate crossings."""
import csv,json,statistics,re
from pathlib import Path
from collections import defaultdict
from datetime import datetime,timedelta
DATE="2026-09-29"; ROUTE="浦东35路"
raw=Path(f"data/shmaas/{DATE}-{ROUTE}.jsonl")
export=Path(f"data/export/{DATE}-{ROUTE}.csv")
out=Path(f"data/export/{DATE}-{ROUTE}-short-turn-audit.json")
ranking=Path(f"data/export/{DATE}-{ROUTE}-combined-ranking.json")
def minute(hm):
 try:return datetime.fromisoformat(DATE+"T"+hm+":00+08:00")
 except:return None
def value(x):
 try:return float(x)
 except:return None
with export.open(encoding="utf-8-sig",newline="") as f: rows=list(csv.DictReader(f))
events=defaultdict(list); samples=0
with raw.open(encoding="utf-8-sig") as f:
 for line in f:
  if not line.strip():continue
  try:s=json.loads(line)
  except ValueError:continue
  if not s.get("success"):continue
  samples+=1
  t=datetime.fromisoformat(s["sample_time_cst"])
  for v in s.get("vehicles",[]):
   plate=v.get("plate")
   for o in v.get("observations",[]):
    try:
     d=int(o["direction"]); probe=int(o["stop_seq"]); remaining=int(o["remaining_stops"])
    except (KeyError,TypeError,ValueError):continue
    if not plate or o.get("role") not in ("current","next"):continue
    pos=max(1,probe-remaining)
    events[(plate,str(d))].append((t,pos))
for key in events:events[key]=sorted(set(events[key]))
def crossing(plate,direction,dep,arrival,seq):
 """Require a physical position bracketing target, no extrapolation."""
 if not dep or not arrival:return None
 points=[(t,p) for t,p in events.get((plate,direction),[]) if dep<=t<=arrival]
 candidates=[]
 for (t1,p1),(t2,p2) in zip(points,points[1:]):
  gap=(t2-t1).total_seconds()/60
  if 0<gap<=15 and p1<=seq<=p2 and 0<p2-p1<=10:
   at=t1+(t2-t1)*(seq-p1)/(p2-p1)
   elapsed=(at-dep).total_seconds()/60
   if elapsed>0:candidates.append(elapsed)
 return min(candidates) if candidates else None
full=[r for r in rows if r["班次类型"]=="全程车" and value(re.search(r"\d+",r.get("全程时间","")).group(0)) if re.search(r"\d+",r.get("全程时间",""))]
short=[r for r in rows if r["班次类型"]=="疑似区间车"]
by_plate=defaultdict(lambda:{"full":[],"short":[],"excluded":[]})
for r in full:
 m=re.search(r"\d+",r["全程时间"])
 if m:by_plate[r["车牌号"]]["full"].append(float(m.group()))
report={"date":DATE,"route":ROUTE,"raw_samples":samples,"observed_plates":len({p for p,d in events}),"method":"Physical position=probe stop_seq-remaining_stops. Reference crossing must be bracketed by two same-trip observations <=15 min apart, <=10-stop progression; >=2 same-direction full-trip peers; >=25% route coverage; no unobserved crossing extrapolation.","short_turns":[]}
for r in short:
 plate=r["车牌号"]; dep=minute(r["发车时间"]); arr=minute(r.get("区间站到达时间",""))
 match=re.search(r"seq(\d+)",r.get("区间站",""))
 seq=int(match.group(1)) if match else None
 direction=r["方向"]; reason=""
 if not dep or not arr or not seq:reason="Missing reliable turnaround arrival or physical sequence mapping"
 elif seq<17:reason="Turnaround below 25% route coverage (conservative 66-stop denominator)"
 else:
  peers=[]
  for f in full:
   if f["方向"]!=direction or f["车牌号"]==plate:continue
   fd=minute(f["发车时间"]); fa=minute(f["到达时间"])
   if fa and fd and fa<fd:fa+=timedelta(days=1)
   segment=crossing(f["车牌号"],direction,fd,fa,seq)
   total=value(re.search(r"\d+",f["全程时间"]).group()) if re.search(r"\d+",f["全程时间"]) else None
   if segment and total and segment<total:peers.append((segment,total,f["车牌号"]))
  if len(peers)<2:reason="Fewer than 2 independently bracketed same-direction full-trip reference crossings"
  else:
   elapsed=(arr-dep).total_seconds()/60
   if elapsed<=0:reason="Nonpositive short-turn elapsed time"
   else:
    ratios=[total/segment for segment,total,p in peers]
    standardized=round(elapsed*statistics.median(ratios),1)
    by_plate[plate]["short"].append(standardized)
    report["short_turns"].append({"plate":plate,"departure":r["发车时间"],"direction":direction,"turn_seq":seq,"segment_minutes":round(elapsed,1),"reference_count":len(peers),"reference_ratio_median":round(statistics.median(ratios),3),"standardized_full_minutes":standardized,"eligible":True})
    continue
 by_plate[plate]["excluded"].append(r["发车时间"])
 report["short_turns"].append({"plate":plate,"departure":r["发车时间"],"direction":direction,"turn_seq":seq,"eligible":False,"reason":reason})
results=[]
for plate,b in by_plate.items():
 vals=b["full"]+b["short"]
 results.append({"plate":plate,"full_trips":len(b["full"]),"standardized_short_trips":len(b["short"]),"excluded_short_trips":len(b["excluded"]),"average_standardized_minutes":round(statistics.mean(vals),1) if vals else None,"short_turn_marker":bool(b["short"])})
eligible=sorted((r for r in results if r["average_standardized_minutes"] is not None),key=lambda r:r["average_standardized_minutes"])
for i,r in enumerate(eligible):
 r["rank"]=1+sum(x["average_standardized_minutes"]<r["average_standardized_minutes"] for x in eligible)
for r in results:
 if "rank" not in r:r["rank"]=None
ranking.write_text(json.dumps({"date":DATE,"route":ROUTE,"status":"conservative_partial_ranking","vehicles":sorted(results,key=lambda r:r["rank"] if r["rank"] else 999)},ensure_ascii=False,indent=2)+"\n",encoding="utf-8")
report["eligible_short_turn_count"]=sum(bool(r["eligible"]) for r in report["short_turns"])
out.write_text(json.dumps(report,ensure_ascii=False,indent=2)+"\n",encoding="utf-8")
print(json.dumps({"samples":samples,"short_turns":len(short),"eligible":report["eligible_short_turn_count"],"ranking":str(ranking)},ensure_ascii=False))
