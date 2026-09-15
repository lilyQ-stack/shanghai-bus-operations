from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from src.private_short_turn_classifier import Event, classify_short_turn

TZ = ZoneInfo("Asia/Shanghai")
BASE = datetime(2026, 9, 15, 6, 36, tzinfo=TZ)


def ev(minutes, direction, seq, name="中途站"):
    return Event(BASE + timedelta(minutes=minutes), direction, "current", seq, name, 3.0)


def test_private_reversal_is_primary():
    result = classify_short_turn(
        events=[ev(30, 0, 25), ev(45, 1, 40)], direction=0, dep_dt=BASE,
        end_stop="康恩路康浦路", stop_count=64, terminal_eta_confirmed=False,
    )
    assert result.service_type == "疑似区间车"
    assert result.evidence == "private_reversal"


def test_terminal_eta_guard_beats_opposite_observation():
    result = classify_short_turn(
        events=[ev(30, 0, 25), ev(45, 1, 40)], direction=0, dep_dt=BASE,
        end_stop="康恩路康浦路", stop_count=64, terminal_eta_confirmed=True,
        temporal_impossibility=lambda: (True, "should not override terminal proof"),
    )
    assert result.service_type == "全程车"
    assert result.evidence == "terminal_eta_guard"


def test_temporal_impossibility_supplements_private_miss():
    result = classify_short_turn(
        events=[ev(30, 0, 25), ev(80, 1, 50)], direction=0, dep_dt=BASE,
        end_stop="康恩路康浦路", stop_count=64, terminal_eta_confirmed=False,
        temporal_impossibility=lambda: (True, "历史可信下界仍需160分钟，实际139分钟"),
    )
    assert result.service_type == "疑似区间车"
    assert result.evidence == "temporal_impossibility"


def test_no_hardcoded_cycle_threshold():
    result = classify_short_turn(
        events=[], direction=0, dep_dt=BASE, end_stop="康恩路康浦路", stop_count=64,
        terminal_eta_confirmed=False, temporal_impossibility=lambda: (False, "样本不足"),
    )
    assert result.service_type == "全程车"
