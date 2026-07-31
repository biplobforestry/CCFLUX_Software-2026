import json
from pathlib import Path

import pytest

from core.exceptions import ProjectOverwriteError
from core.logging_manager import (
    LogLevel,
    ProcessingLogManager,
    WorkerError,
)


def _manager(tmp_path: Path) -> ProcessingLogManager:
    return ProcessingLogManager(tmp_path / "output" / "flight" / "logs" / "processing.jsonl")


def test_all_severity_levels_and_gui_callback(tmp_path: Path):
    manager = _manager(tmp_path)
    delivered = []
    manager.subscribe(delivered.append)

    for level in LogLevel:
        manager.log(level, "test", f"{level.value} message")

    assert [item.severity for item in manager.records()] == list(LogLevel)
    assert delivered == list(manager.records())


def test_exception_capture_records_type_and_full_traceback(tmp_path: Path):
    manager = _manager(tmp_path)
    try:
        raise ValueError("bad input")
    except ValueError as exception:
        record = manager.capture_exception(
            "parser",
            "Input could not be parsed",
            exception,
            instrument="miro",
            job_id="job-7",
            processing_step="load",
        )

    assert record.exception_type == "ValueError"
    assert "ValueError: bad input" in record.traceback
    assert record.user_message == "Input could not be parsed"
    assert "bad input" not in record.user_message


def test_worker_error_forwarding_preserves_diagnostics(tmp_path: Path):
    manager = _manager(tmp_path)
    error = WorkerError(
        component="worker-process",
        message="Detailed processing failed",
        instrument="flir",
        job_id="worker-2",
        exception_type="RuntimeError",
        traceback="worker traceback text",
        processing_step="radiometric-conversion",
    )

    record = manager.forward_worker_error(error)

    assert record.severity is LogLevel.ERROR
    assert record.job_id == "worker-2"
    assert record.traceback == "worker traceback text"


def test_persistent_json_lines_file_contains_structured_records(tmp_path: Path):
    manager = _manager(tmp_path)
    source = tmp_path / "data.csv"
    manager.log(
        LogLevel.SUCCESS,
        "scanner",
        "Instrument detected",
        instrument="noseboom",
        file_path=source,
    )

    payload = json.loads(manager.persistent_log_file.read_text(encoding="utf-8"))

    assert payload["severity"] == "SUCCESS"
    assert payload["instrument"] == "noseboom"
    assert payload["file_path"] == str(source)
    assert payload["timestamp"].endswith("+00:00")


def test_filter_by_severity_and_instrument(tmp_path: Path):
    manager = _manager(tmp_path)
    manager.log(LogLevel.INFO, "test", "MIRO info", instrument="miro")
    manager.log(LogLevel.ERROR, "test", "MIRO error", instrument="miro")
    manager.log(LogLevel.ERROR, "test", "FLIR error", instrument="flir")

    records = manager.records(
        severities={LogLevel.ERROR}, instrument="miro"
    )

    assert len(records) == 1
    assert records[0].message == "MIRO error"
    assert "MIRO error" in manager.copy_logs(
        severities={LogLevel.ERROR}, instrument="miro"
    )


def test_clearing_visible_records_preserves_file_and_session_history(tmp_path: Path):
    manager = _manager(tmp_path)
    manager.log(LogLevel.INFO, "test", "before clear")
    persistent_before = manager.persistent_log_file.read_text(encoding="utf-8")

    manager.clear_visible()

    assert manager.records() == ()
    assert len(manager.records(visible_only=False)) == 1
    assert manager.persistent_log_file.read_text(encoding="utf-8") == persistent_before


def test_export_full_persistent_log_after_gui_clear(tmp_path: Path):
    manager = _manager(tmp_path)
    manager.log(LogLevel.WARNING, "resource-manager", "RAM usage high")
    manager.clear_visible()
    export = tmp_path / "exports" / "diagnostics.jsonl"

    result = manager.export_logs(export)

    assert result == export
    assert export.read_text(encoding="utf-8") == manager.persistent_log_file.read_text(
        encoding="utf-8"
    )
    with pytest.raises(ProjectOverwriteError):
        manager.export_logs(export)


def test_gui_scroll_and_collapse_state(tmp_path: Path):
    state = _manager(tmp_path).gui_state
    state.pause_auto_scroll()
    state.collapse()
    assert not state.auto_scroll
    assert state.collapsed
    state.resume_auto_scroll()
    state.expand()
    assert state.auto_scroll
    assert not state.collapsed


def test_diagnostic_helpers_preserve_context(tmp_path: Path):
    manager = _manager(tmp_path)
    source = tmp_path / "broken.csv"
    manager.invalid_timestamp(
        "Two timestamps are invalid", instrument="opc_hbx4", file_path=source
    )
    manager.missing_columns(
        ["_time"], instrument="opc_hbx4", file_path=source
    )
    manager.corrupted_file(source, instrument="opc_hbx4")
    manager.cancelled_job("job-cancelled", instrument="opc_hbx4")
    manager.resource_warning("CPU allocation exceeds recommended level")
    try:
        raise OSError("disk full")
    except OSError as exception:
        manager.output_write_failure(
            exception, tmp_path / "output.csv", instrument="opc_hbx4"
        )

    records = manager.records()
    assert len(records) == 6
    assert records[0].processing_step == "timestamp-validation"
    assert records[1].processing_step == "column-validation"
    assert records[-1].exception_type == "OSError"
