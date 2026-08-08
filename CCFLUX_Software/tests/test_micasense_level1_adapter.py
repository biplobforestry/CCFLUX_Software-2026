from datetime import datetime, timedelta, timezone
from pathlib import Path
import zipfile

import pytest
from PIL import Image

from core.detector import InputCandidate
from core.enums import ProcessingStatus
from core.resource_manager import CameraBatchPolicy, ResourceLimits
from instruments.micasense import MicaSenseLevel1Adapter


def _image(path: Path, *, valid: bool = True) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if valid:
        Image.new("L", (24, 18), color=120).save(path, format="TIFF")
    else:
        path.write_bytes(b"not-a-tiff")


def _metadata(path: Path):
    capture = int(path.stem.split("_")[1])
    band = int(path.stem.rsplit("_", 1)[1])
    timestamp = datetime(2026, 7, 26, 10, tzinfo=timezone.utc) + timedelta(
        seconds=2 * capture
    )
    return {
        "CaptureId": f"IMG_{capture:04d}",
        "BandNumber": band,
        "BandName": f"Band {band}",
        "DateTimeOriginal": timestamp.strftime("%Y:%m:%d %H:%M:%S"),
        "GPSLatitude": 52.0,
        "GPSLongitude": 13.0,
        "GPSAltitude": 500.0,
        "ExposureTime": 0.002,
        "ISOSpeed": 100,
    }


def _adapter(tmp_path: Path, **kwargs) -> MicaSenseLevel1Adapter:
    return MicaSenseLevel1Adapter(
        output_root=tmp_path / "output",
        flight_name="flight",
        resource_limits=ResourceLimits(cpu_cores=2, memory_bytes=64 * 1024**2),
        batch_policy=kwargs.pop(
            "batch_policy",
            CameraBatchPolicy(
                maximum_batch_files=8,
                maximum_thumbnail_count=3,
                maximum_thumbnail_bytes=4 * 1024**2,
            ),
        ),
        metadata_reader=kwargs.pop("metadata_reader", _metadata),
        unusually_small_bytes=kwargs.pop("unusually_small_bytes", 1),
        **kwargs,
    )


def _candidate(folder: Path) -> InputCandidate:
    return InputCandidate(
        "micasense", tuple(sorted(folder.glob("*.tif"))), 0.98, "synthetic"
    )


def test_complete_capture_and_metadata_qc(tmp_path: Path):
    folder = tmp_path / "images"
    for band in range(1, 7):
        _image(folder / f"IMG_0001_{band}.tif")
    adapter = _adapter(tmp_path)

    result = adapter.process_quicklook(adapter.load(_candidate(folder)), {})

    assert result.processing_status is ProcessingStatus.COMPLETE
    assert result.metadata["complete_capture_count"] == 1
    assert result.metadata["incomplete_capture_count"] == 0
    assert result.metadata["bands"] == [1, 2, 3, 4, 5, 6]
    assert result.utc_start_time == result.utc_end_time
    assert result.metadata["gps_present_count"] == 6
    assert result.metadata["exposure_present_count"] == 6


def test_incomplete_capture_is_reported(tmp_path: Path):
    folder = tmp_path / "images"
    for band in (1, 2, 3):
        _image(folder / f"IMG_0001_{band}.tif")
    adapter = _adapter(tmp_path)

    result = adapter.process_quicklook(adapter.load(_candidate(folder)), {})

    assert result.processing_status is ProcessingStatus.WARNING
    assert result.metadata["incomplete_capture_count"] == 1


def test_missing_band_is_listed_in_capture_qc(tmp_path: Path):
    folder = tmp_path / "images"
    for band in (1, 2, 3, 4, 6):
        _image(folder / f"IMG_0001_{band}.tif")
    adapter = _adapter(tmp_path)
    adapter.process_quicklook(adapter.load(_candidate(folder)), {})

    assert adapter._captures[0]["missing_bands"] == [5]
    assert not adapter._captures[0]["complete"]


