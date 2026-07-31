from datetime import datetime, timedelta, timezone
from pathlib import Path

import pandas as pd
import pytest

from core.detector import InputCandidate
from core.enums import DetectionStatus, ProcessingStatus
from core.logging_manager import ProcessingLogManager
from core.scanner import ScanEntry, ScanIndex
from instruments.noseboom import NoseboomAdapter


def _noseboom_csv(path: Path, rows: int = 240) -> Path:
    start = datetime(2026, 7, 26, 10, tzinfo=timezone.utc)
    records = []
    for index in range(rows):
        timestamp = start + timedelta(milliseconds=100 * index)
        records.append(
            {
                "Airflow_UTCcorr_Nanoseconds_ns": int(
                    timestamp.timestamp() * 1_000_000_000
                ),
                "TIMESTAMP": timestamp.isoformat(),
                "INS_Filter_LLHPos_Latitude_deg": 52.0 + index * 0.000001,
                "INS_Filter_LLHPos_Longitude_deg": 13.0 + index * 0.000001,
                "GNSSRecv1_LLHPos_Latitude_deg": 52.0 + index * 0.000001,
                "GNSSRecv1_LLHPos_Longitude_deg": 13.0 + index * 0.000001,
                "GNSSRecv1_LLHPos_MSLHeight_m": 100.0,
                "INS_Filter_LLHPos_ElipsoidHeight_m": 105.0,
                "GNSSRecv1_vNED_GroundSpeed_m/s": 12.0,
                "GNSSRecv1_vNED_Heading_deg": 90.0,
                "INS_Filter_EulerAngles_Roll_rad": 0.01,
                "GNSSRecv1_vNED_z_m/s": 0.0,
                "WIND_vWind_m/s": 5.0,
                "WIND_vWind_x_m/s": 4.0,
                "WIND_vWind_y_m/s": 2.0,
                "WIND_vWind_z_m/s": 0.2,
                "WIND_dir_deg": 45.0,
                "Airflow_Flow_OAT_degC": 18.0,
                "Airflow_Flow_rel_humidity_": 55.0,
                "Airflow_Sensor_pstat_hPa": 1000.0,
            }
        )
    pd.DataFrame(records).to_csv(path, index=False, encoding="utf-8-sig")
    return path


def _adapter(tmp_path: Path):
    logger = ProcessingLogManager(tmp_path / "logs" / "processing.jsonl")
    adapter = NoseboomAdapter(
        output_root=tmp_path / "output",
        flight_name="20260726_100000",
        logger=logger,
    )
    return adapter, logger


def test_detect_validate_gps_and_timestamp_coverage(tmp_path: Path):
    source = _noseboom_csv(tmp_path / "noseboom.csv")
    adapter, _ = _adapter(tmp_path)
    index = ScanIndex(
        tmp_path,
        (ScanEntry(source, source.stat().st_size, True),),
    )

    candidates = adapter.detect(index)
    metadata = adapter.inspect_metadata(candidates[0])
    validation = adapter.validate(candidates[0])

    assert len(candidates) == 1
    assert metadata["has_ins_gps"]
    assert metadata["has_gnss_gps"]
    assert validation.detection_status is DetectionStatus.READY
    assert validation.utc_start_time == datetime(
        2026, 7, 26, 10, tzinfo=timezone.utc
    )
    assert validation.utc_end_time > validation.utc_start_time


def test_missing_required_gps_columns_fails_validation(tmp_path: Path):
    source = tmp_path / "invalid.csv"
    source.write_text(
        "Airflow_UTCcorr_Nanoseconds_ns,WIND_vWind_x_m/s\n"
        "1785060000000000000,1\n",
        encoding="utf-8",
    )
    adapter, _ = _adapter(tmp_path)
    candidate = InputCandidate("noseboom", (source,), 0.5, "synthetic")

    result = adapter.validate(candidate)

    assert result.detection_status is DetectionStatus.FAILED
    assert any("GPS" in error for error in result.errors)


def test_quicklook_delegates_legacy_routines_and_obeys_time_filter(tmp_path: Path):
    source = _noseboom_csv(tmp_path / "noseboom.csv")
    adapter, logger = _adapter(tmp_path)
    progress = []
    adapter.report_progress(progress.append)
    candidate = InputCandidate("noseboom", (source,), 1.0, "synthetic")
    loaded = adapter.load(candidate)

    result = adapter.process_quicklook(
        loaded,
        {
            "analysis_start": "2026-07-26T10:00:05Z",
            "analysis_end": "2026-07-26T10:00:15Z",
            "trim_minutes": 0,
        },
    )

    assert result.processing_status is ProcessingStatus.COMPLETE
    assert result.utc_start_time >= datetime(
        2026, 7, 26, 10, 0, 5, tzinfo=timezone.utc
    )
    assert result.utc_end_time <= datetime(
        2026, 7, 26, 10, 0, 15, tzinfo=timezone.utc
    )
    assert result.metadata["one_hz_rows"] > 0
    browser = result.metadata["map"]
    assert browser["available"]
    assert browser["hist"]["wind_mps"]
    assert browser["frequency"]
    assert "spectra" in browser
    assert "straight_settings" in browser
    assert "altitude_m" in browser["points"][0]
    assert browser["altitude_profile"]
    assert browser["time_bounds"]["start"]
    assert browser["browser_limits"]["map_points"] == 6000
    assert len(browser["points"]) <= 6000
    assert progress
    assert any(
        record.component == "noseboom-adapter" for record in logger.records()
    )


def test_export_is_confined_to_selected_output_and_protected(tmp_path: Path):
    source = _noseboom_csv(tmp_path / "noseboom.csv")
    adapter, _ = _adapter(tmp_path)
    candidate = InputCandidate("noseboom", (source,), 1.0, "synthetic")
    result = adapter.process_quicklook(
        adapter.load(candidate), {"trim_minutes": 0}
    )

    outputs = adapter.export_results(
        result, adapter.output_root, ("csv",)
    )

    assert outputs[0].path.is_file()
    assert outputs[0].path.resolve().is_relative_to(
        adapter.output_root.resolve()
    )
    with pytest.raises(FileExistsError, match="not overwritten"):
        adapter.export_results(result, adapter.output_root, ("csv",))
    with pytest.raises(ValueError, match="selected output"):
        adapter.export_results(result, tmp_path / "outside", ("txt",))


def test_selected_interval_without_data_is_rejected(tmp_path: Path):
    source = _noseboom_csv(tmp_path / "noseboom.csv")
    adapter, _ = _adapter(tmp_path)
    candidate = InputCandidate("noseboom", (source,), 1.0, "synthetic")
    loaded = adapter.load(candidate)

    with pytest.raises(ValueError, match="does not intersect"):
        adapter.process_quicklook(
            loaded,
            {
                "analysis_start": "2026-07-27T10:00:00Z",
                "analysis_end": "2026-07-27T11:00:00Z",
            },
        )


def test_original_timestamp_column_is_not_modified(tmp_path: Path):
    source = _noseboom_csv(tmp_path / "noseboom.csv")
    original = source.read_bytes()
    adapter, _ = _adapter(tmp_path)
    candidate = InputCandidate("noseboom", (source,), 1.0, "synthetic")
    adapter.process_quicklook(adapter.load(candidate), {"trim_minutes": 0})
    assert source.read_bytes() == original
