from __future__ import annotations
import csv,json,statistics
from collections import defaultdict
from datetime import datetime,timedelta
from pathlib import Path
from zoneinfo import ZoneInfo
ROOT=Path(__file__).resolve().parents[1];TZ=ZoneInfo("Asia/Shanghai");MAX_TRIP_MIN=240;REVISION_WINDOW_MIN=20
ROUTES=["浦东78路","浦东35路","182路"]

def parse_dt(date,hm):
    try:return datetime.fromisoformat(f"{date}T{hm}:00+08:00").astimezone(TZ)
    except Exception:return None

def read_csv(path):
    with path.open(encoding="utf-8-sig",newline="") as f:return list(csv.DictReader(f))
def write_csv(path,rows,fields):
    with path.open("w",encoding="utf-8-sig",newline="") as f:
        w=csv.DictWriter(f,fieldnames=fields);w.writeheader();w.writerows({k:r.get(k,"") for k in fields} for r in rows)

def load_raw(date):
    route_info={};events=defaultdict(list);sightings=defaultdict(lambda:defaultdict(list))
    for path in sorted((ROOT/"data"/"shmaas").glob(f"{date}-*.jsonl")):
        if "reverse-watch" in path.name:continue
        for line in path.read_text(encoding="utf-8").splitlines():
            try:
                snap=json.loads(line)
                if not snap.get("success"):continue
                route=str(snap.get("route") or "");captured=datetime.fromisoformat(snap["sample_time_cst"]).astimezone(TZ)
            except Exception:continue
            for item in snap.get("directions") or []:
                try:direction=int(item.get("direction"))
                except Exception:continue
                route_info[(route,direction)]={"start":str(item.get("start_stop") or ""),"end":str(item.get("end_stop") or ""),"count":int(item.get("stop_count") or 0)}
            for vehicle in snap.get("vehicles") or []:
                plate=str(vehicle.get("plate") or "").strip()
                if not plate:continue
                for obs in vehicle.get("observations") or []:
                    try:direction=int(obs.get("direction"))
                    except Exception:continue
                    role=str(obs.get("role") or "");dispatch=str(obs.get("dispatch_time") or "").strip()
                    if role=="dispatch" and dispatch:
                        sightings[(route,plate,direction)][dispatch].append(captured);continue
                    if role not in {"current","next"}:continue
                    try:eta=float(obs.get("eta_min",obs.get("arrive_time")))
                    except Exception:eta=None
                    try:seq=int(obs.get("stop_seq"))
                    except Exception:seq=None
                    events[(route,plate,direction)].append({"time":captured,"eta":eta,"seq":seq,"stop":str(obs.get("stop_name") or "")})
    for key in events:events[key].sort(key=lambda e:e["time"])
    return route_info,events,sightings

def infer_direction(route,row,route_info):
    start=row.get("发车站","");end=row.get("终点站","")
    exact=[d for d in (0,1) if (route_info.get((route,d)) or {}).get("start")==start and (route_info.get((route,d)) or {}).get("end")==end]
    if len(exact)==1:return exact[0]
    end_only=[d for d in (0,1) if (route_info.get((route,d)) or {}).get("end")==end]
    return end_only[0] if len(end_only)==1 else None

def row_evidence_score(row):
    score={"全程车":60,"疑似区间车":55,"运行中待确认":35,"运行异常待查":10}.get(row.get("班次类型",""),0)
    if row.get("最后可靠采集站点"):score+=20
    if row.get("预计到达时间"):score+=15
    if row.get("到达置信度") in {"A","B"}:score+=10
    return score

def dedupe(rows,date,sightings,route_info):
    groups=defaultdict(list)
    for row in rows:
        route=row.get("线路","");direction=infer_direction(route,row,route_info)
        key=(route,row.get("车牌号",""),direction,row.get("发车站",""),row.get("终点站",""));groups[key].append(row)
    kept=[];removed=0
    for key,group in groups.items():
        route,plate,direction,_,_=key;ordered=sorted(group,key=lambda r:r.get("发车时间",""));clusters=[]
        for row in ordered:
            dt=parse_dt(date,row.get("发车时间",""));prev=parse_dt(date,clusters[-1][-1].get("发车时间","")) if clusters else None
            if clusters and dt and prev and 0<=(dt-prev).total_seconds()/60<=REVISION_WINDOW_MIN:clusters[-1].append(row)
            else:clusters.append([row])
        schedule=sightings.get((route,plate,direction),{}) if direction is not None else {}
        for cluster in clusters:
            if len(cluster)==1:kept.append(cluster[0]);continue
            def rank(row):
                seen=schedule.get(row.get("发车时间",""),[]);last=max(seen) if seen else datetime.min.replace(tzinfo=TZ)
                return (row_evidence_score(row),last,len(seen),row.get("发车时间",""))
            kept.append(max(cluster,key=rank));removed+=len(cluster)-1
    print(f"dispatch revision dedupe removed {removed} rows")
    return sorted(kept,key=lambda r:(r.get("线路",""),r.get("发车时间",""),r.get("车牌号","")))

