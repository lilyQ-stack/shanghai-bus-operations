from __future__ import annotations

# Restored from the proven private pipeline.  The mature exporter body lives here;
# the migration-specific short-turn rule is isolated in private_short_turn_classifier.

import argparse, csv, json, statistics
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from private_short_turn_classifier import Event, classify_short_turn

ROOT=Path(__file__).resolve().parents[1]; TZ=ZoneInfo('Asia/Shanghai')

def write_csv(path,fields,rows):
    with path.open('w',encoding='utf-8-sig',newline='') as f:
        w=csv.DictWriter(f,fieldnames=fields); w.writeheader(); w.writerows({k:r.get(k,'') for k in fields} for r in rows)

def load_jsonl(path):
    out=[]
    with path.open(encoding='utf-8') as f:
        for line in f:
            try: out.append(json.loads(line))
            except Exception: pass
    return out

def num(x):
    try:return float(x)
    except (TypeError,ValueError):return None

def t(v): return datetime.fromisoformat(v).astimezone(TZ)
def hm(v): return v.astimezone(TZ).strftime('%H:%M') if v else ''

def load_shmaas(date):
    files=[p for p in sorted((ROOT/'data/shmaas').glob(f'{date}-*.jsonl')) if not p.name.endswith('-reverse-watch.jsonl')]
    snaps=[]
    for p in files: snaps.extend(load_jsonl(p))
    snaps=[s for s in snaps if s.get('success') and s.get('sample_time_cst') and s.get('route')]
    snaps.sort(key=lambda s:t(s['sample_time_cst'])); return snaps,[p.name for p in files]

def build(date,snaps):
    dirs={}; events=defaultdict(list); dispatch={}; plates=defaultdict(set); sample_times=[]
    for s in snaps:
        route=str(s['route']); now=t(s['sample_time_cst']); sample_times.append(now)
        for d in s.get('directions',[]) or []:
            k=(route,int(d.get('direction',0))); dirs[k]={'start':str(d.get('start_stop') or ''),'end':str(d.get('end_stop') or ''),'stop_count':int(d.get('stop_count') or 0)}
        for v in s.get('vehicles',[]) or []:
            plate=str(v.get('plate') or '').strip()
            if not plate: continue
            plates[route].add(plate)
            for o in v.get('observations',[]) or []:
                try: direction=int(o.get('direction'))
                except: continue
                e={'time':now,'direction':direction,'role':str(o.get('role') or ''),'stop_seq':o.get('stop_seq'),'stop_name':str(o.get('stop_name') or ''),'arrive_minutes':num(o.get('arrive_time')),'distance_m':num(o.get('distance')),'remaining_stops':num(o.get('remaining_stops')),'service_hints':o.get('service_hints') or []}
                events[(route,plate)].append(e)
                dep=str(o.get('dispatch_time') or '').strip()
                if dep:
                    k=(route,plate,direction,dep); old=dispatch.get(k,{})
                    dispatch[k]={'route':route,'plate':plate,'direction':direction,'departure':dep,'first_seen':min(now,old.get('first_seen',now)),'last_seen':max(now,old.get('last_seen',now)),'service_hints':o.get('service_hints') or old.get('service_hints',[])}
    def depdt(x):
        try:return datetime.fromisoformat(f'{date}T{x}:00+08:00').astimezone(TZ)
        except:return None
    def terminal_eta(route,ev,direction,dep):
        info=dirs.get((route,direction),{}); end=info.get('end',''); n=info.get('stop_count',0); vals=[]
        for e in ev:
            if e['direction']!=direction or e['time']<dep or e['role'] not in {'current','next'}:continue
            if not (e['stop_name']==end or (n and e['stop_seq']==n)):continue
            eta=e['arrive_minutes']; dist=e['distance_m']
            if eta is None or not 0<=eta<=180:continue
            pred=e['time']+timedelta(minutes=eta)
            if dep<=pred<=dep+timedelta(hours=4): vals.append((e,pred,dist))
        direct=[x for x in vals if x[0]['arrive_minutes']<=1 or (x[2] is not None and x[2]<=100)]
        if direct:return min(direct,key=lambda x:(x[0]['arrive_minutes'],x[0]['time']))[1],'SHMAAS终点近站ETA直接确认，约±2分钟',2,'terminal_eta_near'
        near=[x for x in vals if x[0]['arrive_minutes']<=5]
        if near:return min(near,key=lambda x:x[0]['arrive_minutes'])[1],'SHMAAS终点ETA近站估算，约±5分钟',5,'terminal_eta_near'
        preds=sorted(x[1] for x in vals)
        if len(preds)>=2 and (preds[-1]-preds[0]).total_seconds()/60<=10:
            spread=(preds[-1]-preds[0]).total_seconds()/60; med=datetime.fromtimestamp(statistics.median([p.timestamp() for p in preds]),tz=TZ); u=max(3,min(5,round(spread/2)+1)); return med,f'SHMAAS多次终点ETA一致估算，约±{u}分钟',u,'terminal_eta_consensus'
        return None
    rows=[]
    for d in sorted(dispatch.values(),key=lambda x:(x['route'],x['departure'],x['plate'])):
        route,plate,direction=d['route'],d['plate'],d['direction']; info=dirs.get((route,direction),{}); dep=depdt(d['departure']); ev=events[(route,plate)]
        eta=terminal_eta(route,ev,direction,dep) if dep else None
        arrival,note,u,method=(hm(eta[0]),eta[1],eta[2],eta[3]) if eta else ('待确认','尚无满足±5分钟标准的终点ETA','',None)
        cevents=[Event(e['time'],e['direction'],e['role'],e['stop_seq'],e['stop_name'],e['arrive_minutes'],e['remaining_stops']) for e in ev]
        cls=classify_short_turn(events=cevents,direction=direction,dep_dt=dep,end_stop=info.get('end',''),stop_count=info.get('stop_count',0),terminal_eta_confirmed=method in {'terminal_eta_near','terminal_eta_consensus'}) if dep else None
        stype=cls.service_type if cls else '全程车'; reason=cls.reason if cls else '无有效发车时间'
        duration='待确认'
        if arrival!='待确认' and dep:
            arr=datetime.fromisoformat(f'{date}T{arrival}:00+08:00').astimezone(TZ); arr += timedelta(days=1) if arr<dep else timedelta(); mins=round((arr-dep).total_seconds()/60); duration=f'{mins}分钟' if 0<=mins<=240 else '待确认'
        rows.append({'线路':route,'临时车辆编号':'','车牌号':plate,'方向':direction,'始发站':info.get('start',''),'发车时间':d['departure'],'终点站':info.get('end',''),'班次类型':stype,'班次类型判定依据':reason,'到达时间':arrival,'全程时间':duration,'车速（km/h）':'','当日平均车速（km/h）':'','当日车速排名':'','到达时间说明':note,'到达误差估计（分钟）':u,'本车首次观测':hm(min((e['time'] for e in ev),default=None)),'本车末次观测':hm(max((e['time'] for e in ev),default=None)),'数据源':'SHMAAS'})
    return rows,plates,sample_times,len(dispatch)

