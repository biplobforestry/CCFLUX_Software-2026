"""A selected interval inside a recording gap must not be offered as available.

On Flight_CCT0803 the SIF files ran 07:19 - 11:05 and again 13:21 - 16:54. The
dashboard reported the envelope, so 12:00 - 12:05 showed as 100% available;
processing then refused it with "No sif source file covers the selected Time
Filter". Coverage is now kept per source file.
"""
import unittest
from datetime import datetime, timedelta, timezone

from core.dashboard_time import (
    SAMPLED_COVERAGE_INSTRUMENTS,
    DashboardTimeState,
    recorded_coverage_segments,
)
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


class SampledCoverageIsNotAGapTests(unittest.TestCase):
    """A camera's coverage is sampled, so it must not be read as complete.

    MicaSense timestamps come from a bounded sample of the 2,371 archives and a
    FLIR export is read from its two edges. Treating the stretches missing from
    that sample as gaps in the recording removed MicaSense from the selectable
    jobs for 12:00 - 12:05, when it had in fact been flying and capturing.
    """

    def test_a_camera_records_no_coverage_segments(self):
        for instrument_id in ("micasense", "flir", "gopro"):
            with self.subTest(instrument_id):
                self.assertEqual(
                    recorded_coverage_segments(instrument_id, SIF_SEGMENTS), []
                )

    def test_an_instrument_read_in_full_keeps_its_segments(self):
        for instrument_id in ("sif", "noseboom", "picarro", "opc_hbx4"):
            with self.subTest(instrument_id):
                self.assertEqual(
                    recorded_coverage_segments(instrument_id, SIF_SEGMENTS),
                    list(SIF_SEGMENTS),
                )

    def test_the_sampled_set_matches_what_the_scanner_samples(self):
        from core.scanner import BOUNDED_SAMPLE_INSTRUMENTS

        self.assertTrue(BOUNDED_SAMPLE_INSTRUMENTS <= SAMPLED_COVERAGE_INSTRUMENTS)

    def test_micasense_stays_selectable_for_an_interval_it_flew(self):
        # The scan samples 37 of 2,371 archives, none of them inside the
        # window; the envelope must still speak for the camera.
        state = DashboardTimeState.from_instrument_ranges(
            {"micasense": (_utc(11, 21, 32), _utc(15, 51, 35), ())},
            coverage_segments={
                "micasense": recorded_coverage_segments(
                    "micasense", ((_utc(11, 21, 32), _utc(11, 21, 32)),
                                  (_utc(15, 51, 35), _utc(15, 51, 35)))
                )
            },
        )
        state.selected_analysis_start = _utc(12, 0)
        state.selected_analysis_end = _utc(12, 5)
        state._refresh_availability()
        micasense = state.instruments["micasense"]
        self.assertFalse(micasense.outside_selected_range)
        self.assertEqual(micasense.availability_percentage, 100.0)


class UnsetCameraClockWarningTests(unittest.TestCase):
    """A few boot-time frames must not be reported as the whole delivery.

    Flight_CCT0803 had 8 MicaSense images stamped before 2000 while the rest ran
    from 11:21 to 15:51, yet the warning said the images could not be placed on
    the flight timeline at all - contradicting the coverage shown beside it.
    """

    def test_some_images_placed_says_which_ones_were_not(self):
        from core.time_extraction import _unset_clock_warning

        message = _unset_clock_warning(8, True)
        self.assertIn("8 image(s)", message)
        self.assertIn("remaining images are placed", message)
        self.assertNotIn("cannot be placed", message)

    def test_no_image_placed_keeps_the_blanket_warning(self):
        from core.time_extraction import _unset_clock_warning

        message = _unset_clock_warning(120, False)
        self.assertIn("cannot be placed on the", message)
        self.assertIn("without time filtering", message)


if __name__ == "__main__":
    unittest.main()
