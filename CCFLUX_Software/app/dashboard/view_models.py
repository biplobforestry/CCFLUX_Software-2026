"""Typed, bounded view models for future dashboard rendering."""

from __future__ import annotations

from dataclasses import dataclass

from core.enums import DetectionStatus, ProcessingStatus


@dataclass(frozen=True, slots=True)
class InstrumentCardView:
    instrument_id: str
    display_name: str
    physical_group: str
    detection_status: DetectionStatus
    processing_status: ProcessingStatus
    progress: float | None = None
    warning_count: int = 0
    error_count: int = 0
