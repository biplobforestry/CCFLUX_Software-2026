from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from core.enums import DetectionStatus, ProcessingStatus
from core.models import InstrumentResult, SamplingFrequency, SourceFile


class InstrumentResultTests(unittest.TestCase):
    def test_empty_result_is_explicitly_unprocessed(self) -> None:
        result = InstrumentResult.empty("noseboom", "Noseboom", "NOSEBOOM")
        self.assertEqual(result.detection_status, DetectionStatus.NOT_DETECTED)
        self.assertEqual(result.processing_status, ProcessingStatus.IDLE)
        self.assertEqual(result.file_count, 0)
        self.assertEqual(result.source_files, [])

    def test_typed_complete_result(self) -> None:
        start = datetime(2026, 7, 20, 6, 15, tzinfo=timezone.utc)
        source = SourceFile(Path("/read-only/input.csv"), size_bytes=10)
        result = InstrumentResult(
            instrument_id="example",
            display_name="Example",
            physical_group="TEST",
            detection_status=DetectionStatus.READY,
            processing_status=ProcessingStatus.COMPLETE,
            source_files=[source],
            file_count=1,
            original_start_time=start,
            original_end_time=start + timedelta(hours=1),
            utc_start_time=start,
            utc_end_time=start + timedelta(hours=1),
            sampling_frequency=SamplingFrequency(1.0),
            completeness_percentage=100.0,
            progress=100.0,
            elapsed_time=timedelta(seconds=2),
        )
        self.assertEqual(result.sampling_frequency.hertz, 1.0)

    def test_file_count_must_match_sources(self) -> None:
        with self.assertRaises(ValueError):
            InstrumentResult(
                instrument_id="x",
                display_name="X",
                physical_group="TEST",
                file_count=1,
            )

    def test_percentages_are_bounded(self) -> None:
        with self.assertRaises(ValueError):
            InstrumentResult(
                instrument_id="x",
                display_name="X",
                physical_group="TEST",
                progress=101.0,
            )

    def test_time_ranges_are_ordered(self) -> None:
        start = datetime(2026, 7, 20, tzinfo=timezone.utc)
        with self.assertRaises(ValueError):
            InstrumentResult(
                instrument_id="x",
                display_name="X",
                physical_group="TEST",
                utc_start_time=start,
                utc_end_time=start - timedelta(seconds=1),
            )

    def test_sampling_frequency_must_be_positive(self) -> None:
        with self.assertRaises(ValueError):
            SamplingFrequency(0.0)


if __name__ == "__main__":
    unittest.main()
