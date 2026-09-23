from __future__ import annotations

"""Short-turn classification core for the private-pipeline migration.

Current operational rule:
  * no hard elapsed-time window;
  * if the vehicle disappears while still inside the first 80% of its source trip,
    it becomes a reverse-tracking candidate immediately;
  * once reconstructed reverse physical position advances >=2 stops from the first
    credible reverse physical point, classify as 疑似区间车.

Reliable same-direction terminal ETA remains the hard full-trip guard.  A learned
spatiotemporal-impossibility check remains supplemental evidence only.
"""

from dataclasses import dataclass
from datetime import datetime
from typing import Callable, Iterable

SOURCE_PROGRESS_LIMIT = 0.80
REVERSE_CONFIRM_GAIN_STOPS = 2


@dataclass(frozen=True)
class Event:
    time: datetime
    direction: int
    role: str
    stop_seq: int | None
    stop_name: str
    arrive_minutes: float | None
    remaining_stops: float | None = None


@dataclass(frozen=True)
class Classification:
    service_type: str
    reason: str
    evidence: str


def physical_seq(e: Event) -> int | None:
    try:
        if e.remaining_stops is None:
            return None
        return max(1, int(e.stop_seq) - int(e.remaining_stops))
    except (TypeError, ValueError):
        return None


def progress(seq: int, stop_count: int) -> float | None:
    if stop_count < 2 or seq < 1 or seq > stop_count:
        return None
    return (seq - 1) / (stop_count - 1)


def reverse_progress_evidence(
    events: Iterable[Event],
    direction: int,
    dep_dt: datetime,
    stop_count: int,
    terminal_eta_confirmed: bool,
) -> Classification | None:
    if terminal_eta_confirmed:
        return None

    moving = [e for e in events if e.time >= dep_dt and e.role in {"current", "next"}]
    source = [(e, physical_seq(e)) for e in moving if e.direction == direction]
    source = [(e, seq) for e, seq in source if seq is not None]
    if not source:
        return None

    last_source, last_source_seq = max(source, key=lambda x: x[0].time)
    source_fraction = progress(last_source_seq, stop_count)
    if source_fraction is None or source_fraction >= SOURCE_PROGRESS_LIMIT:
        return None

    reverse = [(e, physical_seq(e)) for e in moving if e.direction != direction and e.time > last_source.time]
    reverse = [(e, seq) for e, seq in reverse if seq is not None]
    if not reverse:
        return None

    reverse.sort(key=lambda x: x[0].time)
    first_seq = reverse[0][1]
    max_seq = max(seq for _, seq in reverse)
    gain = max_seq - first_seq
    if gain < REVERSE_CONFIRM_GAIN_STOPS:
        return None

    return Classification(
        "疑似区间车",
        f"原方向在全程{source_fraction:.0%}处后消失；随后反向物理轨迹由seq{first_seq}推进至seq{max_seq}，推进{gain}站，达到≥{REVERSE_CONFIRM_GAIN_STOPS}站确认条件",
        "reverse_physical_progress",
    )


def classify_short_turn(
    *,
    events: Iterable[Event],
    direction: int,
    dep_dt: datetime,
    end_stop: str,
    stop_count: int,
    terminal_eta_confirmed: bool,
    temporal_impossibility: Callable[[], tuple[bool, str]] | None = None,
) -> Classification:
    if terminal_eta_confirmed:
        return Classification("全程车", "已取得可靠同向线路终点ETA，按全程车保护", "terminal_eta_guard")

    reverse_hit = reverse_progress_evidence(events, direction, dep_dt, stop_count, False)
    if reverse_hit:
        return reverse_hit

    if temporal_impossibility is not None:
        impossible, note = temporal_impossibility()
        if impossible:
            return Classification("疑似区间车", f"正常跑完全程后再折返在时间上不可实现：{note}", "temporal_impossibility")

    return Classification("待确认", "既无可靠终点到达证据，也未形成反向≥2站物理推进证据或可靠时空矛盾", "insufficient_evidence")
