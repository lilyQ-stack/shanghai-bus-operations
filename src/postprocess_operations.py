from __future__ import annotations

import argparse,csv,json
from collections import defaultdict
from datetime import datetime,timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT=Path(__file__).resolve().parents[1]; TZ=ZoneInfo('Asia/Shanghai')
# This 20-minute window is ONLY the proven timetable-revision dedupe rule.
# It is not, and must never again become, a short-turn classification threshold.
REVISION_WINDOW_MIN=20
MAX_SHORT_TURN_ERROR_MIN=10

def parse_dt(date,hm):
    try:return datetime.fromisoformat(f'{date}T{hm}:00+08:00').astimezone(TZ)
    except:return None
def fmt(x):return x.astimezone(TZ).strftime('%H:%M') if x else ''
def num(v):
    try:return float(v)
    except (TypeError,ValueError):return None

def load_raw(date):
    events=defaultdict(list); sightings=defaultdict(lambda:defaultdict(list))
    for path in sorted((ROOT/'data/shmaas').glob(f'{date}-*.jsonl')):
        if path.name.endswith('-reverse-watch.jsonl'):continue
        for line in path.open(encoding='utf-8'):
            if not line.strip():continue
            try:
                snap=json.loads(line)
                if not snap.get('success'):continue
                route=str(snap.get('route') or ''); captured=datetime.fromisoformat(snap['sample_time_cst']).astimezone(TZ)
            except:continue
            for vehicle in snap.get('vehicles',[]) or []:
                plate=str(vehicle.get('plate') or '').strip()
                if not plate:continue
                for obs in vehicle.get('observations',[]) or []:
                    try:direction=int(obs.get('direction'))
                    except:continue
                    role=str(obs.get('role') or ''); eta=num(obs.get('arrive_time'))
                    events[(route,plate)].append({'time':captured,'direction':direction,'role':role,'stop_name':str(obs.get('stop_name') or ''),'stop_seq':obs.get('stop_seq'),'eta':eta})
                    dispatch=str(obs.get('dispatch_time') or '').strip()
                    if dispatch:sightings[(route,plate,direction)][dispatch].append(captured)
    for k in events:events[k].sort(key=lambda x:x['time'])
    return events,sightings

def dedupe_rows(rows,date,sightings):
    grouped=defaultdict(list)
    for r in rows:grouped[(r.get('线路',''),r.get('车牌号',''),r.get('方向',''))].append(r)
    kept=[];removed=0
    for key,group in grouped.items():
        ordered=sorted(group,key=lambda r:r.get('发车时间',''));clusters=[]
        for row in ordered:
            d=parse_dt(date,row.get('发车时间',''))
            if not d or not clusters:clusters.append([row]);continue
            prev=parse_dt(date,clusters[-1][-1].get('发车时间',''))
            if prev and 0<=(d-prev).total_seconds()/60<=REVISION_WINDOW_MIN:clusters[-1].append(row)
            else:clusters.append([row])
        route,plate,dtext=key
        try:direction=int(dtext)
        except:direction=None
        schedule=sightings.get((route,plate,direction),{}) if direction is not None else {}
        for cluster in clusters:
            if len(cluster)==1:kept.append(cluster[0]);continue
            def rank(row):
                times=schedule.get(row.get('发车时间',''),[]); last=max(times) if times else datetime.min.replace(tzinfo=TZ)
                return last,len(times),row.get('发车时间','')
            kept.append(max(cluster,key=rank));removed+=len(cluster)-1
    kept.sort(key=lambda r:(r.get('线路',''),r.get('发车时间',''),r.get('车牌号','')));return kept,removed

def seq_num(e):
    try:return int(e.get('stop_seq') or 0)
    except:return 0

