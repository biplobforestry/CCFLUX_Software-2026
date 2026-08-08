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


def test_pre_campaign_timestamps_are_dropped_from_the_calculation(adapter):
    """A frame dated before the campaign was dated by an unlocked GPS.

    Its stamp says nothing about when the image was taken, so it cannot bound
    the coverage, join a capture or contribute a trigger interval. It is counted
    and reported instead of being used.
    """
    import inspect

    from instruments.micasense.adapter import MINIMUM_CAPTURE_YEAR

    source = inspect.getsource(adapter.process_quicklook)
    assert MINIMUM_CAPTURE_YEAR == 2025
    assert "stamp.year < MINIMUM_CAPTURE_YEAR" in source
    assert "GPS had not" in source or "GPS had not locked" in source


def test_the_time_range_pass_reads_one_band_per_capture(adapter, tmp_path):
    """It decompressed all six, including the 10 MB panchromatic one.

    On Flight_CCT0803 that was 14,226 decompressions for 2,371 captures, and it
    is where MicaSense spent most of a 55-minute run. All six bands are written
    by one trigger and carry one acquisition time, so the other five tell the
    time range nothing new - which is what ArchiveImage.read_metadata already
    marks and this pass ignored.
    """
    archives = [
        _capture_archive(tmp_path / f"IMG_{index:04d}.zip", f"IMG_{index:04d}")
        for index in range(3)
    ]
    release_archive_handle()

    decoded: list[str] = []
    original = adapter._metadata

    def counting(source, payload=None):
        decoded.append(getattr(source, "member", str(source)))
        return original(source, payload)

    adapter._metadata = counting
    adapter.extract_time_range(InputCandidate("micasense", tuple(archives), 1.0, "t"))
    release_archive_handle()

    # Three captures of six bands each: three decodes, not eighteen.
    assert len(decoded) == 3, decoded
    assert not [name for name in decoded if name.endswith("_6.tif")]


class TestCapturesAreReadAlongsideEachOther:
    """Flight_CC0806's 59 978 images were read one batch of 32 at a time.

    Each batch was a barrier: the pool was built, its files read, and every
    worker then waited for the slowest before the next batch could start, 1 865
    times over. One pool for the whole delivery measured 27% faster warm and no
    slower cold, where the disk sets the pace regardless.

    The unit of work is the archive, not the file. _open_archive keeps one
    handle per thread and drops it as soon as that thread is handed a member of
    a different archive, so scattering a capture's six bands over six workers
    would make every band pay its own central-directory read.
    """

    SOURCE = (
        Path(__file__).resolve().parents[1]
        / "instruments" / "micasense" / "adapter.py"
    ).read_text(encoding="utf-8")

    def _pass(self) -> str:
        start = self.SOURCE.index("    def process_quicklook(")
        end = self.SOURCE.index("self._share_capture_metadata()", start)
        return self.SOURCE[start:end]

    def test_the_batch_barrier_is_gone(self):
        body = self._pass()
        assert "iter_batches" not in body
        assert "iter_batches" not in self.SOURCE, "the import outlived its use"

    def test_one_pool_covers_the_delivery(self):
        body = self._pass()
        assert body.count("ThreadPoolExecutor(") == 1
        assert "ccflux-micasense-meta" in body

    def test_the_reader_count_is_declared_and_bounded(self):
        from instruments.micasense.adapter import MICASENSE_METADATA_READERS

        assert 1 <= MICASENSE_METADATA_READERS <= 32

    def test_a_captures_bands_are_one_unit_of_work(self, tmp_path):
        from instruments.micasense.adapter import _reading_groups

        first = _capture_archive(tmp_path / "IMG_0000.zip", "IMG_0000", bands=6)
        second = _capture_archive(tmp_path / "IMG_0001.zip", "IMG_0001", bands=6)
        images = _all_images((first, second))

        groups = _reading_groups(images)

        assert [len(group) for group in groups] == [6, 6]
        for group in groups:
            archives = {images[index].archive for index in group}
            assert len(archives) == 1

    def test_loose_images_parallelise_one_by_one(self, tmp_path):
        """A plain file has no handle to reuse, so nothing is gained by pairing
        it with its neighbour and a worker would sit idle behind it."""
        from instruments.micasense.adapter import _reading_groups

        loose = [tmp_path / f"IMG_{index:04d}_1.tif" for index in range(4)]

        assert _reading_groups(loose) == [[0], [1], [2], [3]]

    def test_no_archive_is_left_open_by_a_worker(self, adapter, tmp_path):
        """The handle lives in thread-local storage, so a worker that keeps one
        keeps it until the thread dies - and on Windows an archive still open is
        an archive the next stage cannot move or delete."""
        archive = _capture_archive(tmp_path / "IMG_0000.zip", "IMG_0000", bands=3)
        images = _all_images((archive,))
        release_archive_handle()

        closed: list[bool] = []
        original = adapter._inspect_one

        def watching(item):
            record = original(item)
            closed.append(True)
            return record

        adapter._inspect_one = watching
        adapter._read_group(images, list(range(len(images))))

        from instruments.micasense.adapter import _ARCHIVE_HANDLES

        assert len(closed) == 3
        assert getattr(_ARCHIVE_HANDLES, "handle", None) is None

    def test_a_group_gives_its_handle_back_even_when_a_band_fails(
        self, adapter, tmp_path
    ):
        archive = _capture_archive(tmp_path / "IMG_0000.zip", "IMG_0000", bands=2)
        images = _all_images((archive,))
        release_archive_handle()
        adapter._inspect_one = lambda item: (_ for _ in ()).throw(RuntimeError("x"))

        with pytest.raises(RuntimeError):
            adapter._read_group(images, [0, 1])

        from instruments.micasense.adapter import _ARCHIVE_HANDLES

        assert getattr(_ARCHIVE_HANDLES, "handle", None) is None

    def test_records_stay_in_delivery_order(self, adapter, tmp_path):
        """Bands finish in whatever order the disk returns them, and the capture
        counts join on position."""
        for index in range(4):
            _capture_archive(tmp_path / f"IMG_{index:04d}.zip", f"IMG_{index:04d}")
        release_archive_handle()
        candidate = InputCandidate(
            "micasense", tuple(sorted(tmp_path.glob("*.zip"))), 1.0, "fixture"
        )

        adapter.process_quicklook(adapter.load(candidate), {})

        read = [record["capture_id"] for record in adapter._records]
        assert read == sorted(read), read

    def test_cancelling_does_not_wait_for_the_queue(self):
        """Leaving the pool otherwise blocks on every queued capture."""
        body = self._pass()
        assert "future.cancel()" in body
        assert "self._check_cancelled()" in body


def test_a_loose_tiff_is_still_read(adapter, tmp_path):
    """The skip keys on ArchiveImage.read_metadata; a plain file has no flag."""
    import io as _io

    from PIL import Image as _Image

    path = tmp_path / "IMG_0000_1.tif"
    buffer = _io.BytesIO()
    _Image.new("L", (8, 8), color=5).save(buffer, format="TIFF")
    path.write_bytes(buffer.getvalue())

    decoded: list[str] = []
    original = adapter._metadata
    adapter._metadata = lambda s, p=None: (decoded.append(str(s)), original(s, p))[1]
    adapter.extract_time_range(InputCandidate("micasense", (path,), 1.0, "t"))
    assert len(decoded) == 1
