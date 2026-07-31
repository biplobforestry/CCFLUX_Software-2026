"""Streaming generic and instrument-aware timestamp extraction."""

from __future__ import annotations

import csv
import io
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import MappingProxyType
from typing import Iterable, Iterator, Mapping
from zoneinfo import ZoneInfo

from PIL import ExifTags, Image, UnidentifiedImageError

from .time_manager import (
    GlobalTimeRangeResult,
    TimeRangeResult,
    TimestampQualityFlag,
    TimestampQualitySample,
)

MAX_SOURCE_FILE_SAMPLES = 20
MAX_QUALITY_SAMPLES = 100
FLIR_EDGE_SCAN_BYTES = 16 * 1024 * 1024
FLIR_TIMESTAMP_BYTES_RE = re.compile(
    rb'"timestamp"\s*:\s*(?:\{\s*"\$date"\s*:\s*)?"([^"\\]+)"'
)


@dataclass(frozen=True, slots=True)
class _RawTimestamp:
    original: str
    row_number: int
    source_file: Path
    format_hint: str | None = None
    assume_utc: bool = False
    timezone_name: str | None = None


@dataclass(frozen=True, slots=True)
class _ParsedTimestamp:
    value: datetime
    format_name: str
    timezone_label: str


@dataclass(slots=True)
class _Accumulator:
    instrument_id: str
    source_file_count: int = 0
    source_file_samples: list[Path] = field(default_factory=list)
    timestamp_columns: set[str] = field(default_factory=set)
    missing_timestamp_columns: list[Path] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    formats: set[str] = field(default_factory=set)
    timezone_labels: set[str] = field(default_factory=set)
    seen: set[tuple[str, str]] = field(default_factory=set)
    quality_samples: list[TimestampQualitySample] = field(default_factory=list)
    records_examined: int = 0
    valid: int = 0
    missing: int = 0
    invalid: int = 0
    duplicates: int = 0
    non_monotonic: int = 0
    aware_min: tuple[datetime, str] | None = None
    aware_max: tuple[datetime, str] | None = None
    naive_min: tuple[datetime, str] | None = None
    naive_max: tuple[datetime, str] | None = None
    previous_in_file: datetime | None = None
    applied_time_offsets: set[timedelta] = field(default_factory=set)

    def begin_file(self, path: Path) -> None:
        self.source_file_count += 1
        if len(self.source_file_samples) < MAX_SOURCE_FILE_SAMPLES:
            self.source_file_samples.append(path)
        self.previous_in_file = None

    def observe(self, raw: _RawTimestamp) -> None:
        self.records_examined += 1
        original = raw.original
        if not original.strip():
            self.missing += 1
            self._sample(raw, None, (TimestampQualityFlag.MISSING,))
            return
        try:
            parsed = _parse_timestamp(
                original,
                format_hint=raw.format_hint,
                assume_utc=raw.assume_utc,
                timezone_name=raw.timezone_name,
            )
        except (ValueError, OverflowError, OSError):
            self.invalid += 1
            self._sample(raw, None, (TimestampQualityFlag.INVALID,))
            return

        value = parsed.value
        if raw.timezone_name and value.utcoffset() is not None:
            self.applied_time_offsets.add(-value.utcoffset())
        self.valid += 1
        self.formats.add(parsed.format_name)
        self.timezone_labels.add(parsed.timezone_label)
        flags = [TimestampQualityFlag.VALID]
        if value.tzinfo is None:
            flags.append(TimestampQualityFlag.TIMEZONE_UNKNOWN)
            normalized = value
            identity = ("naive", value.isoformat())
        else:
            normalized = value.astimezone(timezone.utc)
            identity = ("utc", normalized.isoformat())

        if identity in self.seen:
            self.duplicates += 1
            flags.append(TimestampQualityFlag.DUPLICATE)
        else:
            self.seen.add(identity)

        if self.previous_in_file is not None:
            previous = self.previous_in_file
            comparable = (previous.tzinfo is None) == (value.tzinfo is None)
            if comparable:
                previous_value = (
                    previous
                    if previous.tzinfo is None
                    else previous.astimezone(timezone.utc)
                )
                if normalized < previous_value:
                    self.non_monotonic += 1
                    flags.append(TimestampQualityFlag.NON_MONOTONIC)
        self.previous_in_file = value

        if value.tzinfo is None:
            self.naive_min = _minimum(self.naive_min, normalized, original)
            self.naive_max = _maximum(self.naive_max, normalized, original)
        else:
            self.aware_min = _minimum(self.aware_min, normalized, original)
            self.aware_max = _maximum(self.aware_max, normalized, original)
        self._sample(raw, value, tuple(flags))

    def _sample(
        self,
        raw: _RawTimestamp,
        parsed: datetime | None,
        flags: tuple[TimestampQualityFlag, ...],
    ) -> None:
        if len(self.quality_samples) >= MAX_QUALITY_SAMPLES:
            return
        self.quality_samples.append(
            TimestampQualitySample(
                source_file=raw.source_file,
                row_number=raw.row_number,
                original_timestamp=raw.original,
                parsed_timestamp=parsed,
                quality_flags=flags,
            )
        )

    def result(self) -> TimeRangeResult:
        warnings = list(self.warnings)
        if self.duplicates:
            warnings.append(f"{self.duplicates} duplicated timestamp(s) detected.")
        if self.missing:
            warnings.append(f"{self.missing} missing timestamp value(s) detected.")
        if self.invalid:
            warnings.append(f"{self.invalid} invalid timestamp value(s) detected.")
        if self.non_monotonic:
            warnings.append(
                f"{self.non_monotonic} non-monotonic timestamp transition(s) detected."
            )
        if self.aware_min and self.naive_min:
            warnings.append(
                "Mixed timezone-aware and timezone-naive timestamps; naive values "
                "are excluded from UTC range calculations."
            )
        if self.valid and not self.aware_min:
            warnings.append(
                "Valid timestamps have no confirmed timezone and are excluded "
                "from global UTC calculations."
            )
        if not self.valid:
            warnings.append("No valid timestamps were found.")

        selected_min = self.aware_min or self.naive_min
        selected_max = self.aware_max or self.naive_max
        parsed_start = selected_min[0] if selected_min else None
        parsed_end = selected_max[0] if selected_max else None
        utc_start = self.aware_min[0] if self.aware_min else None
        utc_end = self.aware_max[0] if self.aware_max else None
        return TimeRangeResult(
            instrument_id=self.instrument_id,
            source_file_count=self.source_file_count,
            source_file_samples=tuple(self.source_file_samples),
            timestamp_columns=tuple(sorted(self.timestamp_columns)),
            original_min_timestamp=selected_min[1] if selected_min else None,
            original_max_timestamp=selected_max[1] if selected_max else None,
            parsed_start_time=parsed_start,
            parsed_end_time=parsed_end,
            utc_start_time=utc_start,
            utc_end_time=utc_end,
            timestamp_format=_format_summary(self.formats),
            timezone_information=_timezone_summary(self.timezone_labels),
            applied_time_offset=(
                next(iter(self.applied_time_offsets))
                if len(self.applied_time_offsets) == 1
                else timedelta(0)
            ),
            timestamp_quality_warnings=tuple(dict.fromkeys(warnings + self.errors)),
            duplicated_timestamps=self.duplicates,
            missing_timestamps=self.missing,
            invalid_timestamps=self.invalid,
            non_monotonic_timestamps=self.non_monotonic,
            valid_timestamps=self.valid,
            records_examined=self.records_examined,
            missing_timestamp_columns=tuple(self.missing_timestamp_columns),
            quality_samples=tuple(self.quality_samples),
        )


