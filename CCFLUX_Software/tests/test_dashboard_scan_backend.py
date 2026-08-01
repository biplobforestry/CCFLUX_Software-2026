import json
import subprocess
import sys
import threading
import time
import types
import urllib.request
from types import SimpleNamespace
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from PIL import Image

from app.scan_backend import DashboardScanBackend, FolderDialog
from app.server import create_server, open_dashboard_in_browser
from core.dashboard_time import DashboardTimeState
from core.enums import DetectionStatus, ProcessingStatus
from core.flight_project import FlightProject, FlightProjectStore, InstrumentProjectState
from core.logging_manager import ProcessingLogManager
from core.scanner import InstrumentCandidate, ScanProgress, ScanReport


class _Dialog:
    def __init__(self, folder: Path | None):
        self.folder = folder

    def choose_flight_folder(self) -> Path | None:
        return self.folder


class _ProjectFolderDialog(_Dialog):
    def choose_project_folder(self) -> Path | None:
        return self.folder


class _CameraCancelledDialog(_Dialog):
    def choose_camera_folder(self) -> Path | None:
        return None


class _CameraSelectedDialog(_Dialog):
    def __init__(self, flight: Path, camera: Path):
        super().__init__(flight)
        self.camera = camera

    def choose_camera_folder(self) -> Path | None:
        return self.camera


