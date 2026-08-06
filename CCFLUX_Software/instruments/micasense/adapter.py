"""Bounded MicaSense Level 1 metadata and image-integrity quick check.

Capture grouping, band extraction, timestamp parsing, and completeness rules are
adapted from the repository's ``MicaSense_Metadata.py`` and
``MicaSense_Flight_Metadata_Audit.py``.  No radiometric or reflectance operation
is performed here.
"""

from __future__ import annotations

import csv
import io
import json
import re
import threading
import time
import zipfile
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from math import degrees
from pathlib import Path
from statistics import median
from typing import Any, Callable, Mapping, Sequence
from xml.etree import ElementTree

from PIL import ExifTags, Image, UnidentifiedImageError

from core.detector import InputCandidate
from core.enums import DetectionStatus, ProcessingStatus
from core.logging_manager import LogLevel, ProcessingLogManager
from core.models import (
    FigureArtifact,
    InstrumentDescriptor,
    InstrumentResult,
    OutputFile,
    ProgressUpdate,
    SourceFile,
)
from core.resource_manager import CameraBatchPolicy, ResourceLimits, iter_batches
from core.scanner import ScanIndex
from core.time_manager import TimeRange, TimezoneState
from instruments.base.interface import InstrumentBase, ProgressCallback

EXPECTED_BANDS = frozenset({1, 2, 3, 4, 5, 6})
# The first campaign year. A RedEdge-P whose GPS has not locked dates its frames
# from a default clock, so anything earlier than this was not acquired when it
# says it was. Matches the --minimum-year default of the standalone MicaSense
# scientific overview, so the two agree on which captures are usable.
MINIMUM_CAPTURE_YEAR = 2025
IMAGE_SUFFIXES = frozenset({".tif", ".tiff"})
CAPTURE_PATTERN = re.compile(r"(.+?)_(\d+)\.tiff?$", re.IGNORECASE)
MetadataReader = Callable[[Path], Mapping[str, Any]]


@dataclass(slots=True)
class LoadedMicaSense:
    candidate: InputCandidate
    files: tuple[Any, ...]


@dataclass(frozen=True, slots=True)
class ArchiveImage:
    archive: Path
    member: str
    size_bytes: int
    # Only one band of a capture is decompressed for its metadata. The other
    # bands were written by the same trigger, so their acquisition time, GPS
    # and exposure are the same; what differs between them - that the band is
    # present, and how large it is - the archive's own file list already says.
    # Decompressing all six cost six times the work for the same answer.
    read_metadata: bool = True

    @property
    def name(self) -> str:
        return Path(self.member).name


