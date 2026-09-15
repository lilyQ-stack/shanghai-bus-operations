from __future__ import annotations

import csv
import json
import statistics
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
TZ = ZoneInfo("Asia/Shanghai")
MAX_TRIP_MIN = 240
REVISION_WINDOW_MIN = 20


def parse_dt(date, hm):
    try:
        return datetime.fromisoformat(f"{date}T{hm}:00+08:00").astimezone(TZ)
    except Exception:
        return None


def read_csv(path):
    with path.open(encoding="utf-8-sig", newline="") as f:
        return list(csv.DictReader(f))


def write_csv(path, rows, fields):
    with path.open("w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        w.writerows({k: r.get(k, "") for k in fields} for r in rows)


def load_raw(date):
    terminal = {}
    events = defaultdict(list)
    sightings = defaultdict(lambda: defaultdict(list))
    for path in sorted((ROOT / "data" / "shmaas").glob(f"{date}-*.jsonl")):
        if "reverse-watch" in path.name:
            continue
        for line in path.read_text(encoding="utf-8").splitlines():
            try:
                snap = json.loads(line)
                if not snap.get("success"):
                    continue
                route = str(snap.get("route") or "")
                captured = datetime.fromisoformat(snap["sample_time_cst"]).astimezone(TZ)
            except Exception:
                continue
            for d in snap.get("directions") or []:
                try:
                    direction = int(d.get("direction"))
                except Exception:
                    continue
                terminal[(route, direction)] = (str(d.get("end_stop") or ""), int(d.get("stop_count") or 0))
            for vehicle in snap.get("vehicles") or []:
                plate = str(vehicle.get("plate") or "").strip()
                if not plate:
                    continue
                for o in vehicle.get("observations") or []:
                    try:
                        direction = int(o.get("direction"))
                    except Exception:
                        continue
                    role = str(o.get("role") or "")
                    dispatch = str(o.get("dispatch_time") or "").strip()
                    if role == "dispatch" and dispatch:
                        sightings[(route, plate, str(direction))][dispatch].append(captured)
                        continue
                    if role not in {"current", "next"}:
                        continue
                    try:
                        eta = float(o.get("eta_min", o.get("arrive_time")))
                    except Exception:
                        eta = None
                    try:
                        seq = int(o.get("stop_seq"))
                    except Exception:
                        seq = None
                    try:
                        rem = int(float(o.get("remaining_stops")))
                    except Exception:
                        rem = None
                    pos = max(1, seq - rem) if seq is not None and rem is not None else None
                    events[(route, plate, str(direction))].append({"time": captured, "eta": eta, "seq": seq, "pos": pos, "stop": str(o.get("stop_name") or "")})
    for key in events:
        events[key].sort(key=lambda e: e["time"])
    return terminal, events, sightings


def dedupe(rows, date, sightings):
    groups = defaultdict(list)
    for r in rows:
        groups[(r.get("线路", ""), r.get("车牌号", ""), r.get("方向", ""))].append(r)
    kept = []
    for key, group in groups.items():
        ordered = sorted(group, key=lambda r: r.get("发车时间", ""))
        clusters = []
        for r in ordered:
            dt = parse_dt(date, r.get("发车时间", ""))
            prev = parse_dt(date, clusters[-1][-1].get("发车时间", "")) if clusters else None
            if clusters and dt and prev and 0 <= (dt-prev).total_seconds()/60 <= REVISION_WINDOW_MIN:
                clusters[-1].append(r)
            else:
                clusters.append([r])
        for cluster in clusters:
            if len(cluster) == 1:
                kept.append(cluster[0]); continue
            schedule = sightings.get(key, {})
            def rank(r):
                seen = schedule.get(r.get("发车时间", ""), [])
                return (max(seen) if seen else datetime.min.replace(tzinfo=TZ), len(seen), r.get("发车时间", ""))
            kept.append(max(cluster, key=rank))
    return sorted(kept, key=lambda r: (r.get("线路", ""), r.get("发车时间", ""), r.get("车牌号", "")))


def median_consensus(predictions):
    if len(predictions) < 2:
        return None
    vals = sorted(p.timestamp() for p in predictions)
    med = statistics.median(vals)
    spread = (max(vals)-min(vals))/60
    if spread > 15:
        return None
    return datetime.fromtimestamp(med, TZ), max(5, round(spread/2))


def apply(date, rows, terminal, events):
    by_vehicle = defaultdict(list)
    for r in rows:
        dep = parse_dt(date, r.get("发车时间", ""))
        if dep:
            by_vehicle[(r.get("线路", ""), r.get("车牌号", ""))].append((dep, r))
    for seq in by_vehicle.values(): seq.sort(key=lambda x: x[0])

    history = defaultdict(list)
    for path in sorted((ROOT / "data" / "export").glob("????-??-??-operations.csv")):
        hdate = path.name[:10]
        if hdate >= date: continue
        try: hrows = read_csv(path)
        except Exception: continue
        for r in hrows:
            if r.get("班次类型") != "全程车": continue
            dep, arr = parse_dt(hdate, r.get("发车时间", "")), parse_dt(hdate, r.get("预计到达时间", r.get("到达时间", "")))
            if dep and arr:
                if arr < dep: arr += timedelta(days=1)
                mins = (arr-dep).total_seconds()/60
                if 30 <= mins <= MAX_TRIP_MIN:
                    history[(r.get("线路", ""), r.get("车牌号", ""), r.get("发车站", ""), r.get("终点站", ""))].append(mins)

    for r in rows:
        if r.get("班次类型") == "疑似区间车":
            continue
        route, plate = r.get("线路", ""), r.get("车牌号", "")
        direction = ""
        # Current public exports omit direction; infer it from origin/end metadata.
        for d in ("0", "1"):
            info = terminal.get((route, int(d)))
            if info and info[0] == r.get("终点站", ""):
                direction = d; break
        if not direction: continue
        dep = parse_dt(date, r.get("发车时间", ""))
        if not dep: continue
        next_same = next((x for x, nr in by_vehicle[(route, plate)] if x > dep and nr.get("终点站") == r.get("终点站")), None)
        limit = min(next_same, dep + timedelta(minutes=MAX_TRIP_MIN)) if next_same else dep + timedelta(minutes=MAX_TRIP_MIN)
        end_name, end_seq = terminal.get((route, int(direction)), ("", 0))
        evs = [e for e in events.get((route, plate, direction), []) if dep <= e["time"] < limit]
        terminal_evs = [e for e in evs if e["eta"] is not None and (e["stop"] == end_name or (end_seq and e["seq"] == end_seq)) and 0 <= e["eta"] <= 180]
        predictions = [e["time"] + timedelta(minutes=e["eta"]) for e in terminal_evs]
        consensus = median_consensus(predictions)
        close = [e for e in terminal_evs if e["eta"] <= 5]
        if consensus or close:
            if consensus:
                arrival, err = consensus; method = "终点ETA多样本共识"
            else:
                e = close[-1]; arrival = e["time"] + timedelta(minutes=e["eta"]); err = 5; method = "接近终点ETA"
            r["班次类型"] = "全程车"
            r["区间/异常说明"] = "同向终点ETA形成可靠证据；按全程运行处理"
            r["预计到达时间"] = arrival.strftime("%H:%M")
            r["全程时间（分钟）"] = f"{(arrival-dep).total_seconds()/60:.1f}"
            r["到达置信度"] = "A" if err <= 5 else "B"
            r["到达估算方法"] = method
            r["参与车速排名"] = "是"
            continue
        # Proven legacy fallback: same vehicle's own historical duration only.
        if r.get("班次类型") in {"全程车", "运行中待确认"} and not r.get("预计到达时间"):
            vals = history.get((route, plate, r.get("发车站", ""), r.get("终点站", "")), [])
            if len(vals) >= 2:
                med = statistics.median(vals); spread = max(vals)-min(vals)
                if spread <= 30:
                    arrival = dep + timedelta(minutes=med)
                    r["预计到达时间"] = arrival.strftime("%H:%M")
                    r["全程时间（分钟）"] = f"{med:.1f}"
                    r["到达置信度"] = "B" if spread <= 20 else "C"
                    r["到达估算方法"] = "同车历史全程时间"
                    r["参与车速排名"] = "是"
    return rows


def main():
    import argparse
    p=argparse.ArgumentParser(); p.add_argument("--date"); a=p.parse_args()
    date=a.date or datetime.now(TZ).strftime("%Y-%m-%d")
    export=ROOT/"data"/"export"; combined=export/f"{date}-operations.csv"
    if not combined.exists(): return 0
    rows=read_csv(combined); terminal, events, sightings=load_raw(date)
    rows=dedupe(rows,date,sightings); rows=apply(date,rows,terminal,events)
    fields=list(rows[0].keys()) if rows else []
    write_csv(combined,rows,fields)
    by_route=defaultdict(list)
    for r in rows: by_route[r.get("线路","")].append(r)
    for route, rr in by_route.items():
        write_csv(export/f"{date}-{route.replace('/','_')}.csv",rr,[f for f in fields if f!="线路"])
    print(f"legacy quality overlay: {len(rows)} trips")
    return 0

if __name__ == "__main__": raise SystemExit(main())
