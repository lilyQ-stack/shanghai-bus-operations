from __future__ import annotations

import csv
import json
import statistics
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from openpyxl import Workbook
from openpyxl.styles import PatternFill

ROOT = Path(__file__).resolve().parents[1]
TZ = ZoneInfo("Asia/Shanghai")
ROUTES = ("浦东78路", "浦东35路", "182路")
EARLY_REVERSE_RATIO = 0.80
MIN_REVERSE_ELAPSED_MIN = 20

FIELDS = [
    "线路", "车牌号", "方向", "发车时间", "计划发车时间", "发车时间依据",
    "到达时间", "全程时间", "班次类型", "班次类型判定依据",
    "到达置信度", "到达估算方法", "参与车速排名", "本车首次观测",
    "最后可靠采集站点", "最后可靠预计到达时间", "备注",
]


def parse_dt(date: str, hm: str):
    try:
        return datetime.fromisoformat(f"{date}T{hm}:00+08:00").astimezone(TZ)
    except Exception:
        return None


def fmt_hm(dt):
    return dt.astimezone(TZ).strftime("%H:%M") if dt else ""


def number(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def load_snapshots(date: str):
    snapshots = []
    source_files = []
    for path in sorted((ROOT / "data" / "shmaas").glob(f"{date}-*.jsonl")):
        source_files.append(path.name)
        with path.open(encoding="utf-8") as f:
            for line in f:
                try:
                    row = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if row.get("success") and row.get("sample_time_cst") and row.get("route"):
                    snapshots.append(row)
    snapshots.sort(key=lambda x: datetime.fromisoformat(x["sample_time_cst"]))
    return snapshots, source_files


def collect_evidence(snapshots):
    route_info = {}
    events = defaultdict(list)
    dispatches = {}
    first_seen = {}
    for snap in snapshots:
        route = str(snap["route"])
        captured = datetime.fromisoformat(snap["sample_time_cst"]).astimezone(TZ)
        for d in snap.get("directions", []) or []:
            try:
                direction = int(d.get("direction"))
            except Exception:
                continue
            route_info[(route, direction)] = {
                "start": str(d.get("start_stop") or ""),
                "end": str(d.get("end_stop") or ""),
                "stop_count": int(d.get("stop_count") or 0),
            }
        for vehicle in snap.get("vehicles", []) or []:
            plate = str(vehicle.get("plate") or "").strip()
            if not plate:
                continue
            key = (route, plate)
            first_seen[key] = min(first_seen.get(key, captured), captured)
            for obs in vehicle.get("observations", []) or []:
                try:
                    direction = int(obs.get("direction"))
                except Exception:
                    continue
                event = {
                    "time": captured, "direction": direction,
                    "role": str(obs.get("role") or ""),
                    "stop_seq": int(obs.get("stop_seq") or 0),
                    "stop_name": str(obs.get("stop_name") or ""),
                    "eta": number(obs.get("arrive_time")),
                    "distance": number(obs.get("distance")),
                    "hints": obs.get("service_hints") or [],
                }
                events[key].append(event)
                dispatch_time = str(obs.get("dispatch_time") or "").strip()
                if dispatch_time:
                    dk = (route, plate, direction, dispatch_time)
                    existing = dispatches.get(dk)
                    dispatches[dk] = {
                        "route": route, "plate": plate, "direction": direction,
                        "departure": dispatch_time,
                        "first_seen": captured if existing is None else min(existing["first_seen"], captured),
                        "last_seen": captured if existing is None else max(existing["last_seen"], captured),
                        "hints": (existing or {}).get("hints") or (obs.get("service_hints") or []),
                    }
    for key in events:
        events[key].sort(key=lambda x: x["time"])
    return route_info, events, dispatches, first_seen


def explicit_service(hints, terminal):
    if not hints:
        return None
    values = " ".join(str(x.get("value") or "") for x in hints)
    fields = " ".join(str(x.get("field") or "") for x in hints).lower()
    if any(w in values for w in ("区间", "短线", "短途", "折返")) or any(w in fields for w in ("short", "section")):
        return "区间车", "SHMAAS返回明确区间/短线标识"
    if terminal and terminal in values:
        return "全程车", "SHMAAS返回明确线路终点"
    return None


def terminal_eta(route_info, vehicle_events, route, direction, dep):
    info = route_info.get((route, direction), {})
    terminal = info.get("end", "")
    count = info.get("stop_count", 0)
    candidates = []
    for e in vehicle_events:
        if e["direction"] != direction or e["time"] < dep or e["role"] not in {"current", "next"}:
            continue
        at_terminal_query = e["stop_name"] == terminal or (count and e["stop_seq"] == count)
        if not at_terminal_query or e["eta"] is None or not 0 <= e["eta"] <= 180:
            continue
        predicted = e["time"] + timedelta(minutes=e["eta"])
        if dep <= predicted <= dep + timedelta(hours=4):
            candidates.append((e, predicted))
    if not candidates:
        return None
    near = [x for x in candidates if x[0]["eta"] <= 5 or (x[0]["distance"] is not None and x[0]["distance"] <= 100)]
    if near:
        e, predicted = min(near, key=lambda x: (x[0]["eta"], x[0]["time"]))
        confirmed = e["eta"] <= 1 or (e["distance"] is not None and e["distance"] <= 100)
        return predicted, "终点近站ETA", 2 if confirmed else 5, confirmed
    predictions = sorted(x[1] for x in candidates)
    if len(predictions) >= 2:
        spread = (predictions[-1] - predictions[0]).total_seconds() / 60
        if spread <= 10:
            median = datetime.fromtimestamp(statistics.median([x.timestamp() for x in predictions]), TZ)
            return median, "多次终点ETA一致", max(3, min(5, round(spread / 2) + 1)), False
    e, predicted = candidates[-1]
    if e["eta"] <= 20:
        return predicted, "末次终点ETA", 10, False
    if e["eta"] <= 45:
        return predicted, "末次终点ETA", 15, False
    return predicted, "末次终点ETA", 20, False


def derive_min_full_runtimes(date, route_info, events, dispatches):
    runtimes = defaultdict(list)
    for d in dispatches.values():
        dep = parse_dt(date, d["departure"])
        if dep is None:
            continue
        route, plate, direction = d["route"], d["plate"], d["direction"]
        eta = terminal_eta(route_info, events.get((route, plate), []), route, direction, dep)
        if eta:
            minutes = (eta[0] - dep).total_seconds() / 60
            if 30 <= minutes <= 240:
                runtimes[(route, direction)].append(minutes)
    return {key: min(values) for key, values in runtimes.items() if values}


def trajectory_classification(route_info, vehicle_events, route, direction, dep, next_same_dep, min_full_runtime):
    info = route_info.get((route, direction), {})
    count = info.get("stop_count", 0)
    terminal = info.get("end", "")
    window = [e for e in vehicle_events if e["time"] >= dep and (next_same_dep is None or e["time"] < next_same_dep)]
    same = [e for e in window if e["direction"] == direction and e["role"] in {"current", "next"} and e["eta"] is not None]
    opp = [e for e in window if e["direction"] != direction and e["role"] in {"current", "next"} and e["eta"] is not None and (e["time"] - dep).total_seconds() / 60 >= MIN_REVERSE_ELAPSED_MIN]
    if not same or not opp:
        return "全程车", "未发现可靠中途折返证据", "", ""
    first_opp = min(opp, key=lambda x: x["time"])
    last_same = max((e for e in same if e["time"] < first_opp["time"]), key=lambda x: x["time"], default=None)
    if not last_same:
        return "全程车", "未发现可靠中途折返证据", "", ""
    elapsed = (first_opp["time"] - dep).total_seconds() / 60
    near_terminal = last_same["stop_name"] == terminal or (count and last_same["stop_seq"] / count >= 0.85)
    predicted = last_same["time"] + timedelta(minutes=last_same["eta"])
    if min_full_runtime is not None:
        threshold = min_full_runtime * EARLY_REVERSE_RATIO
        if elapsed < threshold and not near_terminal:
            return "疑似区间车", f"同车发车{round(elapsed)}分钟后已反向运行，早于该方向已确认全程最短{round(min_full_runtime)}分钟的80%阈值（{round(threshold, 1)}分钟）", last_same["stop_name"], fmt_hm(predicted)
        if elapsed < threshold and near_terminal:
            return "运行异常待查", f"同车提前反向运行，但末次同向观测已进入线路末段；需排除终点漏采（{round(elapsed)}<{round(threshold, 1)}分钟）", last_same["stop_name"], fmt_hm(predicted)
    gap = (first_opp["time"] - last_same["time"]).total_seconds() / 60
    if min_full_runtime is None and not near_terminal and 0 <= gap <= 20:
        return "疑似区间车", f"尚无全程基线；原方向末次观测停留在中途站，{round(gap)}分钟后同车反向运行", last_same["stop_name"], fmt_hm(predicted)
    if near_terminal:
        return "全程车", "已观测至线路末段且未满足提前反向判据；按全程车处理，终点到达证据单独评估", last_same["stop_name"], fmt_hm(predicted)
    return "运行异常待查", "存在方向切换，但未达到提前反向80%判据", last_same["stop_name"], fmt_hm(predicted)


def confidence(error):
    if error is None:
        return "D", "否"
    if error <= 5:
        return "A", "是"
    if error <= 10:
        return "B", "是"
    if error <= 15:
        return "C", "是"
    return "D", "否"


def build_rows(date, route_info, events, dispatches, first_seen):
    rows = []
    min_full_runtimes = derive_min_full_runtimes(date, route_info, events, dispatches)
    by_vehicle_direction = defaultdict(list)
    for d in dispatches.values():
        dep = parse_dt(date, d["departure"])
        if dep:
            by_vehicle_direction[(d["route"], d["plate"], d["direction"])].append(dep)
    for key in by_vehicle_direction:
        by_vehicle_direction[key].sort()
    ordered = sorted(dispatches.values(), key=lambda x: (x["route"], x["plate"], x["departure"], x["direction"]))
    for d in ordered:
        route, plate, direction = d["route"], d["plate"], d["direction"]
        dep = parse_dt(date, d["departure"])
        if dep is None:
            continue
        ve = events.get((route, plate), [])
        next_same = next((x for x in by_vehicle_direction[(route, plate, direction)] if x > dep), None)
        terminal = route_info.get((route, direction), {}).get("end", "")
        explicit = explicit_service(d.get("hints") or [], terminal)
        eta = terminal_eta(route_info, ve, route, direction, dep)
        if explicit:
            service_type, service_basis = explicit
            last_stop = last_eta = ""
        else:
            service_type, service_basis, last_stop, last_eta = trajectory_classification(route_info, ve, route, direction, dep, next_same, min_full_runtimes.get((route, direction)))
        arrival = "待确认"
        arrival_method = "证据不足"
        error = None
        arrival_confirmed = False
        note = ""
        if eta and service_type == "全程车":
            arrival_dt, arrival_method, error, arrival_confirmed = eta
            arrival = fmt_hm(arrival_dt)
            note = f"{arrival_method}推算，约±{error}分钟"
            if last_stop:
                note += "；末段轨迹已观测，班次类型与到达置信度分开判定"
        elif eta and service_type == "运行异常待查":
            note = "存在终点ETA，但运行轨迹仍需复核"
        elif service_type == "全程车" and last_stop:
            note = "已进入线路末段，未发现中途折返证据；终点存在漏采，到达时间待确认"
        grade, rank_ok = confidence(error)
        duration = ""
        if arrival != "待确认":
            arr_dt = parse_dt(date, arrival)
            if arr_dt and arr_dt < dep:
                arr_dt += timedelta(days=1)
            if arr_dt:
                duration = str(round((arr_dt - dep).total_seconds() / 60))
        rows.append({
            "线路": route, "车牌号": plate, "方向": str(direction), "发车时间": d["departure"],
            "计划发车时间": d["departure"], "发车时间依据": "SHMAAS待发计划", "到达时间": arrival,
            "全程时间": duration, "班次类型": service_type, "班次类型判定依据": service_basis,
            "到达置信度": grade, "到达估算方法": arrival_method,
            "参与车速排名": rank_ok if service_type == "全程车" else "否",
            "本车首次观测": fmt_hm(first_seen.get((route, plate))), "最后可靠采集站点": last_stop,
            "最后可靠预计到达时间": last_eta, "备注": note, "_终点确认到达": arrival_confirmed,
        })
    reconcile_departures(date, rows)
    return rows


def reconcile_departures(date, rows):
    by_vehicle = defaultdict(list)
    for row in rows:
        by_vehicle[(row["线路"], row["车牌号"])].append(row)
    for group in by_vehicle.values():
        group.sort(key=lambda r: parse_dt(date, r["发车时间"]) or datetime.max.replace(tzinfo=TZ))
        for prev, cur in zip(group, group[1:]):
            if prev["班次类型"] != "全程车" or prev["到达时间"] == "待确认" or not prev.get("_终点确认到达", False):
                continue
            arr = parse_dt(date, prev["到达时间"])
            planned = parse_dt(date, cur["计划发车时间"])
            if not arr or not planned:
                continue
            if planned < arr:
                actual = arr + timedelta(minutes=2)
                cur["发车时间"] = fmt_hm(actual)
                cur["发车时间依据"] = "终点确认到达+2分钟（覆盖计划时间）"
                if cur["到达时间"] != "待确认":
                    end = parse_dt(date, cur["到达时间"])
                    if end and end <= actual:
                        cur["到达时间"] = "待确认"; cur["全程时间"] = ""; cur["到达置信度"] = "D"; cur["到达估算方法"] = "证据不足"; cur["参与车速排名"] = "否"; cur["备注"] = "实际发车晚于原到达估计，原到达估计作废"; cur["_终点确认到达"] = False
                    elif end:
                        cur["全程时间"] = str(round((end - actual).total_seconds() / 60))


def add_rankings(rows):
    trip_times = defaultdict(list)
    for row in rows:
        if row["班次类型"] != "全程车" or row["参与车速排名"] != "是":
            continue
        try:
            minutes = int(row["全程时间"])
        except Exception:
            continue
        if minutes > 0:
            trip_times[(row["线路"], row["车牌号"])].append(minutes)
    ranks = {}
    by_route = defaultdict(list)
    for (route, plate), values in trip_times.items():
        by_route[route].append((statistics.mean(values), plate))
    for route, values in by_route.items():
        values.sort()
        for rank, (_, plate) in enumerate(values, 1):
            ranks[(route, plate)] = rank
    for row in rows:
        row["当日车速排名"] = ranks.get((row["线路"], row["车牌号"]), "")
        row["当日计入排名班次"] = len(trip_times.get((row["线路"], row["车牌号"]), []))
        vals = trip_times.get((row["线路"], row["车牌号"]), [])
        row["当日平均全程时间（分钟）"] = round(statistics.mean(vals), 1) if vals else ""


def write_csv(path, rows, fields):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fields); writer.writeheader()
        for row in rows:
            writer.writerow({k: row.get(k, "") for k in fields})


