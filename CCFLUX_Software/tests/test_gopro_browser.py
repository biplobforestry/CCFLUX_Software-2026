from pathlib import Path

from PIL import Image

from app.scan_backend import DashboardScanBackend
from core.enums import ProcessingStatus
from core.flight_project import FlightProject


def test_gopro_browser_hides_paths_and_resolves_only_camera_images(tmp_path: Path):
    application_root = Path(__file__).resolve().parents[1]
    camera = tmp_path / "camera"
    source = camera / "GoPro" / "GOPR0001.jpg"
    source.parent.mkdir(parents=True)
    Image.new("RGB", (16, 12), (30, 90, 150)).save(source)
    backend = DashboardScanBackend(application_root)
    backend._flight_project = FlightProject(
        flight_id="F01",
        flight_folder_path=tmp_path / "flight",
        output_folder_path=tmp_path / "output",
        camera_folder_path=camera,
    )
    backend._instruments["gopro"].quicklook = {
        "available": True,
        "inventory": [{"source_file": "GoPro/GOPR0001.jpg"}],
        "captures": [{
            "capture_id": "1",
            "image_id": "GOPR0001",
            "source_file": "GoPro/GOPR0001.jpg",
            "latitude": 50.9,
            "longitude": 6.4,
            "altitude_m": 411.2,
            "capture_time_utc": "2026-07-26T10:00:00Z",
        }],
    }

    payload = backend.gopro_view()

    assert payload["ready"] is True
    assert "inventory" not in payload["data"]
    assert "source_file" not in payload["data"]["captures"][0]
    assert backend.gopro_image_file("1") == source.resolve()


def test_gopro_view_repairs_stale_zero_match_warning_after_noseboom_finishes(
    tmp_path: Path,
):
    application_root = Path(__file__).resolve().parents[1]
    camera = tmp_path / "camera"
    source = camera / "GoPro" / "GOPR0001.jpg"
    source.parent.mkdir(parents=True)
    Image.new("RGB", (16, 12), (30, 90, 150)).save(source)
    backend = DashboardScanBackend(application_root)
    backend._flight_project = FlightProject(
        flight_id="F01",
        flight_folder_path=tmp_path / "flight",
        output_folder_path=tmp_path / "output",
        camera_folder_path=camera,
    )
    warning = (
        "No GoPro image timestamp could be matched to processed "
        "Noseboom navigation within 2.5 seconds."
    )
    gopro_state = backend._instruments["gopro"]
    gopro_state.processing_status = "warning"
    gopro_state.processing_progress = 100.0
    gopro_state.processing_step = warning
    gopro_state.warnings = [warning]
    gopro_state.quicklook = {
        "available": False,
        "reason": warning,
        "image_count": 1,
        "matched_count": 0,
        "unmatched_count": 1,
        "inventory": [{
            "kind": "image",
            "source_file": "GoPro/GOPR0001.jpg",
            "timestamp": "2026-07-26T10:00:00Z",
        }],
        "captures": [],
    }
    backend._instruments["noseboom"].quicklook = {
        "available": True,
        "points": [{
            "time": "2026-07-26T10:00:01Z",
            "lat": 50.9,
            "lon": 6.4,
            "altitude_m": 411.2,
        }],
    }
    job = backend.processing_queue.get("gopro_quick")
    job.status = ProcessingStatus.WARNING
    job.progress = 100.0
    job.current_step = warning

    payload = backend.gopro_view()

    assert payload["ready"] is True
    assert payload["data"]["matched_count"] == 1
    assert payload["data"]["unmatched_count"] == 0
    assert payload["data"]["reason"] is None
    assert gopro_state.processing_status == "complete"
    assert gopro_state.processing_step == "GoPro map ready with 1 capture locations"
    assert warning not in gopro_state.warnings
    assert job.status is ProcessingStatus.COMPLETE