def _wait(backend: DashboardScanBackend, timeout: float = 3.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        state = backend.snapshot()
        if not state["running"]:
            return state
        time.sleep(0.01)
    raise AssertionError("background scan did not finish")


def _application_root() -> Path:
    return Path(__file__).resolve().parents[1]


def _server_or_skip(backend: DashboardScanBackend):
    try:
        return create_server(
            port=0, application_root=_application_root(), backend=backend
        )
    except PermissionError:
        pytest.skip("local socket binding is blocked by the test sandbox")


def test_loaded_project_skips_completed_job_until_explicit_reprocess(
    tmp_path: Path,
):
    raw = tmp_path / "raw"
    output = tmp_path / "output"
    raw.mkdir()
    output.mkdir()
    timestamp = datetime(2026, 7, 27, 5, 24, 17, tzinfo=timezone.utc)
    project = FlightProject(
        flight_id="incremental-flight",
        flight_folder_path=raw,
        output_folder_path=output,
        detected_instruments={
            "gopro": InstrumentProjectState(
                "gopro", utc_start_time=timestamp, utc_end_time=timestamp
            ),
            "flir": InstrumentProjectState(
                "flir", utc_start_time=timestamp, utc_end_time=timestamp
            ),
        },
        completed_jobs=["gopro_quick"],
        enabled_instruments=["gopro"],
    )
    project_file = FlightProjectStore().save_project(project, overwrite=True)
    backend = DashboardScanBackend(
        _application_root(),
        logger=ProcessingLogManager(tmp_path / "application-log.jsonl"),
    )

    backend.open_project(project_file)
    queue = backend.snapshot()["processing_queue"]
    jobs = {job["job_id"]: job for job in queue["jobs"]}

    assert jobs["gopro_quick"]["status"] == "complete"
    assert jobs["gopro_quick"]["enabled"] is False
    assert jobs["gopro_quick"]["previously_completed"] is True
    assert jobs["gopro_quick"]["skip_by_default"] is True
    assert jobs["flir_quick"]["status"] == "paused"
    assert jobs["flir_quick"]["available_for_selection"] is True
    with pytest.raises(ValueError, match="Explicit confirmation"):
        backend.update_queue(
            {"action": "reprocess", "job_id": "gopro_quick"}
        )

    backend.update_queue(
        {
            "action": "reprocess",
            "job_id": "gopro_quick",
            "confirmed": True,
        }
    )

    job = backend.processing_queue.get("gopro_quick")
    assert job.status is ProcessingStatus.QUEUED
    assert job.enabled is True
    assert "gopro_quick" not in backend._flight_project.completed_jobs


def test_sif_options_are_validated_and_persisted_in_flight_project(
    tmp_path: Path,
):
    raw = tmp_path / "raw"
    output = tmp_path / "output"
    raw.mkdir()
    output.mkdir()
    backend = DashboardScanBackend(
        _application_root(),
        logger=ProcessingLogManager(tmp_path / "application-log.jsonl"),
    )
    backend._flight_project = FlightProject(
        flight_id="sif-flight",
        flight_folder_path=raw,
        output_folder_path=output,
    )

    options = backend.update_sif_options({
        "modes": ["FULL", "FLUO"],
        "position_mode": "uav_airship",
        "spectral_shift_correction": True,
        "apply_nonlinearity_correction": False,
        "drop_unmatched_telemetry": True,
        "drop_invalid_spectral_rows": True,
        "altitude_filter": True,
        "raw_min_kb": 100,
        "max_position_gap_seconds": 0.25,
    })

    assert options["spectral_shift_correction"] is True
    assert options["drop_invalid_spectral_rows"] is True
    assert options["raw_min_kb"] == 100
    assert backend._flight_project.instrument_options["sif"] == options
    assert backend.snapshot()["sif_options"] == options
    with pytest.raises(ValueError, match="at least one SIF mode"):
        backend.update_sif_options({"modes": []})
    with pytest.raises(ValueError, match="0.01–10"):
        backend.update_sif_options({"max_position_gap_seconds": 30})
    with pytest.raises(ValueError, match="minimum size"):
        backend.update_sif_options({"raw_min_kb": -1})


def test_incremental_camera_scan_retains_completed_gopro_when_flir_is_added(
    tmp_path: Path,
):
    flight = tmp_path / "flight"
    output = tmp_path / "output"
    camera = tmp_path / "new-camera-delivery"
    flight.mkdir()
    output.mkdir()
    camera.mkdir()
    flir_file = camera / "capture.json"
    flir_file.write_text(
        '{"timestamp":{"$date":"2026-07-27T05:24:17Z"}}',
        encoding="utf-8",
    )
    completed_at = datetime(2026, 7, 27, 5, 24, 17, tzinfo=timezone.utc)
    backend = DashboardScanBackend(_application_root())
    backend._flight_project = FlightProject(
        flight_id="incremental-flight",
        flight_folder_path=flight,
        output_folder_path=output,
        detected_instruments={
            "gopro": InstrumentProjectState(
                "gopro",
                selected_source_files=[tmp_path / "old-gopro.jpg"],
                utc_start_time=completed_at,
                utc_end_time=completed_at,
                output_locations=[output / "processed" / "gopro"],
            ),
        },
        completed_jobs=["gopro_quick"],
    )
    gopro_job = backend.processing_queue.get("gopro_quick")
    gopro_job.status = ProcessingStatus.COMPLETE
    gopro_job.progress = 100.0
    gopro_job.enabled = False
    gopro_job.current_step = "Previously processed — skipped by default"
    backend._instruments["gopro"].detection_status = DetectionStatus.READY
    backend._instruments["gopro"].processing_status = ProcessingStatus.COMPLETE
    backend._instruments["gopro"].quicklook = {"capture_count": 3651}
    candidate = InstrumentCandidate(
        "flir",
        camera,
        ("flir_json_schema",),
        1.0,
        1,
        (flir_file,),
        (),
        (),
        False,
        (flir_file,),
    )
    report = ScanReport(
        root=camera,
        candidates=(candidate,),
        files_scanned=1,
        folders_scanned=1,
        inaccessible_path_count=0,
        malformed_file_count=0,
        warnings=(),
        errors=(),
        cancelled=False,
    )

    backend._apply_report(report)

    project = backend._flight_project
    assert project is not None
    assert set(project.detected_instruments) == {"gopro", "flir"}
    assert project.detected_instruments["gopro"].output_locations == [
        output / "processed" / "gopro"
    ]
    assert backend._instruments["gopro"].quicklook["capture_count"] == 3651
    assert backend.processing_queue.get("gopro_quick").status is ProcessingStatus.COMPLETE
    assert backend.processing_queue.get("gopro_quick").enabled is False
    assert backend._instruments["flir"].detection_status is DetectionStatus.READY


def test_selected_folder_starts_background_scan_and_updates_cards(tmp_path: Path):
    flight = tmp_path / "20260726_204943"
    noseboom = flight / "Noseboom"
    noseboom.mkdir(parents=True)
    source = noseboom / "noseboom.csv"
    source.write_text(
        "Airflow_UTCcorr_Nanoseconds_ns,TIMESTAMP\n"
        "1785098400000000000,2026-07-26T10:00:00Z\n",
        encoding="utf-8",
    )
    backend = DashboardScanBackend(
        _application_root(),
        folder_dialog=_Dialog(flight),
    )

    started = backend.select_and_start()
    state = _wait(backend)

    assert started["folder"] == str(flight)
    assert state["phase"] == "complete"
    assert state["files_scanned"] == 1
    assert state["instruments"]["noseboom"]["file_count"] == 1
    assert state["instruments"]["noseboom"]["detection_status"] in {
        "ready",
        "warning",
    }
    assert state["instruments"]["miro"]["detection_status"] == "not_detected"
    assert state["summary"]["detected_count"] == 1
    assert state["scans"]["flight"]["progress"] == 100.0
    assert state["scans"]["flight"]["current_file"] == str(source)
    assert "Noseboom" in state["scans"]["flight"]["current_instrument"]
    assert "Processing is done! close the window!" in state["scans"]["flight"]["message"]
    assert any(
        record["component"] == "flight-scanner" for record in backend.visible_logs()
    )


def test_valid_miro_txt_is_ready_without_tdms_or_boundary_row_warning(
    tmp_path: Path,
):
    flight = tmp_path / "flight"
    source = flight / "MIRO" / "segment.txt"
    source.parent.mkdir(parents=True)
    source.write_text(
        "t-stamp;CO wet\n"
        "26.07.2026 10:00:00,000;1\n"
        "26.07.2026 10:00:00,000;1\n"
        ";1\n"
        "26.07.2026 10:05:00,000;2\n",
        encoding="utf-8",
    )
    backend = DashboardScanBackend(_application_root())

    backend.start_scan(flight)
    state = _wait(backend)
    miro = state["instruments"]["miro"]

    assert miro["detection_status"] == "ready"
    assert not any("TDMS" in warning for warning in miro["warnings"])
    assert any("duplicated timestamp" in warning for warning in miro["timestamp_warnings"])
    assert any("missing timestamp" in warning for warning in miro["timestamp_warnings"])


def test_cancelled_folder_dialog_does_not_start_scan(tmp_path: Path):
    backend = DashboardScanBackend(
        _application_root(),
        folder_dialog=_Dialog(None),
    )

    result = backend.select_and_start()

    assert result == {"cancelled": True}
    assert backend.snapshot()["phase"] == "idle"


def test_cancelled_camera_dialog_continues_with_flight_folder_only(tmp_path: Path):
    flight = tmp_path / "flight"
    flight.mkdir()
    backend = DashboardScanBackend(
        _application_root(),
        folder_dialog=_CameraCancelledDialog(flight),
    )

    started = backend.select_and_start()
    state = _wait(backend)

    assert not started["cancelled"]
    assert started["camera_folder"] is None
    assert state["selected_folder"] == str(flight)
    assert state["selected_camera_folder"] is None
    assert state["phase"] == "complete"


def test_camera_folder_is_selected_only_by_explicit_action(tmp_path: Path):
    flight = tmp_path / "flight"
    camera = tmp_path / "camera"
    flight.mkdir(); camera.mkdir()
    backend = DashboardScanBackend(
        _application_root(),
        folder_dialog=_CameraSelectedDialog(flight, camera),
    )

    flight_selection = backend.select_folders()
    camera_selection = backend.select_camera_folder()

    state = backend.snapshot()
    assert flight_selection["folder"] == str(flight)
    assert flight_selection["camera_folder"] is None
    assert state["selected_folder"] == str(flight)
    assert state["phase"] == "folder-selected"
    assert state["running"] is False
    assert state["files_scanned"] == 0
    assert camera_selection == {"cancelled": False, "folder": str(camera)}
    assert state["selected_camera_folder"] == str(camera)
    assert state["scans"]["camera"]["phase"] == "folder-selected"


def test_flight_only_scan_retains_selected_camera_without_scanning_it(
    tmp_path: Path,
):
    flight = tmp_path / "flight"
    camera = tmp_path / "camera"
    (flight / "Noseboom").mkdir(parents=True)
    camera.mkdir()
    (flight / "Noseboom" / "noseboom.csv").write_text(
        "Airflow_UTCcorr_Nanoseconds_ns,TIMESTAMP\n"
        "1785098400000000000,2026-07-26T10:00:00Z\n",
        encoding="utf-8",
    )
    backend = DashboardScanBackend(_application_root())

    started = backend.start_scan(
        flight,
        camera_folder=camera,
        include_camera=False,
    )
    state = _wait(backend)

    assert started["camera_folder"] is None
    assert started["selected_camera_folder"] == str(camera)
    assert state["selected_camera_folder"] == str(camera)
    assert state["scans"]["flight"]["phase"] == "complete"
    assert state["scans"]["camera"]["phase"] == "folder-selected"
    assert state["instruments"]["noseboom"]["file_count"] == 1
    assert state["instruments"]["gopro"]["file_count"] == 0


def test_saved_projects_are_discovered_from_selected_parent_folder(tmp_path: Path):
    search_root = tmp_path / "saved-projects"
    search_root.mkdir()
    store = FlightProjectStore()
    expected_files = []
    for flight_id in ("Flight_A", "Flight_B"):
        raw = tmp_path / f"raw-{flight_id}"
        raw.mkdir()
        project = FlightProject(
            flight_id=flight_id,
            flight_folder_path=raw,
            output_folder_path=search_root,
        )
        expected_files.append(store.save_project(project, overwrite=True))

    ignored_backup = search_root / "backups" / "old" / "flight_project.json"
    ignored_backup.parent.mkdir(parents=True)
    ignored_backup.write_bytes(expected_files[0].read_bytes())
    invalid = search_root / "Broken" / "project" / "flight_project.json"
    invalid.parent.mkdir(parents=True)
    invalid.write_text("{not json", encoding="utf-8")

    backend = DashboardScanBackend(
        _application_root(),
        folder_dialog=_ProjectFolderDialog(search_root),
    )
    discovery = backend.select_project_folder()

    assert discovery["cancelled"] is False
    assert discovery["folder"] == str(search_root.resolve())
    assert discovery["valid_count"] == 2
    assert discovery["invalid_count"] == 1
    assert {item["flight_id"] for item in discovery["projects"]} == {
        "Flight_A", "Flight_B"
    }
    assert {Path(item["project_file"]) for item in discovery["projects"]} == {
        path.resolve() for path in expected_files
    }
    assert not any("backups" in item["relative_path"] for item in discovery["projects"])


def test_reset_system_clears_state_without_removing_raw_folder(tmp_path: Path):
    flight = tmp_path / "flight"
    flight.mkdir()
    backend = DashboardScanBackend(_application_root())
    backend.start_scan(flight)
    _wait(backend)

    state = backend.reset_system()

    assert state["phase"] == "idle"
    assert state["selected_folder"] is None
    assert state["time_filter"]["display_timezone"] == "UTC"
    assert flight.is_dir()


def test_dual_root_scan_updates_flight_and_camera_state(tmp_path: Path):
    flight = tmp_path / "flight"
    camera = tmp_path / "camera"
    noseboom = flight / "Noseboom" / "noseboom.csv"
    noseboom.parent.mkdir(parents=True)
    noseboom.write_text(
        "Airflow_UTCcorr_Nanoseconds_ns,TIMESTAMP\n"
        "1785098400000000000,2026-07-26T10:00:00Z\n",
        encoding="utf-8",
    )
    gopro = camera / "GoPro" / "GX010001.mp4"
    gopro.parent.mkdir(parents=True)
    gopro.touch()
    backend = DashboardScanBackend(_application_root())

    started = backend.start_scan(flight, camera_folder=camera)
    state = _wait(backend)

    assert started["camera_folder"] == str(camera)
    assert state["selected_camera_folder"] == str(camera)
    assert state["files_scanned"] == 2
    assert state["instruments"]["noseboom"]["file_count"] == 1
    assert state["instruments"]["gopro"]["file_count"] == 1
    assert backend._flight_project.camera_folder_path == camera


def test_gopro_time_is_corrected_before_remote_processing(tmp_path: Path):
    flight = tmp_path / "flight"
    camera = tmp_path / "camera"
    flight.mkdir()
    source = camera / "GoPro" / "GOPR0001.jpg"
    source.parent.mkdir(parents=True)
    exif = Image.Exif()
    exif[36867] = "2026:07:26 12:00:00"
    Image.new("RGB", (16, 12), (30, 90, 150)).save(source, exif=exif)
    backend = DashboardScanBackend(_application_root())

    backend.start_scan(flight, camera_folder=camera)
    state = _wait(backend)
    gopro = state["instruments"]["gopro"]

    assert gopro["utc_start_time"] == "2026-07-26T10:00:00+00:00"
    assert gopro["utc_end_time"] == "2026-07-26T10:00:00+00:00"
    assert gopro["processing_status"] == "idle"
    assert gopro["processing_step"] == (
        "Time corrected during detection: Europe/Berlin → UTC"
    )
    assert gopro["quicklook"]["time_correction_complete"] is True
    assert any(
        "GoPro camera time corrected" in message
        for message in state["messages"]
    )


def test_gopro_validation_expands_beyond_camera_discovery_sample(tmp_path: Path):
    flight = tmp_path / "flight"
    camera = tmp_path / "camera"
    flight.mkdir()
    gopro = camera / "GoPro"
    gopro.mkdir(parents=True)
    for index in range(22):
        timestamp = "2026:07:27 11:00:00"
        if index == 20:
            timestamp = "2026:07:27 07:24:17"
        elif index == 21:
            timestamp = "2026:07:27 12:28:26"
        exif = Image.Exif()
        exif[36867] = timestamp
        Image.new("RGB", (16, 12), (30, 90, 150)).save(
            gopro / f"GOPR{index:04d}.jpg", exif=exif
        )
    backend = DashboardScanBackend(_application_root())

    backend.start_scan(flight, camera_folder=camera)
    state = _wait(backend)
    result = state["instruments"]["gopro"]

    assert result["file_count"] == 22
    assert result["utc_start_time"] == "2026-07-27T05:24:17+00:00"
    assert result["utc_end_time"] == "2026-07-27T10:28:26+00:00"
    assert len(
        backend._flight_project.detected_instruments[
            "gopro"
        ].selected_source_files
    ) == 22


def test_ambiguous_candidate_can_be_explicitly_confirmed(tmp_path: Path):
    backend = DashboardScanBackend(_application_root())
    first = tmp_path / "MIRO-A"
    second = tmp_path / "MIRO-B"
    state = backend._instruments["miro"]
    state.ambiguous = True
    state.candidate_paths = [str(first), str(second)]
    state.warnings = ["Multiple candidate paths matched miro"]
    state.detection_status = DetectionStatus.WARNING

    backend.confirm_candidate("miro", second)
    confirmed = backend.snapshot()["instruments"]["miro"]

    assert confirmed["candidate_paths"] == [str(second)]
    assert not confirmed["ambiguous"]
    assert confirmed["detection_status"] == "ready"
    with pytest.raises(ValueError, match="no ambiguous candidates"):
        backend.confirm_candidate("miro", first)


def test_csv_matching_both_opc_identities_is_blocked_as_ambiguous(tmp_path: Path):
    shared = tmp_path / "HATCH-BOX" / "OPC_unknown.csv"
    shared.parent.mkdir()
    shared.write_text("_time\n2026-07-26T10:00:00Z\n", encoding="utf-8")
    candidate4 = InstrumentCandidate(
        "opc_hbx4", shared.parent, ("required_csv_columns",), 0.9,
        1, (shared,), (), (), False,
    )
    candidate5 = InstrumentCandidate(
        "opc_hbx5", shared.parent, ("required_csv_columns",), 0.9,
        1, (shared,), (), (), False,
    )
    report = ScanReport(
        root=tmp_path,
        candidates=(candidate4, candidate5),
        files_scanned=1,
        folders_scanned=2,
        inaccessible_path_count=0,
        malformed_file_count=0,
        warnings=(),
        errors=(),
        cancelled=False,
    )
    backend = DashboardScanBackend(_application_root())

    backend._apply_report(report)
    state = backend.snapshot()["instruments"]

    assert state["opc_hbx4"]["ambiguous"]
    assert state["opc_hbx5"]["ambiguous"]
    assert str(shared) in state["opc_hbx4"]["candidate_paths"]
    assert str(shared) in state["opc_hbx5"]["candidate_paths"]
    assert any(
        "both OPC identities" in warning
        for warning in state["opc_hbx4"]["warnings"]
    )


def test_background_scan_cancellation_is_safe(tmp_path: Path):
    flight = tmp_path / "flight"
    flight.mkdir()

    class _SlowScanner:
        def scan(self, root, *, cancellation, progress_callback):
            count = 0
            while not cancellation.is_cancelled:
                count += 1
                progress_callback(
                    ScanProgress(root, None, count, None, (), "scanning_folder")
                )
                time.sleep(0.005)
            return ScanReport(
                root=root,
                candidates=(),
                files_scanned=count,
                folders_scanned=1,
                inaccessible_path_count=0,
                malformed_file_count=0,
                warnings=(),
                errors=(),
                cancelled=True,
            )

    backend = DashboardScanBackend(
        _application_root(), scanner_factory=_SlowScanner
    )
    backend.start_scan(flight)
    assert backend.cancel()

    state = _wait(backend)
    assert state["cancelled"]
    assert state["phase"] == "cancelled"
    assert state["error"] is None


def test_http_bridge_serves_dashboard_scan_state_and_javascript(tmp_path: Path):
    backend = DashboardScanBackend(_application_root())
    server = _server_or_skip(backend)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_port}"
    try:
        with urllib.request.urlopen(base + "/") as response:
            html = response.read().decode("utf-8")
        with urllib.request.urlopen(base + "/dashboard.js") as response:
            javascript = response.read().decode("utf-8")
        with urllib.request.urlopen(base + "/data_products.txt") as response:
            products = response.read().decode("utf-8")
        with urllib.request.urlopen(base + "/software_update.txt") as response:
            update = response.read().decode("utf-8")
        with urllib.request.urlopen(base + "/License.txt") as response:
            license_text = response.read().decode("utf-8")
        with urllib.request.urlopen(base + "/api/scan") as response:
            state = json.load(response)

        assert "CC-FLUX Campaign 2026" in html
        assert "Scanning Flight Data" in html
        assert "/api/select-flight-folder" in javascript
        assert "uni-koeln.sciebo.de/s/CCFLUX" in products
        # The update notice is operator-editable and may be empty; the
        # route only has to serve it.
        assert isinstance(update, str)
        assert "Copyright © 2026 Biplob Dey" in license_text
        assert state["phase"] == "idle"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_http_scan_endpoint_accepts_folder_and_clear_only_hides_gui_logs(
    tmp_path: Path,
):
    flight = tmp_path / "empty-flight"
    flight.mkdir()
    backend = DashboardScanBackend(_application_root())
    server = _server_or_skip(backend)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_port}"
    try:
        body = json.dumps({"folder": str(flight)}).encode("utf-8")
        request = urllib.request.Request(
            base + "/api/scan",
            data=body,
            headers={"Content-Type": "application/json", "Origin": base},
            method="POST",
        )
        with urllib.request.urlopen(request) as response:
            assert response.status == 202
        _wait(backend)
        persistent_size = backend.logger.persistent_log_file.stat().st_size
        clear = urllib.request.Request(
            base + "/api/logs/clear", data=b"{}", method="POST",
            headers={"Origin": base},
        )
        with urllib.request.urlopen(clear):
            pass

        assert backend.visible_logs() == []
        assert backend.logger.persistent_log_file.stat().st_size == persistent_size
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_http_exit_endpoint_stops_server_cleanly():
    backend = DashboardScanBackend(_application_root())
    server = _server_or_skip(backend)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    origin = f"http://127.0.0.1:{server.server_port}"
    request = urllib.request.Request(
        origin + "/api/application/exit",
        data=b"{}",
        method="POST",
        headers={"Origin": origin},
    )
    try:
        with urllib.request.urlopen(request) as response:
            payload = json.load(response)
        thread.join(timeout=2)
        assert payload == {"exiting": True}
        assert not thread.is_alive()
    finally:
        server.server_close()


