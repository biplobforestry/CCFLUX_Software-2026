from datetime import datetime, timezone

import pytest

from core.dashboard_time import DashboardTimeState, parse_dashboard_datetime


UTC = timezone.utc


def _time(hour: int, minute: int = 0) -> datetime:
    return datetime(2026, 7, 26, hour, minute, tzinfo=UTC)


def _state() -> DashboardTimeState:
    return DashboardTimeState.from_instrument_ranges(
        {
            "miro": (_time(10), _time(11), ()),
            "picarro": (_time(10, 30), _time(12), ()),
            "flir": (None, None, ("Valid timestamps have no confirmed timezone.",)),
        }
    )


def test_detected_global_and_common_intervals():
    state = _state()
    assert state.detected_global_start == _time(10, 2)
    assert state.detected_global_end == _time(11, 59)
    assert state.common_overlap_start == _time(10, 32)
    assert state.common_overlap_end == _time(10, 59)
    assert state.selected_analysis_start == _time(10, 2)
    assert state.selected_analysis_end == _time(11, 59)


def test_use_full_common_and_reset_intervals():
    state = _state()
    state.use_common_overlap()
    assert (state.selected_analysis_start, state.selected_analysis_end) == (
        _time(10, 32),
        _time(10, 59),
    )
    state.set_instrument_override("miro", _time(10, 40), _time(10, 50))
    state.reset_to_detected_limits()
    assert state.selected_analysis_start == _time(10, 2)
    assert state.selected_analysis_end == _time(11, 59)
    assert state.instruments["miro"].override_start is None


def test_start_must_be_earlier_than_end():
    state = _state()
    with pytest.raises(ValueError, match="earlier"):
        state.set_selected_interval(_time(11), _time(11))
    with pytest.raises(ValueError, match="earlier"):
        state.set_selected_interval(_time(12), _time(11))


def test_selected_interval_must_intersect_available_data():
    with pytest.raises(ValueError, match="does not intersect"):
        _state().set_selected_interval(_time(13), _time(14))


def test_instruments_outside_selected_range_are_marked():
    state = _state()
    state.set_selected_interval(_time(11, 15), _time(11, 45))
    assert state.instruments["miro"].outside_selected_range
    assert not state.instruments["picarro"].outside_selected_range
    assert state.instruments["miro"].availability_percentage == 0.0


def test_per_instrument_override_validation_and_clear():
    state = _state()
    state.set_selected_interval(_time(10, 15), _time(11, 30))
    state.set_instrument_override("miro", _time(10, 20), _time(10, 50))
    assert state.instruments["miro"].effective_start == _time(10, 20)
    assert state.instruments["miro"].effective_end == _time(10, 50)
    with pytest.raises(ValueError, match="available data"):
        state.set_instrument_override("miro", _time(12), _time(13))
    state.set_instrument_override("miro", None, None)
    assert state.instruments["miro"].effective_start == _time(10, 2)


def test_timezone_warnings_remain_in_serialized_state():
    state = _state()
    assert "confirmed timezone" in state.timezone_warnings[0]
    assert (
        state.to_dict()["instruments"]["flir"]["timezone_warnings"]
        == ["Valid timestamps have no confirmed timezone."]
    )


def test_dashboard_datetime_requires_explicit_timezone():
    assert parse_dashboard_datetime("2026-07-26T10:00:00Z") == _time(10)
    with pytest.raises(ValueError, match="timezone"):
        parse_dashboard_datetime("2026-07-26T10:00:00")


def test_display_timezone_defaults_to_utc():
    state = DashboardTimeState()
    assert state.display_timezone == "UTC"
    assert state.to_dict()["display_timezone"] == "UTC"


def test_input_range_objects_are_not_modified():
    original_start = _time(10)
    original_end = _time(11)
    state = DashboardTimeState.from_instrument_ranges(
        {"miro": (original_start, original_end, ())}
    )
    state.set_selected_interval(_time(10, 10), _time(10, 50))
    assert original_start == _time(10)
    assert original_end == _time(11)
    assert state.instruments["miro"].raw_start is original_start
    assert state.instruments["miro"].raw_end is original_end
    assert state.instruments["miro"].available_start == _time(10, 2)
    assert state.instruments["miro"].available_end == _time(10, 59)


def test_short_sif_session_keeps_its_complete_valid_interval():
    start = _time(10)
    end = _time(10, 1)
    state = DashboardTimeState.from_instrument_ranges(
        {"sif": (start, end, ())}
    )
    assert state.instruments["sif"].available_start == start
    assert state.instruments["sif"].available_end == end
    assert not state.instruments["sif"].timezone_warnings


def test_noseboom_anchor_excludes_wrong_flight_camera_from_global_interval():
    previous_day = datetime(2026, 7, 25, 12, tzinfo=UTC)
    state = DashboardTimeState.from_instrument_ranges(
        {
            "noseboom": (_time(10), _time(11), ()),
            "sif": (_time(10, 30), _time(10, 31), ()),
            "flir": (
                previous_day,
                previous_day.replace(hour=13),
                (),
            ),
        },
        analysis_anchor_id="noseboom",
    )

    assert state.detected_global_start == _time(10, 2)
    assert state.detected_global_end == _time(10, 59)
    assert state.instruments["sif"].availability_percentage == 1.8
    assert not state.instruments["sif"].outside_selected_range
    assert state.instruments["flir"].availability_percentage == 0.0
    assert state.instruments["flir"].outside_selected_range
