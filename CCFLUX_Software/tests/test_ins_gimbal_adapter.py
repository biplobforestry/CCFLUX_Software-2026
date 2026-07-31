import json
from datetime import datetime, timedelta
from pathlib import Path

from core.detector import InputCandidate
from core.enums import DetectionStatus, ProcessingStatus
from core.scanner import ScanEntry, ScanIndex
from instruments.ins_gimbal import InsGimbalAdapter


def _gimbal(path: Path, rows: int = 80) -> Path:
    columns = [
        "_time",
        "gimbal_acc_x_counts", "gimbal_acc_y_counts", "gimbal_acc_z_counts",
        "gimbal_gyro_x_counts", "gimbal_gyro_y_counts", "gimbal_gyro_z_counts",
        "gimbal_pitch_deg", "gimbal_roll_deg", "gimbal_yaw_deg",
    ]
    start = datetime(2026, 7, 26, 10)
    lines = [",".join(columns)]
    for index in range(rows):
        values = [
            (start + timedelta(seconds=index)).isoformat() + "Z",
            str(index % 5), str((index + 1) % 5), str(8192 + index % 3),
            str(index % 7), str(index % 9), str(index % 11),
            "0", "0", str(index % 360),
        ]
        lines.append(",".join(values))
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def test_ins_gimbal_detection_validation_and_quicklook(tmp_path: Path):
    source = _gimbal(tmp_path / "Gremsy_T3V3_Gimbal.csv")
    adapter = InsGimbalAdapter(
        output_root=tmp_path / "output", flight_name="flight"
    )
    candidate = adapter.detect(ScanIndex(
        tmp_path, (ScanEntry(source, source.stat().st_size, True),)
    ))[0]
    validation = adapter.validate(candidate)
    result = adapter.process_quicklook(
        adapter.load(candidate),
        {"rms_seconds": 5, "analysis_start": "2026-07-26T10:00:10",
         "analysis_end": "2026-07-26T10:01:00"},
    )

    assert validation.detection_status is DetectionStatus.READY
    assert validation.utc_start_time is not None
    assert validation.utc_start_time.utcoffset().total_seconds() == 0
    assert result.instrument_id == "ins_gimbal"
    assert result.original_start_time == datetime(2026, 7, 26, 10, 0, 10)
    assert result.metadata["summary"]["dataset"]["signal_filter"] == "none"
    assert result.processing_status is ProcessingStatus.COMPLETE
    assert result.warnings == []

    browser = adapter.export_browser_data(result, adapter.output_root)
    payload = json.loads(browser.path.read_text(encoding="utf-8"))
    assert payload["schema"] == "ccflux-ins-gimbal-browser-v1"
    assert payload["time_basis"] == "UTC as recorded by the CC-FLUX campaign instrument"
    assert payload["series"]["acc_norm_g"]
    assert payload["asd"]["acceleration"]["frequency_hz"]
    assert payload["summary"]["dataset"]["signal_filter"] == "none"


def test_ins_gimbal_missing_imu_columns_fails(tmp_path: Path):
    source = tmp_path / "Gremsy_T3V3_Gimbal.csv"
    source.write_text("_time,gimbal_acc_x_counts\n2026-07-26T10:00:00Z,1\n")
    adapter = InsGimbalAdapter(
        output_root=tmp_path / "output", flight_name="flight"
    )
    result = adapter.validate(
        InputCandidate("ins_gimbal", (source,), 0.4, "synthetic")
    )
    assert result.detection_status is DetectionStatus.FAILED