class MicaSenseLevel1Adapter(InstrumentBase):
    """Metadata-only RedEdge-P audit with bounded thumbnail generation."""

    descriptor = InstrumentDescriptor(
        "micasense",
        "MicaSense Multispectral",
        "HATCHBOX",
        frozenset({"detection", "metadata", "quicklook", "thumbnails", "export"}),
        True,
    )

    def __init__(
        self,
        *,
        output_root: Path,
        flight_name: str,
        resource_limits: ResourceLimits,
        batch_policy: CameraBatchPolicy | None = None,
        metadata_reader: MetadataReader | None = None,
        logger: ProcessingLogManager | None = None,
        unusually_small_bytes: int = 64 * 1024,
    ) -> None:
        self.output_root = Path(output_root)
        self.flight_name = flight_name
        self.resource_limits = resource_limits
        self.batch_policy = batch_policy or CameraBatchPolicy(
            maximum_batch_files=32, maximum_thumbnail_count=24
        )
        self.metadata_reader = metadata_reader or _pillow_metadata
        self.logger = logger
        self.unusually_small_bytes = unusually_small_bytes
        self._callback: ProgressCallback | None = None
        self._cancelled = threading.Event()
        self._records: list[dict[str, Any]] = []
        self._captures: list[dict[str, Any]] = []

    def detect(self, scan_index: ScanIndex) -> Sequence[InputCandidate]:
        files = tuple(
            entry.path
            for entry in scan_index.entries
            if entry.is_file and entry.path.suffix.casefold() in IMAGE_SUFFIXES
        )
        if not files:
            return ()
        matching = tuple(path for path in files if CAPTURE_PATTERN.fullmatch(path.name))
        selected = matching or files
        confidence = 0.98 if matching else 0.65
        return (
            InputCandidate(
                "micasense",
                selected,
                confidence,
                "TIFF images with repository-confirmed capture/band filename pattern",
            ),
        )

    def inspect_metadata(self, candidate: InputCandidate) -> Mapping[str, Any]:
        files = _all_images(candidate.paths)
        bands = sorted(
            {band for path in files if (band := _filename_parts(path)[1]) is not None}
        )
        captures = {
            capture for path in files if (capture := _filename_parts(path)[0])
        }
        return {
            "image_count": len(files),
            "bands_identified_from_filenames": bands,
            "capture_count_from_filenames": len(captures),
            "expected_bands": sorted(EXPECTED_BANDS),
            "level": "Level 1 metadata and quality control only",
        }

    def extract_time_range(self, candidate: InputCandidate) -> TimeRange:
        values: list[datetime] = []
        for path in _all_images(candidate.paths):
            self._check_cancelled()
            try:
                value = _parse_timestamp(self._metadata(path))
            except Exception:
                continue
            if value is not None:
                values.append(value)
        start, end = (min(values), max(values)) if values else (None, None)
        return TimeRange(
            start,
            end,
            start,
            end,
            TimezoneState.EXPLICIT_UTC if values else TimezoneState.UNKNOWN,
            "EXIF DateTimeOriginal plus SubSecTime",
            "DateTimeOriginal; FileModifyDate fallback when provided",
        )

    def validate(self, candidate: InputCandidate) -> InstrumentResult:
        files = _all_images(candidate.paths)
        warnings: list[str] = []
        errors: list[str] = []
        if not files:
            errors.append("No MicaSense TIFF image files were selected.")
        unparsed = [path.name for path in files if _filename_parts(path)[1] is None]
        if unparsed:
            warnings.append(
                f"{len(unparsed)} image filename(s) do not expose a capture and band number."
            )
        coverage = self.extract_time_range(candidate) if files else TimeRange()
        if files and coverage.utc_start is None:
            warnings.append("No valid EXIF acquisition timestamps were extracted.")
        status = (
            DetectionStatus.FAILED
            if errors
            else DetectionStatus.WARNING
            if warnings
            else DetectionStatus.READY
        )
        return InstrumentResult(
            "micasense",
            self.descriptor.display_name,
            "HATCHBOX",
            status,
            source_files=_image_sources(files),
            file_count=len(files),
            original_start_time=coverage.original_start,
            original_end_time=coverage.original_end,
            utc_start_time=coverage.utc_start,
            utc_end_time=coverage.utc_end,
            warnings=warnings,
            errors=errors,
            metadata=dict(self.inspect_metadata(candidate)),
        )

    def load(self, candidate: InputCandidate) -> LoadedMicaSense:
        validation = self.validate(candidate)
        if validation.errors:
            raise ValueError("; ".join(validation.errors))
        self._emit(2, "MicaSense files validated")
        return LoadedMicaSense(candidate, _all_images(candidate.paths))

    def process_quicklook(
        self, loaded: LoadedMicaSense, options: Mapping[str, Any]
    ) -> InstrumentResult:
        started = time.monotonic()
        files = loaded.files
        self._records = []
        corrupt: list[str] = []
        small: list[str] = []
        batch_size = min(
            self.batch_policy.maximum_batch_files,
            max(1, self.resource_limits.memory_bytes // (8 * 1024 * 1024)),
        )
        for batch_number, batch in enumerate(iter_batches(files, batch_size), start=1):
            self._check_cancelled()
            workers = max(1, min(self.resource_limits.worker_count, len(batch)))
            with ThreadPoolExecutor(
                max_workers=workers, thread_name_prefix="ccflux-micasense-meta"
            ) as executor:
                records = executor.map(self._inspect_one, batch)
                for path, record in zip(batch, records):
                    self._check_cancelled()
                    self._records.append(record)
                    if record["corrupt"]:
                        corrupt.append(path.name)
                    if record["unusually_small"]:
                        small.append(path.name)
            completed = min(len(self._records), len(files))
            self._emit(
                5 + 70 * completed / max(1, len(files)),
                f"Metadata batch {batch_number}: {completed}/{len(files)} images",
            )

        self._share_capture_metadata()
        # Every instrument processes only what falls inside the operator's
        # selected interval. MicaSense previously evaluated the whole delivery
        # regardless of the Time Filter, so its capture counts and completeness
        # did not describe the same flight window as every other instrument.
        selected_start = _as_utc(options.get("analysis_start"))
        selected_end = _as_utc(options.get("analysis_end"))
        # A capture dated before the campaign was dated by a camera whose GPS had
        # not locked, so it carries a default clock rather than an acquisition
        # time. Those frames are dropped from every calculation - coverage,
        # capture grouping, trigger cadence and the plotted series - and reported
        # instead. Keeping them made MicaSense claim it recorded from 1970 and
        # put a 56-year gap in the trigger intervals.
        pre_campaign = [
            record for record in self._records
            if (stamp := _as_utc(record.get("timestamp"))) is not None
            and stamp.year < MINIMUM_CAPTURE_YEAR
        ]
        if pre_campaign:
            dropped = {id(record) for record in pre_campaign}
            self._records = [
                record for record in self._records if id(record) not in dropped
            ]
        excluded_by_time = 0
        undated = 0
        if selected_start is not None or selected_end is not None:
            selected_records = []
            for record in self._records:
                stamp = _as_utc(record.get("timestamp"))
                if stamp is None:
                    # An image with no usable acquisition time cannot be placed
                    # in the flight; keep it and say so rather than silently
                    # assuming it belongs.
                    undated += 1
                    selected_records.append(record)
                    continue
                if (selected_start is not None and stamp < selected_start) or (
                    selected_end is not None and stamp > selected_end
                ):
                    excluded_by_time += 1
                    continue
                selected_records.append(record)
            if not selected_records:
                raise ValueError(
                    "No MicaSense image falls inside the selected Time Filter "
                    f"({_iso(selected_start)} to {_iso(selected_end)} UTC)."
                )
            self._records = selected_records

        self._captures = _capture_rows(self._records)
        complete = sum(row["complete"] for row in self._captures)
        incomplete = len(self._captures) - complete
        timestamps = [
            record["timestamp"]
            for record in self._records
            if record["timestamp"] is not None
        ]
        # A camera powered up without a clock write dates its frames from the
        # epoch, and 24 of Flight_CCT0803's carry such a stamp. Including them
        # in the reported coverage made MicaSense claim it recorded from
        # 1970-01-01, which is 56 years of coverage for a four-hour flight. They
        # are still counted and warned about below; they just cannot bound a
        # window they were never placed in.
        trigger_intervals = _trigger_intervals(self._captures)
        thumbnail_paths = self._create_limited_thumbnails(files)
        warnings = []
        if incomplete:
            warnings.append(f"{incomplete} incomplete capture(s) detected.")
        if corrupt:
            warnings.append(f"{len(corrupt)} corrupt or unreadable TIFF file(s) detected.")
        if small:
            warnings.append(f"{len(small)} unusually small TIFF file(s) detected.")
        if excluded_by_time:
            warnings.append(
                f"{excluded_by_time} image(s) outside the selected Time Filter "
                "were excluded from this evaluation."
            )
        if undated:
            warnings.append(
                f"{undated} image(s) have no usable acquisition time — the EXIF "
                "timestamp is missing or the camera clock was not set — and were "
                "retained without time filtering. They cannot be placed on the "
                "flight timeline."
            )
        missing_gps = sum(not record["gps_present"] for record in self._records)
        missing_exposure = sum(not record["exposure_present"] for record in self._records)
        if missing_gps:
            warnings.append(f"GPS metadata missing from {missing_gps} image(s).")
        if missing_exposure:
            warnings.append(f"Exposure metadata missing from {missing_exposure} image(s).")
        static_solar = _static_solar_geometry_warning(self._captures)
        if static_solar:
            warnings.append(static_solar)
        if pre_campaign:
            warnings.append(
                f"{len(pre_campaign)} image(s) are dated before "
                f"{MINIMUM_CAPTURE_YEAR}, which means the camera GPS had not "
                "locked and the frame carries a default clock rather than an "
                "acquisition time. They are excluded from the coverage, capture "
                "grouping, trigger cadence and every plot; the images "
                "themselves are untouched on the delivery."
            )
        status = ProcessingStatus.WARNING if warnings else ProcessingStatus.COMPLETE
        result = InstrumentResult(
            "micasense",
            self.descriptor.display_name,
            "HATCHBOX",
            DetectionStatus.WARNING if warnings else DetectionStatus.READY,
            status,
            _image_sources(files),
            len(files),
            min(timestamps) if timestamps else None,
            max(timestamps) if timestamps else None,
            min(timestamps) if timestamps else None,
            max(timestamps) if timestamps else None,
            completeness_percentage=(
                100.0 * complete / len(self._captures) if self._captures else None
            ),
            warnings=warnings,
            progress=100.0,
            figures=[
                FigureArtifact(path, "MicaSense Level 1 thumbnail")
                for path in thumbnail_paths
            ],
            metadata={
                "level": 1,
                # The counts describe what was evaluated, which is the delivery
                # narrowed to the Time Filter. Counting every delivered file
                # here reported 14,226 images beside 42 captures of 6 bands.
                "image_count": len(self._records),
                "delivered_image_count": len(files),
                "capture_count": len(self._captures),
                "complete_capture_count": complete,
                "incomplete_capture_count": incomplete,
                "bands": sorted(
                    {
                        record["band_number"]
                        for record in self._records
                        if record["band_number"] is not None
                    }
                ),
                "trigger_intervals_seconds": trigger_intervals,
                "median_trigger_interval_seconds": (
                    median(trigger_intervals) if trigger_intervals else None
                ),
                "gps_present_count": len(self._records) - missing_gps,
                "exposure_present_count": len(self._records) - missing_exposure,
                "corrupt_files": corrupt,
                "unusually_small_files": small,
                "thumbnail_count": len(thumbnail_paths),
                "camera_identity": _camera_identity(self._records),
                "batch_size": batch_size,
                "cpu_limit": self.resource_limits.worker_count,
                "ram_limit_bytes": self.resource_limits.memory_bytes,
                "excluded_operations": [
                    "radiometric correction",
                    "panel calibration",
                    "band alignment",
                    "reflectance conversion",
                    "vegetation indices",
                    "georeferencing",
                ],
            },
            elapsed_time=timedelta(seconds=time.monotonic() - started),
        )
        self._emit(100, "MicaSense Level 1 quick check complete")
        if self.logger:
            self.logger.log(
                LogLevel.WARNING if warnings else LogLevel.SUCCESS,
                "micasense-level1",
                "MicaSense Level 1 quick check completed",
                instrument="micasense",
                processing_step="metadata-qc",
            )
        return result

    def process_detailed(self, loaded: Any, options: Mapping[str, Any]) -> InstrumentResult:
        """MicaSense is read for its metadata only, by decision.

        Reflectance and index work belongs to the standalone MicaSense pipeline
        and is not run here, so no job offers this stage; the review software
        reports what the camera recorded and how complete the captures are.
        """
        raise NotImplementedError(
            "MicaSense is processed for acquisition metadata only. Reflectance "
            "and vegetation index work is done by the standalone MicaSense "
            "pipeline, not by the payload review software."
        )

    def create_plots(
        self, result: InstrumentResult, output_directory: Path
    ) -> Sequence[FigureArtifact]:
        self._assert_output(output_directory)
        return tuple(result.figures)

    def export_results(
        self,
        result: InstrumentResult,
        output_directory: Path,
        formats: Sequence[str],
    ) -> Sequence[OutputFile]:
        self._assert_output(output_directory)
        requested = {value.casefold() for value in formats}
        if requested - {"csv", "json"}:
            raise ValueError("MicaSense Level 1 exports support CSV and JSON")
        output_directory.mkdir(parents=True, exist_ok=True)
        outputs: list[OutputFile] = []
        if "csv" in requested:
            for name, rows in (
                ("image_metadata_qc.csv", self._records),
                ("capture_completeness_qc.csv", self._captures),
            ):
                path = output_directory / name
                _reject_existing(path)
                _write_csv(path, rows)
                outputs.append(OutputFile(path, "micasense_level1_csv", "text/csv", path.stat().st_size))
        if "json" in requested:
            path = output_directory / "level1_summary.json"
            _reject_existing(path)
            path.write_text(
                json.dumps(result.metadata, indent=2, default=str), encoding="utf-8"
            )
            outputs.append(OutputFile(path, "micasense_level1_summary", "application/json", path.stat().st_size))
        result.output_files.extend(outputs)
        return tuple(outputs)

    def cancel(self) -> None:
        self._cancelled.set()

    def report_progress(self, callback: ProgressCallback | None) -> None:
        self._callback = callback

    def _share_capture_metadata(self) -> None:
        """Give every band of a capture the metadata its read band carries.

        The bands of one capture were written by a single trigger, so they
        share an acquisition time, a position and an exposure. Reading one and
        sharing it costs a sixth of the decompression and reports the same
        thing; only presence and size, which come from the archive's file list,
        differ between them.
        """
        read: dict[str, Mapping[str, Any]] = {}
        for record in self._records:
            if not record.get("listed_only") and record.get("_filename_capture"):
                read.setdefault(str(record["_filename_capture"]), record)
        for record in self._records:
            if not record.get("listed_only"):
                continue
            source = read.get(str(record.get("_filename_capture")))
            if source is None:
                continue
            # The capture id too: the read band takes it from EXIF, and a
            # listed band that kept its filename id would land in a capture of
            # its own, leaving every capture looking incomplete.
            for field in (
                "capture_id", "timestamp", "gps_present", "gps_latitude",
                "gps_longitude", "gps_altitude", "exposure_present",
                "exposure_time", "iso_speed", "gps_dop",
                *(_qa_record_key(name) for name in NUMERIC_QA_FIELDS),
            ):
                record[field] = source[field]

    def _inspect_one(self, path: Any) -> dict[str, Any]:
        capture_id, band = _filename_parts(path)
        corrupt = False
        metadata: Mapping[str, Any] = {}
        if isinstance(path, ArchiveImage) and not path.read_metadata:
            # A band whose capture is read elsewhere. Its presence and its size
            # come from the archive's file list, which needs no decompression;
            # its acquisition metadata is filled in from the capture below.
            size = _asset_size(path)
            return {
                "source_file": _asset_source(path),
                "file_name": path.name,
                "capture_id": capture_id,
                "band_number": band,
                "band_name": None,
                "timestamp": None,
                "gps_present": None,
                "gps_latitude": None,
                "gps_longitude": None,
                "gps_altitude": None,
                "exposure_present": None,
                "exposure_time": None,
                "iso_speed": None,
                "gps_dop": None,
                **{_qa_record_key(name): None for name in NUMERIC_QA_FIELDS},
                **{key: None for key in TRACEABILITY_FIELDS.values()},
                "size_bytes": size,
                "unusually_small": size < self.unusually_small_bytes,
                "corrupt": False,
                "listed_only": True,
                "_filename_capture": capture_id,
            }
        try:
            # Decompress the band once and share it, rather than paying for it
            # again in verification.
            payload = (
                _archive_member_bytes(path)
                if isinstance(path, ArchiveImage)
                else None
            )
            metadata = self._metadata(path, payload)
            _verify_image(path, payload)
        except (
            OSError, ValueError, KeyError, zipfile.BadZipFile,
            UnidentifiedImageError,
        ):
            corrupt = True
        size = _asset_size(path)
        timestamp = _parse_timestamp(metadata)
        return {
            "source_file": _asset_source(path),
            "file_name": path.name,
            "capture_id": metadata.get("CaptureId") or capture_id,
            "band_number": _integer(metadata.get("BandNumber")) or band,
            "band_name": metadata.get("BandName"),
            "timestamp": timestamp,
            "gps_present": _gps_present(metadata),
            "gps_latitude": metadata.get("GPSLatitude"),
            "gps_longitude": metadata.get("GPSLongitude"),
            "gps_altitude": metadata.get("GPSAltitude"),
            "exposure_present": metadata.get("ExposureTime") is not None,
            "exposure_time": metadata.get("ExposureTime"),
            "iso_speed": metadata.get("ISOSpeed"),
            "gps_dop": _optional_number(metadata.get("GPSDOP")),
            **_qa_fields(metadata),
            **{
                key: (str(metadata.get(name)) if metadata.get(name) else None)
                for name, key in TRACEABILITY_FIELDS.items()
            },
            "size_bytes": size,
            "unusually_small": size < self.unusually_small_bytes,
            "corrupt": corrupt,
            "listed_only": False,
            "_filename_capture": capture_id,
        }

    def _create_limited_thumbnails(self, files: tuple[Any, ...]) -> list[Path]:
        limit = min(self.batch_policy.maximum_thumbnail_count, len(files))
        if not limit:
            return []
        directory = self.output_root / "thumbnails"
        directory.mkdir(parents=True, exist_ok=True)
        indexes = sorted({round(index * (len(files) - 1) / max(1, limit - 1)) for index in range(limit)})
        outputs: list[Path] = []
        bytes_written = 0
        for index in indexes:
            self._check_cancelled()
            source = files[index]
            target = directory / f"{Path(source.name).stem}_thumbnail.jpg"
            if target.exists():
                raise FileExistsError(f"MicaSense thumbnail already exists: {target}")
            try:
                _save_thumbnail(source, target)
                bytes_written += target.stat().st_size
                if bytes_written > self.batch_policy.maximum_thumbnail_bytes:
                    target.unlink(missing_ok=True)
                    break
                outputs.append(target)
            except (
                OSError, ValueError, KeyError, zipfile.BadZipFile,
                UnidentifiedImageError,
            ):
                continue
        return outputs

    def _metadata(
        self, source: Any, payload: bytes | None = None
    ) -> Mapping[str, Any]:
        if isinstance(source, ArchiveImage):
            return _archive_metadata(source, payload)
        return self.metadata_reader(source)

    def _emit(self, progress: float, phase: str) -> None:
        self._check_cancelled()
        if self._callback:
            self._callback(ProgressUpdate("micasense", progress, phase, phase))

    def _check_cancelled(self) -> None:
        if self._cancelled.is_set():
            if self.logger:
                self.logger.log(
                    LogLevel.WARNING,
                    "micasense-level1",
                    "MicaSense Level 1 quick check cancelled",
                    instrument="micasense",
                )
            raise RuntimeError("MicaSense Level 1 processing was cancelled")

    def _assert_output(self, path: Path) -> None:
        root = self.output_root.resolve(strict=False)
        target = Path(path).resolve(strict=False)
        try:
            target.relative_to(root)
        except ValueError as exc:
            raise ValueError(f"MicaSense output must remain below {root}") from exc


def _image_files(paths: Sequence[Path]) -> tuple[Path, ...]:
    return tuple(
        sorted(
            (
                Path(path)
                for path in paths
                if Path(path).is_file()
                and Path(path).suffix.casefold() in IMAGE_SUFFIXES
            ),
            key=lambda path: str(path).casefold(),
        )
    )


def _all_images(paths: Sequence[Path]) -> tuple[Any, ...]:
    images: list[Any] = list(_image_files(paths))
    archives: list[Path] = []
    for value in paths:
        path = Path(value)
        if path.is_dir():
            archives.extend(path.rglob("*.zip"))
        elif path.is_file() and path.suffix.casefold() == ".zip":
            archives.append(path)
    for archive in sorted(dict.fromkeys(archives), key=lambda item: str(item).casefold()):
        try:
            with zipfile.ZipFile(archive) as bundle:
                members = [
                    info for info in bundle.infolist()
                    if not info.is_dir()
                    and Path(info.filename).suffix.casefold() in IMAGE_SUFFIXES
                ]
                # Band 1 carries the metadata for the capture; where a delivery
                # has no band 1, the first member stands in for it.
                metadata_member = next(
                    (
                        info for info in members
                        if _filename_parts(Path(info.filename))[1] == 1
                    ),
                    members[0] if members else None,
                )
                for info in members:
                    images.append(
                        ArchiveImage(
                            archive,
                            info.filename,
                            info.file_size,
                            read_metadata=info is metadata_member,
                        )
                    )
        except (OSError, zipfile.BadZipFile):
            images.append(ArchiveImage(archive, "__CORRUPT_ARCHIVE__.tif", 0))
    return tuple(images)


def _filename_parts(path: Any) -> tuple[str | None, int | None]:
    match = CAPTURE_PATTERN.fullmatch(path.name)
    return (match.group(1), int(match.group(2))) if match else (None, None)


def _pillow_metadata(path: Path) -> Mapping[str, Any]:
    with Image.open(path) as image:
        exif = image.getexif()
        result = {
            ExifTags.TAGS.get(tag, str(tag)): value for tag, value in exif.items()
        }
        result.update(
            {
                key: value
                for key, value in image.info.items()
                if isinstance(value, (str, int, float))
            }
        )
        return result


_ARCHIVE_HANDLES = threading.local()


def _open_archive(archive: Path) -> zipfile.ZipFile:
    """Reuse this thread's handle when consecutive members share an archive.

    A MicaSense capture is one archive holding a band per file. Metadata and
    verification each used to open the archive from scratch, so a six-band
    capture paid twelve central-directory reads over USB. Measured on the
    campaign disk that was 1.2 s per band — 64 minutes for 3,216 bands.

    The handle is kept in thread-local storage because ZipFile is not safe for
    concurrent reads, and the inspection pool maps several bands at once.
    Members arrive sorted, so each worker sees runs from the same archive.
    """
    cached = getattr(_ARCHIVE_HANDLES, "handle", None)
    if cached is not None and getattr(_ARCHIVE_HANDLES, "path", None) == archive:
        return cached
    if cached is not None:
        try:
            cached.close()
        except OSError:
            pass
    handle = zipfile.ZipFile(archive)
    _ARCHIVE_HANDLES.handle = handle
    _ARCHIVE_HANDLES.path = archive
    return handle


def release_archive_handle() -> None:
    """Close this thread's cached archive handle."""
    cached = getattr(_ARCHIVE_HANDLES, "handle", None)
    if cached is not None:
        try:
            cached.close()
        except OSError:
            pass
    _ARCHIVE_HANDLES.handle = None
    _ARCHIVE_HANDLES.path = None


def _archive_member_bytes(source: ArchiveImage) -> bytes:
    """Decompress a band once; metadata and verification then share the buffer."""
    return _open_archive(source.archive).read(source.member)


def _archive_metadata(source: ArchiveImage, payload: bytes | None = None) -> Mapping[str, Any]:
    data = _archive_member_bytes(source) if payload is None else payload
    with Image.open(io.BytesIO(data)) as image:
        return _metadata_from_image(image)


def _metadata_from_image(image: Image.Image) -> Mapping[str, Any]:
    exif = image.getexif()
    result = {ExifTags.TAGS.get(tag, str(tag)): value for tag, value in exif.items()}
    try:
        for tag, value in exif.get_ifd(ExifTags.IFD.Exif).items():
            name = ExifTags.TAGS.get(tag, str(tag))
            result[name] = value
            if name == "SubsecTime":
                result["SubSecTime"] = value
    except (KeyError, TypeError):
        pass
    try:
        gps = exif.get_ifd(ExifTags.IFD.GPSInfo)
        latitude = _gps_decimal(gps.get(2), gps.get(1))
        longitude = _gps_decimal(gps.get(4), gps.get(3))
        if latitude is not None:
            result["GPSLatitude"] = latitude
        if longitude is not None:
            result["GPSLongitude"] = longitude
        if gps.get(6) is not None:
            result["GPSAltitude"] = float(gps[6])
        if gps.get(11) is not None:
            result["GPSDOP"] = float(gps[11])
    except (KeyError, TypeError, ValueError):
        pass
    xmp = image.info.get("xmp")
    if isinstance(xmp, bytes):
        result.update(_xmp_metadata(xmp))
    result.update({
        key: value for key, value in image.info.items()
        if isinstance(value, (str, int, float))
    })
    return result


def _gps_decimal(value: Any, reference: Any) -> float | None:
    if not isinstance(value, (tuple, list)) or len(value) != 3:
        return None
    decimal = float(value[0]) + float(value[1]) / 60 + float(value[2]) / 3600
    return -decimal if str(reference).upper() in {"S", "W"} else decimal


# What the camera records about the conditions it recorded in. All of it is
# metadata already decoded while reading the band, so keeping it costs no extra
# read; discarding it was what left the workspace with nothing to plot.
# Wavelengths and irradiances are the camera's own values, unmodified.
XMP_QA_FIELDS = (
    "Irradiance",
    "SpectralIrradiance",
    "HorizontalIrradiance",
    "DirectIrradiance",
    "ScatteredIrradiance",
    "IrradianceYaw",
    "IrradiancePitch",
    "IrradianceRoll",
    "Yaw",
    "Pitch",
    "Roll",
    "SolarElevation",
    "SolarAzimuth",
    "ImagerTemperatureC",
    "PressureAlt",
    "BootTimestamp",
    "GPSXYAccuracy",
    "GPSZAccuracy",
    "FlightId",
    "Serial",
    "SwVersion",
    "RigName",
)


# The subset that is a number and worth a series. FlightId, Serial, SwVersion
# and RigName are traceability strings and are reported once, not plotted.
NUMERIC_QA_FIELDS = (
    "Irradiance",
    "SpectralIrradiance",
    "HorizontalIrradiance",
    "DirectIrradiance",
    "ScatteredIrradiance",
    "IrradianceYaw",
    "IrradiancePitch",
    "IrradianceRoll",
    "SolarElevation",
    "SolarAzimuth",
    "ImagerTemperatureC",
    "PressureAlt",
    "GPSXYAccuracy",
    "GPSZAccuracy",
)
# The camera writes solar geometry and its own attitude in radians.
# A frozen solar-geometry reading only shows up over a long enough span: over
# ten minutes the sun really does barely move.
STATIC_SOLAR_MIN_HOURS = 1.0
STATIC_SOLAR_MAX_SPREAD_DEG = 1.0

RADIAN_QA_FIELDS = frozenset({
    "SolarElevation", "SolarAzimuth",
    "IrradianceYaw", "IrradiancePitch", "IrradianceRoll",
})


# Which camera and light sensor produced the delivery. Reported once rather than
# plotted, and worth reporting: a mid-campaign sensor swap or firmware change is
# invisible in the imagery but decides whether two flights are comparable.
TRACEABILITY_FIELDS = {
    "RigName": "rig_name",
    "Serial": "dls_serial",
    "SwVersion": "dls_software",
    "FlightId": "flight_id",
}

# Names the split-on-capitals rule gets wrong: runs of capitals are one word.
QA_KEY_OVERRIDES = {
    "GPSXYAccuracy": "gps_xy_accuracy",
    "GPSZAccuracy": "gps_z_accuracy",
    "ImagerTemperatureC": "imager_temperature_c",
}


def _qa_record_key(name: str) -> str:
    """CamelCase XMP name to the snake_case record field, degrees noted."""
    snake = QA_KEY_OVERRIDES.get(name)
    if snake is None:
        snake = re.sub(r"(?<!^)(?=[A-Z])", "_", name).lower()
    return f"{snake}_deg" if name in RADIAN_QA_FIELDS else snake


def _optional_number(value: Any) -> float | None:
    """A float, or None. XMP values arrive as text and may be absent."""
    if value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if number != number or abs(number) == float("inf"):
        return None
    return number


def _qa_fields(metadata: Mapping[str, Any]) -> dict[str, float | None]:
    """The camera's own QA values, radians converted to degrees for reading."""
    result: dict[str, float | None] = {}
    for name in NUMERIC_QA_FIELDS:
        value = _optional_number(metadata.get(name))
        if value is not None and name in RADIAN_QA_FIELDS:
            value = degrees(value)
        result[_qa_record_key(name)] = value
    return result


def _xmp_metadata(payload: bytes) -> dict[str, Any]:
    try:
        root = ElementTree.fromstring(payload.decode("utf-8", errors="ignore"))
    except ElementTree.ParseError:
        return {}
    values: dict[str, Any] = {}
    for element in root.iter():
        local = element.tag.rsplit("}", 1)[-1]
        text = (element.text or "").strip()
        if not text:
            continue
        if local in {"BandName", "CaptureId", "CentralWavelength", "WavelengthFWHM"}:
            key = {
                "CentralWavelength": "CenterWavelength",
                "WavelengthFWHM": "Bandwidth",
            }.get(local, local)
            values[key] = text
        elif local in XMP_QA_FIELDS:
            values[local] = text
    return values


def _verify_image(source: Any, payload: bytes | None = None) -> None:
    if isinstance(source, ArchiveImage):
        data = _archive_member_bytes(source) if payload is None else payload
        with Image.open(io.BytesIO(data)) as image:
            image.verify()
        return
    with Image.open(source) as image:
        image.verify()


# A RedEdge-P band is a 16-bit single-channel TIFF holding raw sensor counts,
# and the useful signal occupies a narrow part of the 0-65535 range: band 1 of
# Flight_CCT0803 runs 4064-65504 with a mean near 15952. Two things follow, and
# both used to break the sample strip.
#
# PIL cannot resize an "I;16" image at all - Image.thumbnail raises
# "image has wrong mode" - so band 6, the 2464x2056 panchromatic one, produced
# no thumbnail and was silently swallowed by the caller's except/continue.
# And I;16 -> RGB clips every value above 255 rather than scaling, so the five
# 1456x1088 bands that did survive came out uniformly 255: pure white boxes.
#
# Scaling to 8 bits first fixes both. The stretch is a percentile one because a
# min/max stretch is set by single hot or dead pixels and leaves the scene flat.
# This is a display transform for a quicklook only: nothing here is calibrated,
# and no reflectance or index is computed anywhere in CC-FLUX.
THUMBNAIL_SIZE = (480, 360)
THUMBNAIL_STRETCH_PERCENTILES = (2.0, 98.0)


def _eight_bit_for_display(image: Image.Image) -> Image.Image:
    """An 8-bit image safe to resize and encode as JPEG.

    Already-8-bit modes are returned untouched. Higher-precision single-channel
    modes are linearly stretched between the 2nd and 98th percentile.
    """
    if image.mode in {"RGB", "L"}:
        return image
    if image.mode not in {"I;16", "I;16B", "I;16L", "I;16N", "I", "F"}:
        return image.convert("RGB")
    import numpy as np

    samples = np.asarray(image)
    finite = samples[np.isfinite(samples)] if samples.dtype.kind == "f" else samples
    if not finite.size:
        return Image.new("L", image.size)
    low, high = np.percentile(finite, THUMBNAIL_STRETCH_PERCENTILES)
    if not np.isfinite(low) or not np.isfinite(high) or high <= low:
        # A genuinely uniform band. Mid-grey says "read, and flat" rather than
        # pretending to a contrast the data does not have.
        return Image.new("L", image.size, color=128)
    scaled = (samples.astype("float32") - float(low)) * (255.0 / (float(high) - float(low)))
    return Image.fromarray(np.clip(scaled, 0, 255).astype("uint8"), mode="L")


def _write_thumbnail(image: Image.Image, target: Path) -> None:
    converted = _eight_bit_for_display(image)
    converted.thumbnail(THUMBNAIL_SIZE)
    converted.save(target, "JPEG", quality=80)


def _save_thumbnail(source: Any, target: Path) -> None:
    if isinstance(source, ArchiveImage):
        with zipfile.ZipFile(source.archive) as bundle:
            with bundle.open(source.member) as stream:
                with Image.open(stream) as image:
                    _write_thumbnail(image, target)
        return
    with Image.open(source) as image:
        _write_thumbnail(image, target)


def _asset_size(source: Any) -> int:
    return source.size_bytes if isinstance(source, ArchiveImage) else source.stat().st_size


def _asset_source(source: Any) -> str:
    return (
        f"{source.archive}::{source.member}"
        if isinstance(source, ArchiveImage)
        else str(source)
    )


def _image_sources(images: Sequence[Any]) -> list[SourceFile]:
    return [
        SourceFile(Path(_asset_source(image)), size_bytes=_asset_size(image))
        for image in images
    ]


def _as_utc(value: Any) -> datetime | None:
    """Normalise an option or record timestamp to UTC.

    Campaign convention: MicaSense records UTC, so a naive EXIF value is read
    as UTC rather than discarded.
    """
    if isinstance(value, datetime):
        parsed = value
    elif value:
        try:
            parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except ValueError:
            return None
    else:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value else None


def _parse_timestamp(metadata: Mapping[str, Any]) -> datetime | None:
    raw = metadata.get("DateTimeOriginal") or metadata.get("FileModifyDate")
    if raw is None:
        return None
    text = str(raw).strip()
    subsec = re.sub(r"\D", "", str(metadata.get("SubSecTime", "")))
    for candidate in (text, re.sub(r"^(\d{4}):(\d{2}):(\d{2})", r"\1-\2-\3", text)):
        try:
            value = datetime.fromisoformat(candidate.replace("Z", "+00:00"))
            if subsec:
                value = value.replace(microsecond=int((subsec + "000000")[:6]))
            return value.astimezone(timezone.utc) if value.tzinfo else value.replace(tzinfo=timezone.utc)
        except ValueError:
            try:
                value = datetime.strptime(candidate, "%Y:%m:%d %H:%M:%S")
                if subsec:
                    value = value.replace(microsecond=int((subsec + "000000")[:6]))
                return value.replace(tzinfo=timezone.utc)
            except ValueError:
                pass
    return None


def _integer(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _gps_present(metadata: Mapping[str, Any]) -> bool:
    return metadata.get("GPSLatitude") is not None and metadata.get("GPSLongitude") is not None


# What a capture reports about the flight and the light it was taken in. Every
# one of these comes from metadata already read; none is a derived radiometric
# quantity. Order is the order the workspace plots them in.
CAPTURE_CONDITION_FIELDS = (
    "gps_latitude",
    "gps_longitude",
    "gps_altitude",
    "pressure_alt",
    "gps_dop",
    "gps_xy_accuracy",
    "gps_z_accuracy",
    "exposure_time",
    "iso_speed",
    "irradiance",
    "spectral_irradiance",
    "horizontal_irradiance",
    "direct_irradiance",
    "scattered_irradiance",
    "irradiance_yaw_deg",
    "irradiance_pitch_deg",
    "irradiance_roll_deg",
    "solar_elevation_deg",
    "solar_azimuth_deg",
    "imager_temperature_c",
)


def _camera_identity(records: Sequence[Mapping[str, Any]]) -> dict[str, str]:
    """The rig, light sensor and flight the delivery names.

    More than one value means the delivery mixes hardware or flights, so all of
    them are reported rather than the first one silently standing for the rest.
    """
    identity: dict[str, str] = {}
    for key in TRACEABILITY_FIELDS.values():
        values = sorted({
            str(record[key]) for record in records if record.get(key)
        })
        if values:
            identity[key] = values[0] if len(values) == 1 else ", ".join(values)
    return identity


def _static_solar_geometry_warning(
    captures: Sequence[Mapping[str, Any]],
) -> str | None:
    """Whether the light sensor stopped recomputing where the sun was.

    Solar elevation moves several degrees an hour. On Flight_CCT0803 the DLS
    reported 55.83 degrees from 13:17 to 15:51 while the true elevation fell
    towards 30, so the recorded geometry is unusable for that part of the
    flight. The values are still reported exactly as recorded; this only says
    that they stopped changing.
    """
    points = [
        (row["trigger_time"], row["solar_elevation_deg"])
        for row in captures
        if row.get("trigger_time") is not None
        and row.get("solar_elevation_deg") is not None
    ]
    if len(points) < 2:
        return None
    span_hours = (points[-1][0] - points[0][0]).total_seconds() / 3600.0
    elevations = [value for _, value in points]
    spread = max(elevations) - min(elevations)
    if span_hours < STATIC_SOLAR_MIN_HOURS or spread > STATIC_SOLAR_MAX_SPREAD_DEG:
        return None
    return (
        f"Light sensor solar elevation varies by only {spread:.2f}° over "
        f"{span_hours:.1f} h, which the sun does not. The recorded solar "
        "geometry stopped updating and should not be used to correct this "
        "flight; the irradiance readings themselves are unaffected."
    )


def _capture_conditions(group: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """The first recorded value of each condition across a capture's bands."""
    result: dict[str, Any] = {}
    for field in CAPTURE_CONDITION_FIELDS:
        result[field] = next(
            (
                record[field]
                for record in group
                if record.get(field) is not None
            ),
            None,
        )
    return result


def _capture_rows(records: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    groups: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    for record in records:
        if record["capture_id"]:
            groups[str(record["capture_id"])].append(record)
    rows: list[dict[str, Any]] = []
    for capture_id, group in groups.items():
        bands = sorted(
            {
                int(record["band_number"])
                for record in group
                if record["band_number"] is not None
            }
        )
        counts = Counter(record["band_number"] for record in group)
        # A camera powered up without a clock write dates its frames from the
        # epoch. Such a stamp says nothing about when the image was taken, and
        # taken as a trigger time it put a 56-year interval in the list.
        times = [
            record["timestamp"]
            for record in group
            if record["timestamp"] and record["timestamp"].year >= 2000
        ]
        missing = sorted(EXPECTED_BANDS - set(bands))
        duplicate_count = sum(max(0, count - 1) for band, count in counts.items() if band is not None)
        rows.append(
            {
                "capture_id": capture_id,
                "trigger_time": min(times) if times else None,
                "file_count": len(group),
                "found_bands": bands,
                "missing_bands": missing,
                "duplicate_band_files": duplicate_count,
                "complete": not missing and len(group) == len(EXPECTED_BANDS) and not duplicate_count,
                # One trigger fires all six bands, so the conditions belong to
                # the capture. Taken from whichever band was read: the others
                # are listed from the archive and carry no metadata.
                **_capture_conditions(group),
            }
        )
    rows.sort(key=lambda row: (row["trigger_time"] is None, row["trigger_time"] or datetime.max.replace(tzinfo=timezone.utc)))
    return rows


def _trigger_intervals(captures: Sequence[Mapping[str, Any]]) -> list[float]:
    times = [row["trigger_time"] for row in captures if row["trigger_time"] is not None]
    return [
        (current - previous).total_seconds()
        for previous, current in zip(times, times[1:])
    ]


def _sources(paths: Sequence[Path]) -> list[SourceFile]:
    return [SourceFile(path, size_bytes=path.stat().st_size) for path in paths]


def _write_csv(path: Path, rows: Sequence[Mapping[str, Any]]) -> None:
    fields = list(rows[0]) if rows else []
    with path.open("w", newline="", encoding="utf-8") as stream:
        writer = csv.DictWriter(stream, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)


def _reject_existing(path: Path) -> None:
    if path.exists():
        raise FileExistsError(f"MicaSense Level 1 output already exists: {path}")
