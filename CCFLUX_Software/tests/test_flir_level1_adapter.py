import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from core.detector import InputCandidate
from core.enums import ProcessingStatus
from core.resource_manager import CameraBatchPolicy, ResourceLimits
from instruments.flir import FlirLevel1Adapter


CALIBRATION = {
    "R": 1, "B": 2, "F": 3, "J0": 4, "J1": 5,
    "X": 6, "alpha1": 7, "alpha2": 8, "beta1": 9, "beta2": 10,
}


def _frame(index: int, *, calibration=True, raw=True):
    timestamp = datetime(2026, 7, 26, 10, tzinfo=timezone.utc) + timedelta(
        seconds=2 * index
    )
    value = {
        "timestamp": timestamp.isoformat().replace("+00:00", "Z"),
        "raw_stats": {"min": index, "max": index + 9, "mean": index + 4.5},
    }
    if calibration:
        value["calibration"] = dict(CALIBRATION)
    if raw:
        value["raw"] = [[index + x + y for x in range(8)] for y in range(6)]
    return value


def _dataset(path: Path, frames) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(list(frames)), encoding="utf-8")
    return path


def _adapter(tmp_path: Path, *, thumbnails=3, memory=1024**2):
    return FlirLevel1Adapter(
        output_root=tmp_path / "output",
        flight_name="flight",
        resource_limits=ResourceLimits(cpu_cores=2, memory_bytes=memory),
        batch_policy=CameraBatchPolicy(
            maximum_batch_files=4,
            maximum_thumbnail_count=thumbnails,
            maximum_thumbnail_bytes=2 * 1024**2,
        ),
        unusually_small_bytes=1,
    )


def _candidate(*paths: Path) -> InputCandidate:
    return InputCandidate("flir", tuple(paths), 1.0, "synthetic")


def test_valid_flir_dataset_reports_frames_intervals_and_radiometric_metadata(
    tmp_path: Path,
):
    source = _dataset(tmp_path / "FLIR" / "camera.FLIR_Zeppelin.json", (_frame(i) for i in range(5)))
    adapter = _adapter(tmp_path)

    result = adapter.process_quicklook(adapter.load(_candidate(source)), {})

    assert result.processing_status is ProcessingStatus.COMPLETE
    assert result.metadata["frame_count"] == 5
    assert result.metadata["acquisition_intervals_seconds"] == [2.0] * 4
    assert result.metadata["radiometric_metadata_available"]
    assert not result.metadata["temperature_conversion_performed"]
    assert result.utc_start_time < result.utc_end_time


def test_missing_radiometric_metadata_is_warning(tmp_path: Path):
    source = _dataset(tmp_path / "FLIR" / "camera.json", [_frame(0, calibration=False)])
    adapter = _adapter(tmp_path)

    result = adapter.process_quicklook(adapter.load(_candidate(source)), {})

    assert result.processing_status is ProcessingStatus.WARNING
    assert not result.metadata["radiometric_metadata_available"]
    assert any("radiometric" in warning for warning in result.warnings)


def test_corrupted_sample_frame_is_isolated(tmp_path: Path):
    source = tmp_path / "FLIR" / "camera.json"
    source.parent.mkdir(parents=True)
    good = json.dumps(_frame(0))
    source.write_text(f"[{good},{{\"timestamp\":\"2026-07-26T10:00:02Z\",\"raw\":[[1,2]", encoding="utf-8")
    adapter = _adapter(tmp_path, thumbnails=2)

    result = adapter.process_quicklook(adapter.load(_candidate(source)), {})

    assert result.processing_status is ProcessingStatus.WARNING
    assert result.metadata["frame_count"] == 2
    assert result.metadata["corrupted_sample_count"] >= 1


def test_large_folder_is_scanned_in_bounded_chunks(tmp_path: Path):
    paths = []
    for file_index in range(12):
        paths.append(_dataset(
            tmp_path / "FLIR" / f"camera_{file_index:02d}.json",
            (_frame(file_index * 20 + index) for index in range(20)),
        ))
    progress = []
    adapter = _adapter(tmp_path, thumbnails=2, memory=512 * 1024)
    adapter.report_progress(progress.append)

    result = adapter.process_quicklook(adapter.load(_candidate(*paths)), {})

    assert result.metadata["frame_count"] == 240
    assert result.metadata["scan_chunk_bytes"] <= 512 * 1024
    assert progress[-1].progress == 100


def test_thumbnail_limit_is_enforced(tmp_path: Path):
    source = _dataset(tmp_path / "FLIR" / "camera.json", (_frame(i) for i in range(20)))
    adapter = _adapter(tmp_path, thumbnails=2)

    result = adapter.process_quicklook(adapter.load(_candidate(source)), {})

    assert result.metadata["thumbnail_count"] == 2
    assert len(result.figures) == 2


def test_cancellation_stops_chunked_scan(tmp_path: Path):
    source = _dataset(tmp_path / "FLIR" / "camera.json", (_frame(i) for i in range(1200)))
    adapter = _adapter(tmp_path, thumbnails=1, memory=128 * 1024)

    def cancel_after_first_update(update):
        if update.phase.startswith("Timestamp chunk"):
            adapter.cancel()

    adapter.report_progress(cancel_after_first_update)
    loaded = adapter.load(_candidate(source))

    with pytest.raises(RuntimeError, match="cancelled"):
        adapter.process_quicklook(loaded, {})


def test_selected_time_filter_limits_flir_quicklook_frames(tmp_path: Path):
    source = _dataset(
        tmp_path / "FLIR" / "camera.json", (_frame(i) for i in range(6))
    )
    adapter = _adapter(tmp_path)

    result = adapter.process_quicklook(
        adapter.load(_candidate(source)),
        {
            "analysis_start": datetime(
                2026, 7, 26, 10, 0, 2, tzinfo=timezone.utc
            ),
            "analysis_end": datetime(
                2026, 7, 26, 10, 0, 6, tzinfo=timezone.utc
            ),
        },
    )

    assert result.metadata["frame_count"] == 3
    assert result.utc_start_time == datetime(
        2026, 7, 26, 10, 0, 2, tzinfo=timezone.utc
    )
    assert result.utc_end_time == datetime(
        2026, 7, 26, 10, 0, 6, tzinfo=timezone.utc
    )
