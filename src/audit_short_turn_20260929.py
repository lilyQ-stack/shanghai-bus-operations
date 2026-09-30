#!/usr/bin/env python3
"""Audit short-turn ranking evidence without inventing missing segment times."""
import csv,json
from pathlib import Path
from collections import defaultdict
date="2026-09-29"; route="浦东35路"
raw=Path(f"data/shmaas/{date}-{route}.jsonl")
export=Path(f"data/export/{date}-{route}.csv")
out=Path(f"data/export/{date}-{route}-short-turn-audit.json")
samples=0; sightings=defaultdict(list); stops=defaultdict(set)
with raw.open(encoding="utf-8-sig") as f:
 for line in f:
  if not line.strip():continue
  try: s=json.loads(line)
  except json.JSONDecodeError:continue
  if not s.get("success"):continue
  samples+=1
  for v in s.get("vehicles",[]):
   plate=v.get("plate","")
   for o in v.get("observations",[]):
    d=o.get("direction"); seq=o.get("stop_seq")
    if plate and d is not None and seq is not None:
     sightings[plate].append({"time":s.get("sample_time_cst"),"direction":d,"stop_seq":seq,"remaining_stops":o.get("remaining_stops"),"role":o.get("role"),"stop_name":o.get("stop_name")})
     stops[str(d)].add(str(seq))
with export.open(encoding="utf-8-sig",newline="") as f: rows=list(csv.DictReader(f))
short=[r for r in rows if r.get("班次类型")=="疑似区间车"]
report={"date":date,"route":route,"raw_samples":samples,"observed_plates":len(sightings),"distinct_stop_sequences_by_direction":{k:len(v) for k,v in stops.items()},"method":"audit only: physical observation stop_seq is probe location, not necessarily actual vehicle position; do not calculate segment times without trip-aligned physical positions","short_turns":[]}
for r in short:
 plate=r["车牌号"]
 report["short_turns"].append({"plate":plate,"departure":r["发车时间"],"direction":r["方向"],"turn_stop":r["区间站"],"turn_arrival":r["区间站到达时间"],"observation_count":len(sightings[plate]),"eligible_for_standardized_ranking":False,"reason":"Need trip-aligned physical positions, matching same-direction same-segment full-trip reference and reliable elapsed time."})
out.write_text(json.dumps(report,ensure_ascii=False,indent=2)+"\n",encoding="utf-8")
print(json.dumps({"samples":samples,"short_turns":len(short),"output":str(out)},ensure_ascii=False))
