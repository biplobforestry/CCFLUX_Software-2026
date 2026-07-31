from datetime import datetime, timedelta
from pathlib import Path

import pytest

from core.detector import InputCandidate
from core.enums import DetectionStatus, ProcessingStatus
from core.logging_manager import ProcessingLogManager
from core.scanner import ScanEntry, ScanIndex
from instruments.opc_hbx4 import OpcHbx4Adapter
from instruments.opc_hbx5 import OpcHbx5Adapter


def _opc_csv(path: Path, sensor: str, rows: int = 40) -> Path:
    suffix = "X4" if sensor == "hbx4" else "X5"
    fields = {
        "pm1": "opc_pm1_1" if sensor == "hbx4" else "opc_pm1_in_1",
        "pm25": "opc_pm2.5_1" if sensor == "hbx4" else "opc_pm2.5_in_1",
        "pm10": "opc_pm10_1" if sensor == "hbx4" else "opc_pm10_in_1",
        "temperature": (
            "opc_temperature_degC"
            if sensor == "hbx4" else "opc_inlet_temp_degC"
        ),
        "rh": "RH_OPC_%" if sensor == "hbx4" else "RH_OPC_in_%",
    }
    columns = [
        "_time",
        *(f"Bin{index}_{suffix}_1" for index in range(24)),
        f"SFR_{suffix}_1",
        f"SP_{suffix}_1",
        f"RejectGlitch_{suffix}_1",
        f"RejectRatio_{suffix}_1",
        f"Laser_status_{suffix}_1",
        *fields.values(),
    ]
    lines = [",".join(columns)]
    start = datetime(2026, 7, 26, 10)
    for index in range(rows):
        record = {
            "_time": (start + timedelta(seconds=index)).isoformat() + "Z",
            **{f"Bin{number}_{suffix}_1": str(0.1 + number * 0.01) for number in range(24)},
            f"SFR_{suffix}_1": "2.0",
            f"SP_{suffix}_1": "1.0",
            f"RejectGlitch_{suffix}_1": "0",
            f"RejectRatio_{suffix}_1": "0",
            f"Laser_status_{suffix}_1": "1",
            fields["pm1"]: "1.0",
            fields["pm25"]: "2.0",
            fields["pm10"]: "3.0",
            fields["temperature"]: "20.0",
            fields["rh"]: "50.0",
        }
        lines.append(",".join(record[column] for column in columns))
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def _adapter(adapter_type, tmp_path: Path):
    return adapter_type(
        output_root=tmp_path / "output",
        flight_name="20260726_100000",
        logger=ProcessingLogManager(tmp_path / "logs" / "processing.jsonl"),
    )


@pytest.mark.parametrize(
    ("adapter_type", "sensor", "instrument_id"),
    (
        (OpcHbx4Adapter, "hbx4", "opc_hbx4"),
        (OpcHbx5Adapter, "hbx5", "opc_hbx5"),
    ),
)
def test_independent_detection_validation_and_timestamp(
    tmp_path: Path, adapter_type, sensor: str, instrument_id: str
):
    source = _opc_csv(tmp_path / f"{instrument_id}.csv", sensor)
    adapter = _adapter(adapter_type, tmp_path)
    candidate = adapter.detect(
        ScanIndex(tmp_path, (ScanEntry(source, source.stat().st_size, True),))
    )[0]

    result = adapter.validate(candidate)

    assert candidate.instrument_id == instrument_id
    assert result.instrument_id == instrument_id
    assert result.detection_status is DetectionStatus.READY
    assert result.original_start_time == datetime(2026, 7, 26, 10)
    assert result.utc_start_time is not None
    assert result.utc_start_time.utcoffset().total_seconds() == 0
    assert result.file_count == 1


def test_hbx4_and_hbx5_do_not_cross_detect(tmp_path: Path):
    hbx4 = _opc_csv(tmp_path / "OPC_HBX4.csv", "hbx4")
    hbx5 = _opc_csv(tmp_path / "OPC_HBX5.csv", "hbx5")
    index = ScanIndex(
        tmp_path,
        (
            ScanEntry(hbx4, hbx4.stat().st_size, True),
            ScanEntry(hbx5, hbx5.stat().st_size, True),
        ),
    )

    candidate4 = _adapter(OpcHbx4Adapter, tmp_path).detect(index)[0]
    candidate5 = _adapter(OpcHbx5Adapter, tmp_path).detect(index)[0]

    assert candidate4.paths == (hbx4,)
    assert candidate5.paths == (hbx5,)


