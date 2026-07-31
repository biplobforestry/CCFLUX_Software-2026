from datetime import datetime, timezone
from pathlib import Path

import pytest
from PIL import Image

from core.time_extraction import TimestampExtractor
from core.time_manager import TimestampQualityFlag


def _write(path: Path, text: str) -> Path:
    path.write_text(text, encoding="utf-8")
    return path


def _opc(path: Path, *timestamps: str) -> Path:
    rows = "\n".join(f"{value},1" for value in timestamps)
    return _write(path, f"_time,value\n{rows}\n")


def _gopro_image(path: Path, timestamp: datetime) -> Path:
    exif = Image.Exif()
    exif[36867] = timestamp.strftime("%Y:%m:%d %H:%M:%S")
    Image.new("RGB", (16, 12), (30, 90, 150)).save(path, exif=exif)
    return path


def test_valid_noseboom_epoch_timestamps_preserve_original_values(tmp_path: Path):
    source = _write(
        tmp_path / "noseboom.csv",
        "Airflow_UTCcorr_Nanoseconds_ns,value\n"
        "1785098400000000000,1\n"
        "1785098460000000000,2\n",
    )

    result = TimestampExtractor().extract_instrument("noseboom", [source])

    assert result.valid_timestamps == 2
    assert result.original_min_timestamp == "1785098400000000000"
    assert result.original_max_timestamp == "1785098460000000000"
    assert result.utc_start_time == datetime.fromtimestamp(
        1785098400, tz=timezone.utc
    )
    assert result.timestamp_format == "unix_epoch_nanoseconds"
    assert result.applied_time_offset.total_seconds() == 0
    assert result.quality_samples[0].original_timestamp == "1785098400000000000"
    assert TimestampQualityFlag.VALID in result.quality_samples[0].quality_flags


def test_gopro_summer_camera_time_is_corrected_during_detection(tmp_path: Path):
    source = _gopro_image(
        tmp_path / "GOPR0001.jpg", datetime(2026, 7, 26, 12, 0, 0)
    )

    result = TimestampExtractor().extract_instrument("gopro", [source])

    assert result.original_min_timestamp == "2026:07:26 12:00:00"
    assert result.utc_start_time == datetime(
        2026, 7, 26, 10, 0, 0, tzinfo=timezone.utc
    )
    assert result.applied_time_offset.total_seconds() == -2 * 3600
    assert result.timezone_information == "Europe/Berlin camera clock → UTC"
    assert not any(
        "excluded from global UTC" in warning
        for warning in result.timestamp_quality_warnings
    )


def test_gopro_winter_camera_time_uses_cet_offset(tmp_path: Path):
    source = _gopro_image(
        tmp_path / "GOPR0002.jpg", datetime(2026, 1, 15, 12, 0, 0)
    )

    result = TimestampExtractor().extract_instrument("gopro", [source])

    assert result.utc_start_time == datetime(
        2026, 1, 15, 11, 0, 0, tzinfo=timezone.utc
    )
    assert result.applied_time_offset.total_seconds() == -3600


def test_flir_json_edge_timestamps_are_confirmed_as_utc(tmp_path: Path):
    source = _write(
        tmp_path / "camera.FLIR_Zeppelin.json",
        '[{"timestamp":{"$date":"2026-07-27 05:24:17.125"},"raw":[[1]]},'
        '{"timestamp":"2026-07-27 10:28:26.875","raw":[[2]]}]',
    )

    result = TimestampExtractor().extract_instrument("flir", [source])

    assert result.valid_timestamps == 2
    assert result.utc_start_time == datetime(
        2026, 7, 27, 5, 24, 17, 125000, tzinfo=timezone.utc
    )
    assert result.utc_end_time == datetime(
        2026, 7, 27, 10, 28, 26, 875000, tzinfo=timezone.utc
    )
    assert result.timezone_information == "UTC (instrument field semantics)"


def test_mixed_timestamp_formats_are_reported(tmp_path: Path):
    source = _write(
        tmp_path / "sif.csv",
        "datetime [UTC];value\n"
        "2026-07-26T10:00:00Z;1\n"
        "26.07.2026 10:01:00,000;2\n",
    )

    result = TimestampExtractor().extract_instrument("sif", [source])

    assert result.valid_timestamps == 2
    assert result.timestamp_format is not None
    assert result.timestamp_format.startswith("mixed:")
    assert result.utc_start_time == datetime(2026, 7, 26, 10, 0, tzinfo=timezone.utc)
    assert result.utc_end_time == datetime(2026, 7, 26, 10, 1, tzinfo=timezone.utc)