def test_time_filter_updates_active_flight_project_and_diagnostics(tmp_path: Path):
    raw = tmp_path / "raw"
    raw.mkdir()
    backend = DashboardScanBackend(_application_root())
    backend._time_state = DashboardTimeState.from_instrument_ranges(
        {
            "miro": (
                datetime(2026, 7, 26, 10, tzinfo=timezone.utc),
                datetime(2026, 7, 26, 12, tzinfo=timezone.utc),
                (),
            )
        }
    )
    backend._flight_project = FlightProject(
        flight_id="flight",
        flight_folder_path=raw,
        output_folder_path=tmp_path / "output",
        detected_instruments={"miro": InstrumentProjectState("miro")},
    )

    backend.update_time_filter(
        {
            "action": "set",
            "start": "2026-07-26T10:30:00Z",
            "end": "2026-07-26T11:30:00Z",
        }
    )
    with pytest.raises(ValueError, match="global Time Filter"):
        backend.update_instrument_time_override(
            "miro", "2026-07-26T10:40:00Z", "2026-07-26T11:20:00Z"
        )

    assert backend._flight_project.selected_analysis_start == datetime(
        2026, 7, 26, 10, 30, tzinfo=timezone.utc
    )
    instrument = backend._flight_project.detected_instruments["miro"]
    assert instrument.analysis_start_time is None
    assert instrument.analysis_end_time is None
    assert any(
        record["component"] == "time-filter" for record in backend.visible_logs()
    )