@pytest.mark.parametrize(
    ("adapter_type", "sensor", "instrument_id"),
    (
        (OpcHbx4Adapter, "hbx4", "opc_hbx4"),
        (OpcHbx5Adapter, "hbx5", "opc_hbx5"),
    ),
)
def test_quicklook_filter_plot_export_progress_and_logging(
    tmp_path: Path, adapter_type, sensor: str, instrument_id: str
):
    source = _opc_csv(tmp_path / f"{instrument_id}.csv", sensor)
    original = source.read_bytes()
    adapter = _adapter(adapter_type, tmp_path)
    progress = []
    adapter.report_progress(progress.append)
    candidate = InputCandidate(instrument_id, (source,), 1.0, "synthetic")

    result = adapter.process_quicklook(
        adapter.load(candidate),
        {
            "bin_units": "number_cm3",
            "analysis_start": "2026-07-26T10:00:05",
            "analysis_end": "2026-07-26T10:00:25",
        },
    )
    figures = adapter.create_plots(result, adapter.output_root)
    outputs = adapter.export_results(
        result, adapter.output_root, ("csv", "json")
    )

    assert result.processing_status is ProcessingStatus.COMPLETE
    assert result.original_start_time == datetime(2026, 7, 26, 10, 0, 5)
    assert result.original_end_time == datetime(2026, 7, 26, 10, 0, 25)
    assert result.metadata["sensor"] == ("HBX-4" if sensor == "hbx4" else "HBX-5")
    assert result.metadata["selected_rows"] == 21
    assert figures[0].path.name == f"{instrument_id}_quicklook.png"
    assert {item.path.name for item in outputs} == {
        f"{instrument_id}_evaluated.csv",
        f"{instrument_id}_summary.json",
    }
    assert progress[-1].instrument_id == instrument_id
    assert progress[-1].progress == 100
    assert source.read_bytes() == original
    assert any(
        record.instrument == instrument_id
        for record in adapter.logger.records()
    )


def test_one_opc_validation_failure_does_not_change_other_identity(tmp_path: Path):
    broken = tmp_path / "OPC_HBX4.csv"
    broken.write_text("_time,Bin0_X4_1\n2026-07-26T10:00:00Z,1\n")
    valid = _opc_csv(tmp_path / "OPC_HBX5.csv", "hbx5")

    hbx4_result = _adapter(OpcHbx4Adapter, tmp_path).validate(
        InputCandidate("opc_hbx4", (broken,), 0.5, "synthetic")
    )
    hbx5_result = _adapter(OpcHbx5Adapter, tmp_path).validate(
        InputCandidate("opc_hbx5", (valid,), 1.0, "synthetic")
    )

    assert hbx4_result.detection_status is DetectionStatus.FAILED
    assert hbx5_result.detection_status is DetectionStatus.READY
    assert hbx5_result.instrument_id == "opc_hbx5"


def test_multiple_candidates_require_confirmation(tmp_path: Path):
    first = _opc_csv(tmp_path / "OPC_HBX4_a.csv", "hbx4")
    second = _opc_csv(tmp_path / "OPC_HBX4_b.csv", "hbx4")
    adapter = _adapter(OpcHbx4Adapter, tmp_path)

    result = adapter.validate(
        InputCandidate("opc_hbx4", (first, second), 0.8, "ambiguous")
    )

    assert result.detection_status is DetectionStatus.FAILED
    assert any("confirmation" in error for error in result.errors)
    with pytest.raises(ValueError, match="confirmation"):
        adapter.load(InputCandidate("opc_hbx4", (first, second), 0.8, "ambiguous"))


def test_invalid_timestamp_is_quarantined_without_failing_valid_rows(
    tmp_path: Path,
):
    source = _opc_csv(tmp_path / "OPC_HBX5.csv", "hbx5", rows=12)
    rows = source.read_text(encoding="utf-8").splitlines()
    invalid = rows[-1].split(",")
    invalid[0] = "not-a-timestamp"
    source.write_text(
        "\n".join([*rows, ",".join(invalid)]) + "\n",
        encoding="utf-8",
    )
    original = source.read_bytes()
    adapter = _adapter(OpcHbx5Adapter, tmp_path)

    result = adapter.process_quicklook(
        adapter.load(InputCandidate("opc_hbx5", (source,), 1.0, "synthetic")),
        {"bin_units": "number_cm3"},
    )

    integrity = result.metadata["source_integrity"]
    quarantine = adapter.output_root / "opc_hbx5_quarantined_rows.csv"
    assert result.processing_status is ProcessingStatus.WARNING
    assert integrity["source_rows"] == 13
    assert integrity["valid_timestamp_rows"] == 12
    assert integrity["invalid_timestamp_rows"] == 1
    assert quarantine.is_file()
    assert "not-a-timestamp" in quarantine.read_text(encoding="utf-8")
    assert source.read_bytes() == original