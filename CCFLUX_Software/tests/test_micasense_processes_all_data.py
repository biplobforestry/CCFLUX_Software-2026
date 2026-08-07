"""MicaSense must process the whole delivery, not the sample it was found by.

Flight_CC0806 delivered 9,999 capture archives spanning 09:41 to 16:01. The
instrument card reported "30 matching files" covering 14:59:28 to 15:00:58 and
"Partial coverage: 0.3%", and the run processed about thirty captures.

The cause is a wrapped filename counter. MicaSense restarts at IMG_0000 after
IMG_9999, so IMG_0000.zip was taken at 15:00:03 and IMG_9999.zip at 15:00:01 -
adjacent in time, at opposite ends of the name order. Discovery sampled exactly
those two ends, so the estimate collapsed to the 90 seconds around the wrap, and
the processing interval was then intersected with it.
"""

from __future__ import annotations

import zipfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

pytest.importorskip("PIL")
from PIL import Image

from core.dashboard_time import (
    SAMPLED_COVERAGE_INSTRUMENTS,
    DashboardTimeState,
    InstrumentTimeSelection,
)
from core.detector import InputCandidate
from core.resource_manager import CameraBatchPolicy, ResourceLimits
from core.scanner import (
    BOUNDED_COVERAGE_SAMPLES,
    DEFAULT_CANDIDATE_FILE_SAMPLES,
    _CandidateAccumulator,
)
from instruments.micasense import MicaSenseLevel1Adapter


class TestDiscoverySurvivesAWrappedCounter:
    def _sample(self, count: int) -> list[Path]:
        accumulator = _CandidateAccumulator("micasense", Path("/delivery"))
        for index in range(count):
            accumulator.add(
                Path(f"/delivery/IMG_{index:04d}.zip"),
                set(), 1.0, [], [], DEFAULT_CANDIDATE_FILE_SAMPLES,
            )
        return list(accumulator.bounded_sample())

    def test_the_sample_reaches_the_middle_of_the_delivery(self):
        sample = self._sample(9999)
        numbers = sorted(int(path.stem.split("_")[1]) for path in sample)

        # The old sample was the first 20 encountered plus ten names at each
        # end: nothing between IMG_0019 and IMG_9990.
        middle = [value for value in numbers if 1000 <= value <= 9000]
        assert len(middle) >= 50, "a wrap makes the two ends adjacent in time"
        assert numbers[0] == 0 and numbers[-1] == 9998

    def test_the_spread_covers_the_whole_name_order(self):
        numbers = sorted(
            int(path.stem.split("_")[1]) for path in self._sample(9999)
        )
        gaps = [b - a for a, b in zip(numbers, numbers[1:])]

        # No stretch of the delivery is unrepresented.
        assert max(gaps) <= 9999 // BOUNDED_COVERAGE_SAMPLES + 1

    def test_the_true_count_is_still_reported(self):
        accumulator = _CandidateAccumulator("micasense", Path("/delivery"))
        for index in range(9999):
            accumulator.add(
                Path(f"/delivery/IMG_{index:04d}.zip"),
                set(), 1.0, [], [], DEFAULT_CANDIDATE_FILE_SAMPLES,
            )

        assert accumulator.matching_file_count == 9999
        assert len(accumulator.bounded_sample()) < 9999

    def test_a_small_delivery_is_sampled_whole(self):
        sample = self._sample(12)
        assert len(sample) == 12

    def test_the_sample_stays_bounded(self):
        assert len(self._sample(100_000)) <= BOUNDED_COVERAGE_SAMPLES + \
            DEFAULT_CANDIDATE_FILE_SAMPLES + 2


class TestARunIsNotNarrowedToTheSample:
    def _backend(self, tmp_path, available, selected):
        from app.scan_backend import DashboardScanBackend

        backend = DashboardScanBackend(tmp_path)
        state = DashboardTimeState()
        state.selected_analysis_start, state.selected_analysis_end = selected
        for name in ("micasense", "noseboom"):
            state.instruments[name] = InstrumentTimeSelection(
                name, available[0], available[1]
            )
        backend._time_state = state
        return backend

    def test_micasense_keeps_the_operator_interval(self, tmp_path):
        wrap = (
            datetime(2026, 8, 6, 14, 59, 28, tzinfo=timezone.utc),
            datetime(2026, 8, 6, 15, 0, 58, tzinfo=timezone.utc),
        )
        flight = (
            datetime(2026, 8, 6, 9, 41, tzinfo=timezone.utc),
            datetime(2026, 8, 6, 16, 1, tzinfo=timezone.utc),
        )
        backend = self._backend(tmp_path, wrap, flight)

        start, end = backend._instrument_processing_interval("micasense")

        assert (start, end) == flight, "the sampled 90 s must not bound the run"

    def test_an_instrument_with_real_coverage_is_still_narrowed(self, tmp_path):
        available = (
            datetime(2026, 8, 6, 10, tzinfo=timezone.utc),
            datetime(2026, 8, 6, 12, tzinfo=timezone.utc),
        )
        selected = (
            datetime(2026, 8, 6, 9, tzinfo=timezone.utc),
            datetime(2026, 8, 6, 16, tzinfo=timezone.utc),
        )
        backend = self._backend(tmp_path, available, selected)

        assert backend._instrument_processing_interval("noseboom") == available

    def test_the_sampled_set_is_the_declared_one(self):
        assert "micasense" in SAMPLED_COVERAGE_INSTRUMENTS


