from __future__ import annotations
import argparse,csv,json
from collections import defaultdict
from datetime import datetime
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]

def load_ops(date):
 p=ROOT/"data/export"/f"{date}-operations.csv"
 if not p.exists(): return []
 with p.open(encoding="utf-8-sig",newline="") as f:return list(csv.DictReader(f))

def load_snaps(date):
 out=[]
 for p in sorted((ROOT/"data/shmaas").glob(f"{date}-*.jsonl")):
  if p.name.endswith("-reverse-watch.jsonl"):continue
  for line in p.open(encoding="utf-8"):
   try:
    s=json.loads(line)
    if s.get("success") and s.get("sample_time_cst"):out.append(s)
   except:pass
 return sorted(out,key=lambda s:s["sample_time_cst"])

def phys(o):
 try:return max(1,int(o["stop_seq"])-int(float(o["remaining_stops"])))
 except:return None

def main():
 ap=argparse.ArgumentParser();ap.add_argument("--date",required=True);a=ap.parse_args()
 ops=load_ops(a.date);snaps=load_snaps(a.date); by=defaultdict(list)
 for s in snaps:
  tm=s["sample_time_cst"]
  for v in s.get("vehicles") or []:
   plate=v.get("plate")
   for o in v.get("observations") or []:
    if o.get("role") not in ("current","next"):continue
    try:d=int(o.get("direction"))
    except:continue
    ps=phys(o)
    if ps is None:continue
    by[(s.get("route"),plate,d)].append({"time":tm,"physical_seq":ps,"eta_min":o.get("arrive_time"),"queried_stop_seq":o.get("stop_seq"),"remaining_stops":o.get("remaining_stops")})
 out=[]
 for r in ops:
  try:dep=datetime.fromisoformat(f'{a.date}T{r["发车时间"]}:00+08:00')
  except:continue
  key=(r.get("线路"),r.get("车牌号"),int(r.get("方向") or 0))
  pts=[]
  for x in by.get(key,[]):
   try:t=datetime.fromisoformat(x["time"])
   except:continue
   if t>=dep:pts.append(x)
  # stop at next operation of same plate
  nextdeps=[]
  for q in ops:
   if q.get("线路")==r.get("线路") and q.get("车牌号")==r.get("车牌号"):
    try:nd=datetime.fromisoformat(f'{a.date}T{q["发车时间"]}:00+08:00')
    except:continue
    if nd>dep:nextdeps.append(nd)
  if nextdeps:
   end=min(nextdeps);pts=[x for x in pts if datetime.fromisoformat(x["time"])<end]
  # compact duplicate observations at same sample/physical seq
  seen=set();compact=[]
  for x in pts:
   k=(x["time"],x["physical_seq"])
   if k not in seen:seen.add(k);compact.append(x)
  out.append({"route":r.get("线路"),"plate":r.get("车牌号"),"direction":r.get("方向"),"departure":r.get("发车时间"),"service_type":r.get("班次类型"),"arrival":r.get("到达时间"),"trajectory":compact})
 p=ROOT/"data/export"/f"{a.date}-trip-trajectories.json"
 p.write_text(json.dumps(out,ensure_ascii=False,separators=(",",":"))+"\n",encoding="utf-8")
 print(f"Wrote {len(out)} trip trajectories to {p}")
if __name__=="__main__":main()
