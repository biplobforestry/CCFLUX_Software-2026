"""A saved project carries GoPro capture identity, never the pictures.

The images live on the campaign hard disk. A project opened elsewhere must
still draw the map and, when the disk is reattached, find the pictures again by
identity rather than by a path from the machine that produced the project.
"""

import json
from pathlib import Path

import pytest

from app.scan_backend import (
    GOPRO_FOLDER_NAME,
    GOPRO_NO_DISK_MESSAGE,
    DashboardScanBackend,
    _gopro_project_payload,
)

CAPTURE = {
    "capture_id": "1",
    "image_id": "GPAA7798",
    "file_name": "GPAA7798.JPG",
    "source_file": "GoPro/DCIM/169GOPRO/GPAA7798.JPG",
    "capture_time_utc": "2026-07-27T07:30:01Z",
    "capture_time_camera": "2026-07-27T09:30:01+02:00",
    "noseboom_time_utc": "2026-07-27T07:30:01Z",
    "time_delta_seconds": 0.0,
    "latitude": 47.68367,
    "longitude": 9.35266,
    "altitude_m": 760.4,
}
PAYLOAD = {
    "available": True,
    "image_count": 1,
    "matched_count": 1,
    "unmatched_count": 0,
    "camera_timezone": "Europe/Berlin (CET/CEST)",
    "inventory": [{"source_file": "GoPro/DCIM/169GOPRO/GPAA7798.JPG", "kind": "image"}],
    "captures": [dict(CAPTURE)],
}


def _media(root: Path, *names: str) -> Path:
    """Create a GoPro media tree holding the named images."""
    folder = root / GOPRO_FOLDER_NAME / "DCIM" / "169GOPRO"
    folder.mkdir(parents=True, exist_ok=True)
    for name in names:
        (folder / name).write_bytes(b"\xff\xd8\xff\xe0not-a-real-jpeg")
    return root


def test_project_payload_keeps_identity_and_drops_paths_and_inventory():
    saved = _gopro_project_payload(PAYLOAD)

    assert saved["images_stored_in_project"] is False
    assert "inventory" not in saved
    capture = saved["captures"][0]
    # Identity and geometry are what the map and a later download need.
    assert capture["image_id"] == "GPAA7798"
    assert capture["file_name"] == "GPAA7798.JPG"
    assert capture["latitude"] == CAPTURE["latitude"]
    # The on-disk location belongs to the machine that processed it.
    assert "source_file" not in capture
    assert len(json.dumps(saved)) < len(json.dumps(PAYLOAD))


def test_inventory_is_kept_by_name_only_while_georeferencing_is_pending():
    pending = {**PAYLOAD, "available": False}

    saved = _gopro_project_payload(pending)

    # gopro_view() retries the match from the inventory, so it must survive,
    # but without a path from another machine.
    assert saved["inventory"][0]["source_file"] == "GPAA7798.JPG"


@pytest.fixture()
def backend(tmp_path):
    made = DashboardScanBackend(tmp_path)
    made._instruments["gopro"].quicklook = {
        "available": True,
        "captures": [{k: v for k, v in CAPTURE.items() if k != "source_file"}],
    }
    return made


def test_image_is_unavailable_while_the_disk_is_missing(backend):
    with pytest.raises(ValueError, match="camera media is not reachable"):
        backend.gopro_image_file("1")

    status = backend.gopro_media_status()
    assert status["media_available"] is False
    assert "hard disk" in status["prompt"]
    assert status["folder_requirement"].count(GOPRO_FOLDER_NAME) >= 1


def test_answering_no_returns_the_contact_message(backend):
    result = backend.reconnect_gopro_media({"has_hard_disk": False})

    assert result["reconnected"] is False
    assert result["message"] == GOPRO_NO_DISK_MESSAGE
    for name in ("Eva Pfannerstill", "Georgios I. Gkatzelis", "Biplob Dey"):
        assert name in result["message"]


def test_an_unanswered_prompt_is_rejected(backend):
    with pytest.raises(ValueError, match="whether the GoPro hard disk"):
        backend.reconnect_gopro_media({})


def test_a_folder_without_gopro_media_is_rejected(backend, tmp_path):
    other = tmp_path / "MicaSense_Zeppelin"
    other.mkdir()

    with pytest.raises(ValueError, match=GOPRO_FOLDER_NAME):
        backend.reconnect_gopro_media(
            {"has_hard_disk": True, "directory": str(other)}
        )


