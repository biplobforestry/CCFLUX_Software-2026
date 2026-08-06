"""Bounded GoPro Level 1 media inventory and representative quick check."""

from __future__ import annotations

import csv
import json
import shutil
import struct
import subprocess
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from PIL import ExifTags, Image, UnidentifiedImageError

from core.detector import InputCandidate
from core.enums import DetectionStatus, ProcessingStatus
from core.gopro_georeference import camera_local_to_utc
from core.logging_manager import LogLevel, ProcessingLogManager
from core.models import FigureArtifact, InstrumentDescriptor, InstrumentResult, OutputFile, ProgressUpdate, SourceFile
from core.resource_manager import CameraBatchPolicy, ResourceLimits, iter_batches
from core.scanner import ScanIndex
from core.time_manager import TimeRange, TimezoneState
from instruments.base.interface import InstrumentBase, ProgressCallback

IMAGE_SUFFIXES = frozenset({".jpg", ".jpeg", ".png"})
VIDEO_SUFFIXES = frozenset({".mp4", ".mov"})
MP4_EPOCH = datetime(1904, 1, 1, tzinfo=timezone.utc)
VideoProbe = Callable[[Path], Mapping[str, Any]]
VideoSampler = Callable[[Path, Path], bool]


@dataclass(slots=True)
class LoadedGoPro:
    candidate: InputCandidate
    images: tuple[Path, ...]
    videos: tuple[Path, ...]