def median_consensus(predictions):
    if len(predictions)<2:return None
    vals=sorted(p.timestamp() for p in predictions);med=statistics.median(vals);spread=(max(vals)-min(vals))/60
    if spread>15:return None
    return datetime.fromtimestamp(med,TZ),max(5,round(spread/2))

def existing_terminal_evidence(row):
    method=row.get("到达估算方法","");confidence=row.get("到达置信度","");arrival=row.get("预计到达时间","")
    return (method=="终点ETA多样本共识" and confidence in {"A","B"} and bool(arrival)) or (method=="接近终点ETA" and confidence=="A" and bool(arrival))

def set_full_trip(row,reason=None):
    row["班次类型"]="全程车"
    if reason:row["区间/异常说明"]=reason
    row["参与车速排名"]="是"

def apply(date,rows,route_info,events):
    by_vehicle=defaultdict(list)
    for row in rows:
        dep=parse_dt(date,row.get("发车时间",""))
        if dep:by_vehicle[(row.get("线路",""),row.get("车牌号",""))].append((dep,row))
    for seq in by_vehicle.values():seq.sort(key=lambda x:x[0])
    refreshed_full=0;promoted_waiting=0;promoted_anomaly=0;new_terminal_promotions=0
    for row in rows:
        original_class=row.get("班次类型","")
        if original_class=="疑似区间车":continue
        if existing_terminal_evidence(row):
            if original_class=="全程车":
                refreshed_full+=1
            else:
                set_full_trip(row,"已有可靠同向终点ETA证据；分类与终到证据统一为全程车")
                if original_class=="运行中待确认":promoted_waiting+=1
                elif original_class=="运行异常待查":promoted_anomaly+=1
            continue
        route,plate=row.get("线路",""),row.get("车牌号","");direction=infer_direction(route,row,route_info)
        if direction is None:continue
        dep=parse_dt(date,row.get("发车时间",""))
        if not dep:continue
        next_same=next((x for x,nr in by_vehicle[(route,plate)] if x>dep and nr.get("终点站")==row.get("终点站")),None);limit=min(next_same,dep+timedelta(minutes=MAX_TRIP_MIN)) if next_same else dep+timedelta(minutes=MAX_TRIP_MIN)
        info=route_info.get((route,direction)) or {};end_name=info.get("end","");end_seq=info.get("count",0);evs=[e for e in events.get((route,plate,direction),[]) if dep<=e["time"]<limit];terminal_evs=[e for e in evs if e["eta"] is not None and (e["stop"]==end_name or (end_seq and e["seq"]==end_seq)) and 0<=e["eta"]<=180]
        predictions=[e["time"]+timedelta(minutes=e["eta"]) for e in terminal_evs];consensus=median_consensus(predictions);close=[e for e in terminal_evs if e["eta"]<=5]
        if consensus or close:
            if consensus:arrival,err=consensus;method="终点ETA多样本共识"
            else:e=close[-1];arrival=e["time"]+timedelta(minutes=e["eta"]);err=5;method="接近终点ETA"
            set_full_trip(row,"同向终点ETA形成可靠证据；按全程运行处理")
            row["预计到达时间"]=arrival.strftime("%H:%M");row["全程时间（分钟）"]=f"{(arrival-dep).total_seconds()/60:.1f}";row["到达置信度"]="A" if err<=5 else "B";row["到达估算方法"]=method
            new_terminal_promotions+=1
            if original_class=="运行中待确认":promoted_waiting+=1
            elif original_class=="运行异常待查":promoted_anomaly+=1
    print(f"terminal evidence audit: refreshed_existing_full={refreshed_full}, promoted_waiting={promoted_waiting}, promoted_anomaly={promoted_anomaly}, newly_proven_by_raw_terminal_eta={new_terminal_promotions}")
    return rows

def main():
    import argparse
    parser=argparse.ArgumentParser();parser.add_argument("--date");args=parser.parse_args();date=args.date or datetime.now(TZ).strftime("%Y-%m-%d");export=ROOT/"data"/"export"
    rows=[];fields=None
    for route in ROUTES:
        path=export/f"{date}-{route}.csv"
        if not path.exists():continue
        route_rows=read_csv(path)
        if route_rows and fields is None:fields=list(route_rows[0].keys())
        for row in route_rows:
            row["线路"]=route;rows.append(row)
    if not rows:
        print(f"legacy quality overlay: no per-route exports found for {date}");return 0
    print(f"legacy quality overlay: loaded {len(rows)} per-route rows")
    route_info,events,sightings=load_raw(date);rows=dedupe(rows,date,sightings,route_info);rows=apply(date,rows,route_info,events)
    by_route=defaultdict(list)
    for row in rows:by_route[row.get("线路","")].append(row)
    out_fields=fields or []
    for route in ROUTES:write_csv(export/f"{date}-{route}.csv",by_route.get(route,[]),out_fields)
    print(f"legacy quality overlay: wrote {len(rows)} trips after dedupe");return 0
if __name__=="__main__":raise SystemExit(main())
