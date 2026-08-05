"""A delivery that keeps the camera data inside the flight folder.

Flight_CCT0803 arrived as one folder: the FLIR export at its root and the
MicaSense archives beneath it. Selecting that folder for both refused with
"Camera Folder and Flight Folder must be independent", which described a
layout rule the campaign does not follow.
"""
import inspect

import pytest

from app.scan_backend import DashboardScanBackend

SELECT = inspect.getsource(DashboardScanBackend.select_camera_folder)
SCAN = inspect.getsource(DashboardScanBackend.start_scan)


def test_an_overlapping_camera_folder_is_no_longer_refused():
    assert "must be independent" not in SELECT
    assert "must be independent" not in SCAN


def test_the_same_tree_is_scanned_once():
    """Reading the files twice would double every count and every warning."""
    assert "camera_inside_flight" in SCAN
    assert "None if camera_inside_flight else" in SCAN


@pytest.mark.parametrize("relationship", [
    "selected_camera_root == root",
    "selected_camera_root.is_relative_to(root)",
    "root.is_relative_to(selected_camera_root)",
])
def test_every_overlapping_arrangement_is_recognised(relationship):
    assert relationship in SCAN


def test_the_operator_is_told_one_scan_covers_both():
    assert "one scan covers both" in SCAN
    assert "one scan covers" in SELECT


def test_a_separate_camera_folder_still_scans_separately():
    """The two-folder layout is unchanged; only the overlapping case is new."""
    assert "(selected_camera_root if include_camera else None)" in SCAN


class TestRemoteSensingAvailability:
    """The panel showed only processing progress, so a camera that recorded
    nothing in the window and one that had not started both read "0%"."""

    from pathlib import Path as _Path

    SCRIPT = (_Path(__file__).resolve().parents[1] / "app" / "assets"
              / "dashboard.js").read_text(encoding="utf-8")

    def test_the_time_filter_reaches_the_camera_rows(self):
        assert "renderCameraStatus(state.processing_queue || {}, state.time_filter || {})" in self.SCRIPT
        assert "function renderCameraStatus(queue, timeFilter = {})" in self.SCRIPT

    def test_each_row_states_its_coverage(self):
        assert "% of the interval" in self.SCRIPT

    def test_a_camera_outside_the_window_says_so(self):
        assert "outside the Time Filter" in self.SCRIPT

    def test_a_camera_with_no_data_is_not_called_zero_percent(self):
        assert "'not detected'" in self.SCRIPT