def test_raw_airflox_uses_every_numbered_spectral_cycle(tmp_path: Path):
    source = _write(
        tmp_path / "F071941.CSV",
        "1;260727;072033;manual_mode;IT_WR[us]=;1000000\n"
        "WR;1;2;3\n"
        "VEG;1;2;3\n"
        "2;260727;072108;manual_mode;IT_WR[us]=;1000000\n"
        "WR;1;2;3\n"
        "381;260727;122502;manual_mode;IT_WR[us]=;1000000\n"
        "WR;1;2;3\n",
    )

    result = TimestampExtractor().extract_instrument("sif", [source])

    assert result.valid_timestamps == 3
    assert result.timestamp_columns == (
        "cycle_id",
        "record_date",
        "record_time",
    )
    assert result.utc_start_time == datetime(
        2026, 7, 27, 7, 20, 33, tzinfo=timezone.utc
    )
    assert result.utc_end_time == datetime(
        2026, 7, 27, 12, 25, 2, tzinfo=timezone.utc
    )


def test_duplicate_timestamps_are_counted_and_flagged(tmp_path: Path):
    source = _opc(
        tmp_path / "opc.csv",
        "2026-07-26T10:00:00Z",
        "2026-07-26T10:00:00Z",
    )

    result = TimestampExtractor().extract_instrument("opc_hbx4", [source])

    assert result.duplicated_timestamps == 1
    assert TimestampQualityFlag.DUPLICATE in result.quality_samples[1].quality_flags


def test_gimbal_rotated_export_tail_is_normalized_for_quality_scan(tmp_path: Path):
    source = _write(
        tmp_path / "Gremsy_T3V3_Gimbal.csv",
        "_time,gimbal_acc_x_counts\n"
        "2026-07-26T10:00:03Z,1\n"
        "2026-07-26T10:00:04Z,1\n"
        "2026-07-26T10:00:00Z,1\n"
        "2026-07-26T10:00:01Z,1\n"
        "2026-07-26T10:00:02Z,1\n",
    )

    result = TimestampExtractor().extract_instrument("ins_gimbal", [source])

    assert result.non_monotonic_timestamps == 0
    assert result.utc_start_time == datetime(2026, 7, 26, 10, tzinfo=timezone.utc)
    assert result.utc_end_time == datetime(
        2026, 7, 26, 10, 0, 4, tzinfo=timezone.utc
    )


def test_all_matching_miro_segments_contribute_to_utc_coverage(tmp_path: Path):
    early = _write(
        tmp_path / "early.txt",
        "t-stamp;CO wet\n26.07.2026 10:00:00,000;1\n",
    )
    late = _write(
        tmp_path / "late.txt",
        "t-stamp;CO wet\n26.07.2026 11:30:00,000;2\n",
    )

    result = TimestampExtractor().extract_instrument("miro", [late, early])

    assert result.utc_start_time == datetime(
        2026, 7, 26, 10, 0, tzinfo=timezone.utc
    )
    assert result.utc_end_time == datetime(
        2026, 7, 26, 11, 30, tzinfo=timezone.utc
    )
    assert result.timezone_information.startswith("UTC")


def test_picarro_campaign_date_and_time_are_interpreted_as_utc(tmp_path: Path):
    source = _write(
        tmp_path / "picarro.dat",
        "DATE TIME CO2_sync\n"
        "2026-07-26 10:00:00.000 410.0\n"
        "2026-07-26 10:05:00.000 411.0\n",
    )

    result = TimestampExtractor().extract_instrument("picarro", [source])

    assert result.utc_start_time == datetime(
        2026, 7, 26, 10, 0, tzinfo=timezone.utc
    )
    assert result.utc_end_time == datetime(
        2026, 7, 26, 10, 5, tzinfo=timezone.utc
    )
    assert not any(
        "timezone is unknown" in warning
        for warning in result.timestamp_quality_warnings
    )


def test_invalid_and_missing_timestamps_are_counted(tmp_path: Path):
    source = _opc(
        tmp_path / "opc.csv",
        "2026-07-26T10:00:00Z",
        "not-a-time",
        "",
    )

    result = TimestampExtractor().extract_instrument("opc_hbx5", [source])

    assert result.valid_timestamps == 1
    assert result.invalid_timestamps == 1
    assert result.missing_timestamps == 1
    assert TimestampQualityFlag.INVALID in result.quality_samples[1].quality_flags
    assert TimestampQualityFlag.MISSING in result.quality_samples[2].quality_flags