def test_resource_selection_is_saved_to_active_flight_project(tmp_path: Path):
    raw = tmp_path / "raw"
    raw.mkdir()
    backend = DashboardScanBackend(_application_root())
    backend._flight_project = FlightProject(
        flight_id="flight",
        flight_folder_path=raw,
        output_folder_path=tmp_path / "output",
    )
    safe_workers = backend.resource_manager.system.safely_available_workers
    workers = min(2, safe_workers)
    ram = min(
        2 * 1024**3,
        backend.resource_manager.system.safely_available_ram_bytes,
    )

    backend.update_resources(worker_count=workers, memory_bytes=ram)

    assert backend._flight_project.cpu_allocation == workers
    assert backend._flight_project.ram_allocation_bytes == ram
    snapshot = backend.snapshot()["resources"]
    assert snapshot["selected_worker_count"] == workers
    assert snapshot["selected_ram_bytes"] == ram


def test_processing_preflight_requires_output_scan_time_and_unambiguous_data(
    tmp_path: Path,
):
    flight = tmp_path / "flight"; flight.mkdir()
    output = tmp_path / "output"; output.mkdir()
    backend = DashboardScanBackend(_application_root())
    with pytest.raises(ValueError, match="Flight Folder"):
        backend.start_processing()
    backend._selected_folder = flight
    with pytest.raises(ValueError, match="Output Folder"):
        backend.start_processing()
    backend._selected_output_folder = output
    with pytest.raises(ValueError, match="discovery and validation must complete"):
        backend.start_processing()
    backend._phase = "complete"
    backend._report = ScanReport(
        flight, (), 0, 1, 0, 0, (), (), False
    )
    with pytest.raises(ValueError, match="time interval"):
        backend.start_processing()


