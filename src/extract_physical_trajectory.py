from __future__ import annotations

import argparse
import json
from dataclasses import dataclass, asdict
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
TZ = ZoneInfo("Asia/Shanghai")

@dataclass(frozen=True)
class Point:
    time: str
    direction: int
    role: str
    queried_seq: int
    remaining_stops: int
    physical_seq: int
    queried_stop: str


def physical_seq(stop_seq, remaining_stops):
    try:
        return max(1, int(stop_seq) - int(remaining_stops))
    except (TypeError, ValueError):
        return None


def extract(date: str, route: str, plate: str) -> list[Point]:
    path = ROOT / "data" / "shmaas" / f"{date}-{route}.jsonl"
    points = {}
    with path.open(encoding="utf-8") as f:
        for line in f:
            if not line.strip():
                continue
            try:
                snap = json.loads(line)
                if not snap.get("success") or snap.get("route") != route:
                    continue
                captured = datetime.fromisoformat(snap["sample_time_cst"]).astimezone(TZ)
            except Exception:
                continue
            for vehicle in snap.get("vehicles", []) or []:
                if str(vehicle.get("plate") or "").strip() != plate:
                    continue
                for obs in vehicle.get("observations", []) or []:
                    if str(obs.get("role") or "") not in {"current", "next"}:
                        continue
                    seq = physical_seq(obs.get("stop_seq"), obs.get("remaining_stops"))
                    if seq is None:
                        continue
                    try:
                        direction = int(obs.get("direction")); qseq = int(obs.get("stop_seq")); rem = int(obs.get("remaining_stops"))
                    except (TypeError, ValueError):
                        continue
                    # Stop-centric rows from several queried stops in one snapshot collapse
                    # to one physical point when reconstruction agrees.
                    key = (captured.isoformat(), direction, seq)
                    points.setdefault(key, Point(captured.isoformat(), direction, str(obs.get("role") or ""), qseq, rem, seq, str(obs.get("stop_name") or "")))
    return sorted(points.values(), key=lambda p: (p.time, p.direction, p.physical_seq))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", required=True); ap.add_argument("--route", required=True); ap.add_argument("--plate", required=True)
    args = ap.parse_args()
    for point in extract(args.date, args.route, args.plate):
        print(json.dumps(asdict(point), ensure_ascii=False))

if __name__ == "__main__":
    main()
