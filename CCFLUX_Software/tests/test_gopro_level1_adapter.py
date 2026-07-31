from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from PIL import Image

from core.detector import InputCandidate
from core.enums import ProcessingStatus
from core.resource_manager import CameraBatchPolicy, ResourceLimits
from instruments.gopro import GoProLevel1Adapter


def _image(path: Path, timestamp: datetime | None) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    exif = Image.Exif()
    if timestamp:
        exif[36867] = timestamp.strftime("%Y:%m:%d %H:%M:%S")
    Image.new("RGB", (32, 24), (20, 80, 140)).save(path, exif=exif)
    return path


def _video(path: Path, size: int = 4096) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"video" + b"\0" * size)
    return path


def _probe(path: Path):
    index = int(path.stem.rsplit("_", 1)[-1])
    return {
        "creation_time": datetime(2026, 7, 26, 11, tzinfo=timezone.utc)
        + timedelta(minutes=index),
        "duration_seconds": 60.0 + index,
    }


def _sample(path: Path, target: Path):
    Image.new("RGB", (40, 30), (100, 50, 10)).save(target)
    return True


def _adapter(tmp_path: Path, *, thumbnails=4, probe=_probe, sampler=_sample):
    return GoProLevel1Adapter(
        output_root=tmp_path / "output",
        flight_name="flight",
        resource_limits=ResourceLimits(cpu_cores=2, memory_bytes=32 * 1024**2),
        batch_policy=CameraBatchPolicy(
            maximum_batch_files=5,
            maximum_thumbnail_count=thumbnails,
            maximum_thumbnail_bytes=2 * 1024**2,
        ),
        video_probe=probe,
        video_sampler=sampler,
        unusually_small_image_bytes=1,
        unusually_small_video_bytes=1,
    )


def _candidate(paths):
    return InputCandidate("gopro", tuple(paths), 1.0, "synthetic")


def test_image_sequence_count_intervals_and_thumbnails(tmp_path: Path):
    start = datetime(2026, 7, 26, 10, tzinfo=timezone.utc)
    paths = [_image(tmp_path / "GoPro" / f"GOPR{i:04d}.jpg", start + timedelta(seconds=2 * i)) for i in range(6)]
    adapter = _adapter(tmp_path, thumbnails=3)
    result = adapter.process_quicklook(adapter.load(_candidate(paths)), {})
    assert result.metadata["image_count"] == 6
    assert result.metadata["video_count"] == 0
    assert result.metadata["image_acquisition_intervals_seconds"] == [2.0] * 5
    assert result.metadata["thumbnail_count"] == 3


def test_video_files_count_duration_and_sampled_frames(tmp_path: Path):
    paths = [_video(tmp_path / "GoPro" / f"GX01000_{i}.MP4") for i in range(2)]
    adapter = _adapter(tmp_path)
    result = adapter.process_quicklook(adapter.load(_candidate(paths)), {})
    assert result.metadata["video_count"] == 2
    assert result.metadata["recording_duration_seconds"] == 121.0
    assert result.metadata["sampled_video_frame_count"] == 2
    assert not result.metadata["full_frame_extraction_performed"]


def test_mixed_images_and_videos(tmp_path: Path):
    image = _image(tmp_path / "GoPro" / "GOPR0001.jpg", datetime(2026, 7, 26, 10))
    video = _video(tmp_path / "GoPro" / "GX01000_0.mp4")
    adapter = _adapter(tmp_path)
    result = adapter.process_quicklook(adapter.load(_candidate([image, video])), {})
    assert result.file_count == 2
    assert result.metadata["image_count"] == 1
    assert result.metadata["video_count"] == 1


def test_missing_timestamps_are_reported(tmp_path: Path):
    image = _image(tmp_path / "GoPro" / "GOPR0001.jpg", None)
    adapter = _adapter(tmp_path)
    result = adapter.process_quicklook(adapter.load(_candidate([image])), {})
    assert result.processing_status is ProcessingStatus.WARNING
    assert result.metadata["missing_timestamp_count"] == 1


def test_acquisition_gap_detection(tmp_path: Path):
    start = datetime(2026, 7, 26, 10)
    seconds = [0, 2, 4, 30]
    paths = [_image(tmp_path / "GoPro" / f"GOPR{i:04d}.jpg", start + timedelta(seconds=value)) for i, value in enumerate(seconds)]
    adapter = _adapter(tmp_path)
    result = adapter.process_quicklook(adapter.load(_candidate(paths)), {})
    assert result.metadata["obvious_gap_count"] == 1
    assert result.metadata["obvious_gap_intervals_seconds"] == [26.0]


def test_large_video_is_probed_and_only_one_frame_sampled(tmp_path: Path):
    video = _video(tmp_path / "GoPro" / "GX01000_0.mp4", size=8 * 1024 * 1024)
    calls = []
    def sampler(path, target):
        calls.append(path)
        return _sample(path, target)
    adapter = _adapter(tmp_path, thumbnails=1, sampler=sampler)
    result = adapter.process_quicklook(adapter.load(_candidate([video])), {})
    assert result.metadata["sampled_video_frame_count"] == 1
    assert len(calls) == 1


def test_cancellation_stops_between_metadata_batches(tmp_path: Path):
    start = datetime(2026, 7, 26, 10)
    paths = [_image(tmp_path / "GoPro" / f"GOPR{i:04d}.jpg", start + timedelta(seconds=i)) for i in range(20)]
    adapter = _adapter(tmp_path)
    def cancel(update):
        if update.phase.startswith("Media metadata batch"):
            adapter.cancel()
    adapter.report_progress(cancel)
    loaded = adapter.load(_candidate(paths))
    with pytest.raises(RuntimeError, match="cancelled"):
        adapter.process_quicklook(loaded, {})


def test_user_time_filter_is_applied_after_berlin_to_utc_correction(
    tmp_path: Path,
):
    paths = [
        _image(
            tmp_path / "GoPro" / "GOPR0001.jpg",
            datetime(2026, 7, 27, 7, 24, 17),
        ),
        _image(
            tmp_path / "GoPro" / "GOPR0002.jpg",
            datetime(2026, 7, 27, 11, 0, 0),
        ),
        _image(
            tmp_path / "GoPro" / "GOPR0003.jpg",
            datetime(2026, 7, 27, 12, 28, 26),
        ),
    ]
    adapter = _adapter(tmp_path)

    result = adapter.process_quicklook(
        adapter.load(_candidate(paths)),
        {
            "analysis_start": datetime(
                2026, 7, 27, 5, 22, 4, tzinfo=timezone.utc
            ),
            "analysis_end": datetime(
                2026, 7, 27, 10, 19, 58, tzinfo=timezone.utc
            ),
        },
    )

    assert result.file_count == 2
    assert result.metadata["source_media_count"] == 3
    assert result.metadata["selected_media_count"] == 2
    assert result.utc_start_time == datetime(
        2026, 7, 27, 5, 24, 17, tzinfo=timezone.utc
    )
    assert result.utc_end_time == datetime(
        2026, 7, 27, 9, 0, 0, tzinfo=timezone.utc
    )