def test_processing_preflight_allows_ready_instrument_and_skips_ambiguous_one(
    tmp_path: Path,
):
    flight = tmp_path / "flight"; flight.mkdir()
    output = tmp_path / "output"; output.mkdir()
    backend = DashboardScanBackend(_application_root())
    backend._selected_folder = flight
    backend._selected_output_folder = output
    backend._phase = "complete"
    backend._report = ScanReport(flight, (), 0, 1, 0, 0, (), (), False)
    backend._time_state = DashboardTimeState.from_instrument_ranges({
        "noseboom": (
            datetime(2026, 7, 26, 10, tzinfo=timezone.utc),
            datetime(2026, 7, 26, 11, tzinfo=timezone.utc),
            (),
        ),
        "miro": (
            datetime(2026, 7, 26, 10, tzinfo=timezone.utc),
            datetime(2026, 7, 26, 11, tzinfo=timezone.utc),
            (),
        ),
    })
    backend._instruments["noseboom"].detection_status = DetectionStatus.WARNING
    backend._instruments["noseboom"].ambiguous = True
    backend._instruments["miro"].detection_status = DetectionStatus.READY
    backend.processing_queue.set_enabled("miro", True)
    backend._flight_project = FlightProject(
        flight_id="flight", flight_folder_path=flight,
        output_folder_path=output,
    )
    backend._validate_processing_preflight({"noseboom": object(), "miro": object()})