def main():
    ap=argparse.ArgumentParser(); ap.add_argument('--date'); a=ap.parse_args(); date=a.date or datetime.now(TZ).date().isoformat(); snaps,files=load_shmaas(date)
    if not snaps: raise SystemExit(f'No successful SHMAAS snapshots found for {date}')
    rows,plates,times,dc=build(date,snaps); out=ROOT/'data/export'; out.mkdir(parents=True,exist_ok=True)
    fields=['临时车辆编号','车牌号','方向','始发站','发车时间','终点站','班次类型','班次类型判定依据','到达时间','全程时间','车速（km/h）','当日平均车速（km/h）','当日车速排名','到达时间说明','到达误差估计（分钟）','本车首次观测','本车末次观测','数据源']
    by=defaultdict(list)
    for r in rows:by[r['线路']].append(r)
    for route,rr in by.items(): write_csv(out/f'{date}-{route.replace("/","_")}.csv',fields,sorted(rr,key=lambda x:(x['发车时间'],x['车牌号'])))
    write_csv(out/f'{date}-operations.csv',['线路']+fields,sorted(rows,key=lambda x:(x['线路'],x['发车时间'],x['车牌号'])))
    meta={'date':date,'source_files':files,'shmaas_snapshot_count':len(snaps),'shmaas_dispatch_event_count':dc,'trip_row_count':len(rows),'service_type_status':'No hard time window. A vehicle disappearing before 80% source-route progress is a short-turn candidate; reconstructed reverse physical progress >=2 stops confirms 疑似区间车. Reliable same-direction terminal ETA remains the full-trip guard.','first_shmaas_snapshot':min(times).isoformat() if times else None,'last_shmaas_snapshot':max(times).isoformat() if times else None}
    (out/f'{date}-operations-meta.json').write_text(json.dumps(meta,ensure_ascii=False,indent=2),encoding='utf-8'); print(f'Wrote {len(rows)} operations rows for {date}'); return 0

if __name__=='__main__': raise SystemExit(main())
