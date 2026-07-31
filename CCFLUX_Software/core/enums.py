"""Shared status enumerations."""

from __future__ import annotations

from .compat import StrEnum


class DetectionStatus(StrEnum):
    NOT_DETECTED = "not_detected"
    DETECTED = "detected"
    VALIDATING = "validating"
    READY = "ready"
    WARNING = "warning"
    FAILED = "failed"


class ProcessingStatus(StrEnum):
    IDLE = "idle"
    QUEUED = "queued"
    PROCESSING = "processing"
    COMPLETE = "complete"
    WARNING = "warning"
    FAILED = "failed"
    CANCELLED = "cancelled"
    PAUSED = "paused"
