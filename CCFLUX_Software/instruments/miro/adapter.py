"""Modular adapter around the unchanged legacy MIRO implementation."""

from __future__ import annotations

import csv
import shutil
import threading
import time
from uuid import uuid4
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence

import pandas as pd

from core.detector import InputCandidate
from core.enums import DetectionStatus, ProcessingStatus
from core.logging_manager import LogLevel, ProcessingLogManager
from core.models import (
    FigureArtifact,
    InstrumentDescriptor,
    InstrumentResult,
    OutputFile,
    ProgressUpdate,
    SamplingFrequency,
    SourceFile,
)
from core.scanner import ScanIndex
from core.time_manager import TimeRange, TimezoneState
from instruments.base.interface import InstrumentBase, ProgressCallback

from .legacy_bridge import LegacyMiroBridge

TIMESTAMP_COLUMN = "t-stamp"
VALVE_COLUMN = "VValve 0"
GAS_COLUMNS = (
    "CO wet", "N2O wet", "H2O wet", "NO wet", "NO2 wet",
    "CH4 wet", "SO2 wet", "NH3 wet", "O3 wet", "CO2 wet",
)
OPERATOR_GAP_WARNING_SECONDS = 4 * 60
_SEGMENT_GAP_WARNING_TEXT = "missing-sample time gaps longer than"


@dataclass(slots=True)
class LoadedMiro:
    candidate: InputCandidate
    data: Any
    load_metadata: dict[str, Any]


