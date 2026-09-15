from __future__ import annotations
import argparse,csv,json
from collections import defaultdict
from datetime import datetime,timedelta
from pathlib import Path
from zoneinfo import ZoneInfo
ROOT=Path(__file__).resolve().parents[1];TZ=ZoneInfo('Asia/Shanghai');MAX_TRIP_HOURS=4
def parse_dt(date,hm):
    try:return datetime.fromisoformat(f'{date}T{hm}:00+08:00').astimezone(TZ)
    except:return None
def read_csv(p):
    with p.open(encoding='utf-8-sig',newline='') as f:return list(csv.DictReader(f))
def write_csv(p,rows,fields):
    with p.open('w',encoding='utf-8-sig',newline='') as f:w=csv.DictWriter(f,fieldnames=fields);w.writeheader();w.writerows({k:r.get(k,'') for k in fields} for r in rows)
def load_terminal_events(date):
    info={};events=defaultdict(list)
    for p in sorted((ROOT/'data/shmaas').glob(f'{date}-*.jsonl')):
        if p.name.endswith('-reverse-watch.jsonl'):continue
        for line in p.open(encoding='utf-8'):
            if not line.strip():continue
            try:
                s=json.loads(line)
                if not s.get('success'):continue
                route=str(s.get('route') or '');cap=datetime.fromisoformat(s['sample_time_cst']).astimezone(TZ)
            except:continue
            for d in s.get('directions',[]) or []:
                try:direction=int(d.get('direction'));count=int(d.get('stop_count') or 0)
                except:continue
                info[(route,direction)]={'name':str(d.get('end_stop') or ''),'seq':count}
            for v in s.get('vehicles',[]) or []:
                plate=str(v.get('plate') or '').strip()
                for o in v.get('observations',[]) or []:
                    try:direction=int(o.get('direction'));eta=float(o.get('arrive_time'))
                    except:continue
                    if str(o.get('role') or '') not in {'current','next'}:continue
                    x=info.get((route,direction),{});name=str(o.get('stop_name') or '')
                    try:seq=int(o.get('stop_seq') or 0)
                    except:seq=0
                    if not (name==x.get('name') or (x.get('seq') and seq==x.get('seq'))) or not 0<=eta<=180:continue
                    events[(route,plate,direction)].append({'time':cap,'eta':eta,'predicted':cap+timedelta(minutes=eta)})
    for k in events:events[k].sort(key=lambda e:e['time'])
    return events
def main():
    ap=argparse.ArgumentParser();ap.add_argument('--date');a=ap.parse_args();date=a.date or datetime.now(TZ).date().isoformat();export=ROOT/'data/export';combined=export/f'{date}-operations.csv'
    if not combined.exists():print('No operations export; skip full-trip arrival fallback');return 0
    rows=read_csv(combined)
    if not rows:return 0
    fields=list(rows[0].keys());events=load_terminal_events(date);deps=defaultdict(list)
    for r in rows:
        d=parse_dt(date,r.get('发车时间',''))
        if d:deps[(r.get('线路',''),r.get('车牌号',''),r.get('方向',''))].append(d)
    for k in deps:deps[k].sort()
    filled=0
    for r in rows:
        # Never touch short-turn/anomaly rows and never overwrite an existing arrival.
        if r.get('班次类型')!='全程车' or r.get('到达时间') not in {'','待确认'}:continue
        route,plate,dtext=r.get('线路',''),r.get('车牌号',''),r.get('方向','');dep=parse_dt(date,r.get('发车时间',''))
        if not dep:continue
        try:direction=int(dtext)
        except:continue
        nextdep=next((x for x in deps[(route,plate,dtext)] if x>dep),None);limit=min(nextdep,dep+timedelta(hours=MAX_TRIP_HOURS)) if nextdep else dep+timedelta(hours=MAX_TRIP_HOURS)
        cand=[e for e in events.get((route,plate,direction),[]) if dep<=e['time']<limit and dep<=e['predicted']<=dep+timedelta(hours=MAX_TRIP_HOURS)]
        if not cand:continue
        last=max(cand,key=lambda e:e['time']);arr=last['predicted'];r['到达时间']=arr.astimezone(TZ).strftime('%H:%M');r['到达时间说明']=f"SHMAAS末次终点ETA推算：{last['time'].astimezone(TZ).strftime('%H:%M')}采样，终点ETA {round(last['eta'])}分钟";r['到达误差估计（分钟）']='';mins=round((arr-dep).total_seconds()/60)
        if 0<=mins<=MAX_TRIP_HOURS*60:r['全程时间']=f'{mins}分钟'
        filled+=1
    write_csv(combined,rows,fields);by=defaultdict(list)
    for r in rows:by[r.get('线路','')].append(r)
    for route,rr in by.items():write_csv(export/f'{date}-{route.replace("/","_")}.csv',rr,[f for f in fields if f!='线路'])
    mp=export/f'{date}-operations-meta.json'
    if mp.exists():
        meta=json.loads(mp.read_text(encoding='utf-8'));meta['last_terminal_eta_fallback_count']=filled;meta['arrival_fallback_rule']='Only full trips still lacking arrival use the last valid same-direction terminal ETA in the trip window; short-turn/anomaly rows are excluded and existing arrivals are never overwritten.';mp.write_text(json.dumps(meta,ensure_ascii=False,indent=2),encoding='utf-8')
    print(f'Filled {filled} full-trip arrivals from last terminal ETA');return 0
if __name__=='__main__':raise SystemExit(main())