def test_processing_preflight_allows_ready_flight_job_during_camera_scan(
    tmp_path: Path,
):
    flight = tmp_path / "flight"; flight.mkdir()
    output = tmp_path / "output"; output.mkdir()
    backend = DashboardScanBackend(_application_root())
    backend._selected_folder = flight
    backend._selected_output_folder = output
    backend._phase = "scanning_camera"
    backend._report = ScanReport(flight, (), 1, 1, 0, 0, (), (), False)
    backend._time_state = DashboardTimeState.from_instrument_ranges({
        "noseboom": (
            datetime(2026, 7, 26, 10, tzinfo=timezone.utc),
            datetime(2026, 7, 26, 11, tzinfo=timezone.utc),
            (),
        )
    })
    backend._instruments["noseboom"].detection_status = DetectionStatus.READY
    backend.processing_queue.set_enabled("noseboom", True)
    backend._flight_project = FlightProject(
        flight_id="flight", flight_folder_path=flight,
        output_folder_path=output,
    )

    backend._validate_processing_preflight({"noseboom": object()})


def test_output_folder_selection_updates_project(tmp_path: Path):
    flight = tmp_path / "flight"; flight.mkdir()
    output = tmp_path / "output"; output.mkdir()

    class _OutputDialog(_Dialog):
        def choose_output_folder(self):
            return output

    backend = DashboardScanBackend(
        _application_root(), folder_dialog=_OutputDialog(flight)
    )
    backend._selected_folder = flight
    backend._flight_project = FlightProject(
        flight_id="flight", flight_folder_path=flight,
        output_folder_path=tmp_path / "initial-output",
    )
    result = backend.select_output_folder()
    assert result["folder"] == str(output)
    assert backend._flight_project.output_folder_path == output


def test_manual_project_save_uses_independent_output_folder(tmp_path: Path):
    flight = tmp_path / "flight"
    output = tmp_path / "output"
    flight.mkdir()
    output.mkdir()
    backend = DashboardScanBackend(_application_root())
    backend._selected_folder = flight
    backend._selected_output_folder = output
    backend._flight_project = FlightProject(
        flight_id="flight",
        flight_folder_path=flight,
        output_folder_path=output,
    )

    project_file = backend.save_project()

    assert project_file.exists()
    assert output in project_file.parents
    assert flight not in project_file.parents
    assert any(
        record["message"] == "Flight Project saved"
        for record in backend.visible_logs()
    )


def test_dashboard_contains_complete_live_status_surfaces():
    html = (_application_root() / "app/assets/dashboard.html").read_text(
        encoding="utf-8"
    )
    script = (_application_root() / "app/assets/dashboard.js").read_text(
        encoding="utf-8"
    )

    for label in (
        "Flight Folder",
        "Output Folder",
        "Save .ccflux",
        "Refresh Status",
        "Start Processing",
        "Processing Priority",
        "Processing Queue",
        "Instrument Availability Timeline",
        "Processing Log &amp; Diagnostics",
    ):
        assert label in html
    assert 'data-name="INS Gimbal"' in html
    assert "processing_elapsed_seconds" in script
    assert "renderCameraStatus" in script
    assert "/api/project/save" in script


def test_dual_scans_have_independent_cancellation_tokens(tmp_path: Path):
    flight = tmp_path / "flight"
    camera = tmp_path / "camera"
    flight.mkdir(); camera.mkdir()

    class _IndependentScanner:
        def scan(self, root, *, cancellation, progress_callback):
            count = 0
            limit = 30 if root.name == "flight" else 10_000
            while count < limit and not cancellation.is_cancelled:
                count += 1
                progress_callback(
                    ScanProgress(root, None, count, None, (), "scanning_folder")
                )
                time.sleep(0.002)
            return ScanReport(
                root=root,
                candidates=(),
                files_scanned=count,
                folders_scanned=1,
                inaccessible_path_count=0,
                malformed_file_count=0,
                warnings=(),
                errors=(),
                cancelled=cancellation.is_cancelled,
            )

    backend = DashboardScanBackend(
        _application_root(), scanner_factory=_IndependentScanner
    )
    backend.start_scan(flight, camera_folder=camera)
    assert backend.cancel("camera")
    state = _wait(backend)

    assert state["scans"]["camera"]["phase"] == "cancelled"
    assert state["scans"]["flight"]["phase"] == "complete"
    assert not state["scans"]["flight"]["cancelled"]
    assert not state["camera_scan_ready"]
    assert state["phase"] == "complete"


def test_remote_sensing_workflow_decisions_are_persistently_logged(tmp_path: Path):
    backend = DashboardScanBackend(_application_root())

    backend.log_remote_sensing_workflow({
        "message": "Remote-sensing products confirmed by user",
        "step": "confirmation",
    })

    record = backend.visible_logs()[-1]
    assert record["component"] == "remote-sensing-workflow"
    assert record["message"] == "Remote-sensing products confirmed by user"
    assert record["processing_step"] == "confirmation"
    with pytest.raises(ValueError, match="message is required"):
        backend.log_remote_sensing_workflow({"message": "", "step": "test"})

def test_remote_sensing_dispatches_only_camera_pool_with_custom_time(
    tmp_path: Path,
):
    flight = tmp_path / "flight"; flight.mkdir()
    camera = tmp_path / "camera"; camera.mkdir()
    output = tmp_path / "output"; output.mkdir()
    backend = DashboardScanBackend(_application_root())
    backend._selected_folder = flight
    backend._selected_camera_folder = camera
    backend._selected_output_folder = output
    backend._scan_channels["camera"] = backend._new_scan_channel("camera", camera)
    backend._scan_channels["camera"]["phase"] = "complete"
    backend._instruments["gopro"].detection_status = DetectionStatus.READY
    backend._time_state = DashboardTimeState.from_instrument_ranges({
        "gopro": (
            datetime(2026, 7, 26, 10, tzinfo=timezone.utc),
            datetime(2026, 7, 26, 11, tzinfo=timezone.utc),
            (),
        )
    })
    backend._flight_project = FlightProject(
        flight_id="flight", flight_folder_path=flight,
        camera_folder_path=camera, output_folder_path=output,
    )
    backend._gopro_quick_task = lambda context: None

    registered = backend.start_remote_sensing({
        "time_mode": "custom",
        "start": "2026-07-26T10:03:00Z",
        "end": "2026-07-26T10:08:00Z",
    })

    assert registered == ["gopro_quick"]
    assert backend.processing_queue.get("gopro_quick").worker_group.value == "camera_metadata"
    assert backend.processing_queue.get("noseboom").task is None
    assert backend.snapshot()["time_filter"]["selected_analysis_start"].startswith(
        "2026-07-26T10:03:00"
    )
    backend.shutdown()

