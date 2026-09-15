from __future__ import annotations

import csv
import re
import statistics
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

TZ = ZoneInfo("Asia/Shanghai")
MIN_HISTORY_SAMPLES = 5
LOW_QUANTILE = 0.10
MIN_TURNAROUND_MIN = 2.0
SAFETY_MARGIN_MIN = 5.0


@dataclass(frozen=True)
class DirectionBound:
    route: str
    direction_key: str
    samples: int
    lower_full_trip_min: float
    median_full_trip_min: float


def _minutes(value: str) -> float | None:
    if not value:
        return None
    m = re.search(r"(\d+(?:\.\d+)?)", str(value))
    if not m:
        return None
    x = float(m.group(1))
    return x if 20 <= x <= 300 else None


def _direction_key(row: dict) -> str:
    direction = str(row.get("方向") or "").strip()
    if direction:
        return direction
    return f"{row.get('始发站') or row.get('发车站') or ''}->{row.get('终点站') or ''}"


def _rank_eligible(row: dict) -> bool:
    if str(row.get("班次类型") or "") != "全程车":
        return False
    if str(row.get("参与车速排名") or "").strip() in {"否", "0", "False", "false"}:
        return False
    confidence = str(row.get("到达置信度") or "").strip().upper()
    if confidence and confidence not in {"A", "B", "C"}:
        return False
    note = str(row.get("到达时间说明") or row.get("到达估算方法") or "")
    return "终点" in note or confidence in {"A", "B", "C"}


def load_history_bounds(export_dir: Path, before_date: str, route: str) -> dict[str, DirectionBound]:
    values = defaultdict(list)
    for path in sorted(export_dir.glob("????-??-??-*.csv")):
        date = path.name[:10]
        if date >= before_date or path.name.endswith("-vehicles.csv") or path.name.endswith("-operations.csv"):
            continue
        try:
            with path.open(encoding="utf-8-sig", newline="") as f:
                rows = list(csv.DictReader(f))
        except Exception:
            continue
        for row in rows:
            row_route = str(row.get("线路") or path.name[11:-4])
            if row_route != route or not _rank_eligible(row):
                continue
            duration = _minutes(row.get("全程时间") or row.get("全程时间（分钟）") or "")
            if duration is not None:
                values[_direction_key(row)].append(duration)
    out = {}
    for key, samples in values.items():
        if len(samples) < MIN_HISTORY_SAMPLES:
            continue
        s = sorted(samples)
        # Conservative empirical lower bound: 10th percentile, not the single fastest outlier.
        idx = max(0, min(len(s) - 1, int((len(s) - 1) * LOW_QUANTILE)))
        out[key] = DirectionBound(route, key, len(s), s[idx], statistics.median(s))
    return out


def normal_roundtrip_lower_bound(outbound: DirectionBound, reverse: DirectionBound) -> float:
    return outbound.lower_full_trip_min + MIN_TURNAROUND_MIN + reverse.lower_full_trip_min


def impossible_roundtrip(*, elapsed_min: float, outbound: DirectionBound, reverse: DirectionBound) -> tuple[bool, str]:
    bound = normal_roundtrip_lower_bound(outbound, reverse)
    impossible = elapsed_min + SAFETY_MARGIN_MIN < bound
    note = (
        f"历史可信下界：去程{outbound.lower_full_trip_min:.1f}分（n={outbound.samples}）+"
        f"终点最短停站{MIN_TURNAROUND_MIN:.0f}分+返程{reverse.lower_full_trip_min:.1f}分（n={reverse.samples}）"
        f"={bound:.1f}分；实际仅{elapsed_min:.1f}分；安全余量{SAFETY_MARGIN_MIN:.0f}分"
    )
    return impossible, note
