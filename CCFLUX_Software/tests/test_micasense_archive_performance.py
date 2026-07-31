"""MicaSense archives must be opened once per band, not once per read.

A capture is one ZIP holding a band per file. Metadata extraction and image
verification each opened the archive from scratch, so a six-band capture paid
twelve central-directory reads. On the campaign disk that measured 1.2 s per
band — 64 minutes for 3,216 bands, which is why the camera pass never finished.
"""

import io
import zipfile
from datetime import datetime, timezone
from pathlib import Path

import pytest

from PIL import Image

from core.detector import InputCandidate
from core.resource_manager import CameraBatchPolicy, ResourceLimits
from instruments.micasense.adapter import (
    ArchiveImage,
    MicaSenseLevel1Adapter,
    _all_images,
    release_archive_handle,
)


def _capture_archive(path: Path, capture: str, bands: int = 6) -> Path:
    """Write one MicaSense-shaped capture archive with a TIFF per band."""
    with zipfile.ZipFile(path, "w") as bundle:
        for band in range(1, bands + 1):
            buffer = io.BytesIO()
            Image.new("L", (8, 8), color=band * 10).save(buffer, format="TIFF")
            bundle.writestr(f"{capture}_{band}.tif", buffer.getvalue())
    return path


@pytest.fixture()
def adapter(tmp_path):
    made = MicaSenseLevel1Adapter(
        output_root=tmp_path / "out",
        flight_name="Flight_2707",
        resource_limits=ResourceLimits(2, 4 * 1024**3),
        batch_policy=CameraBatchPolicy(
            maximum_batch_files=32, maximum_thumbnail_count=4
        ),
    )
    yield made
    release_archive_handle()


def test_bands_of_one_capture_reuse_a_single_archive_handle(adapter, tmp_path, monkeypatch):
    archive = _capture_archive(tmp_path / "IMG_0000.zip", "IMG_0000")
    images = _all_images((archive,))
    assert len(images) == 6

    opens: list[Path] = []
    original = zipfile.ZipFile

    def counting(file, *args, **kwargs):
        opens.append(Path(str(file)))
        return original(file, *args, **kwargs)

    monkeypatch.setattr(zipfile, "ZipFile", counting)
    release_archive_handle()
    for image in images:
        adapter._inspect_one(image)

    # One open for six bands, not one per read and certainly not two.
    assert len(opens) == 1, f"opened the archive {len(opens)} times for 6 bands"


def test_switching_archive_closes_the_previous_handle(adapter, tmp_path):
    first = _capture_archive(tmp_path / "IMG_0000.zip", "IMG_0000", bands=2)
    second = _capture_archive(tmp_path / "IMG_0001.zip", "IMG_0001", bands=2)
    release_archive_handle()

    for image in _all_images((first, second)):
        record = adapter._inspect_one(image)
        assert not record["corrupt"]


def test_inspection_still_reports_bands_and_integrity(adapter, tmp_path):
    archive = _capture_archive(tmp_path / "IMG_0000.zip", "IMG_0000")
    release_archive_handle()

    records = [adapter._inspect_one(image) for image in _all_images((archive,))]

    assert sorted(record["band_number"] for record in records) == [1, 2, 3, 4, 5, 6]
    assert not any(record["corrupt"] for record in records)
    assert all(record["size_bytes"] > 0 for record in records)


def test_a_corrupt_member_is_reported_not_raised(adapter, tmp_path):
    archive = tmp_path / "IMG_0002.zip"
    with zipfile.ZipFile(archive, "w") as bundle:
        bundle.writestr("IMG_0002_1.tif", b"not an image")
    release_archive_handle()

    record = adapter._inspect_one(
        ArchiveImage(archive, "IMG_0002_1.tif", size_bytes=12)
    )

    assert record["corrupt"] is True


def test_unset_camera_clock_does_not_exclude_images(adapter, tmp_path):
    """Epoch timestamps say nothing about when an image was taken.

    The MicaSense delivered for Flight_2707 was recorded with the camera clock
    unset, so every band is dated 1970. Filtering on that would discard the
    whole delivery; the images are kept and the operator is told why.
    """
    archive = _capture_archive(tmp_path / "IMG_0000.zip", "IMG_0000", bands=2)
    release_archive_handle()
    loaded = adapter.load(InputCandidate("micasense", (archive,), 1.0, "fixture"))

    result = adapter.process_quicklook(
        loaded,
        {
            "analysis_start": datetime(2026, 7, 27, 7, 30, tzinfo=timezone.utc),
            "analysis_end": datetime(2026, 7, 27, 8, 0, tzinfo=timezone.utc),
        },
    )

    # Both bands survive a filter they cannot possibly satisfy, and the reason
    # is stated rather than the delivery silently vanishing.
    assert result.file_count == 2
    assert any(
        "no usable acquisition time" in warning for warning in result.warnings
    )


def test_epoch_timestamps_are_treated_as_an_unset_clock(adapter):
    """A 1970 stamp must not be used to exclude an image from the window."""
    import inspect

    source = inspect.getsource(adapter.process_quicklook)
    assert "stamp.year < 2000" in source
    assert "camera clock" in source.lower()
