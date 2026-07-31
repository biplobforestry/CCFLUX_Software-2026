"""Adapter for the validated Gremsy/INS Gimbal full-flight quicklook."""

from __future__ import annotations

import csv
import json
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence

import pandas as pd

from core.detector import InputCandidate
from core.enums import DetectionStatus, ProcessingStatus
from core.logging_manager import LogLevel, ProcessingLogManager
from core.models import (
    FigureArtifact, InstrumentDescriptor, InstrumentResult, OutputFile,
    ProgressUpdate, SamplingFrequency, SourceFile,
)
from core.scanner import ScanIndex
from core.time_manager import TimeRange, TimezoneState
from instruments.base.interface import InstrumentBase, ProgressCallback
from instruments.hatchbox_payload import ins_gimbal_payload, write_json_atomic

from .legacy_bridge import LegacyInsGimbalBridge

REQUIRED = (
    "_time",
    "gimbal_acc_x_counts", "gimbal_acc_y_counts", "gimbal_acc_z_counts",
    "gimbal_gyro_x_counts", "gimbal_gyro_y_counts", "gimbal_gyro_z_counts",
)


@dataclass(slots=True)
class LoadedInsGimbal:
    candidate: InputCandidate
    source_path: Path


class InsGimbalAdapter(InstrumentBase):
    descriptor = InstrumentDescriptor(
        "ins_gimbal", "INS Gimbal", "HATCHBOX",
        frozenset({"detection", "quicklook", "plots", "export"}), True,
    )

    def __init__(self, *, output_root: Path, flight_name: str,
                 bridge=None, logger: ProcessingLogManager | None = None):
        self.output_root, self.flight_name = Path(output_root), flight_name
        self.bridge = bridge or LegacyInsGimbalBridge()
        self.logger = logger
        self._callback: ProgressCallback | None = None
        self._cancelled = threading.Event()
        self._data = self._sessions = None
        self._summary = None
        self._spectra = None

    def detect(self, scan_index: ScanIndex) -> Sequence[InputCandidate]:
        paths = [
            entry.path for entry in scan_index.entries
            if entry.is_file and entry.path.suffix.casefold() == ".csv"
            and set(REQUIRED).issubset(_columns(entry.path))
        ]
        return (InputCandidate(
            "ins_gimbal", tuple(paths), 1.0,
            "Complete Gremsy RAW_IMU acceleration and gyroscope schema",
        ),) if paths else ()

    def inspect_metadata(self, candidate):
        return {
            "file_count": len(candidate.paths),
            "required_columns": list(REQUIRED),
            "columns": [sorted(_columns(path)) for path in candidate.paths],
            "scientific_identity": "Gremsy gimbal RAW_IMU",
        }

    def extract_time_range(self, candidate):
        starts, ends = [], []
        for path in candidate.paths:
            try:
                values = pd.read_csv(path, usecols=["_time"])["_time"]
                parsed = self.bridge.module.recorded_time(values).dropna()
            except (OSError, ValueError, KeyError, pd.errors.ParserError):
                continue
            if not parsed.empty:
                starts.append(parsed.min().to_pydatetime())
                ends.append(parsed.max().to_pydatetime())
        return TimeRange(
            min(starts) if starts else None, max(ends) if ends else None,
            _campaign_utc(min(starts) if starts else None),
            _campaign_utc(max(ends) if ends else None),
            TimezoneState.EXPLICIT_UTC, "source-dependent",
            "_time; UTC by campaign convention",
        )

    def validate(self, candidate):
        errors = []
        if len(candidate.paths) != 1:
            errors.append("INS Gimbal requires exactly one confirmed source CSV.")
        for path in candidate.paths:
            missing = sorted(set(REQUIRED) - _columns(path))
            if missing:
                errors.append(f"{path.name}: missing columns: {', '.join(missing)}")
        coverage = self.extract_time_range(candidate)
        if candidate.paths and coverage.original_start is None:
            errors.append("No valid INS Gimbal timestamps were found.")
        warnings = []
        return InstrumentResult(
            "ins_gimbal", "INS Gimbal", "HATCHBOX",
            DetectionStatus.FAILED if errors else DetectionStatus.READY,
            source_files=_sources(candidate.paths), file_count=len(candidate.paths),
            original_start_time=coverage.original_start,
            original_end_time=coverage.original_end,
            utc_start_time=coverage.utc_start,
            utc_end_time=coverage.utc_end,
            warnings=warnings, errors=errors,
            metadata=dict(self.inspect_metadata(candidate)),
        )

    def load(self, candidate):
        result = self.validate(candidate)
        if result.errors:
            raise ValueError("; ".join(result.errors))
        self._check()
        self._emit(10, "INS Gimbal source assigned")
        return LoadedInsGimbal(candidate, candidate.paths[0])

    def process_quicklook(self, loaded, options: Mapping[str, Any]):
        started, module = time.monotonic(), self.bridge.module
        # Pandas can expose the boolean transition array as read-only. Preserve the
        # validated legacy calculation while ensuring only the temporary mask is writable.
        module.no_gap_bridge = _no_gap_bridge_writable
        gap = float(options.get("gap_seconds", 10.0))
        rms_seconds = float(options.get("rms_seconds", 30.0))
        threshold = float(options.get("maneuver_threshold_dps", 10.0))
        if gap <= 0 or rms_seconds <= 0:
            raise ValueError("INS Gimbal gap and RMS durations must be positive")
        self._emit(20, "Loading unchanged Gremsy RAW_IMU conversion")
        try:
            data = module.load_csv(loaded.source_path, gap)
            data = _time_filter(data, options.get("analysis_start"), options.get("analysis_end"))
            sessions = module.describe_sessions(data)
            self._emit(45, "Calculating unchanged rolling diagnostics")
            data = module.rolling_statistics(data, sessions, rms_seconds, threshold)
            acc_asd = module.full_flight_asd(data, sessions, "acc_deviation_g")
            gyro_asd = module.full_flight_asd(data, sessions, "gyro_norm_dps")
            spectrogram = module.full_flight_spectrogram(
                data, sessions, "acc_deviation_g"
            )
            logger_rate = float(sessions["logger_rate_hz"].median())
            update_rate = float(sessions["imu_update_rate_hz"].median())
            elapsed = (
                data["recorded_time"].iloc[-1] - data["recorded_time"].iloc[0]
            ).total_seconds() / 3600.0
            summary = {
                "input_csv": str(loaded.source_path.resolve()),
                "dataset": {
                    "start_recorded_time": data["recorded_time"].iloc[0].isoformat(sep=" "),
                    "end_recorded_time": data["recorded_time"].iloc[-1].isoformat(sep=" "),
                    "elapsed_hours": elapsed, "rows_in_csv": int(len(data)),
                    "rows_evaluated": int(len(data)), "sessions": int(len(sessions)),
                    "time_window_selection": "adapter-selected recorded-time interval",
                    "signal_filter": "none",
                },
                "sampling": {
                    "median_logger_rate_hz": module.finite(logger_rate),
                    "logger_nyquist_hz": module.finite(logger_rate / 2),
                    "median_imu_update_rate_hz": module.finite(update_rate),
                    "effective_update_nyquist_hz": module.finite(update_rate / 2),
                },
                "configuration": {
                    "gap_seconds": gap, "rms_seconds": rms_seconds,
                    "maneuver_threshold_dps": threshold,
                },
                "metrics": {
                    "unfiltered_acceleration_deviation_rms_g": module.rms(data["acc_deviation_g"]),
                    "unfiltered_acceleration_deviation_peak_abs_g": module.finite(data["acc_deviation_g"].abs().max()),
                    "unfiltered_angular_rate_rms_dps": module.rms(data["gyro_norm_dps"]),
                    "unfiltered_angular_rate_peak_dps": module.finite(data["gyro_norm_dps"].max()),
                    "dominant_acceleration_frequency_below_update_nyquist_hz": module.dominant(acc_asd, update_rate / 2),
                    "dominant_angular_rate_frequency_below_update_nyquist_hz": module.dominant(gyro_asd, update_rate / 2),
                    "maneuver_fraction": module.finite(data["maneuver_flag"].mean()),
                },
                "limitations": [
                    "Single-point IMU quicklook; it is not mount transmissibility.",
                    "Intentional gimbal motion can overlap measured vibration.",
                    "Frequencies above effective RAW_IMU update Nyquist are unresolved.",
                ],
            }
        except Exception as exc:
            if self.logger:
                self.logger.capture_exception(
                    "ins-gimbal-adapter", "INS Gimbal quicklook failed", exc,
                    instrument="ins_gimbal",
                )
            raise
        self._data, self._sessions = data, sessions
        self._summary, self._spectra = summary, (acc_asd, gyro_asd, spectrogram)
        self._emit(100, "INS Gimbal quicklook complete")
        if self.logger:
            self.logger.log(
                LogLevel.SUCCESS, "ins-gimbal-adapter",
                "INS Gimbal quicklook completed", instrument="ins_gimbal",
            )
        return InstrumentResult(
            "ins_gimbal", "INS Gimbal", "HATCHBOX", DetectionStatus.READY,
            ProcessingStatus.COMPLETE, _sources(loaded.candidate.paths), 1,
            data["recorded_time"].min().to_pydatetime(),
            data["recorded_time"].max().to_pydatetime(),
            sampling_frequency=SamplingFrequency(
                logger_rate, method="legacy median logger rate"
            ) if pd.notna(logger_rate) and logger_rate > 0 else None,
            warnings=[],
            progress=100.0, metadata={"summary": summary},
            elapsed_time=timedelta(seconds=time.monotonic() - started),
        )

    def process_detailed(self, loaded, options):
        raise NotImplementedError("No separate INS Gimbal detailed algorithm exists")

    def create_plots(self, result, output_directory):
        self._assert_output(output_directory)
        if self._summary is None:
            raise RuntimeError("INS Gimbal quicklook must complete first")
        png = output_directory / "ins_gimbal_quicklook.png"
        pdf = output_directory / "ins_gimbal_quicklook.pdf"
        _protect(png); _protect(pdf)
        self.bridge.module.make_plot(
            self._data, self._summary, self._spectra[0], self._spectra[1],
            self._spectra[2], png, pdf,
        )
        figures = (FigureArtifact(png, "INS Gimbal quicklook PNG"),
                   FigureArtifact(pdf, "INS Gimbal quicklook PDF"))
        result.figures.extend(figures)
        return figures

    def export_results(self, result, output_directory, formats):
        self._assert_output(output_directory)
        if self._summary is None:
            raise RuntimeError("INS Gimbal quicklook must complete first")
        selected, outputs = {value.casefold() for value in formats}, []
        if selected - {"csv", "json", "md"}:
            raise ValueError("INS Gimbal exports support CSV, JSON, and MD")
        if "csv" in selected:
            for name, table, role in (
                ("sessions.csv", self._sessions, "sessions"),
                ("evaluated_full_flight.csv", self._data, "evaluated_data"),
            ):
                path = output_directory / name; _protect(path)
                table.to_csv(path, index=False)
                outputs.append(OutputFile(path, role, size_bytes=path.stat().st_size))
        if "json" in selected:
            path = output_directory / "summary.json"; _protect(path)
            path.write_text(json.dumps(self._summary, indent=2, allow_nan=False))
            outputs.append(OutputFile(path, "summary", size_bytes=path.stat().st_size))
        if "md" in selected:
            path = output_directory / "README.md"; _protect(path)
            self.bridge.module.write_method(path, self._summary)
            outputs.append(OutputFile(path, "methodology", size_bytes=path.stat().st_size))
        result.output_files.extend(outputs)
        return tuple(outputs)


    def export_browser_data(self, result, output_directory):
        """Persist precomputed Plotly-ready Gremsy products in the Flight Project."""
        self._assert_output(output_directory)
        if self._summary is None or self._data is None or self._spectra is None:
            raise RuntimeError("INS Gimbal quicklook must complete before browser export")
        path = output_directory / "ins_gimbal_browser.json"
        _protect(path)
        write_json_atomic(
            path,
            ins_gimbal_payload(
                self._data,
                self._sessions,
                self._summary,
                self._spectra,
                flight_id=self.flight_name,
            ),
        )
        output = OutputFile(path, "browser_payload", size_bytes=path.stat().st_size)
        result.output_files.append(output)
        return output

    def cancel(self): self._cancelled.set()
    def report_progress(self, callback): self._callback = callback
    def _emit(self, value, phase):
        self._check()
        if self._callback:
            self._callback(ProgressUpdate("ins_gimbal", value, phase, phase))
    def _check(self):
        if self._cancelled.is_set():
            raise RuntimeError("INS Gimbal processing was cancelled")
    def _assert_output(self, path):
        root, target = self.output_root.resolve(strict=False), Path(path).resolve(strict=False)
        try: target.relative_to(root)
        except ValueError as exc:
            raise ValueError(f"INS Gimbal output must remain below {root}") from exc
        target.mkdir(parents=True, exist_ok=True)


