from __future__ import annotations

# Proven private estimator, adapted only to accept --date and to ignore auxiliary
# reverse-watch JSONL.  Its same-vehicle-only training hierarchy is preserved.
import argparse,csv,json,re,statistics
from collections import defaultdict
from datetime import datetime,timedelta
from pathlib import Path
from zoneinfo import ZoneInfo
ROOT=Path(__file__).resolve().parents[1]; TZ=ZoneInfo('Asia/Shanghai'); MAX_TRIP_MIN=240; MAX_RANK_ERROR_MIN=15; MAX_LAYOVER_MIN=45

def parse_dt(date,hm):
    try:return datetime.fromisoformat(f'{date}T{hm}:00+08:00').astimezone(TZ)
    except:return None
def fmt(x):return x.astimezone(TZ).strftime('%H:%M')
def read_csv(p):
    with p.open(encoding='utf-8-sig',newline='') as f:return list(csv.DictReader(f))
def write_csv(p,rows,fields):
    with p.open('w',encoding='utf-8-sig',newline='') as f:w=csv.DictWriter(f,fieldnames=fields);w.writeheader();w.writerows({k:r.get(k,'') for k in fields} for r in rows)
def bucket(date,hm):
    d=parse_dt(date,hm)
    if not d:return 'unknown'
    if d.weekday()>=5:return 'weekend'
    m=d.hour*60+d.minute
    return 'weekday_am_peak' if 390<=m<=570 else ('weekday_pm_peak' if 990<=m<=1170 else 'weekday_offpeak')
def robust_stats(values,min_n=2):
    v=sorted(x for x in values if x>=0)
    if len(v)<min_n:return None
    med=statistics.median(v); mad=statistics.median(abs(x-med) for x in v); span=v[-1]-v[0]; err=max(5,round(max(mad*1.5,span/2)))
    return None if err>MAX_RANK_ERROR_MIN else (med,err)
def existing_confidence(r):
    a=r.get('到达时间','')
    if a in {'','待确认'}:return 'D','待确认','否'
    note=r.get('到达时间说明','')
    try:u=float(r.get('到达误差估计（分钟）',''))
    except:u=None
    if u is not None and u<=5:return 'A','终点ETA确认/一致估算','是'
    if '末次终点ETA推算' in note:
        m=re.search(r'终点ETA\s*(\d+)分钟',note); eta=int(m.group(1)) if m else 999
        return ('B','末次终点ETA兜底','是') if eta<=20 else (('C','末次终点ETA兜底','是') if eta<=45 else ('D','末次终点ETA兜底（误差可能>15分钟）','否'))
    return 'C','已有到达估算','是'
def load_history(cur):
    out=[]
    for p in sorted((ROOT/'data/export').glob('????-??-??-operations.csv')):
        d=p.name[:10]
        if d>cur:continue
        try:rr=read_csv(p)
        except:continue
        out.extend((d,r) for r in rr)
    return out
def duration_history(hist):
    z=defaultdict(list)
    for d,r in hist:
        if r.get('班次类型')!='全程车' or r.get('到达时间') in {'','待确认'}:continue
        dep=parse_dt(d,r.get('发车时间','')); arr=parse_dt(d,r.get('到达时间',''))
        if not dep or not arr:continue
        if arr<dep:arr+=timedelta(days=1)
        mins=(arr-dep).total_seconds()/60
        if 30<=mins<=MAX_TRIP_MIN:z[(r.get('线路',''),r.get('车牌号',''),r.get('方向',''),bucket(d,r.get('发车时间','')))].append(mins)
    return z
def layover_history(hist):
    by=defaultdict(list);z=defaultdict(list)
    for d,r in hist:
        dep=parse_dt(d,r.get('发车时间',''))
        if dep:by[(d,r.get('线路',''),r.get('车牌号',''))].append((dep,r))
    for (d,route,plate),seq in by.items():
        seq.sort(key=lambda x:x[0])
        for i,(dep,r) in enumerate(seq):
            if r.get('班次类型')!='全程车' or r.get('到达时间') in {'','待确认'}:continue
            arr=parse_dt(d,r.get('到达时间',''))
            if not arr:continue
            if arr<dep:arr+=timedelta(days=1)
            for nd,nr in seq[i+1:]:
                if nr.get('方向')==r.get('方向'):continue
                lay=(nd-arr).total_seconds()/60
                if 0<=lay<=MAX_LAYOVER_MIN:z[(route,plate,r.get('方向',''),bucket(d,r.get('发车时间','')))].append(lay)
                break
    return z
def moving_events(date):
    ev=defaultdict(list)
    for p in sorted((ROOT/'data/shmaas').glob(f'{date}-*.jsonl')):
        if p.name.endswith('-reverse-watch.jsonl'):continue
        for line in p.open(encoding='utf-8'):
            try:s=json.loads(line); route=str(s.get('route') or ''); cap=datetime.fromisoformat(s['sample_time_cst']).astimezone(TZ)
            except:continue
            if not s.get('success'):continue
            for v in s.get('vehicles',[]) or []:
                plate=str(v.get('plate') or '').strip()
                for o in v.get('observations',[]) or []:
                    if str(o.get('role') or '') not in {'current','next'}:continue
                    try:direction=int(o.get('direction'));eta=float(o.get('arrive_time'))
                    except:continue
                    if 0<=eta<=180:ev[(route,plate,str(direction))].append({'time':cap,'stop':str(o.get('stop_name') or ''),'eta':eta,'predicted_stop':cap+timedelta(minutes=eta)})
    for k in ev:ev[k].sort(key=lambda e:e['time'])
    return ev