def test_tk_folder_dialog_fallback_is_foreground_and_owned(monkeypatch, tmp_path: Path):
    monkeypatch.setattr("app.scan_backend.sys.platform", "linux")
    selected = tmp_path / "selected-flight"
    selected.mkdir()
    calls: dict[str, object] = {}

    class _Root:
        def withdraw(self): calls["withdraw"] = True
        def attributes(self, name, value): calls["attributes"] = (name, value)
        def lift(self): calls["lift"] = True
        def update_idletasks(self): calls["update_idletasks"] = True
        def update(self): calls["update"] = True
        def destroy(self): calls["destroy"] = True

    root = _Root()
    filedialog = SimpleNamespace(
        askdirectory=lambda **options: calls.update(options=options) or str(selected)
    )
    tkinter = types.ModuleType("tkinter")
    tkinter.Tk = lambda: root
    tkinter.TclError = RuntimeError
    tkinter.filedialog = filedialog
    monkeypatch.setitem(sys.modules, "tkinter", tkinter)

    result = FolderDialog().choose_flight_folder()

    assert result == selected
    assert calls["attributes"] == ("-topmost", True)
    assert calls["lift"] is True
    assert calls["update"] is True
    assert calls["destroy"] is True
    assert calls["options"]["parent"] is root


def test_windows_dialog_uses_topmost_sta_process(monkeypatch, tmp_path: Path):
    selected = tmp_path / "selected-flight"
    selected.mkdir()
    calls: list[tuple[list[str], dict[str, object]]] = []

    def _run(arguments, **options):
        calls.append((arguments, options))
        return SimpleNamespace(returncode=0, stdout=str(selected), stderr="")

    monkeypatch.setattr("app.scan_backend.subprocess.run", _run)
    monkeypatch.setattr("app.scan_backend.sys.platform", "win32")

    result = FolderDialog().choose_flight_folder()

    assert result == selected
    arguments, options = calls[0]
    assert "-STA" in arguments
    assert "FolderBrowserDialog" in arguments[-1]
    assert "$owner.TopMost = $true" in arguments[-1]
    assert "SetWindowPos" in arguments[-1]
    assert "980, 700" in arguments[-1]
    assert "0x0056" in arguments[-1]
    assert "Select the root folder for one Zeppelin flight" in arguments[-1]
    assert arguments[arguments.index("-WindowStyle") + 1] == "Hidden"
    # The window-hiding options exist only in the Windows subprocess module.
    # sys.platform is monkeypatched above so the script itself can be asserted
    # on any host, but these flags can only be checked where they are real —
    # the production code guards them with getattr for exactly this reason.
    if hasattr(subprocess, "CREATE_NO_WINDOW"):
        assert options["creationflags"] & subprocess.CREATE_NO_WINDOW
        assert options["startupinfo"].dwFlags & subprocess.STARTF_USESHOWWINDOW
        assert options["startupinfo"].wShowWindow == subprocess.SW_HIDE
    else:
        assert "creationflags" not in options

def test_dashboard_startup_opens_default_browser_and_logs(monkeypatch):
    opened: list[tuple[str, int, bool]] = []
    records: list[tuple[object, str, str]] = []

    class _Logger:
        def log(self, severity, component, message):
            records.append((severity, component, message))

        def capture_exception(self, component, message, error):
            raise AssertionError(f"Unexpected browser error: {error}")

    monkeypatch.setattr(
        "app.server.webbrowser.open",
        lambda url, new, autoraise: opened.append((url, new, autoraise)) or True,
    )
    server = SimpleNamespace(backend=SimpleNamespace(logger=_Logger()))

    open_dashboard_in_browser(server, "http://127.0.0.1:8765/")

    assert opened == [("http://127.0.0.1:8765/", 2, True)]
    assert records[0][1] == "dashboard-startup"
    assert "opened automatically" in records[0][2]

def test_completed_file_inventory_switches_to_real_validation_state(tmp_path: Path):
    root = tmp_path / "flight"
    root.mkdir()
    backend = DashboardScanBackend(_application_root())
    with backend._lock:
        backend._scan_channels["flight"] = backend._new_scan_channel(
            "flight", root, running=True
        )

    backend._on_progress(
        ScanProgress(root, None, 28, 100.0, ("noseboom",), "complete"),
        "flight",
    )
    channel = backend.snapshot()["scans"]["flight"]

    assert channel["running"]
    assert channel["phase"] == "post_scan_checks"
    assert channel["progress"] is None
    assert "validation" in channel["message"].casefold()
    assert "Processing is done" not in channel["message"]

def test_default_resources_are_pc_adaptive_and_report_min_selected_max():
    backend = DashboardScanBackend(_application_root())
    resources = backend.snapshot()["resources"]

    assert resources["selection_mode"] == "automatic"
    assert resources["minimum_worker_count"] == 1
    assert resources["minimum_worker_count"] <= resources["selected_worker_count"]
    assert resources["selected_worker_count"] == resources["recommended_worker_count"]
    assert resources["selected_worker_count"] <= resources["maximum_worker_count"]
    assert resources["selected_worker_count"] <= max(
        1, resources["total_logical_cores"] // 4
    )
    assert resources["minimum_ram_bytes"] <= resources["selected_ram_bytes"]
    assert resources["selected_ram_bytes"] == resources["recommended_ram_bytes"]
    assert resources["selected_ram_bytes"] <= resources["maximum_ram_bytes"]


