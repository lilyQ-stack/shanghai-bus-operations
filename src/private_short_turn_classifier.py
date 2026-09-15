from __future__ import annotations

"""Private-pipeline short-turn classification core.

The mature private rule remains the primary classifier:
  intermediate last same-direction running observation -> opposite running reappearance,
  with no reliable same-direction terminal ETA.

Public trajectory-v3/reverse-watch are deliberately not authoritative here.
Two safety additions are isolated and auditable:
  1) reliable terminal ETA is a hard full-trip guard;
  2) temporal-impossibility evidence can supplement the private rule, but its lower
     bound must be learned from credible route history (never a hard-coded 2.5 h).
"""

from dataclasses import dataclass
from datetime import datetime
from typing import Callable, Iterable

PRIVATE_REAPPEAR_WINDOW_MIN = 20


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


def _seq(value) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def private_reversal_evidence(
    events: Iterable[Event],
    direction: int,
    dep_dt: datetime,
    end_stop: str,
    stop_count: int,
    terminal_eta_confirmed: bool,
) -> Classification | None:
    """Exact private-style reversal rule, with terminal ETA protection first."""
    if terminal_eta_confirmed:
        return None
    moving = [
        e for e in events
        if e.time >= dep_dt and e.role in {"current", "next"} and e.arrive_minutes is not None
    ]
    same = [e for e in moving if e.direction == direction]
    opposite = [e for e in moving if e.direction != direction]
    if not same or not opposite:
        return None
    first_opp = min(opposite, key=lambda e: e.time)
    last_same = max((e for e in same if e.time < first_opp.time), key=lambda e: e.time, default=None)
    if last_same is None:
        return None
    gap = (first_opp.time - last_same.time).total_seconds() / 60
    intermediate = last_same.stop_name != end_stop and not (stop_count and _seq(last_same.stop_seq) == stop_count)
    if not intermediate or gap < 0 or gap > PRIVATE_REAPPEAR_WINDOW_MIN:
        return None
    where = last_same.stop_name or str(last_same.stop_seq or "未知站")
    return Classification(
        "疑似区间车",
        f"同车原方向末次运行观测在中途站{where}，未见终点ETA，{round(gap)}分钟后反方向重新出现",
        "private_reversal",
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
    """Final migration classifier entry point.

    Priority is intentionally simple:
      terminal ETA guard > private reversal evidence > learned temporal impossibility > full trip.
    """
    if terminal_eta_confirmed:
        return Classification("全程车", "已取得可靠同向线路终点ETA，按全程车保护", "terminal_eta_guard")

    private_hit = private_reversal_evidence(
        events, direction, dep_dt, end_stop, stop_count, terminal_eta_confirmed=False
    )
    if private_hit:
        return private_hit

    if temporal_impossibility is not None:
        impossible, note = temporal_impossibility()
        if impossible:
            return Classification(
                "疑似区间车",
                f"正常跑完全程后再折返在时间上不可实现：{note}",
                "temporal_impossibility",
            )

    return Classification("全程车", "未发现private中途折返证据或可靠时空矛盾", "no_short_turn_evidence")