def _no_gap_bridge_writable(frame: pd.DataFrame, column: str):
    values = frame[column].to_numpy(dtype=float, copy=True)
    starts = frame["session_id"].ne(frame["session_id"].shift()).to_numpy(copy=True)
    if len(starts):
        starts[0] = False
    values[starts] = float("nan")
    return values


def _campaign_utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return (
        value.replace(tzinfo=timezone.utc)
        if value.tzinfo is None
        else value.astimezone(timezone.utc)
    )


def _columns(path):
    with Path(path).open("r", encoding="utf-8-sig", errors="replace", newline="") as stream:
        return {value.strip() for value in next(csv.reader(stream), ())}

def _sources(paths):
    return [SourceFile(path, size_bytes=path.stat().st_size) for path in paths]

def _time_filter(data, start, end):
    if start is None and end is None: return data
    if start is None or end is None: raise ValueError("Both INS Gimbal bounds are required")
    bounds = []
    for value in (start, end):
        stamp = pd.Timestamp(value)
        if stamp.tzinfo is not None: stamp = stamp.tz_localize(None)
        bounds.append(stamp)
    selected = data[data["recorded_time"].between(*bounds)].copy()
    if selected.empty: raise ValueError("Selected interval does not intersect INS Gimbal data")
    return selected

def _protect(path):
    if path.exists(): raise FileExistsError(f"Output exists and was not overwritten: {path}")