def write_xlsx(path, rows, fields):
    wb = Workbook(); wb.remove(wb.active)
    yellow = PatternFill(fill_type="solid", fgColor="FFF2CC")
    for name, subset in [("全部班次", rows)] + [(route, [r for r in rows if r["线路"] == route]) for route in ROUTES]:
        ws = wb.create_sheet(name[:31]); visible = [f for f in fields if f != "方向"]; ws.append(visible)
        for row in subset:
            ws.append([row.get(f, "") for f in visible])
            if row.get("班次类型") == "运行异常待查":
                for field in ("最后可靠采集站点", "最后可靠预计到达时间"):
                    if field in visible:
                        ws.cell(ws.max_row, visible.index(field) + 1).fill = yellow
        ws.freeze_panes = "A2"; ws.auto_filter.ref = ws.dimensions
        for col in ws.columns:
            width = min(max(len(str(c.value or "")) for c in col) + 2, 40)
            ws.column_dimensions[col[0].column_letter].width = width
    path.parent.mkdir(parents=True, exist_ok=True); wb.save(path)


def main():
    date = datetime.now(TZ).date().isoformat()
    snapshots, source_files = load_snapshots(date)
    if not snapshots:
        raise RuntimeError(f"No successful SHMAAS snapshots for {date}")
    route_info, events, dispatches, first_seen = collect_evidence(snapshots)
    rows = build_rows(date, route_info, events, dispatches, first_seen); add_rankings(rows)
    fields = FIELDS + ["当日车速排名", "当日计入排名班次", "当日平均全程时间（分钟）"]
    export = ROOT / "data" / "export"
    write_csv(export / f"{date}-operations.csv", rows, fields)
    for route in ROUTES:
        write_csv(export / f"{date}-{route}.csv", [r for r in rows if r["线路"] == route], [f for f in fields if f != "线路"])
    write_xlsx(export / f"{date}-上海公交运营.xlsx", rows, fields)
    meta = {
        "date": date, "source_files": source_files, "successful_snapshots": len(snapshots), "trip_count": len(rows),
        "routes": {route: sum(1 for r in rows if r["线路"] == route) for route in ROUTES},
        "confidence": {grade: sum(1 for r in rows if r["到达置信度"] == grade) for grade in "ABCD"},
        "service_types": dict((k, sum(1 for r in rows if r["班次类型"] == k)) for k in sorted({r["班次类型"] for r in rows})),
        "ranking_rule": "Within-route ranking uses each vehicle's arithmetic mean of rank-eligible full-trip runtimes; lower mean runtime ranks faster. Rank is numeric only.",
        "arrival_rule": "Terminal ETA evidence is preferred. Terminal sampling gaps do not by themselves make a trip operationally abnormal; service type and arrival confidence are evaluated separately. Only a near-terminal observation with ETA <=1 minute or distance <=100m is treated as confirmed enough to override an impossible next planned departure. Confidence A/B/C/D corresponds to estimated uncertainty <=5, <=10, <=15, >15 or insufficient minutes.",
        "short_turn_rule": "A suspected short turn requires the same vehicle to be observed running in the reverse direction at least 20 minutes after departure. If that reverse observation occurs before 80% of the route/direction's same-day confirmed minimum full-trip runtime and the last same-direction observation is not near the terminal, it is classified as a suspected short turn. Missing progression alone is never sufficient.",
    }
    (export / f"{date}-operations-meta.json").write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(meta, ensure_ascii=False))


if __name__ == "__main__":
    main()
