"""Modular adapter around unchanged Partector Pro scientific routines."""

from __future__ import annotations

import csv
import json
import shutil
import threading
import time
from argparse import Namespace
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence
from uuid import uuid4

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
from instruments.hatchbox_payload import partector_payload, write_json_atomic

from .legacy_bridge import LegacyPartectorBridge

CANONICAL_REQUIRED = (
    "_time",
    "instrument_time_s",
    "number_cm3",
    "mean_diameter_nm",
    "ldsa_um2_cm3",
    "mass_ug_m3",
    "flow_lpm",
    "rh_percent",
    "temperature_c",
    "pressure_hpa",
    "error",
)


@dataclass(slots=True)
class LoadedPartector:
    candidate: InputCandidate
    source_path: Path
    raw: Any


class PartectorAdapter(InstrumentBase):
    descriptor = InstrumentDescriptor(
        instrument_id="partector",
        display_name="Partector Pro",
        physical_group="HATCHBOX",
        capabilities=frozenset(
            {"detection", "quicklook", "plots", "export"}
        ),
        integrated=True,
    )

    def __init__(
        self,
        *,
        output_root: Path,
        flight_name: str,
        bridge: LegacyPartectorBridge | None = None,
        logger: ProcessingLogManager | None = None,
    ) -> None:
        self.output_root = Path(output_root)
        self.flight_name = flight_name
        self.bridge = bridge or LegacyPartectorBridge()
        self.logger = logger
        self._progress_callback: ProgressCallback | None = None
        self._cancelled = threading.Event()
        self._data = None
        self._selected = None
        self._sessions = None
        self._size_columns: list[str] = []
        self._centers_nm = None
        self._selected_ids: list[int] = []
        self._summary: dict[str, Any] | None = None
        self._source_path: Path | None = None
        self._source_integrity: dict[str, Any] = {}

    def detect(self, scan_index: ScanIndex) -> Sequence[InputCandidate]:
        paths = []
        for entry in scan_index.entries:
            if not entry.is_file or entry.path.suffix.casefold() != ".csv":
                continue
            try:
                canonical = self._canonical_header(entry.path)
                self.bridge.module.require_columns(canonical, CANONICAL_REQUIRED)
                self.bridge.module.find_size_columns(canonical)
            except (OSError, ValueError, KeyError):
                continue
            paths.append(entry.path)
        return (
            InputCandidate(
                "partector",
                tuple(paths),
                1.0,
                "Complete canonical Partector fields and exactly eight size channels",
            ),
        ) if paths else ()

    def inspect_metadata(self, candidate: InputCandidate) -> Mapping[str, Any]:
        details = []
        for path in candidate.paths:
            canonical = self._canonical_header(path)
            size_columns, centers = self.bridge.module.find_size_columns(canonical)
            details.append(
                {
                    "path": str(path),
                    "raw_columns": sorted(_columns(path)),
                    "canonical_columns": sorted(canonical.columns),
                    "size_columns": size_columns,
                    "size_channel_centers_nm": centers.tolist(),
                }
            )
        return {
            "file_count": len(candidate.paths),
            "files": details,
            "timestamp_column": "_time",
            "timestamp_policy": (
                "UTC by campaign instrument convention"
            ),
        }

    def extract_time_range(self, candidate: InputCandidate) -> TimeRange:
        minimum = maximum = None
        for path in candidate.paths:
            try:
                raw = pd.read_csv(path, usecols=["_time"], low_memory=False)
                parsed = self.bridge.module.canonicalize_columns(raw)["_time"].dropna()
            except (OSError, ValueError, KeyError, pd.errors.ParserError):
                continue
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
            precision="source-dependent",
            basis="_time; UTC by campaign convention",
        )

    def validate(self, candidate: InputCandidate) -> InstrumentResult:
        errors, warnings = [], []
        if not candidate.paths:
            errors.append("No Partector Pro CSV file was selected.")
        if len(candidate.paths) > 1:
            errors.append(
                "Multiple Partector Pro candidates require user confirmation."
            )
        for path in candidate.paths:
            try:
                canonical = self._canonical_header(path)
                self.bridge.module.require_columns(canonical, CANONICAL_REQUIRED)
                self.bridge.module.find_size_columns(canonical)
            except (OSError, ValueError, KeyError) as exc:
                errors.append(f"{path.name}: {exc}")
        time_range = self.extract_time_range(candidate)
        if candidate.paths and time_range.original_start is None:
            errors.append("No valid Partector recorded timestamps were found.")
        status = (
            DetectionStatus.FAILED if errors else
            DetectionStatus.WARNING if warnings else DetectionStatus.READY
        )
        metadata = {}
        if candidate.paths:
            try:
                metadata = dict(self.inspect_metadata(candidate))
            except (OSError, ValueError, KeyError):
                pass
        return InstrumentResult(
            instrument_id="partector",
            display_name="Partector Pro",
            physical_group="HATCHBOX",
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

    def load(self, candidate: InputCandidate) -> LoadedPartector:
        validation = self.validate(candidate)
        if validation.errors:
            raise ValueError("; ".join(validation.errors))
        self._check_cancelled()
        path = candidate.paths[0]
        self._emit(5, "Loading Partector Pro CSV")
        try:
            raw = pd.read_csv(path, low_memory=False)
        except Exception as exc:
            self._log_exception("Partector Pro loading failed", exc)
            raise
        self._emit(20, "Partector Pro CSV loaded")
        return LoadedPartector(candidate, path, raw)

    def process_quicklook(
        self, loaded: LoadedPartector, options: Mapping[str, Any]
    ) -> InstrumentResult:
        started = time.monotonic()
        module = self.bridge.module
        gap = float(options.get("session_gap_minutes", 10.0))
        trim = float(options.get("trim_start_minutes", 0.0))
        flow_min = float(options.get("flow_min_lpm", 0.45))
        flow_max = float(options.get("flow_max_lpm", 0.55))
        selector = str(options.get("session", "longest"))
        if gap <= 0 or trim < 0 or flow_min >= flow_max:
            raise ValueError("Invalid Partector session, trim, or flow configuration")
        try:
            self._check_cancelled()
            self._emit(25, "Canonicalizing validated Partector fields")
            frame = module.canonicalize_columns(loaded.raw)
            module.require_columns(frame, CANONICAL_REQUIRED)
            valid_time = frame["_time"].notna()
            source_times = frame.loc[valid_time, "_time"]
            source_clock = pd.to_numeric(frame["instrument_time_s"], errors="coerce")
            self._source_integrity = {
                "source_file": str(loaded.source_path),
                "source_rows": int(len(frame)),
                "valid_timestamp_rows": int(valid_time.sum()),
                "invalid_timestamp_rows": int((~valid_time).sum()),
                "out_of_order_transitions": int((source_times.diff().dt.total_seconds() < 0).sum()),
                "instrument_clock_resets": int((source_clock.diff() < 0).sum()),
                "duplicated_instrument_clock_rows": int(source_clock.duplicated().sum()),
                "sorting": "stable chronological sort before session and QC analysis",
                "raw_source_modified": False,
            }
            size_columns, centers_nm = module.find_size_columns(frame)
            self._emit(40, "Assigning Partector sessions and QC flags")
            data = module.assign_sessions(frame, gap)
            config = module.QCConfig(
                flow_min_lpm=flow_min, flow_max_lpm=flow_max
            )
            data = module.apply_qc(data, size_columns, config, trim)
            self._emit(58, "Integrating unchanged logarithmic size distributions")
            data = module.integrate_size_distribution(
                data, size_columns, centers_nm
            )
            sessions = module.session_table(data)
            selected_ids = module.choose_sessions(sessions, selector)
            selected = data[data["session"].isin(selected_ids)].copy()
            selected, filter_warning = _filter_time(
                selected,
                options.get("analysis_start"),
                options.get("analysis_end"),
            )
            args = Namespace(
                csv=loaded.source_path,
                session_gap_minutes=gap,
                trim_start_minutes=trim,
            )
            summary = module.create_summary(
                len(loaded.raw),
                data,
                selected,
                sessions,
                selected_ids,
                centers_nm,
                config,
                args,
            )
            independent = selected["qc_unique_instrument_record"].fillna(False).astype(bool)
            valid_independent = independent & selected["qc_valid"].fillna(False).astype(bool)
            independent_count = int(independent.sum())
            valid_independent_count = int(valid_independent.sum())
            summary["selected_independent_rows"] = independent_count
            summary["selected_valid_rows"] = valid_independent_count
            summary["selected_logger_replication_rows"] = int(len(selected) - independent_count)
            summary["selected_qc_pass_fraction"] = (
                float(valid_independent_count / independent_count)
                if independent_count else 0.0
            )
            summary["qc_fraction_denominator"] = "independent instrument records"
            summary["source_integrity"] = dict(self._source_integrity)
        except Exception as exc:
            self._log_exception("Partector Pro quicklook failed", exc)
            raise
        self._data, self._selected, self._sessions = data, selected, sessions
        self._size_columns, self._centers_nm = size_columns, centers_nm
        self._selected_ids, self._summary = selected_ids, summary
        self._source_path = loaded.source_path

        warnings = []

        if self._source_integrity.get("invalid_timestamp_rows"):
            warnings.append(
                f"{self._source_integrity['invalid_timestamp_rows']} source row(s) without "
                "a valid timestamp remain documented in the raw input and are excluded."
            )
        if self._source_integrity.get("out_of_order_transitions"):
            warnings.append(
                f"{self._source_integrity['out_of_order_transitions']} out-of-order logger "
                "transition(s) were corrected with a stable chronological sort."
            )
        if filter_warning:
            warnings.append(filter_warning)
        invalid = int((independent & ~selected["qc_valid"].fillna(False).astype(bool)).sum())
        if invalid:
            warnings.append(
                f"{invalid} independent instrument records failed one or more QC checks; "
                "they remain in the QC export."
            )
        times = selected["_time"].sort_values().diff().dt.total_seconds()
        interval = times[times > 0].median()
        result = InstrumentResult(
            instrument_id="partector",
            display_name="Partector Pro",
            physical_group="HATCHBOX",
            # Detection remains ready: QC annotations describe processed observations,
            # not ambiguity in the validated Partector source identity.
            detection_status=DetectionStatus.READY,
            processing_status=(
                ProcessingStatus.WARNING if warnings else ProcessingStatus.COMPLETE
            ),
            source_files=_sources(loaded.candidate.paths),
            file_count=len(loaded.candidate.paths),
            original_start_time=selected["_time"].min().to_pydatetime(),
            original_end_time=selected["_time"].max().to_pydatetime(),
            sampling_frequency=(
                SamplingFrequency(
                    1.0 / float(interval),
                    method="median positive selected recorded-time interval",
                )
                if pd.notna(interval) and interval > 0 else None
            ),
            completeness_percentage=100.0 * float(summary["selected_qc_pass_fraction"]),
            warnings=warnings,
            progress=100.0,
            metadata={
                "selected_sessions": selected_ids,
                "size_channel_centers_nm": centers_nm.tolist(),
                "summary": summary,
                "source_integrity": dict(self._source_integrity),
                "scientific_source": str(self.bridge.source_path),
            },
            elapsed_time=timedelta(seconds=time.monotonic() - started),
        )
        self._emit(100, "Partector Pro quicklook complete")
        self._log(LogLevel.SUCCESS, "Partector Pro quicklook completed")
        return result

    def process_detailed(
        self, loaded: LoadedPartector, options: Mapping[str, Any]
    ) -> InstrumentResult:
        raise NotImplementedError(
            "No separate validated detailed Partector Pro algorithm exists"
        )

    def create_plots(
        self, result: InstrumentResult, output_directory: Path
    ) -> Sequence[FigureArtifact]:
        self._assert_output_path(output_directory)
        self._require_products()
        targets = (
            output_directory / "partector_quicklook.png",
            output_directory / "partector_quicklook.pdf",
        )
        for path in targets:
            _protect(path)
        staging = output_directory / f".partector-plot-{uuid4().hex}"
        staging.mkdir()
        try:
            self.bridge.module.plot_quicklook(
                self._selected,
                self._size_columns,
                self._centers_nm,
                self._selected_ids,
                staging,
                show_pressure_altitude=True,
            )
            for target in targets:
                (staging / target.name).rename(target)
        finally:
            shutil.rmtree(staging, ignore_errors=True)
        figures = (
            FigureArtifact(targets[0], "Partector Pro quicklook PNG"),
            FigureArtifact(targets[1], "Partector Pro quicklook PDF"),
        )
        result.figures.extend(figures)
        return figures

    def export_results(
        self,
        result: InstrumentResult,
        output_directory: Path,
        formats: Sequence[str],
    ) -> Sequence[OutputFile]:
        self._assert_output_path(output_directory)
        self._require_products()
        selected_formats = {value.casefold() for value in formats}
        if not selected_formats or selected_formats - {"csv", "json", "md"}:
            raise ValueError("Partector exports support CSV, JSON, and MD")
        outputs = []
        if "csv" in selected_formats:
            tables = (
                ("sessions.csv", self._sessions, "sessions"),
                ("qc_records.csv", self._data, "qc_records"),
                (
                    "cleaned_valid_records.csv",
                    self._data[self._data["qc_valid"]],
                    "cleaned_valid_records",
                ),
            )
            for name, table, role in tables:
                path = output_directory / name
                _protect(path)
                table.to_csv(path, index=False)
                outputs.append(OutputFile(path, role, size_bytes=path.stat().st_size))
        if "json" in selected_formats:
            path = output_directory / "summary.json"
            _protect(path)
            path.write_text(
                json.dumps(self._summary, indent=2, allow_nan=False),
                encoding="utf-8",
            )
            outputs.append(OutputFile(path, "summary", size_bytes=path.stat().st_size))
        if "md" in selected_formats:
            path = output_directory / "README.md"
            _protect(path)
            self.bridge.module.write_methodology(
                path, self._source_path, self._selected_ids, self._summary
            )
            outputs.append(OutputFile(path, "methodology", size_bytes=path.stat().st_size))
        result.output_files.extend(outputs)
        return tuple(outputs)

    def export_browser_data(
        self, result: InstrumentResult, output_directory: Path
    ) -> OutputFile:
        """Persist compact Plotly-ready data for the dedicated instrument page."""
        self._assert_output_path(output_directory)
        self._require_products()
        path = output_directory / "partector_browser.json"
        _protect(path)
        write_json_atomic(
            path,
            partector_payload(
                self._selected,
                self._sessions,
                self._size_columns,
                self._centers_nm,
                self._summary,
                flight_id=self.flight_name,
            ),
        )
        output = OutputFile(path, "browser_payload", size_bytes=path.stat().st_size)
        result.output_files.append(output)
        return output

    def cancel(self) -> None:
        self._cancelled.set()

    def report_progress(self, callback: ProgressCallback | None) -> None:
        self._progress_callback = callback

    def _canonical_header(self, path: Path):
        raw = pd.read_csv(path, nrows=0)
        return self.bridge.module.canonicalize_columns(raw)

    def _assert_output_path(self, path: Path) -> None:
        root, target = self.output_root.resolve(strict=False), Path(path).resolve(strict=False)
        try:
            target.relative_to(root)
        except ValueError as exc:
            raise ValueError(
                f"Partector output must remain below selected output folder: {root}"
            ) from exc
        target.mkdir(parents=True, exist_ok=True)

    def _require_products(self) -> None:
        if self._summary is None or self._selected is None:
            raise RuntimeError("Partector quicklook must complete first")

    def _emit(self, progress: float, phase: str) -> None:
        self._check_cancelled()
        if self._progress_callback:
            self._progress_callback(
                ProgressUpdate("partector", progress, phase, phase)
            )

    def _check_cancelled(self) -> None:
        if self._cancelled.is_set():
            raise RuntimeError("Partector Pro processing was cancelled")

    def _log(self, level: LogLevel, message: str) -> None:
        if self.logger:
            self.logger.log(
                level, "partector-adapter", message, instrument="partector"
            )

    def _log_exception(self, message: str, exception: BaseException) -> None:
        if self.logger:
            self.logger.capture_exception(
                "partector-adapter", message, exception, instrument="partector"
            )


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
        return {value.strip() for value in next(csv.reader(stream), ())}


def _sources(paths: Sequence[Path]) -> list[SourceFile]:
    return [
        SourceFile(path, size_bytes=path.stat().st_size if path.exists() else None)
        for path in paths
    ]


def _filter_time(data, start: Any, end: Any):
    if start is None and end is None:
        return data.copy(), None
    if start is None or end is None:
        raise ValueError("Both Partector analysis start and end are required")
    parsed = []
    aware = False
    for value in (start, end):
        item = value if isinstance(value, datetime) else datetime.fromisoformat(
            str(value).replace("Z", "+00:00")
        )
        aware = aware or item.tzinfo is not None
        parsed.append(pd.Timestamp(item.replace(tzinfo=None)))
    if parsed[0] >= parsed[1]:
        raise ValueError("Partector analysis start must be earlier than end")
    selected = data.loc[
        data["_time"].between(parsed[0], parsed[1], inclusive="both")
    ].copy()
    if selected.empty:
        raise ValueError("Selected interval does not intersect Partector data")

    warning = None  # Campaign instrument clocks are authoritative UTC.

    return selected, warning


def _protect(path: Path) -> None:
    if path.exists():
        raise FileExistsError(
            f"Partector output exists and was not overwritten: {path}"
        )