def enrich_short_turn_times(rows,date,events):
    """Timing enrichment only. Never decides whether a trip is a short turn."""
    enriched=0; deps=defaultdict(list)
    for row in rows:
        d=parse_dt(date,row.get('发车时间',''))
        if d:deps[(row.get('线路',''),row.get('车牌号',''),row.get('方向',''))].append(d)
    for k in deps:deps[k].sort()
    for row in rows:
        for k in ('区间站','区间站到达时间','区间站折返发车时间','区间时间说明'):row.setdefault(k,'')
        if row.get('班次类型')!='疑似区间车':continue
        dep=parse_dt(date,row.get('发车时间',''))
        if not dep:continue
        route,plate,dtext=row.get('线路',''),row.get('车牌号',''),row.get('方向','')
        try:direction=int(dtext)
        except:continue
        next_dep=next((x for x in deps[(route,plate,dtext)] if x>dep),None)
        moving=[e for e in events.get((route,plate),[]) if e['time']>=dep and (next_dep is None or e['time']<next_dep) and e['role'] in {'current','next'} and e['eta'] is not None]
        same=[e for e in moving if e['direction']==direction]; opp=[e for e in moving if e['direction']!=direction]
        if not same or not opp:continue
        first_opp=min(opp,key=lambda e:e['time']); last_same=max((e for e in same if e['time']<first_opp['time']),key=lambda e:(e['time'],seq_num(e)),default=None)
        if not last_same:continue
        row['区间站']=last_same.get('stop_name') or str(last_same.get('stop_seq') or '')
        eta=last_same.get('eta'); arrival=None
        if eta is not None and 0<=eta<=MAX_SHORT_TURN_ERROR_MIN:
            arrival=last_same['time']+timedelta(minutes=eta);row['区间站到达时间']=fmt(arrival)
        reta=first_opp.get('eta'); rstation=first_opp.get('stop_name') or str(first_opp.get('stop_seq') or ''); ranchor=first_opp['time']+timedelta(minutes=reta) if reta is not None and 0<=reta<=180 else None
        if arrival:
            upper=min(first_opp['time'],ranchor) if ranchor else first_opp['time'];span=(upper-arrival).total_seconds()/60
            if 0<=span<=2*MAX_SHORT_TURN_ERROR_MIN:
                turn=arrival+(upper-arrival)/2;row['区间站折返发车时间']=fmt(turn);row['区间时间说明']=f'到达按末次同向重点站ETA估算；折返发车结合首次反向运行观测取区间中点，约±{max(1,round(span/2))}分钟'+(f'；反向重点站{rstation} ETA {round(reta)}分钟参与校验' if reta is not None else '');enriched+=1;continue
        row['区间时间说明']='区间折返已由主分类器确认；现有采样不足以把折返发车时间控制在±10分钟'
    return enriched

def read_csv(p):
    with p.open(encoding='utf-8-sig',newline='') as f:return list(csv.DictReader(f))
def write_csv(p,rows,fields):
    with p.open('w',encoding='utf-8-sig',newline='') as f:w=csv.DictWriter(f,fieldnames=fields);w.writeheader();w.writerows({k:r.get(k,'') for k in fields} for r in rows)
def main():
    ap=argparse.ArgumentParser();ap.add_argument('--date');a=ap.parse_args();date=a.date or datetime.now(TZ).date().isoformat();export=ROOT/'data/export';combined=export/f'{date}-operations.csv'
    if not combined.exists():print('No combined operations export; skip postprocess');return 0
    events,sightings=load_raw(date);rows=read_csv(combined);rows,removed=dedupe_rows(rows,date,sightings);enriched=enrich_short_turn_times(rows,date,events)
    base=list(rows[0].keys()) if rows else [];extra=['区间站','区间站到达时间','区间站折返发车时间','区间时间说明'];fields=[f for f in base if f not in extra]+extra;write_csv(combined,rows,fields)
    by=defaultdict(list)
    for r in rows:by[r.get('线路','')].append(r)
    for route,rr in by.items():write_csv(export/f'{date}-{route.replace("/","_")}.csv',rr,[f for f in fields if f!='线路'])
    mp=export/f'{date}-operations-meta.json'
    if mp.exists():
        meta=json.loads(mp.read_text(encoding='utf-8'));meta['dispatch_revision_deduped_count']=removed;meta['trip_row_count']=len(rows);meta['short_turn_time_enriched_count']=enriched;meta['departure_status']='Private-pipeline dispatch revision dedupe only: same route/plate/direction schedule revisions within 20 minutes are clustered and the schedule observed latest by SHMAAS is retained. This 20-minute window is unrelated to short-turn classification.';mp.write_text(json.dumps(meta,ensure_ascii=False,indent=2),encoding='utf-8')
    print(f'Postprocessed {len(rows)} trips; removed {removed} schedule revision rows; enriched {enriched} short-turn timing rows');return 0
if __name__=='__main__':raise SystemExit(main())