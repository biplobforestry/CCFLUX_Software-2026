"""Remote sensing is selected on its own terms, after its own scan.

Three things this pins.

The detected global minimum and maximum are the envelope of the flight
instruments — the earliest start any of them saw and the latest end. It used to
be taken from the Noseboom alone whenever an anchor was named, so Flight_2707
reported 05:21–10:20 while MIRO actually covered 26 Jul 00:00 – 27 Jul 17:03.
The cameras take no part in it: they are scanned separately and cover a
different span.

Because of that the cameras need their own coverage to be selected against,
which is what camera_coverage() provides and what the Remote Sensing dialog
reads. Routing them through the flight Time Filter meant a camera-only project
had no interval to offer at all.

And nothing can be selected before the camera scan has finished, because until
then there is no coverage to select against.
"""

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from app.scan_backend import DashboardScanBackend
from core.dashboard_time import CAMERA_INSTRUMENTS, DashboardTimeState
from core.enums import DetectionStatus
from core.scanner import _top_level_rank


def _at(hour, minute=0, day=27):
    return datetime(2026, 7, day, hour, minute, tzinfo=timezone.utc)


# --------------------------------------------------------------------------
# The detected global minimum and maximum
# --------------------------------------------------------------------------
def test_the_global_range_is_the_envelope_of_the_flight_instruments():
    state = DashboardTimeState.from_instrument_ranges(
        {
            "noseboom": (_at(5), _at(10), ()),
            "miro": (_at(0, 0, day=26), _at(17), ()),     # widest by far
            "picarro": (_at(3), _at(15), ()),
        },
        analysis_anchor_id="noseboom",
    )

    # Trimmed by the existing 2-minute warm-up and 1-minute shutdown rule.
    assert state.detected_global_start == _at(0, 2, day=26)
    assert state.detected_global_end == _at(16, 59)


def test_the_anchor_no_longer_dictates_the_global_range():
    """Naming an anchor must not report the anchor's own window as global."""
    ranges = {"noseboom": (_at(5), _at(10), ()), "miro": (_at(0, 0, day=26), _at(17), ())}

    anchored = DashboardTimeState.from_instrument_ranges(
        ranges, analysis_anchor_id="noseboom"
    )
    unanchored = DashboardTimeState.from_instrument_ranges(ranges)

    assert anchored.detected_global_start == unanchored.detected_global_start
    assert anchored.detected_global_end == unanchored.detected_global_end


def test_cameras_are_left_out_of_the_global_range():
    state = DashboardTimeState.from_instrument_ranges(
        {
            "noseboom": (_at(6), _at(9), ()),
            # Both reach past the flight instruments on either side.
            "flir": (_at(1), _at(23), ()),
            "gopro": (_at(2), _at(22), ()),
        },
        analysis_anchor_id="noseboom",
    )

    assert state.detected_global_start == _at(6, 2)
    assert state.detected_global_end == _at(8, 59)


def test_the_anchor_still_decides_the_common_overlap():
    """An instrument that never meets the navigation reference cannot be
    analysed against it, so it takes no part in the overlap."""
    state = DashboardTimeState.from_instrument_ranges(
        {
            "noseboom": (_at(6), _at(9), ()),
            "miro": (_at(6), _at(9), ()),
            "picarro": (_at(20), _at(23), ()),   # disjoint from the anchor
        },
        analysis_anchor_id="noseboom",
    )

    assert state.common_overlap_start is not None
    assert state.common_overlap_start < state.common_overlap_end
    # The disjoint instrument still widens the global range.
    assert state.detected_global_end > _at(20)


def test_the_camera_set_is_what_the_dashboard_calls_cameras():
    assert CAMERA_INSTRUMENTS == {"flir", "gopro", "micasense"}


# --------------------------------------------------------------------------
# Camera coverage and the selection flow
# --------------------------------------------------------------------------
def _scanned(tmp_path, ranges, phase="complete"):
    backend = DashboardScanBackend(tmp_path)
    backend._scan_channels["camera"]["phase"] = phase
    for instrument_id, value in ranges.items():
        state = backend._instruments[instrument_id]
        state.detection_status = DetectionStatus.READY
        state.file_count = 10
        state.utc_start_time, state.utc_end_time = value
    return backend


def test_coverage_is_not_ready_until_the_scan_finishes(tmp_path):
    backend = _scanned(tmp_path, {"gopro": (_at(6), _at(9))}, phase="scanning_folder")

    assert backend.camera_coverage()["ready"] is False
    with pytest.raises(ValueError, match="must finish"):
        backend.preview_remote_sensing({"time_mode": "global"})


def test_coverage_reports_each_product_in_scan_order(tmp_path):
    backend = _scanned(
        tmp_path, {"gopro": (_at(6), _at(9)), "flir": (_at(5), _at(11))}
    )

    coverage = backend.camera_coverage()

    assert [item["instrument_id"] for item in coverage["products"]] == [
        "gopro", "flir", "micasense"
    ]
    assert coverage["ready"] is True


def test_the_camera_global_and_overlap_are_computed_from_the_cameras(tmp_path):
    backend = _scanned(
        tmp_path, {"gopro": (_at(6), _at(9)), "flir": (_at(5), _at(11))}
    )

    coverage = backend.camera_coverage()

    assert coverage["detected_global_start"] == _at(5).isoformat()
    assert coverage["detected_global_end"] == _at(11).isoformat()
    assert coverage["common_overlap_start"] == _at(6).isoformat()
    assert coverage["common_overlap_end"] == _at(9).isoformat()


