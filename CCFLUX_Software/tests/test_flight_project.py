import json
import zipfile
from datetime import datetime, timezone
from pathlib import Path

import pytest

from core.exceptions import (
    DuplicateFlightIDError,
    ProjectFileError,
    ProjectOverwriteError,
)
from core.flight_project import (
    INSTRUMENT_IDS,
    FlightProjectStore,
    InstrumentProjectState,
    RawFileState,
)


def _project(tmp_path: Path, *, checksum_mode: bool = False):
    raw = tmp_path / "raw" / "20260726_204943"
    raw.mkdir(parents=True)
    source = raw / "OPC_HBX4.csv"
    source.write_text("_time,value\n2026-07-26T20:49:43Z,1\n", encoding="utf-8")
    output = tmp_path / "output"
    state = InstrumentProjectState(
        instrument_id="opc_hbx4",
        selected_source_files=[source],
        selected_source_folders=[raw],
        detection_confidence=0.95,
        ambiguous_candidates=[],
        utc_start_time=datetime(2026, 7, 26, 20, 49, 43, tzinfo=timezone.utc),
        utc_end_time=datetime(2026, 7, 26, 20, 49, 43, tzinfo=timezone.utc),
        timestamp_warnings=["example retained warning"],
        processing_priority=1,
    )
    project = FlightProjectStore().create_project(
        flight_id="20260726_204943",
        flight_folder_path=raw,
        output_folder_path=output,
        detected_instruments={"opc_hbx4": state},
        cpu_allocation=4,
        ram_allocation_bytes=8_000_000_000,
        software_version="0.1.0",
        configuration_version="2026.1",
        checksum_mode=checksum_mode,
    )
    return project, source


def test_save_project_as_compressed_archive_with_readable_manifest(tmp_path: Path):
    """A .ccflux is an archive; its manifest stays indented and human-readable."""
    project, _ = _project(tmp_path)
    store = FlightProjectStore()

    saved = store.save_project(project)
    payload = store.read_manifest(saved)

    assert saved == project.project_file
    assert saved.suffix == ".ccflux"
    assert store.is_compressed(saved)
    assert payload["flight_id"] == "20260726_204943"
    assert payload["detected_instruments"]["opc_hbx4"]["detection_confidence"] == 0.95
    with zipfile.ZipFile(saved) as archive:
        assert "project.json" in archive.namelist()
        assert "manifest.json" in archive.namelist()
        # Indentation is preserved inside the archive so the manifest can be
        # read directly by a human unzipping the project.
        assert "\n  " in archive.read("project.json").decode("utf-8")
    assert not (project.flight_folder_path / "flight_project.json").exists()


def test_plain_json_project_written_before_compression_still_loads(tmp_path: Path):
    """Projects saved by earlier versions are plain JSON and must keep opening."""
    project, _ = _project(tmp_path)
    store = FlightProjectStore()
    destination = project.project_file
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(
        json.dumps(_legacy_payload(project), indent=2, sort_keys=True),
        encoding="utf-8",
    )

    assert not store.is_compressed(destination)
    loaded = store.load_project(destination)
    assert loaded.flight_id == project.flight_id
    assert loaded.detected_instruments["opc_hbx4"].detection_confidence == 0.95


def _legacy_payload(project) -> dict:
    from core.flight_project import _project_to_dict

    return _project_to_dict(project)


def test_legacy_json_project_can_be_loaded_and_next_save_uses_ccflux(
    tmp_path: Path,
):
    project, _ = _project(tmp_path)
    store = FlightProjectStore()
    saved = store.save_project(project)
    legacy = saved.with_name("flight_project.json")
    saved.replace(legacy)

    opened = store.open_project(legacy)
    migrated = store.save_project(opened.project, overwrite=True)

    assert opened.project.project_id == project.project_id
    assert migrated.name == f"{opened.project.flight_id}.ccflux"
    assert migrated.is_file()


