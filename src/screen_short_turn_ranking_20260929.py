#!/usr/bin/env python3
"""Publish a validated ranking view without altering the original provisional calculations."""
import json
from pathlib import Path
base=Path("data/export/2026-09-29-浦东35路")
ranking=json.loads(Path(str(base)+"-combined-ranking.json").read_text(encoding="utf-8"))
checks=json.loads(Path(str(base)+"-endpoint-validation.json").read_text(encoding="utf-8"))["checks"]
audit=json.loads(Path(str(base)+"-short-turn-audit.json").read_text(encoding="utf-8"))["short_turns"]
excluded={(x["plate"],x["departure"]) for x in checks if x["flags"]}
excluded.update((x["plate"],x["departure"]) for x in audit if not x.get("eligible"))
# The aggregate-only file cannot safely subtract individual short-trip times from a rounded mean.
# Recompute from the original full-trip export and the individually audited short trips.
import csv,statistics,re
from collections import defaultdict
with Path(str(base)+".csv").open(encoding="utf-8-sig",newline="") as f:rows=list(csv.DictReader(f))
groups=defaultdict(lambda:{"full":[],"short":[],"excluded":[]})
for r in rows:
 p=r["车牌号"]
 if r["班次类型"]=="全程车":
  m=re.search(r"\d+(?:\.\d+)?",r.get("全程时间",""))
  if m:groups[p]["full"].append(float(m.group()))
 elif r["班次类型"]=="疑似区间车":
  k=(p,r["发车时间"])
  a=next((x for x in audit if (x["plate"],x["departure"])==k),None)
  if k in excluded or not a or not a.get("eligible"):groups[p]["excluded"].append(r["发车时间"])
  else:groups[p]["short"].append(float(a["standardized_full_minutes"]))
result=[]
for plate,g in groups.items():
 vals=g["full"]+g["short"]
 result.append({"plate":plate,"full_trips":len(g["full"]),"validated_short_trips":len(g["short"]),"excluded_short_departures":g["excluded"],"average_minutes":round(statistics.mean(vals),1) if vals else None,"marker":"【区间】" if g["short"] else ""})
valid=[x for x in result if x["average_minutes"] is not None]
for x in result:x["rank"]=1+sum(y["average_minutes"]<x["average_minutes"] for y in valid) if x["average_minutes"] is not None else None
result.sort(key=lambda x:(x["rank"] is None,x["rank"] or 999,x["plate"]))
out=Path(str(base)+"-validated-ranking.json")
out.write_text(json.dumps({"date":"2026-09-29","route":"浦东35路","status":"endpoint_screened_provisional_not_final","screening":"Exclude any short trip with endpoint-validation flags; original full trips retained; do not infer missing actual departure/arrival times.","vehicles":result},ensure_ascii=False,indent=2)+"\n",encoding="utf-8")
print(json.dumps({"vehicles":len(result),"ranked":len(valid),"excluded_short_trips":len(excluded)},ensure_ascii=False))
