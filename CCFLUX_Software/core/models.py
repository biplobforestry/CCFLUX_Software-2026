"""Shared typed data models."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Mapping

from .enums import DetectionStatus, ProcessingStatus


def _validate_percentage(name: str, value: float | None) -> None:
    if value is not None and not 0.0 <= value <= 100.0:
        raise ValueError(f"{name} must be between 0 and 100")


@dataclass(frozen=True, slots=True)
class SourceFile:
    """Read-only source reference; this model never writes the path."""

    path: Path
    role: str = "input"
    size_bytes: int | None = None
    sha256: str | None = None

    def __post_init__(self) -> None:
        if self.size_bytes is not None and self.size_bytes < 0:
            raise ValueError("size_bytes cannot be negative")


@dataclass(frozen=True, slots=True)
class OutputFile:
    path: Path
    role: str
    media_type: str | None = None
    size_bytes: int | None = None
    sha256: str | None = None

    def __post_init__(self) -> None:
        if self.size_bytes is not None and self.size_bytes < 0:
            raise ValueError("size_bytes cannot be negative")


@dataclass(frozen=True, slots=True)
class FigureArtifact:
    path: Path
    title: str
    description: str | None = None


@dataclass(frozen=True, slots=True)
class SamplingFrequency:
    """Observed sampling frequency and its optional uncertainty."""

    hertz: float
    uncertainty_hertz: float | None = None
    method: str | None = None

    def __post_init__(self) -> None:
        if self.hertz <= 0:
            raise ValueError("sampling frequency must be positive")
        if self.uncertainty_hertz is not None and self.uncertainty_hertz < 0:
            raise ValueError("sampling-frequency uncertainty cannot be negative")


@dataclass(slots=True)
class InstrumentResult:
    """Standard bounded result shared by all future instrument adapters."""

    instrument_id: str
    display_name: str
    physical_group: str
    detection_status: DetectionStatus = DetectionStatus.NOT_DETECTED
    processing_status: ProcessingStatus = ProcessingStatus.IDLE
    source_files: list[SourceFile] = field(default_factory=list)
    file_count: int = 0
    original_start_time: datetime | None = None
    original_end_time: datetime | None = None
    utc_start_time: datetime | None = None
    utc_end_time: datetime | None = None
    sampling_frequency: SamplingFrequency | None = None
    completeness_percentage: float | None = None
    warnings: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    progress: float | None = None
    output_files: list[OutputFile] = field(default_factory=list)
    figures: list[FigureArtifact] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)
    elapsed_time: timedelta | None = None

    def __post_init__(self) -> None:
        if not self.instrument_id.strip():
            raise ValueError("instrument_id cannot be blank")
        if not self.display_name.strip():
            raise ValueError("display_name cannot be blank")
        if not self.physical_group.strip():
            raise ValueError("physical_group cannot be blank")
        if self.file_count < 0:
            raise ValueError("file_count cannot be negative")
        if self.file_count != len(self.source_files):
            raise ValueError("file_count must equal the number of source_files")
        _validate_percentage(
            "completeness_percentage", self.completeness_percentage
        )
        _validate_percentage("progress", self.progress)
        if (
            self.original_start_time is not None
            and self.original_end_time is not None
            and self.original_end_time < self.original_start_time
        ):
            raise ValueError("original_end_time cannot precede original_start_time")
        if (
            self.utc_start_time is not None
            and self.utc_end_time is not None
            and self.utc_end_time < self.utc_start_time
        ):
            raise ValueError("utc_end_time cannot precede utc_start_time")
        if self.elapsed_time is not None and self.elapsed_time < timedelta(0):
            raise ValueError("elapsed_time cannot be negative")

    @classmethod
    def empty(
        cls, instrument_id: str, display_name: str, physical_group: str
    ) -> "InstrumentResult":
        """Create an explicitly unintegrated/not-detected result."""
        return cls(
            instrument_id=instrument_id,
            display_name=display_name,
            physical_group=physical_group,
        )


@dataclass(frozen=True, slots=True)
class InstrumentDescriptor:
    instrument_id: str
    display_name: str
    physical_group: str
    capabilities: frozenset[str] = frozenset()
    integrated: bool = False


@dataclass(frozen=True, slots=True)
class ProgressUpdate:
    instrument_id: str
    progress: float | None
    phase: str
    message: str

    def __post_init__(self) -> None:
        _validate_percentage("progress", self.progress)


Metadata = Mapping[str, Any]
