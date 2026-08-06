"""Explicit time metadata models; raw timestamps are never overwritten."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from .compat import StrEnum
from pathlib import Path
from typing import Mapping


# What the AirFloX record clock is set to. FLOX and FULL write campaign local
# time and their GPS often never locks, so nothing in the files says how far
# that is from UTC; the operator declares it when the scan finds SIF. None
# means undeclared, and the scan asks.
#
# Here rather than in the dashboard because both consumers need it and they sit
# on opposite sides of the application: detection reads it to place SIF on the
# timeline, and the SIF adapter reads it to convert the record clock it hands
# to the preserved reader.
SIF_RECORD_CLOCK_TIMEZONES: Mapping[str, Mapping[str, object]] = {
    "utc": {"label": "UTC", "offset_seconds": 0},
    "cest": {"label": "CEST (UTC+2)", "offset_seconds": 7200},
}


def sif_record_clock_declaration(key: object) -> tuple[float, str] | None:
    """The declared offset to subtract to reach UTC, and its label.

    None when the operator has not answered yet, or answered with something
    that is no longer offered.
    """
    if key is None:
        return None
    normalized = str(key).strip().casefold()
    selection = SIF_RECORD_CLOCK_TIMEZONES.get(normalized)
    if selection is None:
        return None
    return float(selection["offset_seconds"]), str(selection["label"])


class TimezoneState(StrEnum):
    EXPLICIT_UTC = "explicit_utc"
    EXPLICIT_OFFSET = "explicit_offset"
    NAMED_ZONE = "named_zone"
    NAIVE = "naive"
    UNKNOWN = "unknown"


class TimestampQualityFlag(StrEnum):
    VALID = "valid"
    MISSING = "missing"
    INVALID = "invalid"
    DUPLICATE = "duplicate"
    NON_MONOTONIC = "non_monotonic"
    TIMEZONE_UNKNOWN = "timezone_unknown"


@dataclass(frozen=True, slots=True)
class TimeRange:
    original_start: datetime | None = None
    original_end: datetime | None = None
    utc_start: datetime | None = None
    utc_end: datetime | None = None
    timezone_state: TimezoneState = TimezoneState.UNKNOWN
    precision: str | None = None
    basis: str = "unknown"


@dataclass(frozen=True, slots=True)
class TimestampQualitySample:
    source_file: Path
    row_number: int
    original_timestamp: str
    parsed_timestamp: datetime | None
    quality_flags: tuple[TimestampQualityFlag, ...]


@dataclass(frozen=True, slots=True)
class TimeRangeResult:
    instrument_id: str
    source_file_count: int
    source_file_samples: tuple[Path, ...]
    timestamp_columns: tuple[str, ...]
    original_min_timestamp: str | None
    original_max_timestamp: str | None
    parsed_start_time: datetime | None
    parsed_end_time: datetime | None
    utc_start_time: datetime | None
    utc_end_time: datetime | None
    timestamp_format: str | None
    timezone_information: str
    applied_time_offset: timedelta
    timestamp_quality_warnings: tuple[str, ...]
    duplicated_timestamps: int
    missing_timestamps: int
    invalid_timestamps: int
    non_monotonic_timestamps: int
    valid_timestamps: int
    records_examined: int
    missing_timestamp_columns: tuple[Path, ...]
    quality_samples: tuple[TimestampQualitySample, ...]
    # What the source files actually cover, per file, so that a gap between
    # them stays visible. The start and end above are only the envelope: SIF
    # reported 07:19 - 16:54 while nothing at all was recorded from 11:05 to
    # 13:21, and a selection inside that gap was shown as fully available.
    coverage_segments: tuple[tuple[datetime, datetime], ...] = ()

    @property
    def has_valid_timestamps(self) -> bool:
        return self.valid_timestamps > 0

    @property
    def has_utc_range(self) -> bool:
        return self.utc_start_time is not None and self.utc_end_time is not None


@dataclass(frozen=True, slots=True)
class GlobalTimeRangeResult:
    instrument_ranges: Mapping[str, TimeRangeResult]
    global_earliest_timestamp: datetime | None
    global_latest_timestamp: datetime | None
    common_overlap_start: datetime | None
    common_overlap_end: datetime | None
    common_overlap_exists: bool
    instruments_outside_expected_interval: tuple[str, ...]
    instruments_with_no_valid_timestamps: tuple[str, ...]
    instruments_without_utc_range: tuple[str, ...]
    warnings: tuple[str, ...]
