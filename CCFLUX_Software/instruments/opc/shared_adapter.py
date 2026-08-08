"""Shared adapter mechanics with strict per-sensor OPC identity."""

from __future__ import annotations

import csv
import json
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence

import matplotlib

# Figures are rendered from processing worker threads in a server process. A
# GUI backend (Tk is present for the native folder dialogs) is not thread-safe
# and aborts on macOS, so the non-interactive backend is selected before pyplot
# is imported. This also applies to the legacy OPC module loaded by the bridge,
# which is imported lazily and therefore always after this line.
matplotlib.use("Agg")

import matplotlib.colors as mcolors  # noqa: E402
import matplotlib.pyplot as plt  # noqa: E402
import pandas as pd  # noqa: E402

from core import figure_standard
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
from instruments.hatchbox_payload import opc_sensor_payload, write_json_atomic

from .legacy_bridge import LegacyOpcBridge


@dataclass(slots=True)
class LoadedOpc:
    candidate: InputCandidate
    source_path: Path
    raw_columns: tuple[str, ...]
    raw_row_count: int


class OpcAdapterBase(InstrumentBase):
    """Common behavior; subclasses provide one immutable sensor identity."""

    instrument_id: str
    display_name: str

    def __init__(
        self,
        *,
        output_root: Path,
        flight_name: str,
        bridge: LegacyOpcBridge | None = None,
        logger: ProcessingLogManager | None = None,
    ) -> None:
        self.output_root = Path(output_root)
        self.flight_name = flight_name
        self.bridge = bridge or LegacyOpcBridge()
        self.logger = logger
        self._progress_callback: ProgressCallback | None = None
        self._cancelled = threading.Event()
        self._evaluated = None
        self._science_metadata: dict[str, Any] | None = None
        self._source_integrity: dict[str, Any] = {}
        self._source_artifacts: list[OutputFile] = []

    @property
    def descriptor(self) -> InstrumentDescriptor:
        return InstrumentDescriptor(
            instrument_id=self.instrument_id,
            display_name=self.display_name,
            physical_group="HATCHBOX",
            capabilities=frozenset(
                {"detection", "quicklook", "plots", "export"}
            ),
            integrated=True,
        )

    @property
    def spec(self):
        return self.bridge.sensor_spec(self.instrument_id)

    @property
    def required(self) -> tuple[str, ...]:
        return tuple(self.bridge.required_columns(self.instrument_id))

    def detect(self, scan_index: ScanIndex) -> Sequence[InputCandidate]:
        candidates = []
        required = set(self.required)
        for entry in scan_index.entries:
            if not entry.is_file or entry.path.suffix.casefold() != ".csv":
                continue
            columns = _columns(entry.path)
            if required.issubset(columns):
                candidates.append(entry.path)
        if not candidates:
            return ()
        return (
            InputCandidate(
                self.instrument_id,
                tuple(candidates),
                1.0,
                f"Complete authoritative {self.spec.name} schema",
            ),
        )

    def inspect_metadata(self, candidate: InputCandidate) -> Mapping[str, Any]:
        column_sets = [_columns(path) for path in candidate.paths]
        return {
            "file_count": len(candidate.paths),
            "candidate_columns": [sorted(value) for value in column_sets],
            "required_columns": list(self.required),
            "sensor": self.spec.name,
            "sensor_label": self.spec.label,
            "timestamp_column": "_time",
            "timestamp_policy": (
                "UTC by campaign instrument convention"
            ),
        }

    def extract_time_range(self, candidate: InputCandidate) -> TimeRange:
        minimum = maximum = None
        parser = self.bridge.module.parse_recorded_time
        for path in candidate.paths:
            try:
                values = pd.read_csv(path, usecols=["_time"], low_memory=False)
            except (OSError, ValueError, pd.errors.ParserError):
                continue
            parsed = parser(values["_time"]).dropna()
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
            errors.append(f"No {self.display_name} CSV file was selected.")
        if len(candidate.paths) > 1:
            errors.append(
                f"Multiple {self.display_name} candidates require user confirmation."
            )
            warnings.extend(str(path) for path in candidate.paths)
        required = set(self.required)
        opposite_suffix = "X5" if self.instrument_id == "opc_hbx4" else "X4"
        for path in candidate.paths:
            columns = _columns(path)
            missing = sorted(required - columns)
            if missing:
                errors.append(f"{path.name}: missing columns: {', '.join(missing)}")
            if any(f"_{opposite_suffix}_" in value for value in columns):
                warnings.append(
                    f"{path.name}: also contains the other OPC schema; "
                    "instrument assignment must be confirmed."
                )
        time_range = self.extract_time_range(candidate)
        if candidate.paths and time_range.original_start is None:
            errors.append("No valid recorded OPC timestamps were found.")
        status = (
            DetectionStatus.FAILED if errors else
            DetectionStatus.WARNING if warnings else DetectionStatus.READY
        )
        return InstrumentResult(
            instrument_id=self.instrument_id,
            display_name=self.display_name,
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
            metadata=dict(self.inspect_metadata(candidate)),
        )

    def load(self, candidate: InputCandidate) -> LoadedOpc:
        validation = self.validate(candidate)
        if validation.errors:
            raise ValueError("; ".join(validation.errors))
        self._check_cancelled()
        path = candidate.paths[0]
        self._emit(10, f"Inspecting {self.display_name} source CSV")
        try:
            row_count = sum(1 for _ in path.open(
                "r", encoding="utf-8-sig", errors="replace"
            )) - 1
        except OSError as exc:
            self._log_exception(f"{self.display_name} loading failed", exc)
            raise
        self._emit(20, f"{self.display_name} source assigned")
        return LoadedOpc(
            candidate, path, tuple(sorted(_columns(path))), max(0, row_count)
        )

    def process_quicklook(
        self, loaded: LoadedOpc, options: Mapping[str, Any]
    ) -> InstrumentResult:
        started = time.monotonic()
        self._check_cancelled()
        gap_seconds = float(options.get("gap_seconds", 10.0))
        if gap_seconds <= 0:
            raise ValueError("OPC gap_seconds must be positive")
        bin_units = str(options.get("bin_units", "auto"))
        if bin_units not in {"auto", "number_cm3", "counts_per_period"}:
            raise ValueError("Invalid OPC bin_units selection")
        self._emit(25, f"Sorting and validating {self.display_name} source records")
        try:
            chronological_source = self._prepare_chronological_source(
                loaded.source_path
            )
            self._emit(34, f"Running unchanged {self.display_name} OPC evaluation")
            evaluated, metadata = self.bridge.load_sensor(
                chronological_source,
                self.instrument_id,
                gap_seconds,
                bin_units,
            )
        except Exception as exc:
            self._log_exception(f"{self.display_name} processing failed", exc)
            raise
        self._check_cancelled()
        filtered, filter_warning = _filter_recorded_time(
            evaluated,
            options.get("analysis_start"),
            options.get("analysis_end"),
        )
        self._evaluated = filtered
        self._science_metadata = dict(metadata)

        warnings = []

        invalid_rows = int(self._source_integrity.get("invalid_timestamp_rows", 0))
        reordered = int(self._source_integrity.get("out_of_order_transitions", 0))
        if invalid_rows:
            warnings.append(
                f"{invalid_rows} source row(s) without a valid timestamp were "
                "quarantined for audit and excluded from chronological evaluation."
            )
        if reordered:
            warnings.append(
                f"{reordered} out-of-order timestamp transition(s) were corrected "
                "with a stable chronological sort."
            )
        if filter_warning:
            warnings.append(filter_warning)
        if metadata.get("qc_flagged_fraction", 0) > 0:
            warnings.append(
                f"{metadata['qc_flagged_fraction']:.1%} of rows have QC flags; "
                "the validated routine retains them."
            )
        interval = metadata.get("median_interval_seconds")
        result = InstrumentResult(
            instrument_id=self.instrument_id,
            display_name=self.display_name,
            physical_group="HATCHBOX",
            detection_status=DetectionStatus.WARNING,
            processing_status=(
                ProcessingStatus.WARNING if warnings else ProcessingStatus.COMPLETE
            ),
            source_files=_sources(loaded.candidate.paths),
            file_count=len(loaded.candidate.paths),
            original_start_time=filtered["recorded_time"].min().to_pydatetime(),
            original_end_time=filtered["recorded_time"].max().to_pydatetime(),
            sampling_frequency=(
                SamplingFrequency(
                    1.0 / float(interval),
                    method="legacy median positive recorded-time interval",
                )
                if interval is not None and interval > 0 else None
            ),
            completeness_percentage=100.0 * (
                1.0 - float(metadata.get("qc_flagged_fraction", 0.0))
            ),
            warnings=warnings,
            progress=100.0,
            metadata={
                "sensor": self.spec.name,
                "source_rows": loaded.raw_row_count,
                "selected_rows": int(len(filtered)),
                "source_integrity": dict(self._source_integrity),
                "legacy_science": metadata,
                "scientific_source": str(self.bridge.source_path),
            },
            elapsed_time=timedelta(seconds=time.monotonic() - started),
        )
        result.output_files.extend(self._source_artifacts)
        self._emit(100, f"{self.display_name} quicklook complete")
        self._log(LogLevel.SUCCESS, f"{self.display_name} quicklook completed")
        return result

    def process_detailed(
        self, loaded: LoadedOpc, options: Mapping[str, Any]
    ) -> InstrumentResult:
        raise NotImplementedError(
            f"No separate validated detailed {self.display_name} algorithm exists"
        )

    def create_plots(
        self, result: InstrumentResult, output_directory: Path
    ) -> Sequence[FigureArtifact]:
        self._assert_output_path(output_directory)
        if self._evaluated is None or self._science_metadata is None:
            raise RuntimeError("OPC quicklook must complete before plot creation")
        path = output_directory / f"{self.instrument_id}_quicklook.png"
        if path.exists():
            raise FileExistsError(f"OPC plot exists and was not overwritten: {path}")
        _single_sensor_plot(
            self.bridge.module,
            self._evaluated,
            self.spec,
            self._science_metadata,
            path,
        )
        artifact = FigureArtifact(path, f"{self.display_name} quicklook")
        result.figures.append(artifact)
        return (artifact,)

    def export_results(
        self,
        result: InstrumentResult,
        output_directory: Path,
        formats: Sequence[str],
    ) -> Sequence[OutputFile]:
        self._assert_output_path(output_directory)
        if self._evaluated is None or self._science_metadata is None:
            raise RuntimeError("OPC quicklook must complete before export")
        supported = {"csv", "json"}
        invalid = {value.casefold() for value in formats} - supported
        if invalid:
            raise ValueError("OPC exports support only CSV and JSON")
        outputs = []
        if "csv" in {value.casefold() for value in formats}:
            path = output_directory / f"{self.instrument_id}_evaluated.csv"
            _protect(path)
            self._evaluated.to_csv(
                path, index=False, date_format="%Y-%m-%d %H:%M:%S.%f"
            )
            outputs.append(OutputFile(path, "evaluated_data", size_bytes=path.stat().st_size))
        if "json" in {value.casefold() for value in formats}:
            path = output_directory / f"{self.instrument_id}_summary.json"
            _protect(path)
            path.write_text(
                json.dumps(self._science_metadata, indent=2, allow_nan=False),
                encoding="utf-8",
            )
            outputs.append(OutputFile(path, "summary", size_bytes=path.stat().st_size))
        result.output_files.extend(outputs)
        return tuple(outputs)

    def export_browser_data(
        self, result: InstrumentResult, output_directory: Path
    ) -> OutputFile:
        """Persist compact Plotly-ready data as part of the immutable run."""
        self._assert_output_path(output_directory)
        if self._evaluated is None or self._science_metadata is None:
            raise RuntimeError("OPC quicklook must complete before browser export")
        path = output_directory / f"{self.instrument_id}_browser.json"
        _protect(path)
        write_json_atomic(
            path,
            opc_sensor_payload(
                self._evaluated,
                self.spec,
                {
                    **self._science_metadata,
                    "source_integrity": dict(self._source_integrity),
                },
                flight_id=self.flight_name,
                instrument_id=self.instrument_id,
            ),
        )
        output = OutputFile(path, "browser_payload", size_bytes=path.stat().st_size)
        result.output_files.append(output)
        return output

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
                f"{self.display_name} output must remain below selected output folder: {root}"
            ) from exc
        target.mkdir(parents=True, exist_ok=True)

    def _prepare_chronological_source(self, source_path: Path) -> Path:
        """Create an auditable, stable-sorted copy without touching raw data."""
        self.output_root.mkdir(parents=True, exist_ok=True)
        raw = pd.read_csv(source_path, low_memory=False)
        if "_time" not in raw:
            raise ValueError(f"{source_path.name}: _time column is missing")
        parsed = self.bridge.module.parse_recorded_time(raw["_time"])
        invalid = parsed.isna()
        valid_times = parsed.loc[~invalid]
        transitions = int((valid_times.diff().dt.total_seconds() < 0).sum())
        chronological = (
            raw.loc[~invalid]
            .assign(__recorded_time_sort=valid_times)
            .sort_values("__recorded_time_sort", kind="stable")
            .drop(columns="__recorded_time_sort")
        )
        sorted_path = self.output_root / f"{self.instrument_id}_chronological_source.csv"
        chronological.to_csv(sorted_path, index=False)
        artifacts = [
            OutputFile(sorted_path, "chronological_source", size_bytes=sorted_path.stat().st_size)
        ]
        rejected_path = None
        if invalid.any():
            rejected = raw.loc[invalid].copy()
            rejected.insert(0, "_source_row", rejected.index + 2)
            rejected.insert(1, "_rejection_reason", "invalid recorded timestamp")
            rejected_path = self.output_root / f"{self.instrument_id}_quarantined_rows.csv"
            rejected.to_csv(rejected_path, index=False)
            artifacts.append(
                OutputFile(rejected_path, "quarantined_source_rows", size_bytes=rejected_path.stat().st_size)
            )
        self._source_artifacts = artifacts
        self._source_integrity = {
            "source_file": str(source_path),
            "source_rows": int(len(raw)),
            "valid_timestamp_rows": int((~invalid).sum()),
            "invalid_timestamp_rows": int(invalid.sum()),
            "out_of_order_transitions": transitions,
            "chronological_copy": str(sorted_path),
            "quarantined_rows": str(rejected_path) if rejected_path else None,
            "raw_source_modified": False,
        }
        self._log(
            LogLevel.WARNING if invalid.any() or transitions else LogLevel.INFO,
            (
                f"{self.display_name} chronology prepared: {len(raw):,} source rows, "
                f"{int(invalid.sum())} quarantined, {transitions} out-of-order transitions"
            ),
        )
        return sorted_path

    def _emit(self, progress: float, phase: str) -> None:
        self._check_cancelled()
        if self._progress_callback:
            self._progress_callback(
                ProgressUpdate(self.instrument_id, progress, phase, phase)
            )

    def _check_cancelled(self) -> None:
        if self._cancelled.is_set():
            raise RuntimeError(f"{self.display_name} processing was cancelled")

    def _log(self, level: LogLevel, message: str) -> None:
        if self.logger:
            self.logger.log(
                level, "opc-adapter", message, instrument=self.instrument_id
            )

    def _log_exception(self, message: str, exception: BaseException) -> None:
        if self.logger:
            self.logger.capture_exception(
                "opc-adapter", message, exception, instrument=self.instrument_id
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


def _filter_recorded_time(data, start: Any, end: Any):
    if start is None and end is None:
        return data.copy(), None
    if start is None or end is None:
        raise ValueError("Both OPC analysis start and end are required")
    parsed = []
    aware = False
    for value in (start, end):
        item = value if isinstance(value, datetime) else datetime.fromisoformat(
            str(value).replace("Z", "+00:00")
        )
        aware = aware or item.tzinfo is not None
        parsed.append(pd.Timestamp(item.replace(tzinfo=None)))
    if parsed[0] >= parsed[1]:
        raise ValueError("OPC analysis start must be earlier than end")
    selected = data.loc[
        data["recorded_time"].between(parsed[0], parsed[1], inclusive="both")
    ].copy()
    if selected.empty:
        raise ValueError("Selected interval does not intersect OPC data")

    warning = None  # Campaign instrument clocks are authoritative UTC.

    return selected, warning


def _single_sensor_plot(module, data, spec, metadata: dict, path: Path) -> None:
    module.plt.style.use("seaborn-v0_8-whitegrid")
    # seaborn-v0_8-whitegrid carries its own font scale, so the campaign sizes
    # go on after it or the tick labels land back under the floor.
    module.plt.rcParams.update(figure_standard.rc_parameters())
    fig = module.plt.figure(
        figsize=(figure_standard.PAGE_WIDTH_INCHES, 8.6), constrained_layout=True
    )
    # A narrow second column, empty except where the heat map's colour bar goes,
    # so all four panels are the same width and can be read against each other.
    grid = fig.add_gridspec(4, 2, width_ratios=(1.0, 0.035))
    axes = [fig.add_subplot(grid[row, 0]) for row in range(4)]
    for label, column, color in (
        ("PM1", spec.pm1, "#0072B2"),
        ("PM2.5", spec.pm25, "#D55E00"),
        ("PM10", spec.pm10, "#009E73"),
    ):
        module.plot_by_session(
            axes[0], data, column, color=color, lw=0.8, label=label
        )
    axes[0].set_yscale("symlog", linthresh=0.01)
    axes[0].set_title(f"{spec.label}: mass concentration")
    axes[0].set_ylabel("PM (µg m$^{-3}$)")
    module.legend_headroom(axes[0])
    axes[0].legend(ncol=3, loc="upper left", handlelength=1.0,
                   columnspacing=0.8, borderpad=0.3, framealpha=0.85)
    module.plot_by_session(
        axes[1], data, "total_number_cm3",
        color="#0072B2", lw=0.8, label="Total, 24 bins",
    )
    axes[1].set_yscale("symlog", linthresh=0.01)
    axes[1].set_title(f"{spec.label}: number concentration")
    axes[1].set_ylabel("N (cm$^{-3}$)")
    module.legend_headroom(axes[1])
    axes[1].legend(loc="upper left", handlelength=1.0, borderpad=0.3,
                   framealpha=0.85)
    low, high = module.concentration_limits([data])
    mesh = module.bin_heatmap(
        fig, axes[2], data, f"{spec.label}: bin-resolved",
        mcolors.LogNorm(vmin=low, vmax=high),
    )
    if mesh is not None:
        bar = fig.colorbar(mesh, cax=fig.add_subplot(grid[2, 1]))
        bar.set_label("N (cm$^{-3}$)")
    module.diagnostics_panel(axes[3], data, spec, metadata)
    # Only the bottom panel names the axis; four repetitions of it would cost a
    # panel's worth of height to say the same thing four times.
    for index, axis in enumerate(axes):
        module.time_axis(axis, label=index == len(axes) - 1)
    start, end = data["recorded_time"].iloc[[0, -1]]
    fig.suptitle(
        f"{spec.label} quicklook\n"
        f"{start.isoformat(sep=' ', timespec='seconds')} to "
        f"{end.isoformat(sep=' ', timespec='seconds')}\n"
        "Times as recorded, timezone removed without conversion · "
        "gaps preserved, no interpolation"
    )
    figure_standard.save(fig, path)
    plt.close(fig)


def _protect(path: Path) -> None:
    if path.exists():
        raise FileExistsError(f"OPC output exists and was not overwritten: {path}")