class MiroAdapter(InstrumentBase):
    descriptor = InstrumentDescriptor(
        instrument_id="miro",
        display_name="MIRO",
        physical_group="MIRO RACK",
        capabilities=frozenset(
            {"detection", "quicklook", "detailed", "plots", "export"}
        ),
        integrated=True,
    )

    def __init__(
        self,
        *,
        output_root: Path,
        flight_name: str,
        bridge: LegacyMiroBridge | None = None,
        logger: ProcessingLogManager | None = None,
    ) -> None:
        self.output_root = Path(output_root)
        self.flight_name = flight_name
        self.bridge = bridge or LegacyMiroBridge()
        self.logger = logger
        self._progress_callback: ProgressCallback | None = None
        self._cancelled = threading.Event()
        self._loaded: LoadedMiro | None = None
        self._analysis: dict[str, Any] | None = None
        self._options: dict[str, Any] = {}

    def detect(self, scan_index: ScanIndex) -> Sequence[InputCandidate]:
        paths = []
        for entry in scan_index.entries:
            if not entry.is_file or entry.path.suffix.casefold() != ".txt":
                continue
            columns = _columns(entry.path)
            if TIMESTAMP_COLUMN in columns and any(gas in columns for gas in GAS_COLUMNS):
                paths.append(entry.path)
        return (
            InputCandidate(
                "miro", tuple(paths), 1.0,
                "Verified MIRO timestamp and trace-gas columns",
            ),
        ) if paths else ()

    def inspect_metadata(self, candidate: InputCandidate) -> Mapping[str, Any]:
        columns = set().union(*(_columns(path) for path in candidate.paths))
        gases = [gas for gas in GAS_COLUMNS if gas in columns]
        return {
            "file_count": len(candidate.paths),
            "columns": sorted(columns),
            "gases": gases,
            "has_valve_column": VALVE_COLUMN in columns,
            "timestamp_column": TIMESTAMP_COLUMN,
            "timestamp_format": "%d.%m.%Y %H:%M:%S,%f",
            "timezone": "UTC (campaign instrument convention)",
        }

    def extract_time_range(self, candidate: InputCandidate) -> TimeRange:
        minimum = maximum = None
        for path in candidate.paths:
            try:
                values = pd.read_csv(
                    path, sep=";", decimal=",", usecols=[TIMESTAMP_COLUMN]
                )[TIMESTAMP_COLUMN]
            except (OSError, ValueError, pd.errors.ParserError):
                continue
            parsed = pd.to_datetime(
                values, format="%d.%m.%Y %H:%M:%S,%f", errors="coerce"
            ).dropna()
            if parsed.empty:
                continue
            start, end = parsed.min().to_pydatetime(), parsed.max().to_pydatetime()
            minimum = start if minimum is None else min(minimum, start)
            maximum = end if maximum is None else max(maximum, end)
        return TimeRange(
            original_start=minimum,
            original_end=maximum,
            utc_start=_campaign_utc(minimum),
            utc_end=_campaign_utc(maximum),
            timezone_state=TimezoneState.EXPLICIT_UTC,
            precision="microseconds",
            basis=f"{TIMESTAMP_COLUMN}; UTC by campaign convention",
        )

    @staticmethod
    def text_only(candidate: InputCandidate) -> tuple[InputCandidate, list[Path]]:
        """Keep the .txt deliveries and hand back whatever else was offered.

        MIRO writes TDMS beside the text export and its timestamp schema was
        never confirmed for this campaign, so it is not read. Detection no
        longer matches it, but a candidate restored from an older project still
        can - and one binary file in the list failed validation for the whole
        instrument, because it parses as a text file with no t-stamp column.
        """
        kept = [path for path in candidate.paths if path.suffix.casefold() == ".txt"]
        ignored = [path for path in candidate.paths if path.suffix.casefold() != ".txt"]
        if not ignored:
            return candidate, []
        return replace(candidate, paths=tuple(kept)), ignored

    def validate(self, candidate: InputCandidate) -> InstrumentResult:
        warnings, errors = [], []
        candidate, ignored = self.text_only(candidate)
        if ignored:
            warnings.append(
                f"{len(ignored)} non-text file(s) were ignored; MIRO is read "
                "from .txt only: "
                + ", ".join(path.name for path in ignored[:3])
                + (" ..." if len(ignored) > 3 else "")
            )
        metadata = dict(self.inspect_metadata(candidate))
        if not candidate.paths:
            errors.append("No MIRO .txt files were selected.")
        for path in candidate.paths:
            columns = _columns(path)
            if TIMESTAMP_COLUMN not in columns:
                errors.append(f"{path.name}: missing required {TIMESTAMP_COLUMN}")
            if not any(gas in columns for gas in GAS_COLUMNS):
                errors.append(f"{path.name}: no supported MIRO trace-gas column")
        if candidate.paths and not metadata["has_valve_column"]:
            warnings.append(
                "Valve column is absent; the unchanged MIRO routine will use "
                "its time-gap fallback."
            )
        time_range = self.extract_time_range(candidate)
        if time_range.original_start is None:
            errors.append("No valid MIRO timestamps were found.")
        status = (
            DetectionStatus.FAILED if errors else
            DetectionStatus.WARNING if warnings else DetectionStatus.READY
        )
        return InstrumentResult(
            instrument_id="miro",
            display_name="MIRO",
            physical_group="MIRO RACK",
            detection_status=status,
            source_files=_sources(candidate.paths),
            file_count=len(candidate.paths),
            original_start_time=time_range.original_start,
            original_end_time=time_range.original_end,
            utc_start_time=time_range.utc_start,
            utc_end_time=time_range.utc_end,
            warnings=warnings,
            errors=errors,
            metadata=metadata,
        )
    def load(self, candidate: InputCandidate) -> LoadedMiro:
        validation = self.validate(candidate)
        if validation.errors:
            raise ValueError("; ".join(validation.errors))
        candidate, _ = self.text_only(candidate)
        roots = {path.parent.resolve() for path in candidate.paths}
        if len(roots) != 1:
            raise ValueError("A MIRO candidate must use files from one confirmed folder")
        self._check_cancelled()
        self._emit(2, "Loading validated MIRO text files")
        try:
            data, metadata = self.bridge.load_folder(
                roots.pop(),
                lambda fraction, message: self._emit(
                    min(45.0, 2.0 + float(fraction) * 43.0), message
                ),
            )
        except Exception as exc:
            self._log_exception("MIRO loading failed", exc)
            raise
        loaded = LoadedMiro(candidate, data, dict(metadata))
        self._loaded = loaded
        self._emit(45, "MIRO data loaded")
        return loaded

    def process_quicklook(
        self, loaded: LoadedMiro, options: Mapping[str, Any]
    ) -> InstrumentResult:
        started = time.monotonic()
        self._check_cancelled()
        selected = dict(options)
        gas = str(selected.get("gas") or "NO2 wet")
        if gas not in loaded.data.columns:
            available = [value for value in GAS_COLUMNS if value in loaded.data.columns]
            if not available:
                raise ValueError("No supported MIRO gas is available")
            gas = available[0]
        start, end, timezone_warning = _legacy_bounds(
            selected.get("analysis_start"), selected.get("analysis_end")
        )
        self._emit(50, f"Running unchanged MIRO quicklook for {gas}")
        try:
            analysis = self.bridge.analyze(
                loaded.data,
                gas=gas,
                smooth_seconds=float(selected.get("smooth_seconds", 300.0)),
                start=start,
                end=end,
                remove_seconds=float(selected.get("remove_seconds", 30.0)),
            )
        except Exception as exc:
            self._log_exception("MIRO quicklook failed", exc)
            raise
        self._check_cancelled()
        self._analysis = analysis
        self._options = {
            "flight_no": self.flight_name,
            "miro_gas": gas,
            "smooth_seconds": float(selected.get("smooth_seconds", 300.0)),
            "miro_start": start,
            "miro_end": end,
            "dpi": int(selected.get("dpi", 300)),
        }
        warnings = _operator_facing_miro_warnings(
            analysis.get("warnings", ()), analysis
        )
        if timezone_warning:
            warnings.append(timezone_warning)
        # What the loader had to repair to produce this frame - duplicate files,
        # unreadable clocks, rows delivered out of order. It reached the result
        # metadata but never the warnings, so a run that silently dropped rows
        # and one that had nothing to drop looked identical on the card.
        warnings.extend(str(value) for value in loaded.load_metadata.get("warnings", ()))
        filtered = self.bridge.miro._slice(loaded.data, start, end)
        intervals = filtered["timestamp"].diff().dt.total_seconds()
        median_interval = intervals[intervals > 0].median()
        result = InstrumentResult(
            instrument_id="miro",
            display_name="MIRO",
            physical_group="MIRO RACK",
            detection_status=DetectionStatus.WARNING if warnings else DetectionStatus.READY,
            processing_status=ProcessingStatus.WARNING if warnings else ProcessingStatus.COMPLETE,
            source_files=_sources(loaded.candidate.paths),
            file_count=len(loaded.candidate.paths),
            original_start_time=filtered["timestamp"].min().to_pydatetime(),
            original_end_time=filtered["timestamp"].max().to_pydatetime(),
            sampling_frequency=(
                SamplingFrequency(
                    1.0 / float(median_interval),
                    method="median positive recorded timestamp interval",
                )
                if pd.notna(median_interval) and median_interval > 0 else None
            ),
            warnings=list(dict.fromkeys(warnings)),
            progress=100.0,
            metadata={
                "gas": gas,
                "unit": analysis["unit"],
                "load": loaded.load_metadata,
                "analysis": analysis,
                "scientific_source": str(self.bridge.source_directory / "miro.py"),
            },
            elapsed_time=timedelta(seconds=time.monotonic() - started),
        )
        self._emit(100, "MIRO quicklook complete")
        self._log(LogLevel.SUCCESS, f"MIRO quicklook completed for {gas}")
        return result

    def process_detailed(
        self, loaded: LoadedMiro, options: Mapping[str, Any]
    ) -> InstrumentResult:
        # The legacy code's detailed path is its all-gas publication export.
        result = self.process_quicklook(loaded, options)
        self.export_results(
            result,
            self.output_root,
            tuple(options.get("formats", ("pdf",))),
        )
        return result

    def create_plots(
        self, result: InstrumentResult, output_directory: Path
    ) -> Sequence[FigureArtifact]:
        self._assert_output_path(output_directory)
        if self._analysis is None:
            raise RuntimeError("MIRO quicklook must complete before plot creation")
        gas = str(self._analysis["gas"]).replace(" ", "_")
        path = output_directory / f"MIRO_{gas}_quicklook.png"
        if path.exists():
            raise FileExistsError(f"MIRO plot exists and was not overwritten: {path}")
        self.bridge.save_quicklook(self._analysis, path, {**self._options, "dpi": 150})
        artifact = FigureArtifact(path, f"MIRO {self._analysis['gas']} quicklook")
        result.figures.append(artifact)
        return (artifact,)

    def export_results(
        self,
        result: InstrumentResult,
        output_directory: Path,
        formats: Sequence[str],
    ) -> Sequence[OutputFile]:
        self._assert_output_path(output_directory)
        if self._loaded is None or self._analysis is None:
            raise RuntimeError("MIRO processing must complete before export")
        staging = output_directory / f".miro-export-{uuid4().hex}"
        staging.mkdir()
        outputs = []
        try:
            staged_paths = self.bridge.export_figures(
                staging,
                formats,
                self._loaded.data,
                self._options,
                lambda fraction, message: self._emit(
                    90.0 + min(9.0, float(fraction) * 9.0), message
                ),
            )
            for value in staged_paths:
                staged = Path(value)
                path = output_directory / staged.name
                if path.exists():
                    raise FileExistsError(
                        f"MIRO export exists and was not overwritten: {path}"
                    )
                staged.rename(path)
                outputs.append(
                    OutputFile(path, "miro_figure", size_bytes=path.stat().st_size)
                )
        finally:
            shutil.rmtree(staging, ignore_errors=True)
        result.output_files.extend(outputs)
        return tuple(outputs)

    def cancel(self) -> None:
        self._cancelled.set()

    def report_progress(self, callback: ProgressCallback | None) -> None:
        self._progress_callback = callback

    def _assert_output_path(self, path: Path) -> None:
        root, target = self.output_root.resolve(strict=False), Path(path).resolve(strict=False)
        try:
            target.relative_to(root)
        except ValueError as exc:
            raise ValueError(
                f"MIRO output must remain below selected output folder: {root}"
            ) from exc
        target.mkdir(parents=True, exist_ok=True)

    def _emit(self, progress: float, phase: str) -> None:
        self._check_cancelled()
        if self._progress_callback:
            self._progress_callback(ProgressUpdate("miro", progress, phase, phase))

    def _check_cancelled(self) -> None:
        if self._cancelled.is_set():
            raise RuntimeError("MIRO processing was cancelled")

    def _log(self, level: LogLevel, message: str) -> None:
        if self.logger:
            self.logger.log(level, "miro-adapter", message, instrument="miro")

    def _log_exception(self, message: str, exception: BaseException) -> None:
        if self.logger:
            self.logger.capture_exception(
                "miro-adapter", message, exception, instrument="miro"
            )


