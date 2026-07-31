import csv
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import pandas as pd
import pytest

from app.scan_backend import _assert_directory_responsive, _merge_scan_reports
from core.flight_project import FlightProject, FlightProjectStore
from core.priority_manager import create_default_priority_queue
from core.scanner import InstrumentCandidate, ScanReport
from core.detector import InputCandidate
from instruments.noseboom import NoseboomAdapter
from instruments.noseboom.adapter import _map_payload


def _report(root: Path, instrument_id: str, candidate: Path) -> ScanReport:
    return ScanReport(
        root,
        (
            InstrumentCandidate(
                instrument_id, candidate, ("folder",), 0.8, 1, (),
                (), (), False,
            ),
        ),
        1, 1, 0, 0, (), (), False,
    )


def test_independent_roots_retain_ambiguous_candidates(tmp_path: Path):
    first = _report(tmp_path / "flight", "micasense", tmp_path / "flight" / "MicaSense")
    second = _report(tmp_path / "camera", "micasense", tmp_path / "camera" / "MicaSense")
    merged = _merge_scan_reports(first, second)
    assert len(merged.candidates) == 2
    assert all(candidate.ambiguous for candidate in merged.candidates)
    assert merged.files_scanned == 2


def test_stalled_external_camera_root_fails_with_concise_message(tmp_path: Path):
    with patch("app.scan_backend.subprocess.run", side_effect=__import__("subprocess").TimeoutExpired("probe", 8)):
        with pytest.raises(ValueError, match="did not respond"):
            _assert_directory_responsive(tmp_path)


def test_camera_root_round_trips_in_project_json(tmp_path: Path):
    raw = tmp_path / "raw"; raw.mkdir()
    camera = tmp_path / "camera"; camera.mkdir()
    output = tmp_path / "output"; output.mkdir()
    project = FlightProject("F1", raw, output, camera_folder_path=camera)
    path = FlightProjectStore().save_project(project)
    loaded = FlightProjectStore().load_project(path)
    assert loaded.camera_folder_path == camera


def test_noseboom_map_payload_preserves_heading_and_straight_classification():
    frame = pd.DataFrame({
        "plot_lat": [47.0, 47.1, 47.2],
        "plot_lon": [9.0, 9.1, 9.2],
        "wind_mps": [2.0, 3.0, 4.0],
        "heading_deg": [10.0, 20.0, 30.0],
        "straight": [False, True, True],
    })
    payload = _map_payload(frame)
    assert payload["available"] is True
    assert payload["points"][1]["heading_deg"] == 20.0
    assert payload["points"][1]["straight"] is True


def test_noseboom_map_contract_is_interactive():
    root = Path(__file__).parents[1] / "app" / "assets"
    javascript = (root / "dashboard.js").read_text(encoding="utf-8")
    html = (root / "dashboard.html").read_text(encoding="utf-8")
    for control in ("mapZoomIn", "mapZoomOut", "mapReset", "mapLineWidth"):
        assert control in javascript
    assert "svg.onwheel" in javascript
    assert "svg.onpointermove" in javascript
    assert "Accepted straight-flight leg" in javascript
    assert ".instrument-map" in html


def test_picarro_job_requires_explicit_operator_selection_by_default():
    job = create_default_priority_queue().get("picarro")
    assert job.enabled is False
    assert job.status.value == "paused"


def test_noseboom_selected_interval_streaming_preserves_raw_source(tmp_path: Path):
    adapter = NoseboomAdapter(
        output_root=tmp_path / "output", flight_name="synthetic"
    )
    module = adapter.bridge.module
    columns = sorted(set(module.FIELDS.values()))
    start = datetime(2026, 7, 27, 5, 20, tzinfo=timezone.utc)
    source = tmp_path / "Noseboom.csv"
    with source.open("w", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=columns)
        writer.writeheader()
        for offset in range(6):
            instant = start + timedelta(seconds=offset)
            row = {column: 0 for column in columns}
            row["Airflow_UTCcorr_Nanoseconds_ns"] = int(
                instant.timestamp() * 1_000_000_000
            )
            row["TIMESTAMP"] = instant.isoformat()
            row["INS_Filter_LLHPos_Latitude_deg"] = 47.6
            row["INS_Filter_LLHPos_Longitude_deg"] = 9.4
            writer.writerow(row)
    original = source.read_bytes()

    loaded = adapter.load_time_window(
        InputCandidate("noseboom", (source,), 1.0, "synthetic"),
        start + timedelta(seconds=2),
        start + timedelta(seconds=3),
    )

    assert len(loaded.data) == 2
    assert loaded.data["time_ns"].min() == int(
        (start + timedelta(seconds=2)).timestamp() * 1_000_000_000
    )
    assert source.read_bytes() == original
