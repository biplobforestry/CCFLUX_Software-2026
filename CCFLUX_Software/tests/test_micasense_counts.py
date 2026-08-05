"""MicaSense counts must describe what was evaluated, not what was delivered.

On Flight_CCT0803 the summary read 14,226 images beside 42 captures of 6 bands,
because the image and GPS counts were taken from the whole delivery while every
other figure came from the images left after the Time Filter. The trigger
interval list also carried a 56-year gap, from the frames whose camera clock
was never set.
"""
import unittest
from datetime import datetime, timedelta, timezone

from instruments.micasense.adapter import _capture_rows, _trigger_intervals


def _record(capture_id, band, stamp):
    return {"capture_id": capture_id, "band_number": band, "timestamp": stamp}


def _utc(hour, minute, second):
    return datetime(2026, 8, 3, hour, minute, second, tzinfo=timezone.utc)


EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)


class TriggerIntervalTests(unittest.TestCase):
    def test_an_unset_clock_does_not_become_a_trigger_time(self):
        rows = _capture_rows(
            [_record("A", band, EPOCH) for band in range(1, 7)]
            + [_record("B", band, _utc(12, 0, 0)) for band in range(1, 7)]
            + [_record("C", band, _utc(12, 0, 8)) for band in range(1, 7)]
        )
        by_id = {row["capture_id"]: row for row in rows}
        self.assertIsNone(by_id["A"]["trigger_time"])
        self.assertEqual(_trigger_intervals(rows), [8.0])

    def test_real_intervals_are_measured_between_consecutive_captures(self):
        rows = _capture_rows(
            [_record("A", 1, _utc(12, 0, 0)), _record("B", 1, _utc(12, 0, 2))]
        )
        self.assertEqual(_trigger_intervals(rows), [2.0])

    def test_a_capture_whose_bands_disagree_takes_the_earliest_real_stamp(self):
        rows = _capture_rows(
            [_record("A", 1, EPOCH), _record("A", 2, _utc(12, 0, 5)),
             _record("A", 3, _utc(12, 0, 6))]
        )
        self.assertEqual(rows[0]["trigger_time"], _utc(12, 0, 5))


def test_the_summary_counts_what_the_time_filter_left(tmp_path):
    """Three captures delivered, one inside the window: 6 images, not 18."""
    from tests.test_micasense_level1_adapter import _adapter, _candidate, _image

    folder = tmp_path / "images"
    for capture in (1, 2, 3):
        for band in range(1, 7):
            _image(folder / f"IMG_{capture:04d}_{band}.tif")
    adapter = _adapter(tmp_path)

    # _metadata dates capture N at 10:00:00 + 2N seconds on 26 July 2026.
    result = adapter.process_quicklook(
        adapter.load(_candidate(folder)),
        {
            "analysis_start": datetime(2026, 7, 26, 10, 0, 3, tzinfo=timezone.utc),
            "analysis_end": datetime(2026, 7, 26, 10, 0, 5, tzinfo=timezone.utc),
        },
    )

    assert result.metadata["capture_count"] == 1
    assert result.metadata["image_count"] == 6
    assert result.metadata["delivered_image_count"] == 18
    assert result.metadata["gps_present_count"] == 6
    assert result.metadata["exposure_present_count"] == 6


if __name__ == "__main__":
    unittest.main()


class TestOneBandPerCaptureIsRead:
    """A capture's bands share a trigger, so one read answers for all six.

    Reading every band cost six times the decompression for the same
    acquisition time, position and exposure: 80 minutes on a 2,371-archive
    delivery. Presence and size come from the archive's own file list.
    """

    def test_only_one_band_of_an_archive_is_opened(self, tmp_path):
        import zipfile

        from PIL import Image

        from instruments.micasense.adapter import ArchiveImage, _all_images

        archive = tmp_path / "IMG_0001.zip"
        with zipfile.ZipFile(archive, "w") as bundle:
            for band in range(1, 7):
                image = tmp_path / f"IMG_0001_{band}.tif"
                Image.new("L", (16, 12), color=10 * band).save(image, format="TIFF")
                bundle.write(image, f"IMG_0001_{band}.tif")
        images = [i for i in _all_images((archive,)) if isinstance(i, ArchiveImage)]
        assert len(images) == 6
        assert sum(1 for i in images if i.read_metadata) == 1

    def test_band_one_is_the_one_read(self, tmp_path):
        import zipfile

        from PIL import Image

        from instruments.micasense.adapter import ArchiveImage, _all_images

        archive = tmp_path / "IMG_0002.zip"
        with zipfile.ZipFile(archive, "w") as bundle:
            for band in (3, 1, 5):
                image = tmp_path / f"IMG_0002_{band}.tif"
                Image.new("L", (16, 12)).save(image, format="TIFF")
                bundle.write(image, f"IMG_0002_{band}.tif")
        read = [i for i in _all_images((archive,))
                if isinstance(i, ArchiveImage) and i.read_metadata]
        assert len(read) == 1
        assert read[0].name.endswith("_1.tif")

    def test_every_band_still_reports_the_capture_metadata(self, tmp_path):
        """Sharing keyed on the wrong id left every capture looking incomplete."""
        from tests.test_micasense_level1_adapter import _adapter, _candidate, _image

        folder = tmp_path / "images"
        for band in range(1, 7):
            _image(folder / f"IMG_0001_{band}.tif")
        adapter = _adapter(tmp_path)
        result = adapter.process_quicklook(adapter.load(_candidate(folder)), {})
        assert result.metadata["capture_count"] == 1
        assert result.metadata["complete_capture_count"] == 1
        assert result.metadata["gps_present_count"] == 6