def _operator_facing_miro_warnings(
    analysis_warnings: Sequence[Any],
    analysis: Mapping[str, Any],
) -> list[str]:
    """Keep scientific segmentation intact but alert operators only for gaps >4 min."""
    warnings = [
        str(item)
        for item in analysis_warnings
        if _SEGMENT_GAP_WARNING_TEXT not in str(item).casefold()
    ]
    raw_times = (analysis.get("series") or {}).get("time", ())
    times = pd.to_datetime(pd.Series(raw_times), errors="coerce").dropna().sort_values()
    if len(times) > 1:
        intervals = times.diff().dt.total_seconds()
        long_gap_count = int(intervals.gt(OPERATOR_GAP_WARNING_SECONDS).sum())
        if long_gap_count:
            warnings.append(
                f"{long_gap_count:,} recorded ambient-data time "
                "gap(s) longer than 4 minutes split the segmented analyses; "
                "no interpolation was applied."
            )
    return list(dict.fromkeys(warnings))


def _campaign_utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return (
        value.replace(tzinfo=timezone.utc)
        if value.tzinfo is None
        else value.astimezone(timezone.utc)
    )


def _columns(path: Path) -> set[str]:
    with Path(path).open(
        "r", encoding="utf-8-sig", errors="replace", newline=""
    ) as stream:
        return {
            value.strip()
            for value in next(csv.reader(stream, delimiter=";"), ())
        }


def _sources(paths: Sequence[Path]) -> list[SourceFile]:
    return [
        SourceFile(path=path, size_bytes=path.stat().st_size if path.exists() else None)
        for path in paths
    ]


def _legacy_bounds(start: Any, end: Any) -> tuple[str | None, str | None, str | None]:
    if start is None and end is None:
        return None, None, None
    if start is None or end is None:
        raise ValueError("Both MIRO analysis start and end are required")
    parsed = []
    for value in (start, end):
        item = value if isinstance(value, datetime) else datetime.fromisoformat(
            str(value).replace("Z", "+00:00")
        )
        if item.tzinfo is not None:
            item = item.astimezone(timezone.utc).replace(tzinfo=None)
        parsed.append(item)
    if parsed[0] >= parsed[1]:
        raise ValueError("MIRO analysis start must be earlier than end")
    return (
        parsed[0].strftime("%Y-%m-%d %H:%M:%S.%f"),
        parsed[1].strftime("%Y-%m-%d %H:%M:%S.%f"),
        None,
    )
