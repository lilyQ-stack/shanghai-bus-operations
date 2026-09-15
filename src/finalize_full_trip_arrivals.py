from __future__ import annotations

import csv
import json
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
TZ = ZoneInfo("Asia/Shanghai")
MAX_TRIP_HOURS = 4

def parse_dt(date,hm):
    try:return datetime.fromisoformat(f"{date}T{hm}:00+08:00").astimezone(TZ)
    except Exception:return None

def read_csv(path):
    with path.open(encoding="utf-8-sig",newline="") as f:return list(csv.DictReader(f))
def write_csv(path,rows,fields):
    with path.open("w",encoding="utf-8-sig",newline="") as f:
        w=csv.DictWriter(f,fieldnames=fields);w.writeheader();w.writerows({k:r.get(k,"") for k in fields} for r in rows)

def load_terminal_events(date):
    terminal_info={};events=defaultdict(list)
    for path in sorted((ROOT/"data"/"shmaas").glob(f"{date}-*.jsonl")):
        with path.open(encoding="utf-8") as f:
            for line in f:
                if not line.strip():continue
                try:
                    snap=json.loads(line)
                    if not snap.get("success"):continue
                    route=str(snap.get("route") or "");captured=datetime.fromisoformat(snap["sample_time_cst"]).astimezone(TZ)
                except Exception:continue
                for d in snap.get("directions",[]) or []:
                    try:direction=int(d.get("direction"))
                    except Exception:continue
                    terminal_info[(route,direction)]={"name":str(d.get("end_stop") or ""),"seq":int(d.get("stop_count") or 0)}
                for vehicle in snap.get("vehicles",[]) or []:
                    plate=str(vehicle.get("plate") or "").strip()
                    if not plate:continue
                    for obs in vehicle.get("observations",[]) or []:
                        try:direction=int(obs.get("direction"));eta=float(obs.get("arrive_time"))
                        except (TypeError,ValueError):continue
                        if str(obs.get("role") or "") not in {"current","next"}:continue
                        info=terminal_info.get((route,direction),{});is_terminal=str(obs.get("stop_name") or "")==info.get("name") or (info.get("seq") and int(obs.get("stop_seq") or 0)==info.get("seq"))
                        if not is_terminal or eta<0 or eta>180:continue
                        events[(route,plate,direction)].append({"time":captured,"eta":eta,"predicted":captured+timedelta(minutes=eta),"stop_name":str(obs.get("stop_name") or "")})
    for key in events:events[key].sort(key=lambda x:x["time"])
    return events

def main():
    date=datetime.now(TZ).date().isoformat();export=ROOT/"data"/"export";combined=export/f"{date}-operations.csv"
    if not combined.exists():print("No operations export; skip full-trip arrival fallback");return 0
    rows=read_csv(combined)
    if not rows:return 0
    fields=list(rows[0].keys());terminal_events=load_terminal_events(date);same_direction_departures=defaultdict(list)
    for row in rows:
        dep=parse_dt(date,row.get("发车时间",""))
        if dep:same_direction_departures[(row.get("线路",""),row.get("车牌号",""),row.get("方向",""))].append(dep)
    for key in same_direction_departures:same_direction_departures[key].sort()
    filled=0
    for row in rows:
        if row.get("班次类型")!="全程车" or row.get("到达时间") not in {"","待确认"}:continue
        route,plate,direction_text=row.get("线路",""),row.get("车牌号",""),row.get("方向","");dep=parse_dt(date,row.get("发车时间",""))
        if not dep:continue
        try:direction=int(direction_text)
        except Exception:continue
        next_same_dep=next((x for x in same_direction_departures[(route,plate,direction_text)] if x>dep),None);limit=min(next_same_dep,dep+timedelta(hours=MAX_TRIP_HOURS)) if next_same_dep else dep+timedelta(hours=MAX_TRIP_HOURS)
        candidates=[e for e in terminal_events.get((route,plate,direction),[]) if dep<=e["time"]<limit and dep<=e["predicted"]<=dep+timedelta(hours=MAX_TRIP_HOURS)]
        if not candidates:continue
        last=max(candidates,key=lambda e:e["time"]);arrival=last["predicted"];row["到达时间"]=arrival.astimezone(TZ).strftime("%H:%M");row["到达时间说明"]=f"SHMAAS末次终点ETA推算：{last['time'].astimezone(TZ).strftime('%H:%M')}采样，终点ETA {round(last['eta'])}分钟";row["到达误差估计（分钟）"]="";minutes=round((arrival-dep).total_seconds()/60)
        if 0<=minutes<=MAX_TRIP_HOURS*60:row["全程时间"]=f"{minutes}分钟"
        filled+=1
    write_csv(combined,rows,fields);by_route=defaultdict(list)
    for row in rows:by_route[row.get("线路","")].append(row)
    for route,route_rows in by_route.items():write_csv(export/f"{date}-{route.replace('/','_')}.csv",route_rows,[f for f in fields if f!="线路"])
    meta_path=export/f"{date}-operations-meta.json"
    if meta_path.exists():
        meta=json.loads(meta_path.read_text(encoding="utf-8"));meta["last_terminal_eta_fallback_count"]=filled;meta["arrival_fallback_rule"]="Only full trips still lacking arrival use the last valid same-direction terminal ETA in the trip window; short-turn/anomaly rows are excluded and existing arrivals are never overwritten.";meta_path.write_text(json.dumps(meta,ensure_ascii=False,indent=2),encoding="utf-8")
    print(f"Filled {filled} full-trip arrivals from last terminal ETA");return 0
if __name__=="__main__":raise SystemExit(main())
