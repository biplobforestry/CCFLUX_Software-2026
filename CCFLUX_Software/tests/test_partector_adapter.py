from datetime import datetime, timedelta
from pathlib import Path

import pytest

from core.detector import InputCandidate
from core.enums import DetectionStatus, ProcessingStatus
from core.logging_manager import ProcessingLogManager
from core.scanner import ScanEntry, ScanIndex
from instruments.partector import PartectorAdapter


SIZE_CENTERS = (10, 20, 30, 50, 70, 100, 200, 300)


def _partector_csv(path: Path, rows: int = 60) -> Path:
    columns = [
        "_time",
        "time__1",
        "number_1",
        "diam_1",
        "LDSA_1",
        "mass_1",
        "flow_1",
        "RH_1",
        "T_1",
        "P_1",
        "error_1",
        *(f"n{diameter}_1" for diameter in SIZE_CENTERS),
    ]
    lines = [",".join(columns)]
    start = datetime(2026, 7, 26, 10)
    for index in range(rows):
        record = {
            "_time": (start + timedelta(seconds=index)).isoformat() + "Z",
            "time__1": str(index),
            "number_1": str(1000 + index),
            "diam_1": "60",
            "LDSA_1": "25",
            "mass_1": "2",
            "flow_1": "0.5",
            "RH_1": "50",
            "T_1": "20",
            "P_1": str(1000 - index * 0.01),
            "error_1": "0",
            **{
                f"n{diameter}_1": str(100 + channel + index * 0.1)
                for channel, diameter in enumerate(SIZE_CENTERS)
            },
        }
        lines.append(",".join(record[column] for column in columns))
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def _adapter(tmp_path: Path):
    logger = ProcessingLogManager(tmp_path / "logs" / "processing.jsonl")
    adapter = PartectorAdapter(
        output_root=tmp_path / "output",
        flight_name="20260726_100000",
        logger=logger,
    )
    return adapter, logger


def test_detection_validation_and_native_timestamp_coverage(tmp_path: Path):
    source = _partector_csv(tmp_path / "partector.csv")
    adapter, _ = _adapter(tmp_path)
    candidate = adapter.detect(
        ScanIndex(tmp_path, (ScanEntry(source, source.stat().st_size, True),))
    )[0]

    result = adapter.validate(candidate)

    assert candidate.instrument_id == "partector"
    assert result.detection_status is DetectionStatus.READY
    assert result.original_start_time == datetime(2026, 7, 26, 10)
    assert result.utc_start_time is not None
    assert result.utc_start_time.utcoffset().total_seconds() == 0
    details = result.metadata["files"][0]
    assert details["size_channel_centers_nm"] == list(SIZE_CENTERS)


def test_missing_column_or_size_channel_is_not_invented(tmp_path: Path):
    source = _partector_csv(tmp_path / "partector.csv")
    rows = [line.split(",") for line in source.read_text(encoding="utf-8").splitlines()]
    missing_index = rows[0].index("n300_1")
    source.write_text(
        "\n".join(
            ",".join(value for index, value in enumerate(row) if index != missing_index)
            for row in rows
        )
        + "\n",
        encoding="utf-8",
    )
    adapter, _ = _adapter(tmp_path)

    result = adapter.validate(
        InputCandidate("partector", (source,), 0.7, "synthetic")
    )

    assert result.detection_status is DetectionStatus.FAILED
    assert any("Expected 8 Partector Pro size channels" in error for error in result.errors)


