"""Bounded FLIR Level 1 quick check; no radiometric temperature conversion."""

from __future__ import annotations

import csv
import json
import math
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence

from PIL import Image

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
from core.resource_manager import CameraBatchPolicy, ResourceLimits
from core.scanner import ScanIndex
from core.time_manager import TimeRange, TimezoneState
from instruments.base.interface import InstrumentBase, ProgressCallback
from .legacy_bridge import LegacyFlirQuickLookBridge

CALIBRATION_FIELDS = ("R", "B", "F", "J0", "J1", "X", "alpha1", "alpha2", "beta1", "beta2")


@dataclass(slots=True)
class LoadedFlir:
    candidate: InputCandidate
    json_files: tuple[Path, ...]


class FlirLevel1Adapter(InstrumentBase):
    descriptor = InstrumentDescriptor(
        "flir",
        "FLIR Thermal Camera",
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
        bridge: LegacyFlirQuickLookBridge | None = None,
        logger: ProcessingLogManager | None = None,
        unusually_small_bytes: int = 1024,
    ) -> None:
        self.output_root = Path(output_root)
        self.flight_name = flight_name
        self.resource_limits = resource_limits
        self.batch_policy = batch_policy or CameraBatchPolicy(
            maximum_batch_files=8, maximum_thumbnail_count=12
        )
        self.bridge = bridge or LegacyFlirQuickLookBridge()
        self.logger = logger
        self.unusually_small_bytes = unusually_small_bytes
        self._callback: ProgressCallback | None = None
        self._cancelled = threading.Event()
        self._entries: list[tuple[str, Path, int]] = []
        self._samples: list[dict[str, Any]] = []
        self._gaps: list[dict[str, Any]] = []

    def detect(self, scan_index: ScanIndex) -> Sequence[InputCandidate]:
        files = tuple(
            entry.path
            for entry in scan_index.entries
            if entry.is_file and entry.path.suffix.casefold() == ".json"
            and ("flir" in entry.path.name.casefold() or "flir" in str(entry.path.parent).casefold())
        )
        return (
            InputCandidate("flir", files, 0.98, "FLIR JSON frame export")
        ,) if files else ()

    def inspect_metadata(self, candidate: InputCandidate) -> Mapping[str, Any]:
        files = _json_files(candidate.paths)
        return {
            "json_file_count": len(files),
            "total_bytes": sum(path.stat().st_size for path in files),
            "format": "streamed JSON frame documents",
            "timestamp_field": "timestamp or timestamp.$date",
            "radiometric_metadata_fields": list(CALIBRATION_FIELDS),
            "level": "Level 1 acquisition and metadata health only",
        }

    def extract_time_range(self, candidate: InputCandidate) -> TimeRange:
        entries = self._scan(_json_files(candidate.paths), emit_progress=False)
        times = [
            value for timestamp, _, _ in entries
            if (value := self.bridge.module.parse_timestamp(timestamp)) is not None
        ]
        start, end = (min(times), max(times)) if times else (None, None)
        if start is not None:
            start = start.replace(tzinfo=start.tzinfo or timezone.utc)
            end = end.replace(tzinfo=end.tzinfo or timezone.utc)
        return TimeRange(
            start, end, start, end,
            TimezoneState.EXPLICIT_UTC if times else TimezoneState.UNKNOWN,
            "microseconds where present",
            "JSON timestamp field scanned by unchanged FLIR byte-range regex",
        )

    def validate(self, candidate: InputCandidate) -> InstrumentResult:
        files = _json_files(candidate.paths)
        errors: list[str] = []
        warnings: list[str] = []
        if not files:
            errors.append("No FLIR JSON files were selected.")
        for path in files:
            if path.stat().st_size < self.unusually_small_bytes:
                warnings.append(f"Unusually small FLIR JSON file: {path.name}")
        coverage = self.extract_time_range(candidate) if files else TimeRange()
        if files and coverage.utc_start is None:
            errors.append("No valid FLIR frame timestamps were found.")
        status = DetectionStatus.FAILED if errors else DetectionStatus.WARNING if warnings else DetectionStatus.READY
        return InstrumentResult(
            "flir", self.descriptor.display_name, "HATCHBOX", status,
            source_files=_sources(files), file_count=len(files),
            original_start_time=coverage.original_start,
            original_end_time=coverage.original_end,
            utc_start_time=coverage.utc_start, utc_end_time=coverage.utc_end,
            warnings=warnings, errors=errors,
            metadata=dict(self.inspect_metadata(candidate)),
        )

    def load(self, candidate: InputCandidate) -> LoadedFlir:
        # Discovery already validates the source and its edge timestamps.
        # Re-running validate() here indexed the complete export once, then
        # process_quicklook() indexed it again. Real flight exports are tens of
        # gigabytes, so the duplicate pass left the GUI at "Starting · 0%".
        files = _json_files(candidate.paths)
        if not files:
            raise ValueError("No FLIR JSON files were selected.")
        unreadable = [
            path for path in files
            if not path.is_file() or path.stat().st_size <= 0
        ]
        if unreadable:
            raise ValueError(
                "FLIR JSON source is empty or unreadable: "
                + ", ".join(path.name for path in unreadable)
            )
        self._emit(2, "FLIR source accepted; indexing selected UTC frames")
        return LoadedFlir(candidate, files)

    def process_quicklook(
        self, loaded: LoadedFlir, options: Mapping[str, Any]
    ) -> InstrumentResult:
        started = time.monotonic()
        self._entries = self._scan(loaded.json_files, emit_progress=True)
        selected_start = _as_utc(options.get("analysis_start"))
        selected_end = _as_utc(options.get("analysis_end"))
        if selected_start is not None or selected_end is not None:
            selected_entries = []
            for entry in self._entries:
                parsed = self.bridge.module.parse_timestamp(entry[0])
                if parsed is None:
                    continue
                parsed = parsed.replace(tzinfo=parsed.tzinfo or timezone.utc)
                parsed = parsed.astimezone(timezone.utc)
                if (
                    (selected_start is None or parsed >= selected_start)
                    and (selected_end is None or parsed <= selected_end)
                ):
                    selected_entries.append(entry)
            self._entries = selected_entries
        if not self._entries:
            raise RuntimeError(
                "No FLIR frames fall inside the selected Time Filter"
            )
        summary, self._gaps = self.bridge.module.calculate_time_statistics(
            self._entries, gap_seconds=list(options.get("gap_seconds", (2.5, 5.0, 10.0)))
        )
        self._check()
        sample_limit = min(
            int(options.get("sample_frames", self.batch_policy.maximum_thumbnail_count)),
            self.batch_policy.maximum_thumbnail_count,
        )
        self._samples = self._inspect_samples(self._entries, sample_limit)
        thumbnails = self._create_thumbnails(self._entries, sample_limit)
        corrupt = [row for row in self._samples if row["read_status"] != "ok"]
        radiometric = [row for row in self._samples if row["calibration_required_present"]]
        unusually_small = [
            path.name for path in loaded.json_files
            if path.stat().st_size < self.unusually_small_bytes
        ]
        warnings: list[str] = []
        if not radiometric:
            warnings.append("Required FLIR radiometric calibration metadata was not found in sampled frames.")
        elif len(radiometric) < len(self._samples):
            warnings.append("Some sampled FLIR frames have incomplete radiometric calibration metadata.")
        if corrupt:
            warnings.append(f"{len(corrupt)} sampled FLIR frame(s) were corrupt or unreadable.")
        if unusually_small:
            warnings.append(f"{len(unusually_small)} unusually small FLIR JSON file(s) detected.")
        if self._gaps:
            warnings.append(f"{len(self._gaps)} acquisition gap(s) exceed the primary threshold.")
        parsed = [
            value for timestamp, _, _ in self._entries
            if (value := self.bridge.module.parse_timestamp(timestamp)) is not None
        ]
        parsed = [value.replace(tzinfo=value.tzinfo or timezone.utc) for value in parsed]
        status = ProcessingStatus.WARNING if warnings else ProcessingStatus.COMPLETE
        result = InstrumentResult(
            "flir", self.descriptor.display_name, "HATCHBOX",
            DetectionStatus.WARNING if warnings else DetectionStatus.READY,
            status, _sources(loaded.json_files), len(loaded.json_files),
            min(parsed) if parsed else None, max(parsed) if parsed else None,
            min(parsed) if parsed else None, max(parsed) if parsed else None,
            warnings=warnings, progress=100.0,
            figures=[FigureArtifact(path, "FLIR Level 1 representative thumbnail") for path in thumbnails],
            metadata={
                **summary,
                "frame_count": len(self._entries),
                "acquisition_intervals_seconds": _intervals(parsed),
                "radiometric_metadata_sampled_frames": len(radiometric),
                "radiometric_metadata_available": bool(radiometric),
                "corrupted_sample_count": len(corrupt),
                "unusually_small_files": unusually_small,
                "thumbnail_count": len(thumbnails),
                "scan_chunk_bytes": self._chunk_bytes(),
                "cpu_limit": self.resource_limits.worker_count,
                "ram_limit_bytes": self.resource_limits.memory_bytes,
                "temperature_conversion_performed": False,
                "time_filter_start_utc": (
                    selected_start.isoformat() if selected_start else None
                ),
                "time_filter_end_utc": (
                    selected_end.isoformat() if selected_end else None
                ),
            },
            elapsed_time=timedelta(seconds=time.monotonic() - started),
        )
        self._emit(100, "FLIR Level 1 quick check complete")
        if self.logger:
            self.logger.log(
                LogLevel.WARNING if warnings else LogLevel.SUCCESS,
                "flir-level1", "FLIR Level 1 quick check completed",
                instrument="flir", processing_step="metadata-qc",
            )
        return result

    def process_detailed(self, loaded: Any, options: Mapping[str, Any]) -> InstrumentResult:
        raise NotImplementedError("Detailed FLIR radiometric processing is not integrated")

    def create_plots(self, result: InstrumentResult, output_directory: Path) -> Sequence[FigureArtifact]:
        self._assert_output(output_directory)
        return tuple(result.figures)

    def export_results(
        self, result: InstrumentResult, output_directory: Path, formats: Sequence[str]
    ) -> Sequence[OutputFile]:
        self._assert_output(output_directory)
        requested = {value.casefold() for value in formats}
        if requested - {"csv", "json"}:
            raise ValueError("FLIR Level 1 exports support CSV and JSON")
        output_directory.mkdir(parents=True, exist_ok=True)
        outputs: list[OutputFile] = []
        if "csv" in requested:
            for name, rows in (("sample_frame_qc.csv", self._samples), ("acquisition_gaps.csv", self._gaps)):
                if not rows:
                    continue
                path = output_directory / name
                _reject(path)
                _write_csv(path, rows)
                outputs.append(OutputFile(path, "flir_level1_csv", "text/csv", path.stat().st_size))
        if "json" in requested:
            path = output_directory / "level1_summary.json"
            _reject(path)
            path.write_text(json.dumps(result.metadata, indent=2, default=str), encoding="utf-8")
            outputs.append(OutputFile(path, "flir_level1_summary", "application/json", path.stat().st_size))
        result.output_files.extend(outputs)
        return tuple(outputs)

    def cancel(self) -> None:
        self._cancelled.set()

    def report_progress(self, callback: ProgressCallback | None) -> None:
        self._callback = callback

    def sample_records(self) -> tuple[dict[str, Any], ...]:
        return tuple(dict(value) for value in self._samples)

    def acquisition_gaps(self) -> tuple[dict[str, Any], ...]:
        return tuple(dict(value) for value in self._gaps)

    def _scan(self, files: tuple[Path, ...], *, emit_progress: bool) -> list[tuple[str, Path, int]]:
        tasks: list[tuple[Path, int, int, int]] = []
        chunk_size = self._chunk_bytes()
        total_bytes = sum(path.stat().st_size for path in files)
        for path in files:
            for start in range(0, path.stat().st_size, chunk_size):
                tasks.append((path, start, min(start + chunk_size, path.stat().st_size), 4096))
        entries: list[tuple[str, Path, int]] = []
        scanned = 0
        workers = max(1, min(self.resource_limits.worker_count, len(tasks)))
        with ThreadPoolExecutor(
            max_workers=workers, thread_name_prefix="ccflux-flir-index"
        ) as executor:
            futures = [
                executor.submit(self.bridge.module.scan_one_byte_range, task)
                for task in tasks
            ]
            for index, future in enumerate(as_completed(futures), 1):
                self._check()
                bytes_done, _, part = future.result()
                scanned += bytes_done
                entries.extend(part)
                if emit_progress:
                    self._emit(5 + 65 * scanned / max(1, total_bytes), f"Timestamp chunk {index}/{len(tasks)}")
        entries.sort(key=lambda item: (item[0], str(item[1]), item[2]))
        return entries

    def _inspect_samples(self, entries, limit: int) -> list[dict[str, Any]]:
        rows = []
        for sample_no, (record_index, timestamp, path, offset) in enumerate(
            self.bridge.module.choose_sample_entries(entries, limit), 1
        ):
            self._check()
            row = {
                "sample_no": sample_no, "record_index": record_index,
                "timestamp": timestamp, "source_file": path.name,
                "read_status": "not_read", "raw_present": False,
                "raw_shape": "", "raw_stats_present": False,
                "calibration_present": False,
                "calibration_required_present": False,
                "missing_calibration_constants": "",
            }
            try:
                text = self.bridge.module.read_object_text_at_timestamp(path, offset)
                light, shape = self.bridge.module.object_without_raw(text)
                doc = json.loads(light)
                calibration = doc.get("calibration")
                missing = [name for name in CALIBRATION_FIELDS if not isinstance(calibration, dict) or calibration.get(name) is None]
                row.update({
                    "read_status": "ok", "raw_present": bool(shape), "raw_shape": shape,
                    "raw_stats_present": isinstance(doc.get("raw_stats"), dict),
                    "calibration_present": isinstance(calibration, dict),
                    "calibration_required_present": not missing,
                    "missing_calibration_constants": ",".join(missing),
                })
            except Exception as exc:
                row["read_status"] = f"error:{type(exc).__name__}"
                if self.logger:
                    self.logger.capture_exception(
                        "flir-level1", "FLIR sample frame could not be inspected",
                        exc, instrument="flir", file_path=path,
                    )
            rows.append(row)
            self._emit(72 + 13 * sample_no / max(1, limit), f"Sample metadata {sample_no}/{limit}")
        return rows

    def _create_thumbnails(self, entries, limit: int) -> list[Path]:
        directory = self.output_root / "thumbnails"
        directory.mkdir(parents=True, exist_ok=True)
        outputs: list[Path] = []
        bytes_written = 0
        for sample_no, (_, _, path, offset) in enumerate(self.bridge.module.choose_sample_entries(entries, limit), 1):
            self._check()
            target = directory / f"flir_frame_{sample_no:03d}.png"
            if target.exists():
                raise FileExistsError(f"FLIR thumbnail already exists: {target}")
            try:
                text = self.bridge.module.read_object_text_at_timestamp(path, offset)
                span = self.bridge.module.find_json_value_span(text, "raw")
                if span is None:
                    continue
                raw = json.loads(text[span[0]:span[1]])
                image = _thumbnail_image(raw)
                image.save(target, "PNG")
                bytes_written += target.stat().st_size
                if bytes_written > self.batch_policy.maximum_thumbnail_bytes:
                    target.unlink(missing_ok=True)
                    break
                outputs.append(target)
            except Exception:
                continue
            self._emit(86 + 12 * sample_no / max(1, limit), f"Representative thumbnail {sample_no}/{limit}")
        return outputs

    def _chunk_bytes(self) -> int:
        per_worker = self.resource_limits.memory_bytes // max(1, self.resource_limits.worker_count)
        return max(64 * 1024, min(8 * 1024 * 1024, per_worker // 8))

    def _emit(self, progress: float, phase: str) -> None:
        self._check()
        if self._callback:
            self._callback(ProgressUpdate("flir", progress, phase, phase))

    def _check(self) -> None:
        if self._cancelled.is_set():
            raise RuntimeError("FLIR Level 1 processing was cancelled")

    def _assert_output(self, path: Path) -> None:
        root, target = self.output_root.resolve(strict=False), Path(path).resolve(strict=False)
        try:
            target.relative_to(root)
        except ValueError as exc:
            raise ValueError(f"FLIR output must remain below {root}") from exc


def _thumbnail_image(raw: Any) -> Image.Image:
    if not isinstance(raw, list) or not raw or not isinstance(raw[0], list):
        raise ValueError("Raw FLIR frame is not a two-dimensional array")
    flat = [float(value) for row in raw for value in row if isinstance(value, (int, float)) and math.isfinite(value)]
    if not flat:
        raise ValueError("Raw FLIR frame has no finite pixels")
    low, high = min(flat), max(flat)
    scale = 255.0 / (high - low) if high > low else 0.0
    pixels = [int(max(0, min(255, (float(value) - low) * scale))) for row in raw for value in row]
    image = Image.new("L", (len(raw[0]), len(raw)))
    image.putdata(pixels)
    image.thumbnail((480, 360))
    return image


def _json_files(paths: Sequence[Path]) -> tuple[Path, ...]:
    result: list[Path] = []
    for value in paths:
        path = Path(value)
        if path.is_dir():
            result.extend(path.rglob("*.json"))
        elif path.is_file() and path.suffix.casefold() == ".json":
            result.append(path)
    return tuple(sorted(dict.fromkeys(result), key=lambda path: str(path).casefold()))


def _intervals(values) -> list[float]:
    return [(current - previous).total_seconds() for previous, current in zip(values, values[1:])]


def _as_utc(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        parsed = value
    elif value:
        try:
            parsed = datetime.fromisoformat(
                str(value).replace("Z", "+00:00")
            )
        except ValueError:
            return None
    else:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _sources(paths: Sequence[Path]) -> list[SourceFile]:
    return [SourceFile(path, size_bytes=path.stat().st_size) for path in paths]


def _write_csv(path: Path, rows: Sequence[Mapping[str, Any]]) -> None:
    fields = list(rows[0])
    with path.open("w", newline="", encoding="utf-8") as stream:
        writer = csv.DictWriter(stream, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def _reject(path: Path) -> None:
    if path.exists():
        raise FileExistsError(f"FLIR Level 1 output already exists: {path}")
