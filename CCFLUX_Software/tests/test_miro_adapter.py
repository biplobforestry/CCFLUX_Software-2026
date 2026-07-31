from datetime import datetime, timedelta
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from core.detector import InputCandidate
from core.enums import DetectionStatus, ProcessingStatus
from core.logging_manager import ProcessingLogManager
from core.scanner import ScanEntry, ScanIndex
from instruments.miro import MiroAdapter
from instruments.miro.adapter import _operator_facing_miro_warnings


GASES = (
    "CO wet", "N2O wet", "H2O wet", "NO wet", "NO2 wet",
    "CH4 wet", "SO2 wet", "NH3 wet", "O3 wet", "CO2 wet",
)


def _miro_file(path: Path, rows: int = 400, *, valve: bool = True) -> Path:
    columns = ["t-stamp", *GASES]
    if valve:
        columns.append("VValve 0")
    lines = [";".join(columns)]
    start = datetime(2026, 7, 26, 10)
    for index in range(rows):
        timestamp = start + timedelta(seconds=index)
        values = [timestamp.strftime("%d.%m.%Y %H:%M:%S,%f")]
        values.extend(
            f"{(1e-6 + gas_index * 1e-7 + index * 1e-10):.10f}".replace(".", ",")
            for gas_index, _ in enumerate(GASES)
        )
        if valve:
            values.append("0")
        lines.append(";".join(values))
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def _adapter(tmp_path: Path):
    logger = ProcessingLogManager(tmp_path / "logs" / "processing.jsonl")
    adapter = MiroAdapter(
        output_root=tmp_path / "output",
        flight_name="20260726_100000",
        logger=logger,
    )
    return adapter, logger


def test_detection_validation_and_native_timestamp_range(tmp_path: Path):
    source = _miro_file(tmp_path / "miro.txt")
    adapter, _ = _adapter(tmp_path)
    candidate = adapter.detect(
        ScanIndex(tmp_path, (ScanEntry(source, source.stat().st_size, True),))
    )[0]

    result = adapter.validate(candidate)

    assert result.detection_status is DetectionStatus.READY
    assert result.original_start_time == datetime(2026, 7, 26, 10)
    assert result.original_end_time > result.original_start_time
    assert result.utc_start_time is not None
    assert result.utc_start_time.utcoffset().total_seconds() == 0
    assert result.metadata["has_valve_column"]
    assert set(result.metadata["gases"]) == set(GASES)


def test_required_columns_are_validated(tmp_path: Path):
    source = tmp_path / "invalid.txt"
    source.write_text("wrong;column\n1;2\n", encoding="utf-8")
    adapter, _ = _adapter(tmp_path)

    result = adapter.validate(
        InputCandidate("miro", (source,), 0.5, "synthetic")
    )

    assert result.detection_status is DetectionStatus.FAILED
    assert any("t-stamp" in error for error in result.errors)
    assert any("trace-gas" in error for error in result.errors)


def test_quicklook_uses_legacy_analysis_filter_progress_and_log(tmp_path: Path):
    source = _miro_file(tmp_path / "miro.txt")
    adapter, logger = _adapter(tmp_path)
    progress = []
    adapter.report_progress(progress.append)
    loaded = adapter.load(InputCandidate("miro", (source,), 1.0, "synthetic"))

    result = adapter.process_quicklook(
        loaded,
        {
            "gas": "NO2 wet",
            "smooth_seconds": 60,
            "analysis_start": "2026-07-26T10:01:00",
            "analysis_end": "2026-07-26T10:05:00",
        },
    )

    assert result.processing_status in {
        ProcessingStatus.COMPLETE, ProcessingStatus.WARNING
    }
    assert result.original_start_time >= datetime(2026, 7, 26, 10, 1)
    assert result.original_end_time <= datetime(2026, 7, 26, 10, 5)
    assert result.metadata["analysis"]["correction_mode"].startswith(
        "MIRO internal background correction"
    )
    assert result.metadata["analysis"]["diagnostic_allan"]["segmented_residual"]
    assert progress[-1].progress == 100
    assert any(record.component == "miro-adapter" for record in logger.records())


def test_existing_plot_and_export_are_output_isolated(tmp_path: Path):
    source = _miro_file(tmp_path / "miro.txt")
    adapter, _ = _adapter(tmp_path)
    result = adapter.process_quicklook(
        adapter.load(InputCandidate("miro", (source,), 1.0, "synthetic")),
        {"gas": "NO2 wet", "smooth_seconds": 60, "dpi": 72},
    )

    plots = adapter.create_plots(result, adapter.output_root)
    outputs = adapter.export_results(result, adapter.output_root, ("png",))

    assert plots[0].path.is_file()
    assert len(outputs) == len(GASES)
    assert all(
        item.path.resolve().is_relative_to(adapter.output_root.resolve())
        for item in outputs
    )
    with pytest.raises(ValueError, match="selected output"):
        adapter.create_plots(result, tmp_path / "outside")


def test_cancellation_and_raw_file_immutability(tmp_path: Path):
    source = _miro_file(tmp_path / "miro.txt")
    original = source.read_bytes()
    adapter, _ = _adapter(tmp_path)
    candidate = InputCandidate("miro", (source,), 1.0, "synthetic")
    adapter.cancel()

    with pytest.raises(RuntimeError, match="cancelled"):
        adapter.load(candidate)
    assert source.read_bytes() == original

def test_zero_air_nan_values_are_reported_as_warnings(tmp_path: Path):
    """Confirmed as intended behaviour by the campaign owner.

    An earlier expectation held that ambient NaNs coinciding with an open
    zero-air valve should be demoted to informational notes. They are reported
    as warnings, and that is correct: an excluded ambient value is excluded
    whatever caused it, and the operator is told how many.
    """
    adapter, _ = _adapter(tmp_path)
    module = adapter.bridge.miro
    timestamps = pd.date_range("2026-07-26T10:00:00", periods=120, freq="1s")
    valve = np.zeros(120)
    valve[43:62] = 1
    values = np.linspace(1.0e-6, 1.2e-6, 120)
    values[40:63] = np.nan
    frame = pd.DataFrame({
        "timestamp": timestamps,
        "NO2 wet": values,
        "VValve 0": valve,
    })

    result = module.analyze(frame, "NO2 wet", 30.0, remove_seconds=0.0)

    excluded = [
        item for item in result["warnings"] if "NaN or infinite ambient" in item
    ]
    assert excluded, "the operator must be told that ambient values were excluded"
    # Only the four NaNs outside the valve window are ambient exclusions; the
    # rows inside it are zero-air samples, not missing ambient data.
    assert "4 NaN" in excluded[0]
    assert result["series"]["ambient"]

def test_operator_gap_warning_requires_more_than_four_minutes():
    scientific_warning = (
        "16 missing-sample time gaps longer than 1.53 s split the "
        "segmented analyses; no interpolation was applied."
    )
    short = {
        "series": {
            "time": pd.to_datetime([
                "2026-07-26T10:00:00",
                "2026-07-26T10:00:02",
                "2026-07-26T10:04:02",
            ])
        }
    }
    long = {
        "series": {
            "time": pd.to_datetime([
                "2026-07-26T10:00:00",
                "2026-07-26T10:04:01",
            ])
        }
    }

    assert _operator_facing_miro_warnings([scientific_warning], short) == []
    warning = _operator_facing_miro_warnings([scientific_warning], long)

    assert len(warning) == 1
    assert "longer than 4 minutes" in warning[0]
    assert "1.53 s" not in warning[0]