def test_quicklook_uses_preserved_pipeline_and_time_filter(tmp_path: Path):
    source = _partector_csv(tmp_path / "partector.csv")
    adapter, logger = _adapter(tmp_path)
    progress = []
    adapter.report_progress(progress.append)
    loaded = adapter.load(
        InputCandidate("partector", (source,), 1.0, "synthetic")
    )

    result = adapter.process_quicklook(
        loaded,
        {
            "analysis_start": "2026-07-26T10:00:10",
            "analysis_end": "2026-07-26T10:00:40",
            "session": "all",
        },
    )

    assert result.processing_status is ProcessingStatus.COMPLETE
    assert result.original_start_time == datetime(2026, 7, 26, 10, 0, 10)
    assert result.original_end_time == datetime(2026, 7, 26, 10, 0, 40)
    summary = result.metadata["summary"]
    assert summary["selected_rows"] == 31
    assert summary["selected_independent_rows"] == 31
    assert summary["selected_valid_rows"] == 31
    assert summary["selected_logger_replication_rows"] == 0
    assert summary["selected_qc_pass_fraction"] == pytest.approx(1.0)
    assert summary["qc_fraction_denominator"] == "independent instrument records"
    assert result.metadata["summary"]["integration_note"].startswith(
        "The eight channels are dN/dlog10(D)"
    )
    assert result.metadata["size_channel_centers_nm"] == list(SIZE_CENTERS)
    assert progress[-1].progress == 100
    assert any(
        record.component == "partector-adapter" for record in logger.records()
    )


def test_existing_plots_and_exports_are_output_isolated(tmp_path: Path):
    source = _partector_csv(tmp_path / "partector.csv")
    adapter, _ = _adapter(tmp_path)
    result = adapter.process_quicklook(
        adapter.load(
            InputCandidate("partector", (source,), 1.0, "synthetic")
        ),
        {"session": "longest"},
    )

    figures = adapter.create_plots(result, adapter.output_root)
    outputs = adapter.export_results(
        result, adapter.output_root, ("csv", "json", "md")
    )

    assert {figure.path.name for figure in figures} == {
        "partector_quicklook.png", "partector_quicklook.pdf"
    }
    assert {output.path.name for output in outputs} == {
        "sessions.csv",
        "qc_records.csv",
        "cleaned_valid_records.csv",
        "summary.json",
        "README.md",
    }
    assert all(
        artifact.path.resolve().is_relative_to(adapter.output_root.resolve())
        for artifact in (*figures, *outputs)
    )
    with pytest.raises(FileExistsError, match="not overwritten"):
        adapter.create_plots(result, adapter.output_root)
    with pytest.raises(ValueError, match="selected output"):
        adapter.export_results(result, tmp_path / "outside", ("csv",))


def test_cancellation_and_raw_immutability(tmp_path: Path):
    source = _partector_csv(tmp_path / "partector.csv")
    original = source.read_bytes()
    adapter, _ = _adapter(tmp_path)
    adapter.cancel()

    with pytest.raises(RuntimeError, match="cancelled"):
        adapter.load(
            InputCandidate("partector", (source,), 1.0, "synthetic")
        )
    assert source.read_bytes() == original


def test_multiple_candidates_require_confirmation(tmp_path: Path):
    first = _partector_csv(tmp_path / "partector_a.csv")
    second = _partector_csv(tmp_path / "partector_b.csv")
    adapter, _ = _adapter(tmp_path)

    result = adapter.validate(
        InputCandidate("partector", (first, second), 0.8, "ambiguous")
    )

    assert result.detection_status is DetectionStatus.FAILED
    assert any("confirmation" in error for error in result.errors)


def test_out_of_order_timestamp_is_sorted_documented_and_processed(tmp_path: Path):
    source = _partector_csv(tmp_path / "partector_out_of_order.csv")
    rows = source.read_text(encoding="utf-8").splitlines()
    values = rows[31].split(",")
    values[0] = "2026-07-26T10:00:05Z"
    rows[31] = ",".join(values)
    source.write_text("\n".join(rows) + "\n", encoding="utf-8")
    original = source.read_bytes()
    adapter, _ = _adapter(tmp_path)

    result = adapter.process_quicklook(
        adapter.load(InputCandidate("partector", (source,), 1.0, "synthetic")),
        {"session": "all"},
    )

    integrity = result.metadata["source_integrity"]
    assert result.processing_status is ProcessingStatus.WARNING
    assert integrity["out_of_order_transitions"] == 1
    assert integrity["sorting"] == "stable chronological sort before session and QC analysis"
    assert adapter._selected["_time"].is_monotonic_increasing
    assert any("stable chronological sort" in warning for warning in result.warnings)
    assert source.read_bytes() == original