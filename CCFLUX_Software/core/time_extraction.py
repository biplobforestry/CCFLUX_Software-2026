"""Streaming generic and instrument-aware timestamp extraction."""

from __future__ import annotations

import copy
import dataclasses
import csv
from concurrent.futures import ThreadPoolExecutor
import io
import re
import zipfile
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import MappingProxyType
from typing import Any, Iterable, Iterator, Mapping
from zoneinfo import ZoneInfo

from PIL import ExifTags, Image, UnidentifiedImageError

from .noseboom_columns import normalize_columns
from .time_manager import (
    GlobalTimeRangeResult,
    TimeRangeResult,
    TimestampQualityFlag,
    TimestampQualitySample,
)

MAX_SOURCE_FILE_SAMPLES = 20
MAX_QUALITY_SAMPLES = 100
# Coverage is recorded per source file so a gap between files stays visible.
# A camera delivers one segment per frame, so the list is capped by merging the
# narrowest gaps first; that only ever widens coverage, never invents it.
MAX_COVERAGE_SEGMENTS = 256
FLIR_EDGE_SCAN_BYTES = 16 * 1024 * 1024
FLIR_TIMESTAMP_BYTES_RE = re.compile(
    rb'"timestamp"\s*:\s*(?:\{\s*"\$date"\s*:\s*)?"([^"\\]+)"'
)


def merge_coverage_segments(
    segments: Iterable[tuple[datetime, datetime]],
    *,
    limit: int = MAX_COVERAGE_SEGMENTS,
) -> tuple[tuple[datetime, datetime], ...]:
    """Overlapping and touching segments become one, then the list is capped.

    Capping closes the narrowest gaps first, so the widest gaps - the ones that
    can leave a selected interval with no data - are the last to disappear.
    """
    ordered = sorted(
        (pair for pair in segments if pair[0] is not None and pair[1] is not None),
        key=lambda pair: (pair[0], pair[1]),
    )
    if not ordered:
        return ()
    merged: list[list[datetime]] = [[ordered[0][0], ordered[0][1]]]
    for start, end in ordered[1:]:
        if start <= merged[-1][1]:
            if end > merged[-1][1]:
                merged[-1][1] = end
        else:
            merged.append([start, end])
    while len(merged) > max(1, limit):
        narrowest = min(
            range(1, len(merged)),
            key=lambda index: merged[index][0] - merged[index - 1][1],
        )
        merged[narrowest - 1][1] = max(merged[narrowest - 1][1], merged[narrowest][1])
        del merged[narrowest]
    return tuple((start, end) for start, end in merged)


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
    coverage_segments: list[tuple[datetime, datetime]] = field(default_factory=list)
    file_aware_min: datetime | None = None
    file_aware_max: datetime | None = None
    unset_clock_images: int = 0

    def begin_file(self, path: Path) -> None:
        self._close_file_segment()
        self.source_file_count += 1
        if len(self.source_file_samples) < MAX_SOURCE_FILE_SAMPLES:
            self.source_file_samples.append(path)
        self.previous_in_file = None

    def _close_file_segment(self) -> None:
        """Record what the file just read actually covered."""
        if self.file_aware_min is not None and self.file_aware_max is not None:
            self.coverage_segments.append((self.file_aware_min, self.file_aware_max))
        self.file_aware_min = None
        self.file_aware_max = None

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
            if self.file_aware_min is None or normalized < self.file_aware_min:
                self.file_aware_min = normalized
            if self.file_aware_max is None or normalized > self.file_aware_max:
                self.file_aware_max = normalized
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
        self._close_file_segment()
        warnings = list(self.warnings)
        if self.unset_clock_images:
            warnings.append(
                _unset_clock_warning(self.unset_clock_images, bool(self.aware_min))
            )
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
            coverage_segments=merge_coverage_segments(self.coverage_segments),
        )


def _snapshot(accumulator) -> dict:
    """Copy an accumulator's fields. It uses slots, so there is no __dict__."""
    return {
        item.name: copy.deepcopy(getattr(accumulator, item.name))
        for item in dataclasses.fields(accumulator)
    }


def _restore(accumulator, snapshot: dict) -> None:
    for name, value in snapshot.items():
        setattr(accumulator, name, value)