def test_one_band_is_read_per_capture(tmp_path, monkeypatch):
    """The bands of a capture share a trigger time; reading two invented a duplicate."""
    from core import time_extraction

    archive_path = tmp_path / "IMG_0000.zip"
    with zipfile.ZipFile(archive_path, "w") as archive:
        for band in range(1, 7):
            archive.writestr(f"IMG_0000_{band}.tif", b"tiff-placeholder")

    consulted: list[str] = []

    def fake_exif(source):
        consulted.append("read")
        return "2026:08:06 15:00:03"

    monkeypatch.setattr(time_extraction, "_exif_original_datetime", fake_exif)
    result = time_extraction.TimestampExtractor().extract_instrument(
        "micasense", [archive_path]
    )

    assert len(consulted) == 1, "one archive is one capture; one band dates it"
    assert result.duplicated_timestamps == 0
    assert not any(
        "duplicated timestamp" in text
        for text in result.timestamp_quality_warnings
    )


def _band(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("L", (16, 12), color=100).save(path, format="TIFF")


def _reader(path: Path):
    capture = int(Path(path).stem.split("_")[1])
    band = int(Path(path).stem.rsplit("_", 1)[1])
    stamp = datetime(2026, 8, 6, 9, 41, tzinfo=timezone.utc) + timedelta(
        minutes=capture
    )
    return {
        "CaptureId": f"IMG_{capture:04d}",
        "BandNumber": band,
        "BandName": f"Band {band}",
        "DateTimeOriginal": stamp.strftime("%Y:%m:%d %H:%M:%S"),
        "GPSLatitude": 52.0,
        "GPSLongitude": 13.0,
        "GPSAltitude": 500.0,
        "ExposureTime": 0.002,
        "ISOSpeed": 100,
    }


def test_the_delivery_coverage_is_reported_over_every_capture(tmp_path):
    """Measured before the Time Filter narrows anything, so it can replace the estimate."""
    raw = tmp_path / "raw"
    for capture in range(6):
        for band in range(1, 4):
            _band(raw / f"IMG_{capture:04d}_{band}.tif")
    adapter = MicaSenseLevel1Adapter(
        output_root=tmp_path / "out",
        flight_name="Flight_CC0806",
        resource_limits=ResourceLimits(cpu_cores=1, memory_bytes=1 << 28),
        batch_policy=CameraBatchPolicy(
            maximum_batch_files=8, maximum_thumbnail_count=2,
            maximum_thumbnail_bytes=4 * 1024**2,
        ),
        metadata_reader=_reader,
        unusually_small_bytes=1,
    )
    candidate = InputCandidate(
        "micasense", tuple(sorted(raw.rglob("*.tif"))), 1.0, "synthetic"
    )
    loaded = adapter.load(candidate)

    # A filter that keeps only the middle of the delivery.
    result = adapter.process_quicklook(
        loaded,
        {
            "analysis_start": datetime(2026, 8, 6, 9, 43, tzinfo=timezone.utc),
            "analysis_end": datetime(2026, 8, 6, 9, 44, tzinfo=timezone.utc),
        },
    )

    # The evaluated window is narrow...
    assert result.metadata["capture_count"] == 2
    # ...but the delivery's own coverage still spans every capture delivered.
    assert result.metadata["delivered_utc_start"].startswith("2026-08-06T09:41")
    assert result.metadata["delivered_utc_end"].startswith("2026-08-06T09:46")


def test_the_run_publishes_measured_coverage_back_to_the_card():
    source = (Path(__file__).parents[1] / "app" / "scan_backend.py").read_text(
        encoding="utf-8"
    )
    body = source[source.index("def _micasense_quick_task("):]
    body = body[: body.find("\n    def ", 1)]

    assert 'result.metadata.get("delivered_utc_start")' in body
    assert "state.utc_start_time = measured_start" in body
    assert "_rebuild_time_state()" in body
