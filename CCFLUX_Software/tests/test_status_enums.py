from __future__ import annotations

import unittest

from core.enums import DetectionStatus, ProcessingStatus


class StatusEnumTests(unittest.TestCase):
    def test_detection_values_are_stable(self) -> None:
        self.assertEqual(
            [status.name for status in DetectionStatus],
            [
                "NOT_DETECTED",
                "DETECTED",
                "VALIDATING",
                "READY",
                "WARNING",
                "FAILED",
            ],
        )
        self.assertEqual(DetectionStatus.NOT_DETECTED.value, "not_detected")

    def test_processing_values_are_stable(self) -> None:
        self.assertEqual(
            [status.name for status in ProcessingStatus],
            [
                "IDLE",
                "QUEUED",
                "PROCESSING",
                "COMPLETE",
                "WARNING",
                "FAILED",
                "CANCELLED",
                "PAUSED",
            ],
        )
        self.assertEqual(ProcessingStatus.CANCELLED.value, "cancelled")

    def test_statuses_are_strings(self) -> None:
        self.assertIsInstance(DetectionStatus.READY, str)
        self.assertIsInstance(ProcessingStatus.COMPLETE, str)


if __name__ == "__main__":
    unittest.main()
