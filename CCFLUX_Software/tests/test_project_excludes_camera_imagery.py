"""A .ccflux carries results, not pictures — and a mistyped interval is named.

Two things this pins, both found by running the whole workflow over Flight_2707
and looking at what actually came out.

The project archive was bundling 12 GoPro thumbnail JPGs. The campaign rule is
that a project carries the processed results a colleague needs to see the plots
and maps, and for GoPro the image identifiers only. The GoPro payload already
does exactly that — 3,560 captures with image_id, file name, UTC time and
position, and images_stored_in_project set to false — so the thumbnails were the
one thing out of line.

FLIR sample frames stay: they are false-colour renderings of the thermal array
rather than copies of a photograph, the FLIR page shows them by URL, and
removing them left that page with twelve broken images.

Separately, the time filter reads 'start' and 'end'. An unknown key was ignored,
and because a half-empty interval is deliberately repaired to the available
range, a misspelled key silently selected the whole flight rather than failing.
"""

from pathlib import Path

import pytest

from core.flight_project import (
    CAMERA_IMAGE_SUFFIXES,
    CAMERA_INSTRUMENT_IDS,
    _is_camera_image_product,
)


@pytest.mark.parametrize("relative", [
    "processed/gopro/runs/20260801T062554Z/thumbnails/GPAA6289_thumbnail.jpg",
    "processed/micasense/runs/1/preview.tif",
    "processed/GoPro/runs/1/frame.JPG",          # case is not significant
    "processed/gopro/runs/1/clip.mp4",
])
def test_camera_imagery_is_recognised(relative):
    assert _is_camera_image_product(Path(relative))


@pytest.mark.parametrize("relative", [
    # Science plots are images too, and they must still travel.
    "processed/noseboom/runs/1/airspeed_map.png",
    "processed/opc_hbx4/runs/1/size_distribution.png",
    "processed/sif/runs/1/sif_transect.png",
    "quicklooks/partector_browser.png",
    # Camera tables and payloads are results, not pictures.
    "processed/gopro/runs/1/media_inventory.csv",
    "processed/flir/runs/1/level2/temperature.csv",
    "quicklooks/gopro_browser.json",
])
def test_everything_else_still_travels(relative):
    assert not _is_camera_image_product(Path(relative))


def test_flir_sample_frames_still_travel():
    """False-colour renderings of the thermal array, shown by the FLIR page."""
    assert not _is_camera_image_product(
        Path("processed/flir/runs/1/thumbnails/flir_frame_001.png")
    )


def test_the_rule_covers_the_capture_instruments():
    assert CAMERA_INSTRUMENT_IDS == {"gopro", "micasense"}
    for suffix in (".jpg", ".jpeg", ".png", ".tif", ".tiff", ".mp4"):
        assert suffix in CAMERA_IMAGE_SUFFIXES


def _project(tmp_path):
    from tests.test_flight_project import _project as build
    return build(tmp_path)


def test_a_saved_project_carries_no_camera_imagery(tmp_path):
    import zipfile
    from core.flight_project import FlightProjectStore

    project, _ = _project(tmp_path)
    root = project.flight_output_root
    camera = root / "processed" / "gopro" / "runs" / "1" / "thumbnails"
    camera.mkdir(parents=True)
    (camera / "GPAA6289_thumbnail.jpg").write_bytes(b"\xff\xd8\xff" + b"0" * 512)
    inventory = root / "processed" / "gopro" / "runs" / "1" / "media_inventory.csv"
    inventory.write_text("image_id,capture_time_utc\nGPAA6289,2026-07-27T05:24:17Z\n",
                         encoding="utf-8")
    plot = root / "processed" / "noseboom" / "runs" / "1"
    plot.mkdir(parents=True)
    (plot / "airspeed_map.png").write_bytes(b"\x89PNG" + b"0" * 512)
    state = next(iter(project.detected_instruments.values()))
    state.output_locations = [
        camera / "GPAA6289_thumbnail.jpg", inventory, plot / "airspeed_map.png",
    ]

    saved = FlightProjectStore().save_project(project, overwrite=True)

    with zipfile.ZipFile(saved) as archive:
        names = archive.namelist()
    assert not any("thumbnail" in name for name in names), "camera imagery was bundled"
    assert any(name.endswith("media_inventory.csv") for name in names), (
        "the GoPro identifiers must still travel"
    )
    assert any(name.endswith("airspeed_map.png") for name in names), (
        "science plots must still travel"
    )


def test_the_skipped_imagery_is_recorded_not_silently_dropped(tmp_path):
    import json
    import zipfile
    from core.flight_project import FlightProjectStore

    project, _ = _project(tmp_path)
    camera = project.flight_output_root / "processed" / "gopro" / "runs" / "1" / "thumbnails"
    camera.mkdir(parents=True)
    (camera / "GPAA6289_thumbnail.jpg").write_bytes(b"\xff\xd8\xff" + b"0" * 512)
    state = next(iter(project.detected_instruments.values()))
    state.output_locations = [camera / "GPAA6289_thumbnail.jpg"]

    saved = FlightProjectStore().save_project(project, overwrite=True)

    with zipfile.ZipFile(saved) as archive:
        manifest = json.loads(archive.read("manifest.json"))
    reasons = {item["reason"] for item in manifest["skipped_products"]}
    assert "camera imagery is not bundled" in reasons


def test_a_mistyped_time_filter_key_is_reported(tmp_path):
    from app.scan_backend import DashboardScanBackend

    backend = DashboardScanBackend(tmp_path)

    with pytest.raises(ValueError, match="start_utc"):
        backend.update_time_filter(
            {"action": "set", "start_utc": "2026-07-27T07:00:00Z"}
        )


def test_the_supported_time_filter_fields_are_what_the_gui_sends():
    from app.scan_backend import DashboardScanBackend

    script = (Path(__file__).parents[1] / "app" / "assets" / "dashboard.js").read_text(
        encoding="utf-8"
    )
    for field in ("action", "start", "end", "display_timezone"):
        assert field in DashboardScanBackend.TIME_FILTER_REQUEST_KEYS
    # The dialog posts start/end; if that ever changes the allow-list must too.
    assert "start: inputToIso(" in script and "end: inputToIso(" in script
