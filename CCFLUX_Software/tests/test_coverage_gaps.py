"""A selected interval inside a recording gap must not be offered as available.

On Flight_CCT0803 the SIF files ran 07:19 - 11:05 and again 13:21 - 16:54. The
dashboard reported the envelope, so 12:00 - 12:05 showed as 100% available;
processing then refused it with "No sif source file covers the selected Time
Filter". Coverage is now kept per source file.
"""
import unittest
from datetime import datetime, timedelta, timezone

from core.dashboard_time import DashboardTimeState
from core.time_extraction import merge_coverage_segments


def _utc(hour: int, minute: int = 0, second: int = 0) -> datetime:
    return datetime(2026, 8, 3, hour, minute, second, tzinfo=timezone.utc)


SIF_SEGMENTS = (
    (_utc(7, 19, 41), _utc(9, 2, 54)),
    (_utc(10, 1, 27), _utc(10, 39, 57)),
    (_utc(11, 3, 22), _utc(11, 5, 25)),
    (_utc(13, 21, 14), _utc(15, 54, 57)),
    (_utc(16, 54, 10), _utc(16, 54, 10)),
)


def _state(segments=SIF_SEGMENTS, instrument_id="sif"):
    state = DashboardTimeState.from_instrument_ranges(
        {instrument_id: (_utc(7, 19, 41), _utc(16, 54, 10), ())},
        coverage_segments={instrument_id: segments},
    )
    return state


class CoverageGapTests(unittest.TestCase):
    def test_selection_inside_a_gap_is_not_available(self):
        state = _state()
        state.selected_analysis_start = _utc(12, 0)
        state.selected_analysis_end = _utc(12, 5)
        state._refresh_availability()
        sif = state.instruments["sif"]
        self.assertEqual(sif.availability_percentage, 0.0)
        self.assertTrue(sif.outside_selected_range)

    def test_selection_inside_a_recorded_stretch_is_fully_available(self):
        state = _state()
        state.selected_analysis_start = _utc(13, 30)
        state.selected_analysis_end = _utc(13, 35)
        state._refresh_availability()
        sif = state.instruments["sif"]
        self.assertEqual(sif.availability_percentage, 100.0)
        self.assertFalse(sif.outside_selected_range)

    def test_a_selection_half_in_a_gap_reports_the_recorded_half(self):
        state = _state()
        state.selected_analysis_start = _utc(11, 4, 25)
        state.selected_analysis_end = _utc(11, 6, 25)
        state._refresh_availability()
        self.assertEqual(state.instruments["sif"].availability_percentage, 50.0)

    def test_an_instrument_without_recorded_coverage_keeps_the_envelope(self):
        state = DashboardTimeState.from_instrument_ranges(
            {"sif": (_utc(7, 19, 41), _utc(16, 54, 10), ())}
        )
        state.selected_analysis_start = _utc(12, 0)
        state.selected_analysis_end = _utc(12, 5)
        state._refresh_availability()
        self.assertEqual(state.instruments["sif"].availability_percentage, 100.0)

    def test_a_camera_capturing_instants_stays_available(self):
        # MicaSense records one frame at a time, so its coverage has no
        # duration; measuring it as seconds would report nothing at all.
        frames = tuple(
            (_utc(12, 0) + timedelta(seconds=step), _utc(12, 0) + timedelta(seconds=step))
            for step in range(0, 300, 10)
        )
        state = DashboardTimeState.from_instrument_ranges(
            {"micasense": (_utc(11, 21), _utc(15, 51), ())},
            coverage_segments={"micasense": frames},
        )
        state.selected_analysis_start = _utc(12, 0)
        state.selected_analysis_end = _utc(12, 5)
        state._refresh_availability()
        micasense = state.instruments["micasense"]
        self.assertFalse(micasense.outside_selected_range)
        self.assertEqual(micasense.availability_percentage, 100.0)

    def test_a_camera_with_no_frame_in_the_window_is_unavailable(self):
        state = DashboardTimeState.from_instrument_ranges(
            {"micasense": (_utc(11, 21), _utc(15, 51), ())},
            coverage_segments={"micasense": ((_utc(11, 21), _utc(11, 21)),
                                             (_utc(15, 51), _utc(15, 51)))},
        )
        state.selected_analysis_start = _utc(12, 0)
        state.selected_analysis_end = _utc(12, 5)
        state._refresh_availability()
        self.assertTrue(state.instruments["micasense"].outside_selected_range)


class MergeCoverageSegmentsTests(unittest.TestCase):
    def test_overlapping_and_touching_segments_become_one(self):
        merged = merge_coverage_segments(
            [(_utc(8), _utc(9)), (_utc(9), _utc(10)), (_utc(8, 30), _utc(8, 45))]
        )
        self.assertEqual(merged, ((_utc(8), _utc(10)),))

    def test_gaps_survive(self):
        merged = merge_coverage_segments([(_utc(8), _utc(9)), (_utc(11), _utc(12))])
        self.assertEqual(merged, ((_utc(8), _utc(9)), (_utc(11), _utc(12))))

    def test_the_cap_closes_the_narrowest_gaps_first(self):
        merged = merge_coverage_segments(
            [(_utc(8), _utc(9)), (_utc(9, 0, 1), _utc(10)), (_utc(14), _utc(15))],
            limit=2,
        )
        self.assertEqual(merged, ((_utc(8), _utc(10)), (_utc(14), _utc(15))))

    def test_no_segments_is_no_coverage(self):
        self.assertEqual(merge_coverage_segments([]), ())


if __name__ == "__main__":
    unittest.main()