def segment_history(hist):
    confirmed=defaultdict(list);z=defaultdict(list)
    for d,r in hist:
        if r.get('班次类型')!='全程车' or r.get('到达时间') in {'','待确认'}:continue
        dep=parse_dt(d,r.get('发车时间',''));arr=parse_dt(d,r.get('到达时间',''))
        if not dep or not arr:continue
        if arr<dep:arr+=timedelta(days=1)
        confirmed[d].append((r,dep,arr))
    for d,trips in confirmed.items():
        if not any((ROOT/'data/shmaas').glob(f'{d}-*.jsonl')):continue
        ev=moving_events(d)
        for r,dep,arr in trips:
            key=(r.get('线路',''),r.get('车牌号',''),r.get('方向',''))
            for e in [e for e in ev.get(key,[]) if dep<=e['time']<=arr and e['predicted_stop']<=arr+timedelta(minutes=5)]:
                rem=(arr-e['predicted_stop']).total_seconds()/60
                if 0<=rem<=MAX_TRIP_MIN:z[key+(e['stop'],bucket(d,r.get('发车时间','')))].append(rem)
    return z
def set_est(r,dep,arr,err,method,detail):
    if arr<dep or arr>dep+timedelta(minutes=MAX_TRIP_MIN):return False
    r['到达时间']=fmt(arr);r['全程时间']=f'{round((arr-dep).total_seconds()/60)}分钟';r['到达时间说明']=detail;r['到达误差估计（分钟）']=str(err);r['到达置信度']='B' if err<=10 else 'C';r['到达估算方法']=method;r['参与车速排名']='是';return True
def main():
    ap=argparse.ArgumentParser();ap.add_argument('--date');a=ap.parse_args();date=a.date or datetime.now(TZ).date().isoformat();export=ROOT/'data/export';combined=export/f'{date}-operations.csv'
    if not combined.exists():print('No operations export; skip individual arrival estimation');return 0
    rows=read_csv(combined)
    for r in rows:r['到达置信度'],r['到达估算方法'],r['参与车速排名']=existing_confidence(r)
    hist=[(d,r) for d,r in load_history(date) if existing_confidence(r)[2]=='是'];dh=duration_history(hist);lh=layover_history(hist);sh=segment_history(hist);cur=moving_events(date)
    by=defaultdict(list)
    for r in rows:
        dep=parse_dt(date,r.get('发车时间',''))
        if dep:by[(r.get('线路',''),r.get('车牌号',''))].append((dep,r))
    for k in by:by[k].sort(key=lambda x:x[0])
    counts=defaultdict(int)
    for r in rows:
        if r.get('班次类型')!='全程车' or r.get('到达时间') not in {'','待确认'}:continue
        route,plate,direction=r.get('线路',''),r.get('车牌号',''),r.get('方向','');dep=parse_dt(date,r.get('发车时间',''))
        if not dep:continue
        b=bucket(date,r.get('发车时间','')); nxt=next((nd for nd,nr in by[(route,plate)] if nd>dep and nr.get('方向')!=direction and nd<=dep+timedelta(minutes=MAX_TRIP_MIN)),None);stats=robust_stats(lh.get((route,plate,direction,b),[]))
        if nxt and stats and set_est(r,dep,nxt-timedelta(minutes=stats[0]),stats[1],'同车反向发车反推',f'同车下一次反向发车{fmt(nxt)}，减去该车自身同类时段历史终点停站中位数{round(stats[0])}分钟，约±{stats[1]}分钟'):counts['reverse']+=1;continue
        for e in sorted([e for e in cur.get((route,plate,direction),[]) if dep<=e['time']<=dep+timedelta(minutes=MAX_TRIP_MIN)],key=lambda x:x['time'],reverse=True):
            stats=robust_stats(sh.get((route,plate,direction,e['stop'],b),[]))
            if stats and set_est(r,dep,e['predicted_stop']+timedelta(minutes=stats[0]),stats[1],'同车重点站剩余时间',f"{fmt(e['time'])}采样：该车到重点站{e['stop']} ETA {round(e['eta'])}分钟；叠加该车自身历史剩余时间，约±{stats[1]}分钟"):counts['segment']+=1;break
        if r.get('到达时间') not in {'','待确认'}:continue
        stats=robust_stats(dh.get((route,plate,direction,b),[]))
        if stats and set_est(r,dep,dep+timedelta(minutes=stats[0]),stats[1],'同车历史全程时间',f'按该车自身同方向、同类时段历史全程时间中位数{round(stats[0])}分钟估算，约±{stats[1]}分钟'):counts['duration']+=1;continue
        r['到达置信度']='D';r['到达估算方法']='证据不足';r['参与车速排名']='否'
    extras=['到达置信度','到达估算方法','参与车速排名'];fields=[f for f in rows[0].keys() if f not in extras]+extras;write_csv(combined,rows,fields)
    br=defaultdict(list)
    for r in rows:br[r.get('线路','')].append(r)
    for route,rr in br.items():write_csv(export/f'{date}-{route.replace("/","_")}.csv',rr,[f for f in fields if f!='线路'])
    print('Individual arrival estimation:',dict(counts));return 0
if __name__=='__main__':raise SystemExit(main())