def test_load_project_and_reuse_unchanged_saved_scan(tmp_path: Path):
    project, source = _project(tmp_path)
    store = FlightProjectStore()
    saved = store.save_project(project)

    opened = store.open_project(saved)

    assert opened.project.project_id == project.project_id
    assert opened.project.detected_instruments["opc_hbx4"].selected_source_files == [
        source
    ]
    assert opened.reused_saved_scan
    assert not opened.rescan_required
    assert opened.raw_file_changes[0].state is RawFileState.UNCHANGED


def test_invalid_project_file(tmp_path: Path):
    invalid = tmp_path / "invalid.json"
    invalid.write_text("{not JSON", encoding="utf-8")

    with pytest.raises(ProjectFileError, match="Invalid or unreadable"):
        FlightProjectStore().load_project(invalid)


def test_changed_source_file_requires_rescan(tmp_path: Path):
    project, source = _project(tmp_path)
    store = FlightProjectStore()
    saved = store.save_project(project)
    source.write_text("changed and longer", encoding="utf-8")

    opened = store.open_project(saved)

    assert opened.rescan_required
    assert not opened.reused_saved_scan
    assert opened.raw_file_changes[0].state is RawFileState.CHANGED


def test_missing_source_file_is_reported_without_load_failure(tmp_path: Path):
    project, source = _project(tmp_path)
    store = FlightProjectStore()
    saved = store.save_project(project)
    source.unlink()

    opened = store.open_project(saved)

    assert opened.rescan_required
    assert opened.missing_raw_files == (source,)
    assert opened.raw_file_changes[0].state is RawFileState.MISSING


def test_output_folder_structure_is_created(tmp_path: Path):
    project, _ = _project(tmp_path)

    expected = {
        "project",
        "metadata",
        "processed",
        "quicklooks",
        "thumbnails",
        "reports",
        "logs",
    }
    assert expected <= {path.name for path in project.flight_output_root.iterdir()}
    assert {
        path.name for path in (project.flight_output_root / "processed").iterdir()
    } == set(INSTRUMENT_IDS)


def test_duplicate_flight_id_is_rejected(tmp_path: Path):
    project, _ = _project(tmp_path)

    with pytest.raises(DuplicateFlightIDError, match="Flight ID"):
        FlightProjectStore().create_project(
            flight_id=project.flight_id,
            flight_folder_path=project.flight_folder_path,
            output_folder_path=project.output_folder_path,
        )


def test_project_overwrite_requires_explicit_permission(tmp_path: Path):
    project, _ = _project(tmp_path)
    store = FlightProjectStore()
    saved = store.save_project(project)

    with pytest.raises(ProjectOverwriteError, match="not overwritten"):
        store.save_project(project)

    assert store.save_project(project, overwrite=True) == saved


def test_optional_checksum_detects_same_size_same_mtime_change(tmp_path: Path):
    project, source = _project(tmp_path, checksum_mode=True)
    store = FlightProjectStore()
    saved = store.save_project(project)
    original_stat = source.stat()
    text = source.read_text(encoding="utf-8")
    source.write_text(text.replace("value", "VALUE"), encoding="utf-8")
    source.touch()
    source_stat = source.stat()
    assert source_stat.st_size == original_stat.st_size
    source.touch()
    import os

    os.utime(source, ns=(original_stat.st_atime_ns, original_stat.st_mtime_ns))

    normal = store.open_project(saved)
    strict = store.open_project(saved, checksum_mode=True)

    assert normal.reused_saved_scan
    assert strict.rescan_required
    assert "checksum changed" in strict.raw_file_changes[0].reason


def test_force_rescan_does_not_require_raw_changes(tmp_path: Path):
    project, _ = _project(tmp_path)
    store = FlightProjectStore()
    saved = store.save_project(project)

    opened = store.force_rescan(saved)

    assert opened.rescan_required
    assert not opened.reused_saved_scan
