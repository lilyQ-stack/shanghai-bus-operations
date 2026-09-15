from __future__ import annotations

import csv
import json
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
TZ = ZoneInfo("Asia/Shanghai")
REVISION_WINDOW_MIN = 20
MAX_SHORT_TURN_ERROR_MIN = 10
MIN_REVERSE_ELAPSED_MIN = 20


def parse_dt(date: str, hm: str) -> datetime | None:
    try:
        return datetime.fromisoformat(f"{date}T{hm}:00+08:00").astimezone(TZ)
    except Exception:
        return None


def fmt(dt: datetime | None) -> str:
    return dt.astimezone(TZ).strftime("%H:%M") if dt else ""


def num(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def load_raw(date: str):
    events = defaultdict(list)
    sightings = defaultdict(lambda: defaultdict(list))
    for path in sorted((ROOT / "data" / "shmaas").glob(f"{date}-*.jsonl")):
        with path.open(encoding="utf-8") as f:
            for line in f:
                if not line.strip():
                    continue
                try:
                    snap = json.loads(line)
                    if not snap.get("success"):
                        continue
                    route = str(snap.get("route") or "")
                    captured = datetime.fromisoformat(snap["sample_time_cst"]).astimezone(TZ)
                except Exception:
                    continue
                for vehicle in snap.get("vehicles", []) or []:
                    plate = str(vehicle.get("plate") or "").strip()
                    if not plate:
                        continue
                    for obs in vehicle.get("observations", []) or []:
                        try:
                            direction = int(obs.get("direction"))
                        except (TypeError, ValueError):
                            continue
                        role = str(obs.get("role") or "")
                        eta = num(obs.get("arrive_time"))
                        events[(route, plate)].append({"time": captured,"direction": direction,"role": role,"stop_name": str(obs.get("stop_name") or ""),"stop_seq": obs.get("stop_seq"),"eta": eta})
                        dispatch = str(obs.get("dispatch_time") or "").strip()
                        if dispatch:
                            sightings[(route, plate, direction)][dispatch].append(captured)
    for key in events:
        events[key].sort(key=lambda x: x["time"])
    return events, sightings


def dedupe_rows(rows: list[dict], date: str, sightings) -> tuple[list[dict], int]:
    grouped = defaultdict(list)
    for row in rows:
        grouped[(row.get("线路", ""), row.get("车牌号", ""), row.get("方向", ""))].append(row)
    kept=[]; removed=0
    for key, group in grouped.items():
        ordered=sorted(group,key=lambda r:r.get("发车时间","")); clusters=[]
        for row in ordered:
            dt=parse_dt(date,row.get("发车时间",""))
            if not dt or not clusters:
                clusters.append([row]); continue
            prev_dt=parse_dt(date,clusters[-1][-1].get("发车时间",""))
            if prev_dt and 0 <= (dt-prev_dt).total_seconds()/60 <= REVISION_WINDOW_MIN: clusters[-1].append(row)
            else: clusters.append([row])
        route,plate,direction_text=key
        try: direction=int(direction_text)
        except Exception: direction=None
        schedule_map=sightings.get((route,plate,direction),{}) if direction is not None else {}
        for cluster in clusters:
            if len(cluster)==1: kept.append(cluster[0]); continue
            def rank(row):
                times=schedule_map.get(row.get("发车时间",""),[])
                last_seen=max(times) if times else datetime.min.replace(tzinfo=TZ)
                return (last_seen,len(times),row.get("发车时间",""))
            kept.append(max(cluster,key=rank)); removed += len(cluster)-1
    kept.sort(key=lambda r:(r.get("线路",""),r.get("发车时间",""),r.get("车牌号","")))
    return kept,removed


def seq_num(event:dict)->int:
    try:return int(event.get("stop_seq") or 0)
    except Exception:return 0


def enrich_short_turn_times(rows,date,events)->int:
    enriched=0; same_direction_departures=defaultdict(list)
    for row in rows:
        dep=parse_dt(date,row.get("发车时间",""))
        if dep:same_direction_departures[(row.get("线路",""),row.get("车牌号",""),row.get("方向",""))].append(dep)
    for key in same_direction_departures:same_direction_departures[key].sort()
    for row in rows:
        for k in ("区间站","区间站到达时间","区间站折返发车时间","区间时间说明"):row.setdefault(k,"")
        if row.get("班次类型")!="疑似区间车":continue
        dep=parse_dt(date,row.get("发车时间",""))
        if not dep:continue
        route,plate,direction_text=row.get("线路",""),row.get("车牌号",""),row.get("方向","")
        try:direction=int(direction_text)
        except Exception:continue
        next_same_dep=next((x for x in same_direction_departures[(route,plate,direction_text)] if x>dep),None)
        moving=[e for e in events.get((route,plate),[]) if e["time"]>=dep and (next_same_dep is None or e["time"]<next_same_dep) and e["role"] in {"current","next"} and e["eta"] is not None]
        same=[e for e in moving if e["direction"]==direction]
        opp=[e for e in moving if e["direction"]!=direction and (e["time"]-dep).total_seconds()/60>=MIN_REVERSE_ELAPSED_MIN]
        if not same or not opp:continue
        first_opp=min(opp,key=lambda e:e["time"])
        last_same=max((e for e in same if e["time"]<first_opp["time"]),key=lambda e:(e["time"],seq_num(e)),default=None)
        if not last_same:continue
        row["区间站"]=last_same.get("stop_name") or str(last_same.get("stop_seq") or "")
        eta=last_same.get("eta"); arrival_est=None
        if eta is not None and 0<=eta<=MAX_SHORT_TURN_ERROR_MIN:
            arrival_est=last_same["time"]+timedelta(minutes=eta); row["区间站到达时间"]=fmt(arrival_est)
        reverse_eta=first_opp.get("eta"); reverse_station=first_opp.get("stop_name") or str(first_opp.get("stop_seq") or "")
        reverse_anchor=first_opp["time"]+timedelta(minutes=reverse_eta) if reverse_eta is not None and 0<=reverse_eta<=180 else None
        if arrival_est:
            upper=min(first_opp["time"],reverse_anchor) if reverse_anchor is not None else first_opp["time"]
            span=(upper-arrival_est).total_seconds()/60
            if 0<=span<=2*MAX_SHORT_TURN_ERROR_MIN:
                turn=arrival_est+(upper-arrival_est)/2; row["区间站折返发车时间"]=fmt(turn)
                row["区间时间说明"]=f"到达按末次同向重点站ETA估算；折返发车结合首次反向运行观测取区间中点，约±{max(1,round(span/2))}分钟"+(f"；反向重点站{reverse_station} ETA {round(reverse_eta)}分钟参与校验" if reverse_eta is not None else "")
                enriched+=1; continue
        row["区间时间说明"]="区间折返已识别，但现有采样不足以把折返发车时间控制在±10分钟"
    return enriched


def read_csv(path):
    with path.open(encoding="utf-8-sig",newline="") as f:return list(csv.DictReader(f))

def write_csv(path,rows,fields):
    with path.open("w",encoding="utf-8-sig",newline="") as f:
        w=csv.DictWriter(f,fieldnames=fields);w.writeheader();w.writerows({k:r.get(k,"") for k in fields} for r in rows)

def main()->int:
    date=datetime.now(TZ).date().isoformat(); export=ROOT/"data"/"export"; combined=export/f"{date}-operations.csv"
    if not combined.exists():print("No combined operations export; skip postprocess");return 0
    events,sightings=load_raw(date); rows=read_csv(combined); rows,removed=dedupe_rows(rows,date,sightings); enriched=enrich_short_turn_times(rows,date,events)
    base_fields=list(rows[0].keys()) if rows else []; extra=["区间站","区间站到达时间","区间站折返发车时间","区间时间说明"]; fields=[f for f in base_fields if f not in extra]+extra; write_csv(combined,rows,fields)
    by_route=defaultdict(list)
    for row in rows:by_route[row.get("线路","")].append(row)
    for route,route_rows in by_route.items():write_csv(export/f"{date}-{route.replace('/','_')}.csv",route_rows,[f for f in fields if f!="线路"])
    meta_path=export/f"{date}-operations-meta.json"
    if meta_path.exists():
        meta=json.loads(meta_path.read_text(encoding="utf-8"));meta["dispatch_revision_deduped_count"]=removed;meta["trip_row_count"]=len(rows);meta["short_turn_time_enriched_count"]=enriched;meta["departure_status"]="Private-pipeline dispatch revision dedupe: same route/plate/direction revisions within 20 minutes are clustered and the schedule observed latest by SHMAAS is retained.";meta_path.write_text(json.dumps(meta,ensure_ascii=False,indent=2),encoding="utf-8")
    print(f"Postprocessed {len(rows)} trips; removed {removed} schedule revision rows; enriched {enriched} short-turn timing rows");return 0

if __name__=="__main__":raise SystemExit(main())
