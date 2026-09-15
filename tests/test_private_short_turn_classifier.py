from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from src.private_short_turn_classifier import Event, classify_short_turn

TZ = ZoneInfo("Asia/Shanghai")
BASE = datetime(2026, 9, 15, 6, 36, tzinfo=TZ)


def ev(minutes, direction, physical_seq, queried_seq=None, name="中途站"):
    # Build a stop-centric ETA row whose reconstructed physical position is explicit.
    queried_seq = queried_seq or physical_seq + 5
    remaining = queried_seq - physical_seq
    return Event(BASE + timedelta(minutes=minutes), direction, "current", queried_seq, name, 3.0, remaining)


def test_reverse_two_stop_progress_confirms_short_turn_without_time_limit():
    result = classify_short_turn(
        events=[ev(30, 0, 25), ev(90, 1, 30), ev(180, 1, 32)],
        direction=0, dep_dt=BASE, end_stop="康恩路康浦路", stop_count=64,
        terminal_eta_confirmed=False,
    )
    assert result.service_type == "疑似区间车"
    assert result.evidence == "reverse_physical_progress"


def test_one_reverse_stop_is_not_enough():
    result = classify_short_turn(
        events=[ev(30, 0, 25), ev(40, 1, 30), ev(50, 1, 31)],
        direction=0, dep_dt=BASE, end_stop="康恩路康浦路", stop_count=64,
        terminal_eta_confirmed=False,
    )
    assert result.service_type == "全程车"


def test_disappearance_at_or_after_80pct_is_not_reverse_rule_candidate():
    # seq 52/64 is ~81%, outside the tracking trigger range.
    result = classify_short_turn(
        events=[ev(30, 0, 52), ev(40, 1, 5), ev(50, 1, 12)],
        direction=0, dep_dt=BASE, end_stop="康恩路康浦路", stop_count=64,
        terminal_eta_confirmed=False,
    )
    assert result.service_type == "全程车"


def test_terminal_eta_guard_beats_reverse_progress():
    result = classify_short_turn(
        events=[ev(30, 0, 25), ev(45, 1, 30), ev(60, 1, 40)],
        direction=0, dep_dt=BASE, end_stop="康恩路康浦路", stop_count=64,
        terminal_eta_confirmed=True,
        temporal_impossibility=lambda: (True, "should not override terminal proof"),
    )
    assert result.service_type == "全程车"
    assert result.evidence == "terminal_eta_guard"


def test_temporal_impossibility_remains_supplemental():
    result = classify_short_turn(
        events=[ev(30, 0, 25)], direction=0, dep_dt=BASE,
        end_stop="康恩路康浦路", stop_count=64, terminal_eta_confirmed=False,
        temporal_impossibility=lambda: (True, "历史可信下界不足以完成正常全程折返"),
    )
    assert result.service_type == "疑似区间车"
    assert result.evidence == "temporal_impossibility"


def test_no_hardcoded_time_threshold():
    result = classify_short_turn(
        events=[ev(30, 0, 25), ev(300, 1, 30), ev(360, 1, 33)],
        direction=0, dep_dt=BASE, end_stop="康恩路康浦路", stop_count=64,
        terminal_eta_confirmed=False,
    )
    assert result.service_type == "疑似区间车"
    assert result.evidence == "reverse_physical_progress"
