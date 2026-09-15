from src.temporal_feasibility import DirectionBound, impossible_partial_transition, progress_fraction


def bound(direction, minutes=130.0):
    return DirectionBound("浦东35路", str(direction), 20, minutes, minutes + 15)


def test_progress_fraction_uses_physical_sequence():
    assert round(progress_fraction(28, 64), 3) == 0.429
    assert round(progress_fraction(62, 65), 3) == 0.953


def test_11062_style_partial_transition_can_be_impossible():
    # Representative geometry only: outbound last physical seq ~28/64 and later
    # vehicle physically near the reverse terminal (~62/65). The production
    # regression must use the actual raw observation timestamp/sequence.
    impossible, note = impossible_partial_transition(
        elapsed_min=80.0,
        outbound=bound(0, 130.0), reverse=bound(1, 130.0),
        outbound_last_seq=28, outbound_stop_count=64,
        reverse_observed_seq=62, reverse_stop_count=65,
    )
    assert impossible
    assert "物理进度" in note


def test_normal_transition_not_flagged_when_time_is_sufficient():
    impossible, _ = impossible_partial_transition(
        elapsed_min=220.0,
        outbound=bound(0), reverse=bound(1),
        outbound_last_seq=28, outbound_stop_count=64,
        reverse_observed_seq=62, reverse_stop_count=65,
    )
    assert not impossible


def test_missing_physical_position_never_flags():
    impossible, _ = impossible_partial_transition(
        elapsed_min=30.0,
        outbound=bound(0), reverse=bound(1),
        outbound_last_seq=0, outbound_stop_count=64,
        reverse_observed_seq=62, reverse_stop_count=65,
    )
    assert not impossible