class TimestampExtractor:
    """Extract timestamps without modifying source files or timestamp values."""

    # Reading one GoPro frame's EXIF costs a file open on the camera disk, and a
    # flight holds thousands. The reads are independent, so they overlap: the
    # campaign's 3,651 frames go from 12.2 s to 9.2 s. The disk plateaus at four
    # concurrent readers, so asking for more only adds threads.
    GOPRO_EXIF_READERS = 4
    GOPRO_PARALLEL_THRESHOLD = 64

    def extract_instrument(
        self, instrument_id: str, source_files: Iterable[Path]
    ) -> TimeRangeResult:
        accumulator = _Accumulator(instrument_id=instrument_id)
        source_files = list(source_files)
        if (
            instrument_id == "gopro"
            and len(source_files) >= self.GOPRO_PARALLEL_THRESHOLD
        ):
            self._gopro_exif = self._read_gopro_exif(source_files)
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
        # Campaign convention: every instrument records UTC. GoPro alone writes a
        # local Europe/Berlin camera clock and is converted explicitly below.
        # A timestamp that already carries an offset always keeps it — assuming
        # UTC only rescues naive values, which would otherwise be classed as
        # "timezone unknown" and dropped from every UTC range calculation,
        # silently making the instrument unprocessable.
        if instrument_id == "noseboom":
            self._delimited(
                path,
                accumulator,
                primary_columns=(
                    ("Airflow_UTCcorr_Nanoseconds_ns", "unix_epoch_nanoseconds", True),
                    ("TIMESTAMP", None, True),
                ),
                normalize_names=True,
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
                primary_columns=(("_time", None, True),),
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
            self._micasense(path, accumulator)
        else:
            self._delimited(
                path,
                accumulator,
                primary_columns=(
                    ("timestamp", None, True),
                    ("datetime", None, True),
                    ("_time", None, True),
                    ("TIMESTAMP", None, True),
                    ("date_time_utc", None, True),
                    ("datetime [UTC]", None, True),
                ),
            )

    @staticmethod
    def _gopro_capture_value(path: Path) -> tuple[str | None, str | None]:
        """One frame's capture time, or the reason it could not be read."""
        try:
            with Image.open(path) as image:
                exif = {
                    ExifTags.TAGS.get(tag, str(tag)): value
                    for tag, value in image.getexif().items()
                }
        except (OSError, ValueError, UnidentifiedImageError) as exc:
            return None, f"{path}: GoPro EXIF metadata could not be read: {exc}"
        value = exif.get("DateTimeOriginal") or exif.get("DateTime")
        return (str(value) if value else None), None

    def _read_gopro_exif(
        self, paths: list[Path]
    ) -> dict[Path, tuple[str | None, str | None]]:
        images = [
            path for path in paths
            if path.suffix.casefold() in {".jpg", ".jpeg", ".png"}
        ]
        if not images:
            return {}
        with ThreadPoolExecutor(max_workers=self.GOPRO_EXIF_READERS) as pool:
            return dict(zip(images, pool.map(self._gopro_capture_value, images)))

    def _gopro(self, path: Path, accumulator: _Accumulator) -> None:
        """Correct GoPro's Europe/Berlin camera clock as soon as it is detected."""
        if path.suffix.casefold() not in {".jpg", ".jpeg", ".png"}:
            return
        cached = getattr(self, "_gopro_exif", {}).get(path)
        value, failure = (
            cached if cached is not None else self._gopro_capture_value(path)
        )
        if failure:
            accumulator.warnings.append(failure)
            return
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

    def _micasense(self, path: Path, accumulator: _Accumulator) -> None:
        """Read EXIF acquisition time from a TIFF, or from TIFFs inside a ZIP.

        MicaSense deliveries arrive either as loose TIFFs or as one ZIP per
        capture. Without this the instrument reported no UTC coverage at all,
        so the Time Filter could not include it and its images were evaluated
        outside the selected interval. Campaign convention is UTC.
        """
        suffix = path.suffix.casefold()
        if suffix in {".tif", ".tiff"}:
            value = _exif_original_datetime(path)
            if value is None:
                accumulator.missing += 1
                return
            if _is_unset_camera_clock(value):
                accumulator.invalid += 1
                accumulator.unset_clock_images += 1
                return
            accumulator.timestamp_columns.add("EXIF DateTimeOriginal")
            accumulator.observe(
                _RawTimestamp(
                    value, 1, path, format_hint="%Y:%m:%d %H:%M:%S", assume_utc=True
                )
            )
            return
        if suffix != ".zip":
            return
        try:
            with zipfile.ZipFile(path) as archive:
                members = [
                    name for name in archive.namelist()
                    if name.casefold().endswith((".tif", ".tiff"))
                ]
                # One capture holds a band per file with the same trigger time;
                # the first and last bound the archive without unpacking it all.
                for row_number, name in enumerate(
                    sorted(members)[:1] + sorted(members)[-1:], start=1
                ):
                    with archive.open(name) as member:
                        value = _exif_original_datetime(io.BytesIO(member.read()))
                    if value is None:
                        continue
                    if _is_unset_camera_clock(value):
                        accumulator.invalid += 1
                        accumulator.unset_clock_images += 1
                        continue
                    accumulator.timestamp_columns.add("EXIF DateTimeOriginal")
                    accumulator.observe(
                        _RawTimestamp(
                            value,
                            row_number,
                            path,
                            format_hint="%Y:%m:%d %H:%M:%S",
                            assume_utc=True,
                        )
                    )
        except (OSError, zipfile.BadZipFile, ValueError) as exc:
            accumulator.warnings.append(
                f"{path}: MicaSense archive could not be inspected: {exc}"
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
                    assume_utc=True,
                )
                for row_number, row in enumerate(reader, start=2)
            ]
        parsed: list[datetime | None] = []
        for raw in raw_values:
            try:
                parsed.append(
                    _parse_timestamp(
                        raw.original, format_hint=None, assume_utc=True
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
        normalize_names: bool = False,
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
            if normalize_names:
                # Renaming is one-to-one, so every column keeps its position and
                # the fast single-column split below is unaffected.
                header = normalize_columns(header, source=path.name)
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
            # csv.reader parses every field of every row to reach one column.
            # The Noseboom delivery is 143 columns and 888,000 rows, so finding
            # its first and last timestamp took 19 seconds of the Initial Check.
            # Splitting only as far as the wanted column does the same job in
            # 3.2 seconds. It is only equivalent while no field is quoted, so a
            # quote anywhere abandons the fast path and re-reads the file with
            # the real parser.
            if '"' not in first_line:
                # Snapshot before the fast pass: if a quoted field turns up the
                # partial results have to be discarded, and the accumulator
                # carries too much state to unwind by hand. It is small here -
                # the quality samples are capped - so copying is cheap.
                snapshot = _snapshot(accumulator)
                stream.seek(0)
                stream.readline()
                quoted = False
                for row_number, line in enumerate(stream, start=2):
                    if '"' in line:
                        quoted = True
                        break
                    parts = line.rstrip("\r\n").split(selected_delimiter, index + 1)
                    value = parts[index] if index < len(parts) else ""
                    accumulator.observe(
                        _RawTimestamp(value, row_number, path, hint, assume_utc)
                    )
                if not quoted:
                    return
                _restore(accumulator, snapshot)
                stream.seek(0)
                reader = csv.reader(
                    stream, delimiter=selected_delimiter, strict=True
                )
                next(reader, None)
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


def _is_unset_camera_clock(value: str) -> bool:
    """Whether an EXIF stamp is a factory default rather than a real time.

    A camera powered up without a clock write dates its frames from the epoch.
    Reporting those as coverage would place the instrument decades away from
    the flight and make it look non-overlapping, which is worse than reporting
    no coverage at all and saying why.
    """
    text = str(value).strip()
    for prefix_length, separator in ((4, ":"), (4, "-")):
        head = text[:prefix_length]
        if head.isdigit() and text[prefix_length:prefix_length + 1] == separator:
            return int(head) < 2000
    return False


def _unset_clock_warning(count: int, any_placed: bool) -> str:
    """Say how much of the delivery the unset clock actually cost.

    The message used to speak for the whole instrument. On Flight_CCT0803 8
    images out of thousands carried a boot-time stamp while the rest ran from
    11:21 to 15:51, so reading that MicaSense could not be placed on the flight
    timeline at all contradicted the coverage shown beside it.
    """
    images = f"{count} image(s)"
    if any_placed:
        return (
            f"{images} carry an EXIF acquisition time before 2000, so the camera "
            "clock was not set for them. They are excluded from the coverage; "
            "the remaining images are placed on the flight timeline normally."
        )
    return (
        f"Every MicaSense EXIF acquisition time read ({images}) predates 2000, "
        "so the camera clock was not set. The images cannot be placed on the "
        "flight timeline and are processed without time filtering."
    )


def _exif_original_datetime(source: Any) -> str | None:
    """Return the raw EXIF DateTimeOriginal string, or None when absent."""
    try:
        with Image.open(source) as image:
            exif = {
                ExifTags.TAGS.get(tag, str(tag)): value
                for tag, value in image.getexif().items()
            }
    except (OSError, ValueError, UnidentifiedImageError):
        return None
    value = exif.get("DateTimeOriginal") or exif.get("DateTime")
    return str(value) if value else None


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