class TimestampExtractor:
    """Extract timestamps without modifying source files or timestamp values."""

    def extract_instrument(
        self, instrument_id: str, source_files: Iterable[Path]
    ) -> TimeRangeResult:
        accumulator = _Accumulator(instrument_id=instrument_id)
        for source_file in source_files:
            path = Path(source_file)
            accumulator.begin_file(path)
            try:
                self._extract_file(instrument_id, path, accumulator)
            except (OSError, UnicodeError, csv.Error) as exc:
                accumulator.errors.append(f"Cannot inspect timestamps in {path}: {exc}")
        return accumulator.result()

    def extract_all(
        self,
        datasets: Mapping[str, Iterable[Path]],
        *,
        expected_flight_start: datetime | None = None,
        expected_flight_end: datetime | None = None,
    ) -> GlobalTimeRangeResult:
        if (expected_flight_start is None) != (expected_flight_end is None):
            raise ValueError(
                "expected_flight_start and expected_flight_end must be provided together"
            )
        if expected_flight_start is not None:
            if expected_flight_start.tzinfo is None or expected_flight_end.tzinfo is None:
                raise ValueError("expected flight interval must be timezone-aware")
            if expected_flight_end < expected_flight_start:
                raise ValueError("expected flight end cannot precede start")

        results = {
            instrument_id: self.extract_instrument(instrument_id, files)
            for instrument_id, files in datasets.items()
        }
        utc_results = [value for value in results.values() if value.has_utc_range]
        no_valid = tuple(
            sorted(
                key for key, value in results.items() if not value.has_valid_timestamps
            )
        )
        without_utc = tuple(
            sorted(
                key
                for key, value in results.items()
                if value.has_valid_timestamps and not value.has_utc_range
            )
        )
        earliest = (
            min(value.utc_start_time for value in utc_results) if utc_results else None
        )
        latest = (
            max(value.utc_end_time for value in utc_results) if utc_results else None
        )
        overlap_start = (
            max(value.utc_start_time for value in utc_results) if utc_results else None
        )
        overlap_end = (
            min(value.utc_end_time for value in utc_results) if utc_results else None
        )
        overlap_exists = bool(
            utc_results and overlap_start is not None and overlap_start <= overlap_end
        )
        if not overlap_exists:
            overlap_start = None
            overlap_end = None

        outside: list[str] = []
        if expected_flight_start is not None and expected_flight_end is not None:
            expected_start = expected_flight_start.astimezone(timezone.utc)
            expected_end = expected_flight_end.astimezone(timezone.utc)
            for instrument_id, result in results.items():
                if not result.has_utc_range:
                    continue
                if (
                    result.utc_end_time < expected_start
                    or result.utc_start_time > expected_end
                ):
                    outside.append(instrument_id)

        warnings: list[str] = []
        if without_utc:
            warnings.append(
                "Timezone-unknown instruments excluded from global UTC calculations: "
                + ", ".join(without_utc)
            )
        if no_valid:
            warnings.append(
                "Instruments with no valid timestamps: " + ", ".join(no_valid)
            )
        if len(utc_results) > 1 and not overlap_exists:
            warnings.append("Detected instruments have no common UTC overlap.")
        return GlobalTimeRangeResult(
            instrument_ranges=MappingProxyType(results),
            global_earliest_timestamp=earliest,
            global_latest_timestamp=latest,
            common_overlap_start=overlap_start,
            common_overlap_end=overlap_end,
            common_overlap_exists=overlap_exists,
            instruments_outside_expected_interval=tuple(sorted(outside)),
            instruments_with_no_valid_timestamps=no_valid,
            instruments_without_utc_range=without_utc,
            warnings=tuple(warnings),
        )

    def _extract_file(
        self, instrument_id: str, path: Path, accumulator: _Accumulator
    ) -> None:
        if instrument_id == "noseboom":
            self._delimited(
                path,
                accumulator,
                primary_columns=(
                    ("Airflow_UTCcorr_Nanoseconds_ns", "unix_epoch_nanoseconds", True),
                    ("TIMESTAMP", None, False),
                ),
            )
        elif instrument_id == "miro":
            if path.suffix.casefold() != ".txt":
                # TDMS is detectable, but its timestamp schema is not confirmed.
                # Never interpret the binary container as delimited text.
                return
            self._delimited(
                path,
                accumulator,
                primary_columns=(("t-stamp", "%d.%m.%Y %H:%M:%S,%f", True),),
                delimiter=";",
            )
        elif instrument_id == "picarro":
            self._picarro(path, accumulator)
        elif instrument_id in {"opc_hbx4", "opc_hbx5", "partector"}:
            self._delimited(
                path,
                accumulator,
                primary_columns=(("_time", None, False),),
            )
        elif instrument_id == "ins_gimbal":
            self._ins_gimbal(path, accumulator)
        elif instrument_id == "sif":
            self._sif(path, accumulator)
        elif instrument_id == "gopro":
            self._gopro(path, accumulator)
        elif instrument_id == "flir":
            self._flir(path, accumulator)
        elif instrument_id == "micasense":
            accumulator.warnings.append(
                f"{instrument_id}: no confirmed camera timestamp field or EXIF "
                "tag is configured; filesystem modification times were not used."
            )
        else:
            self._delimited(
                path,
                accumulator,
                primary_columns=(
                    ("timestamp", None, False),
                    ("datetime", None, False),
                    ("_time", None, False),
                    ("TIMESTAMP", None, False),
                    ("date_time_utc", None, True),
                    ("datetime [UTC]", None, True),
                ),
            )

    def _gopro(self, path: Path, accumulator: _Accumulator) -> None:
        """Correct GoPro's Europe/Berlin camera clock as soon as it is detected."""
        if path.suffix.casefold() not in {".jpg", ".jpeg", ".png"}:
            return
        try:
            with Image.open(path) as image:
                exif = {
                    ExifTags.TAGS.get(tag, str(tag)): value
                    for tag, value in image.getexif().items()
                }
        except (OSError, ValueError, UnidentifiedImageError) as exc:
            accumulator.warnings.append(
                f"{path}: GoPro EXIF metadata could not be read: {exc}"
            )
            return
        value = exif.get("DateTimeOriginal") or exif.get("DateTime")
        if not value:
            accumulator.missing += 1
            accumulator.warnings.append(
                f"{path}: missing GoPro DateTimeOriginal EXIF timestamp"
            )
            return
        accumulator.timestamp_columns.add("EXIF DateTimeOriginal")
        accumulator.observe(
            _RawTimestamp(
                str(value),
                1,
                path,
                format_hint="%Y:%m:%d %H:%M:%S",
                timezone_name="Europe/Berlin",
            )
        )

    def _flir(self, path: Path, accumulator: _Accumulator) -> None:
        """Read confirmed UTC timestamps from the edges of a FLIR JSON stream."""
        if path.suffix.casefold() != ".json":
            return
        try:
            size = path.stat().st_size
            offsets = [0]
            if size > FLIR_EDGE_SCAN_BYTES:
                offsets.append(max(0, size - FLIR_EDGE_SCAN_BYTES))
            matches: list[tuple[int, str]] = []
            with path.open("rb") as stream:
                for offset in offsets:
                    stream.seek(offset)
                    data = stream.read(min(FLIR_EDGE_SCAN_BYTES, size - offset))
                    for match in FLIR_TIMESTAMP_BYTES_RE.finditer(data):
                        try:
                            value = match.group(1).decode("utf-8")
                        except UnicodeDecodeError:
                            continue
                        matches.append((offset + match.start(), value))
        except OSError as exc:
            accumulator.warnings.append(
                f"{path}: FLIR timestamp metadata could not be read: {exc}"
            )
            return
        if not matches:
            accumulator.missing += 1
            accumulator.warnings.append(
                f"{path}: no FLIR JSON timestamp field was found"
            )
            return
        matches.sort(key=lambda item: item[0])
        selected = [matches[0]]
        if matches[-1][0] != matches[0][0]:
            selected.append(matches[-1])
        accumulator.timestamp_columns.add("timestamp")
        for row_number, (_, value) in enumerate(selected, start=1):
            accumulator.observe(
                _RawTimestamp(value, row_number, path, assume_utc=True)
            )

    def _ins_gimbal(self, path: Path, accumulator: _Accumulator) -> None:
        """Normalize the known Influx export rotation without changing raw data."""
        with path.open("r", encoding="utf-8-sig", errors="replace", newline="") as stream:
            reader = csv.reader(stream, strict=True)
            try:
                header = [value.strip() for value in next(reader)]
            except StopIteration:
                accumulator.missing_timestamp_columns.append(path)
                return
            if "_time" not in header:
                accumulator.missing_timestamp_columns.append(path)
                accumulator.warnings.append(f"{path}: missing timestamp column _time")
                return
            index = header.index("_time")
            accumulator.timestamp_columns.add("_time")
            raw_values = [
                _RawTimestamp(
                    row[index] if index < len(row) else "",
                    row_number,
                    path,
                )
                for row_number, row in enumerate(reader, start=2)
            ]
        parsed: list[datetime | None] = []
        for raw in raw_values:
            try:
                parsed.append(
                    _parse_timestamp(
                        raw.original, format_hint=None, assume_utc=False
                    ).value
                )
            except (ValueError, OverflowError, OSError):
                parsed.append(None)
        reversals = [
            position
            for position in range(1, len(parsed))
            if parsed[position - 1] is not None
            and parsed[position] is not None
            and parsed[position] < parsed[position - 1]
        ]
        if len(reversals) == 1:
            split = reversals[0]
            prefix = [value for value in parsed[:split] if value is not None]
            suffix = [value for value in parsed[split:] if value is not None]
            if prefix and suffix and max(suffix) <= min(prefix):
                raw_values = raw_values[split:] + raw_values[:split]
        for raw in raw_values:
            accumulator.observe(raw)

    def _delimited(
        self,
        path: Path,
        accumulator: _Accumulator,
        *,
        primary_columns: tuple[tuple[str, str | None, bool], ...],
        delimiter: str | None = None,
    ) -> None:
        with path.open("r", encoding="utf-8-sig", errors="replace", newline="") as stream:
            first_line = stream.readline()
            if not first_line:
                accumulator.missing_timestamp_columns.append(path)
                accumulator.warnings.append(f"{path}: empty file")
                return
            selected_delimiter = delimiter or _detect_delimiter(first_line)
            stream.seek(0)
            reader = csv.reader(stream, delimiter=selected_delimiter, strict=True)
            try:
                header = next(reader)
            except StopIteration:
                accumulator.missing_timestamp_columns.append(path)
                return
            header = [value.strip() for value in header]
            selected = next(
                (
                    (header.index(column), column, hint, assume_utc)
                    for column, hint, assume_utc in primary_columns
                    if column in header
                ),
                None,
            )
            if selected is None:
                accumulator.missing_timestamp_columns.append(path)
                accumulator.warnings.append(
                    f"{path}: missing timestamp column; expected one of "
                    + ", ".join(item[0] for item in primary_columns)
                )
                return
            index, column, hint, assume_utc = selected
            accumulator.timestamp_columns.add(column)
            for row_number, row in enumerate(reader, start=2):
                value = row[index] if index < len(row) else ""
                accumulator.observe(
                    _RawTimestamp(value, row_number, path, hint, assume_utc)
                )

    def _picarro(self, path: Path, accumulator: _Accumulator) -> None:
        with path.open("r", encoding="utf-8-sig", errors="replace") as stream:
            header_line = stream.readline()
            columns = header_line.split()
            if "DATE" not in columns or "TIME" not in columns:
                accumulator.missing_timestamp_columns.append(path)
                accumulator.warnings.append(
                    f"{path}: missing Picarro DATE and/or TIME columns"
                )
                return
            date_index, time_index = columns.index("DATE"), columns.index("TIME")
            accumulator.timestamp_columns.update(("DATE", "TIME"))
            for row_number, line in enumerate(stream, start=2):
                values = line.split()
                date = values[date_index] if date_index < len(values) else ""
                time_value = values[time_index] if time_index < len(values) else ""
                original = f"{date} {time_value}".strip()
                # Campaign delivery timestamps are defined as UTC. The original
                # DATE/TIME strings remain unchanged in the quality samples.
                accumulator.observe(
                    _RawTimestamp(original, row_number, path, None, True)
                )

    def _sif(self, path: Path, accumulator: _Accumulator) -> None:
        with path.open("r", encoding="utf-8-sig", errors="replace", newline="") as stream:
            first_line = stream.readline()
            if "datetime [UTC]" in first_line:
                stream.seek(0)
                reader = csv.reader(stream, delimiter=";", strict=True)
                header = [value.strip() for value in next(reader)]
                index = header.index("datetime [UTC]")
                accumulator.timestamp_columns.add("datetime [UTC]")
                for row_number, row in enumerate(reader, start=2):
                    value = row[index] if index < len(row) else ""
                    accumulator.observe(
                        _RawTimestamp(value, row_number, path, None, True)
                    )
                return

            stream.seek(0)
            reader = csv.reader(stream, delimiter=";", strict=True)
            found = False
            for row_number, row in enumerate(reader, start=1):
                if len(row) < 3:
                    continue
                cycle_id = row[0].strip().strip('"')
                if not cycle_id.isdigit():
                    continue
                found = True
                original = f"{row[1].strip()} {row[2].strip()}"
                accumulator.observe(
                    _RawTimestamp(
                        original,
                        row_number,
                        path,
                        "%y%m%d %H%M%S",
                        True,
                    )
                )
            if found:
                accumulator.timestamp_columns.update(
                    ("cycle_id", "record_date", "record_time")
                )
            else:
                accumulator.missing_timestamp_columns.append(path)
                accumulator.warnings.append(
                    f"{path}: no SIF datetime [UTC] column or raw record time found"
                )


