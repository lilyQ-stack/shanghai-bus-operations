from __future__ import annotations

import argparse
import csv
import json
import re
import statistics
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
TZ = ZoneInfo("Asia/Shanghai")
MAX_TRIP_MIN = 240
MAX_RANK_ERROR_MIN = 15
MAX_LAYOVER_MIN = 45


def parse_dt(date: str, hm: str) -> datetime | None:
    try:
        return datetime.fromisoformat(f"{date}T{hm}:00+08:00").astimezone(TZ)
    except Exception:
        return None


def fmt(dt: datetime) -> str:
    return dt.astimezone(TZ).strftime("%H:%M")


def read_csv(path: Path) -> list[dict]:
    with path.open(encoding="utf-8-sig", newline="") as f:
        return list(csv.DictReader(f))


def write_csv(path: Path, rows: list[dict], fields: list[str]) -> None:
    with path.open("w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        w.writerows({k: r.get(k, "") for k in fields} for r in rows)


def bucket(date: str, hm: str) -> str:
    dt = parse_dt(date, hm)
    if not dt:
        return "unknown"
    if dt.weekday() >= 5:
        return "weekend"
    minutes = dt.hour * 60 + dt.minute
    if 390 <= minutes <= 570:
        return "weekday_am_peak"
    if 990 <= minutes <= 1170:
        return "weekday_pm_peak"
    return "weekday_offpeak"


def robust_stats(values: list[float], min_n: int = 2) -> tuple[float, int] | None:
    vals = sorted(v for v in values if v >= 0)
    if len(vals) < min_n:
        return None
    med = statistics.median(vals)
    deviations = [abs(v - med) for v in vals]
    mad = statistics.median(deviations)
    pspan = vals[-1] - vals[0]
    error = max(5, round(max(mad * 1.5, pspan / 2)))
    if error > MAX_RANK_ERROR_MIN:
        return None
    return med, error


def existing_confidence(row: dict) -> tuple[str, str, str]:
    arrival = row.get("到达时间", "")
    if arrival in {"", "待确认"}:
        return "D", "待确认", "否"
    note = row.get("到达时间说明", "")
    unc = row.get("到达误差估计（分钟）", "")
    try:
        u = float(unc)
    except Exception:
        u = None
    if u is not None and u <= 5:
        return "A", "终点ETA确认/一致估算", "是"
    if "末次终点ETA推算" in note:
        m = re.search(r"终点ETA\s*(\d+)分钟", note)
        eta = int(m.group(1)) if m else 999
        if eta <= 20:
            return "B", "末次终点ETA兜底", "是"
        if eta <= 45:
            return "C", "末次终点ETA兜底", "是"
        return "D", "末次终点ETA兜底（误差可能>15分钟）", "否"
    return "C", "已有到达估算", "是"


def load_history(current_date: str) -> list[tuple[str, dict]]:
    out = []
    export = ROOT / "data" / "export"
    for path in sorted(export.glob("????-??-??-operations.csv")):
        date = path.name[:10]
        if date > current_date:
            continue
        try:
            rows = read_csv(path)
        except Exception:
            continue
        for row in rows:
            out.append((date, row))
    return out


def build_duration_history(history):
    values = defaultdict(list)
    for date, row in history:
        if row.get("班次类型") != "全程车" or row.get("到达时间") in {"", "待确认"}:
            continue
        dep = parse_dt(date, row.get("发车时间", ""))
        arr = parse_dt(date, row.get("到达时间", ""))
        if not dep or not arr:
            continue
        if arr < dep:
            arr += timedelta(days=1)
        mins = (arr - dep).total_seconds() / 60
        if 30 <= mins <= MAX_TRIP_MIN:
            key = (row.get("线路", ""), row.get("车牌号", ""), row.get("方向", ""), bucket(date, row.get("发车时间", "")))
            values[key].append(mins)
    return values


def build_layover_history(history):
    by_vehicle_date = defaultdict(list)
    for date, row in history:
        dep = parse_dt(date, row.get("发车时间", ""))
        if dep:
            by_vehicle_date[(date, row.get("线路", ""), row.get("车牌号", ""))].append((dep, row))
    values = defaultdict(list)
    for (date, route, plate), seq in by_vehicle_date.items():
        seq.sort(key=lambda x: x[0])
        for i, (dep, row) in enumerate(seq):
            if row.get("班次类型") != "全程车" or row.get("到达时间") in {"", "待确认"}:
                continue
            arr = parse_dt(date, row.get("到达时间", ""))
            if not arr:
                continue
            if arr < dep:
                arr += timedelta(days=1)
            for next_dep, nxt in seq[i + 1:]:
                if nxt.get("方向") == row.get("方向"):
                    continue
                lay = (next_dep - arr).total_seconds() / 60
                if 0 <= lay <= MAX_LAYOVER_MIN:
                    key = (route, plate, row.get("方向", ""), bucket(date, row.get("发车时间", "")))
                    values[key].append(lay)
                break
    return values


def load_moving_events(date: str):
    events = defaultdict(list)
    for path in sorted((ROOT / "data" / "shmaas").glob(f"{date}-*.jsonl")):
        if path.name.endswith("-reverse-watch.jsonl"):
            continue
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
                        if str(obs.get("role") or "") not in {"current", "next"}:
                            continue
                        try:
                            direction = int(obs.get("direction"))
                            eta = float(obs.get("arrive_time"))
                        except Exception:
                            continue
                        if eta < 0 or eta > 180:
                            continue
                        events[(route, plate, str(direction))].append({
                            "time": captured,
                            "stop": str(obs.get("stop_name") or ""),
                            "eta": eta,
                            "predicted_stop": captured + timedelta(minutes=eta),
                        })
    for key in events:
        events[key].sort(key=lambda e: e["time"])
    return events


def build_segment_history(history, current_date: str):
    confirmed = defaultdict(list)
    for date, row in history:
        if row.get("班次类型") != "全程车" or row.get("到达时间") in {"", "待确认"}:
            continue
        dep = parse_dt(date, row.get("发车时间", ""))
        arr = parse_dt(date, row.get("到达时间", ""))
        if not dep or not arr:
            continue
        if arr < dep:
            arr += timedelta(days=1)
        confirmed[date].append((row, dep, arr))

    values = defaultdict(list)
    for date, trips in confirmed.items():
        raw_dir = ROOT / "data" / "shmaas"
        if not any(raw_dir.glob(f"{date}-*.jsonl")):
            continue
        events = load_moving_events(date)
        for row, dep, arr in trips:
            key = (row.get("线路", ""), row.get("车牌号", ""), row.get("方向", ""))
            candidates = [e for e in events.get(key, []) if dep <= e["time"] <= arr and e["predicted_stop"] <= arr + timedelta(minutes=5)]
            for e in candidates:
                rem = (arr - e["predicted_stop"]).total_seconds() / 60
                if 0 <= rem <= MAX_TRIP_MIN:
                    hkey = key + (e["stop"], bucket(date, row.get("发车时间", "")))
                    values[hkey].append(rem)
    return values


def set_estimate(row, dep, arrival, error, method, detail):
    if arrival < dep or arrival > dep + timedelta(minutes=MAX_TRIP_MIN):
        return False
    grade = "B" if error <= 10 else "C"
    row["到达时间"] = fmt(arrival)
    row["全程时间"] = f"{round((arrival - dep).total_seconds() / 60)}分钟"
    row["到达时间说明"] = detail
    row["到达误差估计（分钟）"] = str(error)
    row["到达置信度"] = grade
    row["到达估算方法"] = method
    row["参与车速排名"] = "是"
    return True


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--date")
    args = ap.parse_args()
    date = args.date or datetime.now(TZ).date().isoformat()
    export = ROOT / "data" / "export"
    combined = export / f"{date}-operations.csv"
    if not combined.exists():
        print("No operations export; skip individual arrival estimation")
        return 0
    rows = read_csv(combined)
    if not rows:
        return 0

    for row in rows:
        conf, method, rank = existing_confidence(row)
        row["到达置信度"] = conf
        row["到达估算方法"] = method
        row["参与车速排名"] = rank

    history = [(d, r) for d, r in load_history(date) if existing_confidence(r)[2] == "是"]
    duration_hist = build_duration_history(history)
    layover_hist = build_layover_history(history)
    segment_hist = build_segment_history(history, date)
    current_events = load_moving_events(date)

    by_vehicle = defaultdict(list)
    for row in rows:
        dep = parse_dt(date, row.get("发车时间", ""))
        if dep:
            by_vehicle[(row.get("线路", ""), row.get("车牌号", ""))].append((dep, row))
    for key in by_vehicle:
        by_vehicle[key].sort(key=lambda x: x[0])

    counts = defaultdict(int)
    for row in rows:
        if row.get("班次类型") != "全程车" or row.get("到达时间") not in {"", "待确认"}:
            continue
        route, plate, direction = row.get("线路", ""), row.get("车牌号", ""), row.get("方向", "")
        dep = parse_dt(date, row.get("发车时间", ""))
        if not dep:
            continue
        b = bucket(date, row.get("发车时间", ""))

        next_reverse = None
        for nd, nr in by_vehicle[(route, plate)]:
            if nd > dep and nr.get("方向") != direction and nd <= dep + timedelta(minutes=MAX_TRIP_MIN):
                next_reverse = nd
                break
        stats = robust_stats(layover_hist.get((route, plate, direction, b), []))
        if next_reverse and stats:
            med, err = stats
            if set_estimate(row, dep, next_reverse - timedelta(minutes=med), err, "同车反向发车反推", f"同车下一次反向发车{fmt(next_reverse)}，减去该车自身同类时段历史终点停站中位数{round(med)}分钟，约±{err}分钟"):
                counts["reverse_departure"] += 1
                continue

        evs = [e for e in current_events.get((route, plate, direction), []) if dep <= e["time"] <= dep + timedelta(minutes=MAX_TRIP_MIN)]
        for e in sorted(evs, key=lambda x: x["time"], reverse=True):
            stats = robust_stats(segment_hist.get((route, plate, direction, e["stop"], b), []))
            if not stats:
                continue
            med, err = stats
            est = e["predicted_stop"] + timedelta(minutes=med)
            if set_estimate(row, dep, est, err, "同车重点站剩余时间", f"{fmt(e['time'])}采样：该车到重点站{e['stop']} ETA {round(e['eta'])}分钟；叠加该车自身同类时段从该站到终点历史中位数{round(med)}分钟，约±{err}分钟"):
                counts["own_segment"] += 1
                break
        if row.get("到达时间") not in {"", "待确认"}:
            continue

        stats = robust_stats(duration_hist.get((route, plate, direction, b), []))
        if stats:
            med, err = stats
            if set_estimate(row, dep, dep + timedelta(minutes=med), err, "同车历史全程时间", f"按该车自身同方向、同类时段历史全程时间中位数{round(med)}分钟估算，约±{err}分钟"):
                counts["own_duration"] += 1
                continue

        row["到达置信度"] = "D"
        row["到达估算方法"] = "证据不足"
        row["参与车速排名"] = "否"

    fields = list(rows[0].keys())
    extras = ["到达置信度", "到达估算方法", "参与车速排名"]
    fields = [f for f in fields if f not in extras] + extras
    write_csv(combined, rows, fields)

    by_route = defaultdict(list)
    for row in rows:
        by_route[row.get("线路", "")].append(row)
    for route, route_rows in by_route.items():
        route_path = export / f"{date}-{route.replace('/', '_')}.csv"
        write_csv(route_path, route_rows, [f for f in fields if f != "线路"])

    meta_path = export / f"{date}-operations-meta.json"
    if meta_path.exists():
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        confidence_counts = defaultdict(int)
        rankable = 0
        for row in rows:
            confidence_counts[row.get("到达置信度", "D")] += 1
            if row.get("参与车速排名") == "是":
                rankable += 1
        meta["arrival_confidence_counts"] = dict(confidence_counts)
        meta["speed_rank_eligible_count"] = rankable
        meta["individual_arrival_estimate_counts"] = dict(counts)
        meta["individual_arrival_rule"] = (
            "Arrival estimates never borrow another vehicle's behavior. Historical model inputs are themselves restricted to rank-eligible A/B/C arrivals. Priority after terminal ETA: same vehicle next reverse departure minus its own historical layover; same vehicle key-stop ETA plus its own historical remaining time; same vehicle historical full-trip duration in the same time bucket. Only A/B/C (estimated error <=15 min) may participate in speed ranking; D is display-only or pending."
        )
        meta_path.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")

    print(f"Individual arrival estimates: {dict(counts)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