def test_reconnecting_relinks_captures_and_restores_the_image(backend, tmp_path):
    disk = _media(tmp_path / "reattached", "GPAA7798.JPG", "GPAA7799.JPG")

    # Pointing at the parent is accepted; the GoPro folder is found inside it.
    result = backend.reconnect_gopro_media(
        {"has_hard_disk": True, "directory": str(disk)}
    )

    assert result["reconnected"] is True
    assert result["matched_captures"] == 1
    assert result["missing_captures"] == 0
    assert Path(result["media_root"]).name == GOPRO_FOLDER_NAME

    resolved = backend.gopro_image_file("1")
    assert resolved.name == "GPAA7798.JPG"
    assert resolved.is_file()
    assert backend.gopro_media_status()["media_available"] is True


def test_pointing_directly_at_the_gopro_folder_also_works(backend, tmp_path):
    disk = _media(tmp_path / "reattached", "GPAA7798.JPG")

    result = backend.reconnect_gopro_media(
        {"has_hard_disk": True, "directory": str(disk / GOPRO_FOLDER_NAME)}
    )

    assert result["reconnected"] is True
    assert backend.gopro_image_file("1").name == "GPAA7798.JPG"


def test_a_disk_without_the_saved_captures_is_refused(backend, tmp_path):
    disk = _media(tmp_path / "wrong_flight", "GPAA0001.JPG")

    with pytest.raises(ValueError, match="No saved GoPro capture was found"):
        backend.reconnect_gopro_media(
            {"has_hard_disk": True, "directory": str(disk)}
        )


class TestTheDiskIsNotAskedForWhenItIsAlreadyMounted:
    """A separate camera folder is chosen only when the camera sits apart.

    On Flight_CCT0803 the GoPro folder is inside the flight folder, so no camera
    folder was ever selected and camera_folder_path is None. Looking only there
    declared the media unreachable and asked the operator whether they had the
    disk the software was reading the flight from - and every image 404ed.
    """

    def _project(self, backend, flight_root: Path, tmp_path: Path):
        from core.flight_project import FlightProject

        project = FlightProject(
            flight_id="Flight_CCT0803",
            flight_folder_path=flight_root,
            output_folder_path=tmp_path / "out",
        )
        backend._flight_project = project
        return project

    def test_the_folder_the_scan_recorded_is_used(self, backend, tmp_path):
        flight = _media(tmp_path / "Flight_CCT0803", "GPAA7798.JPG")
        project = self._project(backend, flight, tmp_path)
        state = project.detected_instruments.setdefault(
            "gopro", _detected("gopro")
        )
        state.selected_source_folders = [flight / GOPRO_FOLDER_NAME / "DCIM"]

        assert backend.gopro_media_root() == flight / GOPRO_FOLDER_NAME / "DCIM"
        status = backend.gopro_media_status()
        assert status["media_available"] is True
        assert status["prompt"] is None
        assert backend.gopro_image_file("1").name == "GPAA7798.JPG"

    def test_a_recorded_file_locates_its_own_folder(self, backend, tmp_path):
        flight = _media(tmp_path / "Flight_CCT0803", "GPAA7798.JPG")
        project = self._project(backend, flight, tmp_path)
        state = project.detected_instruments.setdefault(
            "gopro", _detected("gopro")
        )
        state.selected_source_files = [
            flight / GOPRO_FOLDER_NAME / "DCIM" / "169GOPRO" / "GPAA7798.JPG"
        ]

        assert backend.gopro_image_file("1").name == "GPAA7798.JPG"

    def test_the_flight_folder_gopro_folder_is_the_last_resort(
        self, backend, tmp_path
    ):
        flight = _media(tmp_path / "Flight_CCT0803", "GPAA7798.JPG")
        self._project(backend, flight, tmp_path)

        assert backend.gopro_media_root() == flight / GOPRO_FOLDER_NAME
        assert backend.gopro_image_file("1").name == "GPAA7798.JPG"

    def test_a_flight_without_camera_media_still_asks(self, backend, tmp_path):
        flight = tmp_path / "Flight_no_camera"
        (flight / "Noseboom").mkdir(parents=True)
        self._project(backend, flight, tmp_path)

        assert backend.gopro_media_root() is None
        assert "hard disk" in backend.gopro_media_status()["prompt"]


def _detected(instrument_id: str):
    from core.flight_project import InstrumentProjectState

    return InstrumentProjectState(instrument_id=instrument_id)


def test_opening_a_second_image_rebuilds_the_loading_box():
    """The reconnect offer replaces the loading box and its children.

    imageTitle, imageProgress and imagePercent all live inside it, so the next
    open touched a removed element before the modal was shown: clicking "more"
    on any other capture did nothing at all for the rest of the session.
    """
    script = (
        Path(__file__).resolve().parents[1] / "app" / "assets" / "gopro.js"
    ).read_text(encoding="utf-8")

    assert "function resetLoadingBox()" in script
    body = script.split("async function loadImage(captureId)", 1)[1]
    rebuild = body.index("resetLoadingBox()")
    assert rebuild < body.index("byId('imageProgress')")
    assert rebuild < body.index("modal.classList.add('show')")
