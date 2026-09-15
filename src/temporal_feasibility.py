from __future__ import annotations

import csv
import re
import statistics
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path

MIN_HISTORY_SAMPLES = 5
LOW_QUANTILE = 0.10
MIN_TURNAROUND_MIN = 2.0
SAFETY_MARGIN_MIN = 5.0
MIN_PROGRESS_FRACTION = 0.05


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
        idx = max(0, min(len(s) - 1, int((len(s) - 1) * LOW_QUANTILE)))
        out[key] = DirectionBound(route, key, len(s), s[idx], statistics.median(s))
    return out


def progress_fraction(seq: float | int, stop_count: int) -> float | None:
    """Fraction of a route already travelled at a reconstructed physical stop sequence."""
    try:
        seq = float(seq); stop_count = int(stop_count)
    except (TypeError, ValueError):
        return None
    if stop_count < 2 or seq < 1 or seq > stop_count:
        return None
    return max(0.0, min(1.0, (seq - 1.0) / (stop_count - 1.0)))


def partial_normal_lower_bound(
    *,
    outbound: DirectionBound,
    reverse: DirectionBound,
    outbound_last_seq: float | int,
    outbound_stop_count: int,
    reverse_observed_seq: float | int,
    reverse_stop_count: int,
) -> tuple[float, str] | None:
    """Earliest credible time from an outbound physical position to a reverse position.

    Uses route-progress fractions against empirical low-quantile full-trip durations.
    This is intentionally conservative and is not a timetable model. It is only a
    physical-impossibility detector. Callers must pass reconstructed physical seq,
    never the queried stop_seq from a stop-centric ETA row.
    """
    p_out = progress_fraction(outbound_last_seq, outbound_stop_count)
    p_rev = progress_fraction(reverse_observed_seq, reverse_stop_count)
    if p_out is None or p_rev is None or p_rev < MIN_PROGRESS_FRACTION:
        return None
    remaining_out = (1.0 - p_out) * outbound.lower_full_trip_min
    travelled_reverse = p_rev * reverse.lower_full_trip_min
    bound = remaining_out + MIN_TURNAROUND_MIN + travelled_reverse
    note = (
        f"从原方向物理进度{p_out:.1%}到终点至少约{remaining_out:.1f}分 + "
        f"终点最短停站{MIN_TURNAROUND_MIN:.0f}分 + 反向物理进度{p_rev:.1%}至少约{travelled_reverse:.1f}分"
        f" = {bound:.1f}分（历史低10%全程：{outbound.lower_full_trip_min:.1f}/{reverse.lower_full_trip_min:.1f}分，"
        f"样本n={outbound.samples}/{reverse.samples}）"
    )
    return bound, note


def impossible_partial_transition(
    *,
    elapsed_min: float,
    outbound: DirectionBound,
    reverse: DirectionBound,
    outbound_last_seq: float | int,
    outbound_stop_count: int,
    reverse_observed_seq: float | int,
    reverse_stop_count: int,
) -> tuple[bool, str]:
    result = partial_normal_lower_bound(
        outbound=outbound, reverse=reverse,
        outbound_last_seq=outbound_last_seq, outbound_stop_count=outbound_stop_count,
        reverse_observed_seq=reverse_observed_seq, reverse_stop_count=reverse_stop_count,
    )
    if result is None:
        return False, "物理位置或历史样本不足，不能形成时空不可能性证据"
    bound, detail = result
    impossible = elapsed_min + SAFETY_MARGIN_MIN < bound
    return impossible, f"{detail}；两次物理观测实际间隔{elapsed_min:.1f}分，安全余量{SAFETY_MARGIN_MIN:.0f}分"


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