def test_a_product_without_a_clock_is_not_selectable(tmp_path):
    backend = _scanned(tmp_path, {"gopro": (_at(6), _at(9))})
    backend._instruments["micasense"].detection_status = DetectionStatus.WARNING

    coverage = backend.camera_coverage()
    micasense = next(
        item for item in coverage["products"] if item["instrument_id"] == "micasense"
    )

    assert micasense["detected"] is True
    assert micasense["selectable"] is False
    with pytest.raises(ValueError, match="usable UTC coverage"):
        backend.preview_remote_sensing({"instruments": ["micasense"]})


@pytest.mark.parametrize("mode, expected_start, expected_end", [
    ("global", 5, 11),
    ("overlap", 6, 9),
])
def test_each_period_option_resolves(tmp_path, mode, expected_start, expected_end):
    backend = _scanned(
        tmp_path, {"gopro": (_at(6), _at(9)), "flir": (_at(5), _at(11))}
    )

    preview = backend.preview_remote_sensing(
        {"instruments": ["gopro", "flir"], "time_mode": mode}
    )

    assert preview["start"] == _at(expected_start).isoformat()
    assert preview["end"] == _at(expected_end).isoformat()
    assert preview["ready_to_start"] is True


def test_a_custom_period_is_honoured(tmp_path):
    backend = _scanned(tmp_path, {"gopro": (_at(6), _at(9))})

    preview = backend.preview_remote_sensing({
        "instruments": ["gopro"], "time_mode": "custom",
        "start": "2026-07-27T07:00:00Z", "end": "2026-07-27T08:00:00Z",
    })

    assert preview["start"] == _at(7).isoformat()
    assert preview["duration_seconds"] == 3600


def test_a_period_with_no_data_is_refused(tmp_path):
    backend = _scanned(tmp_path, {"gopro": (_at(6), _at(9))})

    with pytest.raises(ValueError):
        backend.preview_remote_sensing({
            "instruments": ["gopro"], "time_mode": "custom",
            "start": "2020-01-01T00:00:00Z", "end": "2020-01-02T00:00:00Z",
        })


def test_the_preview_says_which_products_the_period_actually_covers(tmp_path):
    backend = _scanned(
        tmp_path,
        {"gopro": (_at(6), _at(9)), "flir": (_at(20), _at(22))},
    )

    preview = backend.preview_remote_sensing({
        "instruments": ["gopro", "flir"], "time_mode": "custom",
        "start": "2026-07-27T06:30:00Z", "end": "2026-07-27T07:30:00Z",
    })

    covered = {item["instrument_id"]: item["covers_interval"] for item in preview["products"]}
    assert covered == {"gopro": True, "flir": False}
    assert preview["ready_to_start"] is True     # GoPro alone is still work


def test_the_preview_starts_nothing(tmp_path):
    backend = _scanned(tmp_path, {"gopro": (_at(6), _at(9))})

    backend.preview_remote_sensing({"instruments": ["gopro"], "time_mode": "global"})

    assert backend.camera_coverage()["selected_start"] is None, (
        "verifying a request must not commit it"
    )


def test_an_unknown_period_option_is_refused(tmp_path):
    backend = _scanned(tmp_path, {"gopro": (_at(6), _at(9))})

    with pytest.raises(ValueError, match="time mode"):
        backend._select_remote_sensing_interval("whenever", {})


# --------------------------------------------------------------------------
# Camera scan order
# --------------------------------------------------------------------------
@pytest.mark.parametrize("name, rank", [
    ("GoPro", 0),
    ("camera.FLIR_Zeppelin.json", 1),
    ("MicaSense_Zeppelin", 2),
    ("Something_else", 3),
])
def test_the_camera_folders_rank_in_the_requested_order(name, rank):
    assert _top_level_rank(name, ("gopro", "flir", "micasense")) == rank


def test_the_backend_asks_for_that_order():
    source = (Path(__file__).parents[1] / "app" / "scan_backend.py").read_text(
        encoding="utf-8"
    )
    assert DashboardScanBackend.CAMERA_SCAN_ORDER == ("gopro", "flir", "micasense")
    assert "top_level_order=(" in source
    assert 'self.CAMERA_SCAN_ORDER if source == "camera" else ()' in source


def test_the_flight_scan_keeps_filesystem_order():
    """Only the camera folder is reordered; the flight folder is untouched."""
    source = (Path(__file__).parents[1] / "app" / "scan_backend.py").read_text(
        encoding="utf-8"
    )
    assert 'if source == "camera" else ()' in source


# --------------------------------------------------------------------------
# The dialog
# --------------------------------------------------------------------------
def test_the_button_is_unavailable_until_the_scan_finishes():
    script = (Path(__file__).parents[1] / "app" / "assets" / "dashboard.js").read_text(
        encoding="utf-8"
    )

    assert "&& cameraReady;" in script, "the button must require a finished scan"
    assert "announceCameraCoverage" in script
    assert "cameraCoverageAnnounced" in script


def test_the_dialog_offers_every_period_option():
    script = (Path(__file__).parents[1] / "app" / "assets" / "dashboard.js").read_text(
        encoding="utf-8"
    )

    for label in ("Detected global minimum and maximum",
                  "Common overlapping timeframe", "Custom period"):
        assert label in script
    assert "Verifying your request" in script
    assert "Start processing" in script


def test_the_level_wording_is_gone_from_the_interface():
    assets = Path(__file__).parents[1] / "app" / "assets"
    for name in ("dashboard.js", "dashboard.html"):
        text = (assets / name).read_text(encoding="utf-8")
        for phrase in ("Level 1", "Level 2 only", "Configure Level 2",
                       "Start selected Level 2 job"):
            assert phrase not in text, f"{name} still says {phrase!r}"