class GoProLevel1Adapter(InstrumentBase):
    descriptor = InstrumentDescriptor(
        "gopro", "GoPro", "HATCHBOX",
        frozenset({"detection", "metadata", "quicklook", "thumbnails", "export"}),
        True,
    )

    def __init__(
        self, *, output_root: Path, flight_name: str,
        resource_limits: ResourceLimits,
        batch_policy: CameraBatchPolicy | None = None,
        video_probe: VideoProbe | None = None,
        video_sampler: VideoSampler | None = None,
        logger: ProcessingLogManager | None = None,
        unusually_small_image_bytes: int = 8 * 1024,
        unusually_small_video_bytes: int = 128 * 1024,
        gap_factor: float = 3.0,
        record_clock_offset_seconds: float | None = None,
    ) -> None:
        self.output_root, self.flight_name = Path(output_root), flight_name
        self.resource_limits = resource_limits
        self.batch_policy = batch_policy or CameraBatchPolicy(
            maximum_batch_files=16, maximum_thumbnail_count=12
        )
        self.video_probe = video_probe or _probe_video
        self.video_sampler = video_sampler or _ffmpeg_sample
        self.logger = logger
        self.unusually_small_image_bytes = unusually_small_image_bytes
        self.unusually_small_video_bytes = unusually_small_video_bytes
        self.gap_factor = gap_factor
        # How far the camera clock runs ahead of UTC, as declared by the
        # operator. None keeps the campaign-local assumption this had
        # before the clock could be declared.
        self.record_clock_offset_seconds = record_clock_offset_seconds
        self._callback: ProgressCallback | None = None
        self._cancelled = threading.Event()
        self._records: list[dict[str, Any]] = []

    def detect(self, scan_index: ScanIndex) -> Sequence[InputCandidate]:
        paths = tuple(
            entry.path for entry in scan_index.entries
            if entry.is_file and entry.path.suffix.casefold() in IMAGE_SUFFIXES | VIDEO_SUFFIXES
            and ("gopro" in entry.path.name.casefold() or "gopro" in str(entry.path.parent).casefold())
        )
        return (InputCandidate("gopro", paths, 0.9, "GoPro media files"),) if paths else ()

    def inspect_metadata(self, candidate: InputCandidate) -> Mapping[str, Any]:
        images, videos = _classify(candidate.paths)
        return {
            "image_count": len(images), "video_count": len(videos),
            "image_extensions": sorted({path.suffix.casefold() for path in images}),
            "video_extensions": sorted({path.suffix.casefold() for path in videos}),
            "level": "Level 1 media inventory only",
        }

    def extract_time_range(self, candidate: InputCandidate) -> TimeRange:
        images, videos = _classify(candidate.paths)
        values = []
        for path in images:
            self._check()
            value, _ = _image_timestamp(path, self.record_clock_offset_seconds)
            if value:
                values.append(value)
        for path in videos:
            self._check()
            try:
                value = _as_datetime(self.video_probe(path).get("creation_time"))
            except Exception:
                value = None
            if value:
                values.append(value)
        start, end = (min(values), max(values)) if values else (None, None)
        return TimeRange(
            start, end, start, end,
            TimezoneState.EXPLICIT_UTC if values else TimezoneState.UNKNOWN,
            "seconds", "EXIF DateTimeOriginal and MP4/MOV creation metadata",
        )

    def validate(self, candidate: InputCandidate) -> InstrumentResult:
        images, videos = _classify(candidate.paths)
        errors, warnings = [], []
        if not images and not videos:
            errors.append("No supported GoPro images or videos were selected.")
        coverage = self.extract_time_range(candidate) if images or videos else TimeRange()
        if (images or videos) and coverage.utc_start is None:
            warnings.append("No valid GoPro media timestamps were found.")
        paths = images + videos
        status = DetectionStatus.FAILED if errors else DetectionStatus.WARNING if warnings else DetectionStatus.READY
        return InstrumentResult(
            "gopro", "GoPro", "HATCHBOX", status,
            source_files=_sources(paths), file_count=len(paths),
            original_start_time=coverage.original_start, original_end_time=coverage.original_end,
            utc_start_time=coverage.utc_start, utc_end_time=coverage.utc_end,
            warnings=warnings, errors=errors, metadata=dict(self.inspect_metadata(candidate)),
        )

    def load(self, candidate: InputCandidate) -> LoadedGoPro:
        result = self.validate(candidate)
        if result.errors:
            raise ValueError("; ".join(result.errors))
        images, videos = _classify(candidate.paths)
        self._emit(2, "GoPro media validated")
        return LoadedGoPro(candidate, images, videos)

    def process_quicklook(self, loaded: LoadedGoPro, options: Mapping[str, Any]) -> InstrumentResult:
        started = time.monotonic()
        original_count = len(loaded.images) + len(loaded.videos)
        selected_start = _as_datetime(options.get("analysis_start"))
        selected_end = _as_datetime(options.get("analysis_end"))
        if selected_start is not None or selected_end is not None:
            loaded = self._select_time_interval(
                loaded, selected_start, selected_end
            )
        paths = loaded.images + loaded.videos
        if not paths:
            raise RuntimeError(
                "No GoPro media falls inside the selected Time Filter"
            )
        batch_size = min(
            self.batch_policy.maximum_batch_files,
            max(1, self.resource_limits.memory_bytes // (16 * 1024 * 1024)),
        )
        self._records = []
        for batch_index, batch in enumerate(iter_batches(paths, batch_size), 1):
            self._check()
            for path in batch:
                self._records.append(
                    self._inspect_image(path) if path in loaded.images else self._inspect_video(path)
                )
            self._emit(
                5 + 65 * len(self._records) / max(1, len(paths)),
                f"Media metadata batch {batch_index}: {len(self._records)}/{len(paths)}",
            )
        thumbnails, video_samples = self._representative_thumbnails(loaded)
        times = sorted(record["timestamp"] for record in self._records if record["timestamp"])
        image_times = sorted(record["timestamp"] for record in self._records if record["kind"] == "image" and record["timestamp"])
        image_intervals = _intervals(image_times)
        typical = _median_positive(image_intervals)
        gaps = [
            value for value in image_intervals
            if typical is not None and value > typical * self.gap_factor
        ]
        warnings = []
        missing_timestamps = sum(record["timestamp"] is None for record in self._records)
        small = [record["file_name"] for record in self._records if record["unusually_small"]]
        corrupt = [record["file_name"] for record in self._records if record["corrupt"]]
        if missing_timestamps:
            warnings.append(f"Timestamps missing from {missing_timestamps} GoPro media file(s).")
        if gaps:
            warnings.append(f"{len(gaps)} obvious image-acquisition gap(s) detected.")
        if small:
            warnings.append(f"{len(small)} unusually small GoPro media file(s) detected.")
        if corrupt:
            warnings.append(f"{len(corrupt)} corrupt or unreadable GoPro media file(s) detected.")
        if loaded.videos and video_samples < min(len(loaded.videos), self.batch_policy.maximum_thumbnail_count):
            warnings.append("Some representative video frames could not be sampled; FFmpeg may be unavailable.")
        duration = sum(float(record.get("duration_seconds") or 0) for record in self._records if record["kind"] == "video")
        status = ProcessingStatus.WARNING if warnings else ProcessingStatus.COMPLETE
        result = InstrumentResult(
            "gopro", "GoPro", "HATCHBOX",
            DetectionStatus.WARNING if warnings else DetectionStatus.READY,
            status, _sources(paths), len(paths),
            min(times) if times else None, max(times) if times else None,
            min(times) if times else None, max(times) if times else None,
            warnings=warnings, progress=100.0,
            figures=[FigureArtifact(path, "GoPro Level 1 representative thumbnail") for path in thumbnails],
            metadata={
                "image_count": len(loaded.images), "video_count": len(loaded.videos),
                "source_media_count": original_count,
                "selected_media_count": len(paths),
                "time_filter_start_utc": (
                    selected_start.isoformat() if selected_start else None
                ),
                "time_filter_end_utc": (
                    selected_end.isoformat() if selected_end else None
                ),
                "recording_duration_seconds": duration,
                "image_acquisition_intervals_seconds": image_intervals,
                "obvious_gap_count": len(gaps), "obvious_gap_intervals_seconds": gaps,
                "missing_timestamp_count": missing_timestamps,
                "corrupt_files": corrupt, "unusually_small_files": small,
                "thumbnail_count": len(thumbnails), "sampled_video_frame_count": video_samples,
                "batch_size": batch_size, "cpu_limit": self.resource_limits.worker_count,
                "ram_limit_bytes": self.resource_limits.memory_bytes,
                "full_frame_extraction_performed": False,
                "geotagging_performed": False, "final_video_product_created": False,
            },
            elapsed_time=timedelta(seconds=time.monotonic() - started),
        )
        self._emit(100, "GoPro Level 1 quick check complete")
        if self.logger:
            self.logger.log(
                LogLevel.WARNING if warnings else LogLevel.SUCCESS,
                "gopro-level1", "GoPro Level 1 quick check completed",
                instrument="gopro", processing_step="media-qc",
            )
        return result

    def _select_time_interval(
        self,
        loaded: LoadedGoPro,
        start: datetime | None,
        end: datetime | None,
    ) -> LoadedGoPro:
        """Select media after normalizing camera timestamps to UTC."""
        images: list[Path] = []
        videos: list[Path] = []
        total = len(loaded.images) + len(loaded.videos)
        inspected = 0
        for path in loaded.images:
            self._check()
            timestamp, _ = _image_timestamp(path, self.record_clock_offset_seconds)
            inspected += 1
            if _inside_interval(timestamp, start, end):
                images.append(path)
            self._emit(
                2 + 3 * inspected / max(1, total),
                f"Applying UTC Time Filter {inspected}/{total}",
            )
        for path in loaded.videos:
            self._check()
            try:
                timestamp = _as_datetime(
                    self.video_probe(path).get("creation_time")
                )
            except Exception:
                timestamp = None
            inspected += 1
            if _inside_interval(timestamp, start, end):
                videos.append(path)
            self._emit(
                2 + 3 * inspected / max(1, total),
                f"Applying UTC Time Filter {inspected}/{total}",
            )
        candidate = InputCandidate(
            loaded.candidate.instrument_id,
            tuple(images + videos),
            loaded.candidate.confidence,
            loaded.candidate.reason,
        )
        return LoadedGoPro(candidate, tuple(images), tuple(videos))

    def process_detailed(self, loaded: Any, options: Mapping[str, Any]) -> InstrumentResult:
        raise NotImplementedError("Detailed GoPro processing is not integrated")

    def create_plots(self, result: InstrumentResult, output_directory: Path) -> Sequence[FigureArtifact]:
        self._assert_output(output_directory)
        return tuple(result.figures)

    def export_results(self, result: InstrumentResult, output_directory: Path, formats: Sequence[str]) -> Sequence[OutputFile]:
        self._assert_output(output_directory)
        requested = {value.casefold() for value in formats}
        if requested - {"csv", "json"}:
            raise ValueError("GoPro Level 1 exports support CSV and JSON")
        output_directory.mkdir(parents=True, exist_ok=True)
        outputs = []
        if "csv" in requested and self._records:
            path = output_directory / "media_inventory.csv"; _reject(path)
            with path.open("w", newline="", encoding="utf-8") as stream:
                writer = csv.DictWriter(stream, fieldnames=list(self._records[0]), extrasaction="ignore")
                writer.writeheader(); writer.writerows(self._records)
            outputs.append(OutputFile(path, "gopro_level1_csv", "text/csv", path.stat().st_size))
        if "json" in requested:
            path = output_directory / "level1_summary.json"; _reject(path)
            path.write_text(json.dumps(result.metadata, indent=2, default=str), encoding="utf-8")
            outputs.append(OutputFile(path, "gopro_level1_summary", "application/json", path.stat().st_size))
        result.output_files.extend(outputs)
        return tuple(outputs)

    def cancel(self) -> None:
        self._cancelled.set()

    def report_progress(self, callback: ProgressCallback | None) -> None:
        self._callback = callback

    def media_records(self) -> tuple[dict[str, Any], ...]:
        """Return a stable copy for the GoPro/Noseboom georeferencing bridge."""
        return tuple(dict(record) for record in self._records)

    def _inspect_image(self, path: Path) -> dict[str, Any]:
        corrupt = False
        try:
            with Image.open(path) as image:
                image.verify()
        except (OSError, ValueError, UnidentifiedImageError):
            corrupt = True
        timestamp, source = _image_timestamp(path, self.record_clock_offset_seconds)
        size = path.stat().st_size
        return _record(path, "image", timestamp, source, None, size < self.unusually_small_image_bytes, corrupt)

    def _inspect_video(self, path: Path) -> dict[str, Any]:
        corrupt = False
        try:
            metadata = self.video_probe(path)
        except Exception as exc:
            metadata, corrupt = {}, True
            if self.logger:
                self.logger.capture_exception("gopro-level1", "GoPro video metadata could not be read", exc, instrument="gopro", file_path=path)
        timestamp = _as_datetime(metadata.get("creation_time"))
        duration = _float(metadata.get("duration_seconds"))
        if duration is None:
            corrupt = True
        size = path.stat().st_size
        return _record(path, "video", timestamp, "container", duration, size < self.unusually_small_video_bytes, corrupt)

    def _representative_thumbnails(self, loaded: LoadedGoPro) -> tuple[list[Path], int]:
        directory = self.output_root / "thumbnails"; directory.mkdir(parents=True, exist_ok=True)
        available = self.batch_policy.maximum_thumbnail_count
        selected_images = _representative(loaded.images, min(len(loaded.images), available))
        outputs = []
        for path in selected_images:
            self._check()
            target = directory / f"{path.stem}_thumbnail.jpg"; _reject(target)
            try:
                with Image.open(path) as image:
                    image.thumbnail((480, 360)); image.convert("RGB").save(target, "JPEG", quality=80)
                outputs.append(target)
            except (OSError, ValueError, UnidentifiedImageError):
                continue
        available -= len(outputs)
        sampled_videos = 0
        for path in _representative(loaded.videos, min(len(loaded.videos), available)):
            self._check()
            target = directory / f"{path.stem}_sample.jpg"; _reject(target)
            if self.video_sampler(path, target) and target.is_file():
                outputs.append(target); sampled_videos += 1
        return outputs, sampled_videos

    def _emit(self, progress: float, phase: str) -> None:
        self._check()
        if self._callback:
            self._callback(ProgressUpdate("gopro", progress, phase, phase))

    def _check(self) -> None:
        if self._cancelled.is_set():
            raise RuntimeError("GoPro Level 1 processing was cancelled")

    def _assert_output(self, path: Path) -> None:
        root, target = self.output_root.resolve(strict=False), Path(path).resolve(strict=False)
        try: target.relative_to(root)
        except ValueError as exc: raise ValueError(f"GoPro output must remain below {root}") from exc


def _classify(paths: Sequence[Path]) -> tuple[tuple[Path, ...], tuple[Path, ...]]:
    images, videos = [], []
    for value in paths:
        path = Path(value)
        values = path.rglob("*") if path.is_dir() else (path,)
        for item in values:
            if not item.is_file(): continue
            suffix = item.suffix.casefold()
            if suffix in IMAGE_SUFFIXES: images.append(item)
            elif suffix in VIDEO_SUFFIXES: videos.append(item)
    key = lambda path: str(path).casefold()
    return tuple(sorted(dict.fromkeys(images), key=key)), tuple(sorted(dict.fromkeys(videos), key=key))


def _image_timestamp(
    path: Path, offset_seconds: float | None = None
) -> tuple[datetime | None, str]:
    try:
        with Image.open(path) as image:
            exif = {ExifTags.TAGS.get(tag, str(tag)): value for tag, value in image.getexif().items()}
        raw = exif.get("DateTimeOriginal") or exif.get("DateTime")
        if raw:
            local_time = datetime.strptime(str(raw), "%Y:%m:%d %H:%M:%S")
            label = (
                f"EXIF {offset_seconds:+.0f} s→UTC"
                if offset_seconds is not None
                else "EXIF Europe/Berlin→UTC (assumed)"
            )
            return camera_local_to_utc(local_time, offset_seconds), label
    except (OSError, ValueError, UnidentifiedImageError):
        pass
    return None, "missing"


def _probe_video(path: Path) -> Mapping[str, Any]:
    ffprobe = shutil.which("ffprobe")
    if ffprobe:
        command = [ffprobe, "-v", "error", "-show_entries", "format=duration:format_tags=creation_time", "-of", "json", str(path)]
        value = json.loads(subprocess.run(command, check=True, capture_output=True, text=True, timeout=30).stdout)
        fmt = value.get("format", {})
        return {"duration_seconds": _float(fmt.get("duration")), "creation_time": fmt.get("tags", {}).get("creation_time")}
    return _probe_mp4_atoms(path)


def _probe_mp4_atoms(path: Path) -> Mapping[str, Any]:
    with path.open("rb") as stream:
        while True:
            header = stream.read(8)
            if len(header) < 8: break
            size, kind = struct.unpack(">I4s", header)
            if size == 1:
                size = struct.unpack(">Q", stream.read(8))[0]; header_size = 16
            else: header_size = 8
            if size < header_size: raise ValueError("Invalid MP4 atom size")
            if kind == b"moov":
                end = stream.tell() + size - header_size
                while stream.tell() < end:
                    child = stream.read(8)
                    if len(child) < 8: break
                    child_size, child_kind = struct.unpack(">I4s", child)
                    if child_kind == b"mvhd":
                        data = stream.read(child_size - 8)
                        version = data[0]
                        if version == 0:
                            created, _, scale, duration = struct.unpack(">IIII", data[4:20])
                        else:
                            created, _, scale, duration = struct.unpack(">QQIQ", data[4:32])
                        return {"creation_time": MP4_EPOCH + timedelta(seconds=created), "duration_seconds": duration / scale if scale else None}
                    stream.seek(child_size - 8, 1)
                break
            stream.seek(size - header_size, 1)
    raise ValueError("MP4/MOV movie header not found")


def _ffmpeg_sample(path: Path, target: Path) -> bool:
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg: return False
    result = subprocess.run([ffmpeg, "-v", "error", "-ss", "0", "-i", str(path), "-frames:v", "1", "-vf", "scale=480:-2", "-y", str(target)], capture_output=True, timeout=60)
    return result.returncode == 0


def _as_datetime(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value.astimezone(timezone.utc) if value.tzinfo else value.replace(tzinfo=timezone.utc)
    if not value: return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return parsed.astimezone(timezone.utc) if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
    except ValueError: return None


def _inside_interval(
    value: datetime | None,
    start: datetime | None,
    end: datetime | None,
) -> bool:
    if value is None:
        return False
    return (start is None or value >= start) and (end is None or value <= end)


def _record(path, kind, timestamp, timestamp_source, duration, small, corrupt):
    return {"source_file": str(path), "file_name": path.name, "kind": kind, "timestamp": timestamp, "timestamp_source": timestamp_source, "duration_seconds": duration, "size_bytes": path.stat().st_size, "unusually_small": small, "corrupt": corrupt}


def _representative(values: Sequence[Path], count: int) -> tuple[Path, ...]:
    if count <= 0 or not values: return ()
    if count == 1: return (values[0],)
    indexes = sorted({round(i * (len(values) - 1) / (count - 1)) for i in range(count)})
    return tuple(values[index] for index in indexes)


def _intervals(values) -> list[float]:
    return [(current - previous).total_seconds() for previous, current in zip(values, values[1:])]


def _median_positive(values):
    positive = sorted(value for value in values if value > 0)
    if not positive: return None
    middle = len(positive) // 2
    return positive[middle] if len(positive) % 2 else (positive[middle - 1] + positive[middle]) / 2


def _float(value):
    try: return float(value)
    except (TypeError, ValueError): return None


def _sources(paths) -> list[SourceFile]:
    return [SourceFile(path, size_bytes=path.stat().st_size) for path in paths]


def _reject(path: Path) -> None:
    if path.exists(): raise FileExistsError(f"GoPro Level 1 output already exists: {path}")