def test_non_monotonic_timestamps_are_counted_and_flagged(tmp_path: Path):
    source = _opc(
        tmp_path / "opc.csv",
        "2026-07-26T10:01:00Z",
        "2026-07-26T10:00:00Z",
    )

    result = TimestampExtractor().extract_instrument("partector", [source])

    assert result.non_monotonic_timestamps == 1
    assert (
        TimestampQualityFlag.NON_MONOTONIC
        in result.quality_samples[1].quality_flags
    )


def test_missing_timestamp_column_is_reported(tmp_path: Path):
    source = _write(tmp_path / "opc.csv", "other,value\nx,1\n")

    result = TimestampExtractor().extract_instrument("opc_hbx4", [source])

    assert not result.has_valid_timestamps
    assert result.missing_timestamp_columns == (source,)
    assert any("missing timestamp column" in warning for warning in result.timestamp_quality_warnings)


def test_global_minimum_maximum_and_common_overlap(tmp_path: Path):
    hbx4 = _opc(
        tmp_path / "hbx4.csv",
        "2026-07-26T10:00:00Z",
        "2026-07-26T10:10:00Z",
    )
    hbx5 = _opc(
        tmp_path / "hbx5.csv",
        "2026-07-26T10:05:00Z",
        "2026-07-26T10:20:00Z",
    )

    result = TimestampExtractor().extract_all(
        {"opc_hbx4": [hbx4], "opc_hbx5": [hbx5]}
    )

    assert result.global_earliest_timestamp == datetime(
        2026, 7, 26, 10, 0, tzinfo=timezone.utc
    )
    assert result.global_latest_timestamp == datetime(
        2026, 7, 26, 10, 20, tzinfo=timezone.utc
    )
    assert result.common_overlap_exists
    assert result.common_overlap_start == datetime(
        2026, 7, 26, 10, 5, tzinfo=timezone.utc
    )
    assert result.common_overlap_end == datetime(
        2026, 7, 26, 10, 10, tzinfo=timezone.utc
    )


def test_no_common_overlap(tmp_path: Path):
    hbx4 = _opc(
        tmp_path / "hbx4.csv",
        "2026-07-26T10:00:00Z",
        "2026-07-26T10:05:00Z",
    )
    hbx5 = _opc(
        tmp_path / "hbx5.csv",
        "2026-07-26T11:00:00Z",
        "2026-07-26T11:05:00Z",
    )

    result = TimestampExtractor().extract_all(
        {"opc_hbx4": [hbx4], "opc_hbx5": [hbx5]}
    )

    assert not result.common_overlap_exists
    assert result.common_overlap_start is None
    assert result.common_overlap_end is None
    assert any("no common UTC overlap" in warning for warning in result.warnings)


def test_miro_campaign_time_is_assumed_to_be_utc(tmp_path: Path):
    source = _write(
        tmp_path / "miro.txt",
        "t-stamp;value\n26.07.2026 10:00:00,000;1\n",
    )

    result = TimestampExtractor().extract_all({"miro": [source]})

    assert result.instrument_ranges["miro"].has_valid_timestamps
    assert result.instrument_ranges["miro"].has_utc_range
    assert result.instruments_without_utc_range == ()
    assert result.global_earliest_timestamp == datetime(
        2026, 7, 26, 10, 0, tzinfo=timezone.utc
    )


def test_expected_interval_and_no_valid_instrument_reporting(tmp_path: Path):
    outside = _opc(
        tmp_path / "outside.csv",
        "2026-07-26T12:00:00Z",
        "2026-07-26T12:05:00Z",
    )
    missing = _write(tmp_path / "missing.csv", "value\n1\n")

    result = TimestampExtractor().extract_all(
        {"opc_hbx4": [outside], "opc_hbx5": [missing]},
        expected_flight_start=datetime(2026, 7, 26, 10, 0, tzinfo=timezone.utc),
        expected_flight_end=datetime(2026, 7, 26, 11, 0, tzinfo=timezone.utc),
    )

    assert result.instruments_outside_expected_interval == ("opc_hbx4",)
    assert result.instruments_with_no_valid_timestamps == ("opc_hbx5",)


def test_expected_interval_must_be_complete_and_timezone_aware():
    extractor = TimestampExtractor()
    with pytest.raises(ValueError, match="provided together"):
        extractor.extract_all(
            {},
            expected_flight_start=datetime(2026, 7, 26, tzinfo=timezone.utc),
        )
    with pytest.raises(ValueError, match="timezone-aware"):
        extractor.extract_all(
            {},
            expected_flight_start=datetime(2026, 7, 26),
            expected_flight_end=datetime(2026, 7, 27),
        )