def _detect_delimiter(header: str) -> str:
    counts = {value: header.count(value) for value in (",", ";", "\t")}
    delimiter, count = max(counts.items(), key=lambda item: item[1])
    return delimiter if count else ","


def _parse_timestamp(
    original: str,
    *,
    format_hint: str | None,
    assume_utc: bool,
    timezone_name: str | None = None,
) -> _ParsedTimestamp:
    text = original.strip().strip('"')
    if format_hint == "unix_epoch_nanoseconds":
        nanoseconds = int(text)
        value = datetime.fromtimestamp(nanoseconds / 1_000_000_000, tz=timezone.utc)
        return _ParsedTimestamp(value, format_hint, "UTC (Unix epoch)")

    formats: list[tuple[str, str]] = []
    if format_hint:
        formats.append((format_hint, format_hint))
    formats.extend(
        (
            ("%Y-%m-%d %H:%M:%S.%f", "%Y-%m-%d %H:%M:%S.%f"),
            ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M:%S"),
            ("%d.%m.%Y %H:%M:%S,%f", "%d.%m.%Y %H:%M:%S,%f"),
            ("%m-%d-%Y %H:%M", "%m-%d-%Y %H:%M"),
            ("%y%m%d %H%M%S", "%y%m%d %H%M%S"),
        )
    )

    value: datetime | None = None
    format_name = ""
    iso_text = text[:-1] + "+00:00" if text.endswith(("Z", "z")) else text
    try:
        value = datetime.fromisoformat(iso_text)
        format_name = "ISO 8601"
    except ValueError:
        for pattern, name in formats:
            try:
                value = datetime.strptime(text, pattern)
                format_name = name
                break
            except ValueError:
                continue
    if value is None:
        raise ValueError(f"Unsupported timestamp: {original}")

    if value.tzinfo is None and timezone_name:
        value = value.replace(tzinfo=ZoneInfo(timezone_name))
        timezone_label = f"{timezone_name} camera clock → UTC"
    elif value.tzinfo is None and assume_utc:
        value = value.replace(tzinfo=timezone.utc)
        timezone_label = "UTC (instrument field semantics)"
    elif value.tzinfo is None:
        timezone_label = "naive/unspecified"
    else:
        offset = value.utcoffset()
        timezone_label = (
            "UTC"
            if offset == timedelta(0)
            else f"explicit UTC offset {offset}"
        )
    return _ParsedTimestamp(value, format_name, timezone_label)


def _minimum(
    current: tuple[datetime, str] | None, value: datetime, original: str
) -> tuple[datetime, str]:
    return (value, original) if current is None or value < current[0] else current


def _maximum(
    current: tuple[datetime, str] | None, value: datetime, original: str
) -> tuple[datetime, str]:
    return (value, original) if current is None or value > current[0] else current


def _format_summary(formats: set[str]) -> str | None:
    if not formats:
        return None
    values = sorted(formats)
    return values[0] if len(values) == 1 else "mixed: " + ", ".join(values)


def _timezone_summary(labels: set[str]) -> str:
    if not labels:
        return "unknown"
    values = sorted(labels)
    return values[0] if len(values) == 1 else "mixed: " + ", ".join(values)
