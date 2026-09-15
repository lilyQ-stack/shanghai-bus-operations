from __future__ import annotations
import csv,json,statistics
from collections import defaultdict
from datetime import datetime,timedelta
from pathlib import Path
from zoneinfo import ZoneInfo
ROOT=Path(__file__).resolve().parents[1];TZ=ZoneInfo("Asia/Shanghai");MAX_TRIP_MIN=240;REVISION_WINDOW_MIN=20

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
            for d in snap.get("directions") or []:
                try:direction=int(d.get("direction"))
                except Exception:continue
                route_info[(route,direction)]={"start":str(d.get("start_stop") or ""),"end":str(d.get("end_stop") or ""),"count":int(d.get("stop_count") or 0)}
            for vehicle in snap.get("vehicles") or []:
                plate=str(vehicle.get("plate") or "").strip()
                if not plate:continue
                for o in vehicle.get("observations") or []:
                    try:direction=int(o.get("direction"))
                    except Exception:continue
                    role=str(o.get("role") or "");dispatch=str(o.get("dispatch_time") or "").strip()
                    if role=="dispatch" and dispatch:sightings[(route,plate,direction)][dispatch].append(captured);continue
                    if role not in {"current","next"}:continue
                    try:eta=float(o.get("eta_min",o.get("arrive_time")))
                    except Exception:eta=None
                    try:seq=int(o.get("stop_seq"))
                    except Exception:seq=None
                    events[(route,plate,direction)].append({"time":captured,"eta":eta,"seq":seq,"stop":str(o.get("stop_name") or "")})
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
    for r in rows:
        route=r.get("线路","");d=infer_direction(route,r,route_info);groups[(route,r.get("车牌号",""),d,r.get("发车站",""),r.get("终点站","")].append(r)
    kept=[];removed=0
    for key,group in groups.items():
        route,plate,d,_,_=key;ordered=sorted(group,key=lambda r:r.get("发车时间",""));clusters=[]
        for r in ordered:
            dt=parse_dt(date,r.get("发车时间",""));prev=parse_dt(date,clusters[-1][-1].get("发车时间","")) if clusters else None
            if clusters and dt and prev and 0<=(dt-prev).total_seconds()/60<=REVISION_WINDOW_MIN:clusters[-1].append(r)
            else:clusters.append([r])
        schedule=sightings.get((route,plate,d),{}) if d is not None else {}
        for cluster in clusters:
            if len(cluster)==1:kept.append(cluster[0]);continue
            def rank(r):
                seen=schedule.get(r.get("发车时间",""),[]);last=max(seen) if seen else datetime.min.replace(tzinfo=TZ)
                return (row_evidence_score(r),last,len(seen),r.get("发车时间",""))
            kept.append(max(cluster,key=rank));removed+=len(cluster)-1
    print(f"dispatch revision dedupe removed {removed} rows")
    return sorted(kept,key=lambda r:(r.get("线路",""),r.get("发车时间",""),r.get("车牌号","")))

def median_consensus(predictions):
    if len(predictions)<2:return None
    vals=sorted(p.timestamp() for p in predictions);med=statistics.median(vals);spread=(max(vals)-min(vals))/60
    if spread>15:return None
    return datetime.fromtimestamp(med,TZ),max(5,round(spread/2))

def apply(date,rows,route_info,events):
    by_vehicle=defaultdict(list)
    for r in rows:
        dep=parse_dt(date,r.get("发车时间",""))
        if dep:by_vehicle[(r.get("线路",""),r.get("车牌号",""))].append((dep,r))
    for seq in by_vehicle.values():seq.sort(key=lambda x:x[0])
    history=defaultdict(list)
    for path in sorted((ROOT/"data"/"export").glob("????-??-??-operations.csv")):
        hdate=path.name[:10]
        if hdate>=date:continue
        try:hrows=read_csv(path)
        except Exception:continue
        for r in hrows:
            if r.get("班次类型")!="全程车":continue
            dep=parse_dt(hdate,r.get("发车时间",""));arr=parse_dt(hdate,r.get("预计到达时间",r.get("到达时间","")))
            if dep and arr:
                if arr<dep:arr+=timedelta(days=1)
                mins=(arr-dep).total_seconds()/60
                if 30<=mins<=MAX_TRIP_MIN:history[(r.get("线路",""),r.get("车牌号",""),r.get("发车站",""),r.get("终点站",""))].append(mins)
    for r in rows:
        if r.get("班次类型")=="疑似区间车":continue
        route,plate=r.get("线路",""),r.get("车牌号","");d=infer_direction(route,r,route_info)
        if d is None:continue
        dep=parse_dt(date,r.get("发车时间",""))
        if not dep:continue
        next_same=next((x for x,nr in by_vehicle[(route,plate)] if x>dep and nr.get("终点站")==r.get("终点站")),None);limit=min(next_same,dep+timedelta(minutes=MAX_TRIP_MIN)) if next_same else dep+timedelta(minutes=MAX_TRIP_MIN)
        info=route_info.get((route,d)) or {};end_name=info.get("end","");end_seq=info.get("count",0);evs=[e for e in events.get((route,plate,d),[]) if dep<=e["time"]<limit];terminal_evs=[e for e in evs if e["eta"] is not None and (e["stop"]==end_name or (end_seq and e["seq"]==end_seq)) and 0<=e["eta"]<=180]
        predictions=[e["time"]+timedelta(minutes=e["eta"]) for e in terminal_evs];consensus=median_consensus(predictions);close=[e for e in terminal_evs if e["eta"]<=5]
        if consensus or close:
            if consensus:arrival,err=consensus;method="终点ETA多样本共识"
            else:e=close[-1];arrival=e["time"]+timedelta(minutes=e["eta"]);err=5;method="接近终点ETA"
            r["班次类型"]="全程车";r["区间/异常说明"]="同向终点ETA形成可靠证据；按全程运行处理";r["预计到达时间"]=arrival.strftime("%H:%M");r["全程时间（分钟）"]=f"{(arrival-dep).total_seconds()/60:.1f}";r["到达置信度"]="A" if err<=5 else "B";r["到达估算方法"]=method;r["参与车速排名"]="是";continue
        if r.get("班次类型") in {"全程车","运行中待确认"} and not r.get("预计到达时间"):
            vals=history.get((route,plate,r.get("发车站",""),r.get("终点站","")),[])
            if len(vals)>=2:
                med=statistics.median(vals);spread=max(vals)-min(vals)
                if spread<=30:
                    arrival=dep+timedelta(minutes=med);r["预计到达时间"]=arrival.strftime("%H:%M");r["全程时间（分钟）"]=f"{med:.1f}";r["到达置信度"]="B" if spread<=20 else "C";r["到达估算方法"]="同车历史全程时间";r["参与车速排名"]="是"
    return rows

def main():
    import argparse
    p=argparse.ArgumentParser();p.add_argument("--date");a=p.parse_args();date=a.date or datetime.now(TZ).strftime("%Y-%m-%d");export=ROOT/"data"/"export";combined=export/f"{date}-operations.csv"
    if not combined.exists():return 0
    rows=read_csv(combined);route_info,events,sightings=load_raw(date);rows=dedupe(rows,date,sightings,route_info);rows=apply(date,rows,route_info,events);fields=list(rows[0].keys()) if rows else [];write_csv(combined,rows,fields);by_route=defaultdict(list)
    for r in rows:by_route[r.get("线路","")].append(r)
    for route,rr in by_route.items():write_csv(export/f"{date}-{route.replace('/','_')}.csv",rr,[f for f in fields if f!="线路"])
    print(f"legacy quality overlay: {len(rows)} trips");return 0
if __name__=="__main__":raise SystemExit(main())