def test_enabling_ready_instrument_waits_for_explicit_start():
    backend = DashboardScanBackend(_application_root())
    backend._instruments["noseboom"].detection_status = DetectionStatus.READY
    dispatch_calls: list[bool] = []

    class _ExistingScheduler:
        def dispatch(self):
            dispatch_calls.append(True)

    backend._scheduler = _ExistingScheduler()
    backend.update_queue({"action": "enable", "job_id": "noseboom"})

    assert backend.processing_queue.get("noseboom").enabled
    assert dispatch_calls == []


def test_unhealthy_instrument_cannot_be_selected_for_processing():
    backend = DashboardScanBackend(_application_root())

    with pytest.raises(ValueError, match="not ready for selection"):
        backend.update_queue({"action": "enable", "job_id": "noseboom"})

def test_default_processing_queue_requires_explicit_instrument_selection():
    backend = DashboardScanBackend(_application_root())
    queue = backend.snapshot()["processing_queue"]

    assert queue["selected_count"] == 0
    assert not queue["can_start"]
    assert all(not job["enabled"] for job in queue["jobs"])


def test_start_button_is_ready_before_output_folder_and_processing_preflight_still_requires_it(
    tmp_path: Path,
):
    flight = tmp_path / "flight"
    flight.mkdir()
    backend = DashboardScanBackend(_application_root())
    backend._selected_folder = flight
    backend._phase = "complete"
    backend._report = ScanReport(flight, (), 1, 1, 0, 0, (), (), False)
    backend._time_state = DashboardTimeState.from_instrument_ranges({
        "noseboom": (
            datetime(2026, 7, 26, 10, tzinfo=timezone.utc),
            datetime(2026, 7, 26, 11, tzinfo=timezone.utc),
            (),
        )
    })
    backend._instruments["noseboom"].detection_status = DetectionStatus.READY
    backend.processing_queue.set_enabled("noseboom", True)

    queue = backend.snapshot()["processing_queue"]

    assert queue["can_start"] is True
    assert queue["workflow"]["next_step"] == (
        "Ready to start; an Output Folder will be requested"
    )
    with pytest.raises(ValueError, match="Output Folder"):
        backend.start_processing()


def test_processing_configuration_is_blocked_while_dispatched_job_is_active():
    backend = DashboardScanBackend(_application_root())
    job = backend.processing_queue.get("noseboom")
    backend.processing_queue.set_enabled("noseboom", True)
    job.task = lambda context: None
    job.status = ProcessingStatus.QUEUED

    with pytest.raises(ValueError, match="Please wait! System is busy now!"):
        backend.update_queue({"action": "disable", "job_id": "noseboom"})
    with pytest.raises(ValueError, match="Please wait! System is busy now!"):
        backend.update_resources(worker_count=1, memory_bytes=1024**3)
def test_ready_picarro_is_selectable_in_processing_priority():
    backend = DashboardScanBackend(_application_root())
    backend._instruments["picarro"].detection_status = DetectionStatus.READY

    queue = backend.snapshot()["processing_queue"]
    picarro = next(job for job in queue["jobs"] if job["job_id"] == "picarro")

    assert picarro["available_for_selection"] is True
    assert picarro["selection_reason"] == "Ready for processing"
    backend.update_queue({"action": "enable", "job_id": "picarro"})
    assert backend.processing_queue.get("picarro").enabled is True

def test_nonambiguous_warning_instrument_can_be_selected_and_processed():
    backend = DashboardScanBackend(_application_root())
    state = backend._instruments["partector"]
    state.detection_status = DetectionStatus.WARNING
    state.warnings = ["1 non-monotonic timestamp transition detected and documented."]
    state.timestamp_warnings = list(state.warnings)
    state.errors = []
    state.ambiguous = False

    queue = backend.snapshot()["processing_queue"]
    partector = next(job for job in queue["jobs"] if job["job_id"] == "partector")

    assert partector["available_for_selection"] is True
    assert partector["selection_reason"] == (
        "Available with scan warning; review during health check"
    )
    backend.update_queue({"action": "enable", "job_id": "partector"})
    assert backend.processing_queue.get("partector").enabled is True


def test_wrong_flight_flir_is_not_selectable_with_noseboom_time_anchor():
    backend = DashboardScanBackend(_application_root())
    backend._instruments["flir"].detection_status = DetectionStatus.READY
    previous_day = datetime(2026, 7, 25, 12, tzinfo=timezone.utc)
    backend._time_state = DashboardTimeState.from_instrument_ranges(
        {
            "noseboom": (
                datetime(2026, 7, 27, 5, tzinfo=timezone.utc),
                datetime(2026, 7, 27, 10, tzinfo=timezone.utc),
                (),
            ),
            "flir": (
                previous_day,
                previous_day + timedelta(hours=1),
                (),
            ),
        },
        analysis_anchor_id="noseboom",
    )

    queue = backend.snapshot()["processing_queue"]
    flir = next(job for job in queue["jobs"] if job["job_id"] == "flir_quick")
    assert flir["available_for_selection"] is False
    assert flir["selection_reason"] == (
        "UTC data do not overlap the selected Noseboom flight interval"
    )


def test_straight_flight_recalculation_runs_in_background_with_live_progress(monkeypatch):
    backend = DashboardScanBackend(_application_root())
    observed = []

    def fake_preview(settings, *, progress_callback=None):
        for percent, message in ((5, "validating"), (45, "reading rows"), (80, "detecting legs")):
            progress_callback(percent, message)
            observed.append(percent)
            time.sleep(0.01)
        return {"saved": False, "temporary": True, "data": {"points": [], "straight_legs": []}}

    monkeypatch.setattr(backend, "preview_noseboom_straight_settings", fake_preview)
    started = backend.start_noseboom_straight_recalculation({"min_speed_mps": 8})
    backend._noseboom_recalculation_thread.join(timeout=2)
    completed = backend.noseboom_straight_recalculation_progress()

    assert started["job_id"]
    assert observed == [5, 45, 80]
    assert completed["running"] is False
    assert completed["ready"] is True
    assert completed["progress"] == 100
    assert completed["result"]["data"]["straight_legs"] == []
    assert completed["elapsed_seconds"] >= 0