def test_corrupt_file_and_unusually_small_file_are_reported(tmp_path: Path):
    folder = tmp_path / "images"
    _image(folder / "IMG_0001_1.tif", valid=False)
    adapter = _adapter(tmp_path, unusually_small_bytes=1024)

    result = adapter.process_quicklook(adapter.load(_candidate(folder)), {})

    assert result.metadata["corrupt_files"] == ["IMG_0001_1.tif"]
    assert result.metadata["unusually_small_files"] == ["IMG_0001_1.tif"]


def test_large_folder_is_read_with_a_bounded_number_of_readers(tmp_path: Path):
    """The batch barrier is gone; the files in flight are still bounded."""
    folder = tmp_path / "images"
    for capture in range(1, 41):
        for band in range(1, 7):
            _image(folder / f"IMG_{capture:04d}_{band}.tif")
    progress = []
    adapter = _adapter(tmp_path)
    adapter.report_progress(progress.append)

    result = adapter.process_quicklook(adapter.load(_candidate(folder)), {})

    assert result.file_count == 240
    assert result.metadata["metadata_readers"] == adapter.METADATA_READERS
    assert result.metadata["complete_capture_count"] == 40
    assert progress[-1].progress == 100


def test_thumbnail_limit_is_enforced(tmp_path: Path):
    folder = tmp_path / "images"
    for capture in range(1, 5):
        for band in range(1, 7):
            _image(folder / f"IMG_{capture:04d}_{band}.tif")
    adapter = _adapter(
        tmp_path,
        batch_policy=CameraBatchPolicy(
            maximum_batch_files=4,
            maximum_thumbnail_count=2,
            maximum_thumbnail_bytes=1024**2,
        ),
    )

    result = adapter.process_quicklook(adapter.load(_candidate(folder)), {})

    assert result.metadata["thumbnail_count"] == 2
    assert len(result.figures) == 2


def test_cancellation_stops_between_files(tmp_path: Path):
    folder = tmp_path / "images"
    for band in range(1, 7):
        _image(folder / f"IMG_0001_{band}.tif")
    calls = 0
    adapter = None

    def cancelling_reader(path):
        nonlocal calls
        calls += 1
        if calls == 2:
            adapter.cancel()
        return _metadata(path)

    adapter = _adapter(tmp_path, metadata_reader=cancelling_reader)
    loaded = LoadedWithoutValidation(adapter, _candidate(folder))

    with pytest.raises(RuntimeError, match="cancelled"):
        adapter.process_quicklook(loaded, {})


def test_zip_per_capture_is_streamed_without_extraction(tmp_path: Path):
    source = tmp_path / "raw" / "IMG_0042.zip"
    source.parent.mkdir()
    staging = tmp_path / "staging"
    staging.mkdir()
    with zipfile.ZipFile(source, "w") as archive:
        for band in range(1, 7):
            image = staging / f"IMG_0042_{band}.tif"
            _image(image)
            archive.write(image, image.name)
    adapter = _adapter(tmp_path)
    candidate = InputCandidate("micasense", (source,), 0.9, "synthetic ZIP")

    result = adapter.process_quicklook(adapter.load(candidate), {})

    assert result.file_count == 6
    assert result.metadata["complete_capture_count"] == 1
    assert result.metadata["bands"] == [1, 2, 3, 4, 5, 6]
    assert not list(source.parent.glob("*.tif"))
    assert source.read_bytes().startswith(b"PK")


def test_corrupted_zip_is_reported_without_crashing_other_jobs(tmp_path: Path):
    source = tmp_path / "raw" / "IMG_0099.zip"
    source.parent.mkdir()
    source.write_bytes(b"not-a-valid-zip")
    adapter = _adapter(tmp_path)
    candidate = InputCandidate("micasense", (source,), 0.9, "corrupt ZIP")

    result = adapter.process_quicklook(adapter.load(candidate), {})

    assert result.processing_status is ProcessingStatus.WARNING
    assert result.metadata["corrupt_files"] == ["__CORRUPT_ARCHIVE__.tif"]
    assert source.read_bytes() == b"not-a-valid-zip"


def LoadedWithoutValidation(adapter, candidate):
    from instruments.micasense.adapter import LoadedMicaSense

    return LoadedMicaSense(candidate, tuple(candidate.paths))
