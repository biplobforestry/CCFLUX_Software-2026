"""Adapter around the unchanged legacy Noseboom implementation."""

from __future__ import annotations

import csv
import math
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence

from core.detector import InputCandidate
from core.enums import DetectionStatus, ProcessingStatus
from core.logging_manager import LogLevel, ProcessingLogManager
from core.noseboom_columns import normalize_columns
from core.models import (
    FigureArtifact,
    InstrumentDescriptor,
    InstrumentResult,
    OutputFile,
    ProgressUpdate,
    SourceFile,
)
from core.scanner import ScanIndex
from core.time_extraction import TimestampExtractor
from core.time_manager import TimeRange, TimezoneState
from instruments.base.interface import InstrumentBase, ProgressCallback

from .legacy_bridge import LegacyNoseboomBridge

TIME_COLUMN = "Airflow_UTCcorr_Nanoseconds_ns"
INS_GPS = (
    "INS_Filter_LLHPos_Latitude_deg",
    "INS_Filter_LLHPos_Longitude_deg",
)
GNSS_GPS = (
    "GNSSRecv1_LLHPos_Latitude_deg",
    "GNSSRecv1_LLHPos_Longitude_deg",
)
WIND_COLUMNS = (
    "WIND_vWind_x_m/s",
    "WIND_vWind_y_m/s",
    "WIND_vWind_z_m/s",
)


@dataclass(slots=True)
class LoadedNoseboom:
    candidate: InputCandidate
    data: Any
    selected_start: datetime | None = None
    selected_end: datetime | None = None


class NoseboomAdapter(InstrumentBase):
    descriptor = InstrumentDescriptor(
        instrument_id="noseboom",
        display_name="Noseboom",
        physical_group="NOSEBOOM",
        capabilities=frozenset(
            {"detection", "gps", "quicklook", "detailed", "export"}
        ),
        integrated=True,
    )

    def __init__(
        self,
        *,
        output_root: Path,
        flight_name: str,
        bridge: LegacyNoseboomBridge | None = None,
        logger: ProcessingLogManager | None = None,
    ) -> None:
        self.output_root = Path(output_root)
        self.flight_name = flight_name
        self.bridge = bridge or LegacyNoseboomBridge()
        self.logger = logger
        self._progress_callback: ProgressCallback | None = None
        self._cancelled = threading.Event()
        self._last_export_source = None
        self._last_result: InstrumentResult | None = None

    def detect(self, scan_index: ScanIndex) -> Sequence[InputCandidate]:
        paths = []
        for entry in scan_index.entries:
            if not entry.is_file or entry.path.suffix.casefold() != ".csv":
                continue
            columns = _columns(entry.path)
            if TIME_COLUMN in columns and (
                all(value in columns for value in INS_GPS)
                or all(value in columns for value in GNSS_GPS)
            ):
                paths.append(entry.path)
        return (
            InputCandidate(
                "noseboom",
                tuple(paths),
                confidence=1.0,
                reason="Authoritative epoch-nanosecond time and GPS columns",
            ),
        ) if paths else ()

    def inspect_metadata(self, candidate: InputCandidate) -> Mapping[str, Any]:
        column_sets = [_columns(path) for path in candidate.paths]
        all_columns = set().union(*column_sets) if column_sets else set()
        return {
            "file_count": len(candidate.paths),
            "columns": sorted(all_columns),
            "has_ins_gps": all(value in all_columns for value in INS_GPS),
            "has_gnss_gps": all(value in all_columns for value in GNSS_GPS),
            "has_wind_components": all(
                value in all_columns for value in WIND_COLUMNS
            ),
            "authoritative_timestamp_column": TIME_COLUMN,
        }

    def extract_time_range(self, candidate: InputCandidate) -> TimeRange:
        result = TimestampExtractor().extract_instrument(
            "noseboom", candidate.paths
        )
        return TimeRange(
            original_start=result.utc_start_time,
            original_end=result.utc_end_time,
            utc_start=result.utc_start_time,
            utc_end=result.utc_end_time,
            timezone_state=TimezoneState.EXPLICIT_UTC,
            precision="nanoseconds",
            basis=TIME_COLUMN,
        )

    def validate(self, candidate: InputCandidate) -> InstrumentResult:
        warnings: list[str] = []
        errors: list[str] = []
        metadata = self.inspect_metadata(candidate)
        if not candidate.paths:
            errors.append("No Noseboom CSV files were selected.")
        for path in candidate.paths:
            columns = _columns(path)
            if TIME_COLUMN not in columns:
                errors.append(f"{path.name}: missing required {TIME_COLUMN}")
            if not (
                all(value in columns for value in INS_GPS)
                or all(value in columns for value in GNSS_GPS)
            ):
                errors.append(f"{path.name}: no complete INS or GNSS GPS pair")
            missing_wind = [value for value in WIND_COLUMNS if value not in columns]
            if missing_wind:
                warnings.append(
                    f"{path.name}: missing wind components: "
                    + ", ".join(missing_wind)
                )
        time_range = self.extract_time_range(candidate) if candidate.paths else TimeRange()
        status = (
            DetectionStatus.FAILED
            if errors
            else DetectionStatus.WARNING
            if warnings
            else DetectionStatus.READY
        )
        result = InstrumentResult(
            instrument_id="noseboom",
            display_name="Noseboom",
            physical_group="NOSEBOOM",
            detection_status=status,
            source_files=[
                SourceFile(
                    path=path,
                    size_bytes=path.stat().st_size if path.exists() else None,
                )
                for path in candidate.paths
            ],
            file_count=len(candidate.paths),
            original_start_time=time_range.original_start,
            original_end_time=time_range.original_end,
            utc_start_time=time_range.utc_start,
            utc_end_time=time_range.utc_end,
            warnings=warnings,
            errors=errors,
            metadata=dict(metadata),
        )
        return result

    def load(self, candidate: InputCandidate) -> LoadedNoseboom:
        validation = self.validate(candidate)
        if validation.errors:
            raise ValueError("; ".join(validation.errors))
        self._check_cancelled()
        self._emit(3, "Loading validated Noseboom CSV files")
        try:
            data = self.bridge.load_csv_files(
                candidate.paths,
                lambda percent, step: self._emit(min(45, 3 + percent * 0.42), step),
            )
        except Exception as exc:
            self._log_exception("Noseboom loading failed", exc)
            raise
        self._emit(45, "Noseboom data loaded")
        return LoadedNoseboom(candidate, data)

    def load_time_window(
        self,
        candidate: InputCandidate,
        start: datetime,
        end: datetime,
    ) -> LoadedNoseboom:
        """Stream a selected interval instead of retaining the full CSV."""
        validation = self.validate(candidate)
        if validation.errors:
            raise ValueError("; ".join(validation.errors))
        if start.tzinfo is None or end.tzinfo is None or start >= end:
            raise ValueError("A valid timezone-aware Noseboom interval is required")
        start_utc, end_utc = start.astimezone(timezone.utc), end.astimezone(timezone.utc)
        data = self.bridge.load_csv_window(
            candidate.paths,
            int(start_utc.timestamp() * 1_000_000_000),
            int(end_utc.timestamp() * 1_000_000_000),
            lambda percent, step: self._emit(percent, step),
            self._cancelled.is_set,
        )
        self._emit(45, "Selected Noseboom interval loaded")
        return LoadedNoseboom(candidate, data, start_utc, end_utc)

    def process_quicklook(
        self, loaded: LoadedNoseboom, options: Mapping[str, Any]
    ) -> InstrumentResult:
        started = time.monotonic()
        try:
            filtered = self._apply_time_filter(loaded.data, options)
            self._check_cancelled()
            self._emit(50, "Running unchanged legacy Noseboom quicklook routines")
            products = self.bridge.quicklook(
                filtered,
                float(options.get("trim_minutes", 2.0)),
                options.get("straight_settings"),
                include_terrain=bool(options.get("terrain", False)),
            )
            self._last_export_source = products[4]
            self._emit(90, "Noseboom quicklook complete")
            result = self._result_from_data(
                loaded.candidate,
                filtered,
                ProcessingStatus.COMPLETE,
                timedelta(seconds=time.monotonic() - started),
                {
                    "raw_rows": int(len(filtered)),
                    "one_hz_rows": int(len(products[0])),
                    "straight_rows": int(products[1]["straight"].sum()),
                    "spectrum_variables": sorted(products[3]),
                    "gps": self.inspect_metadata(loaded.candidate),
                    "map": _map_payload(products[1], products[2], products[3]),
                },
            )
            self._last_result = result
            self._log(LogLevel.SUCCESS, "Noseboom quicklook completed")
            return result
        except Exception as exc:
            self._log_exception("Noseboom quicklook failed", exc)
            raise

    def process_detailed(
        self, loaded: LoadedNoseboom, options: Mapping[str, Any]
    ) -> InstrumentResult:
        started = time.monotonic()
        filtered = self._apply_time_filter(loaded.data, options)
        self._assert_output_path(self.output_root)
        try:
            products = self.bridge.detailed(
                filtered,
                self.output_root,
                self.flight_name,
                float(options.get("trim_minutes", 2.0)),
            )
            self._last_export_source = products[5]
            result = self._result_from_data(
                loaded.candidate,
                filtered,
                ProcessingStatus.COMPLETE,
                timedelta(seconds=time.monotonic() - started),
                products[4],
            )
            self._last_result = result
            return result
        except Exception as exc:
            self._log_exception("Detailed Noseboom processing failed", exc)
            raise

    def create_plots(
        self, result: InstrumentResult, output_directory: Path
    ) -> Sequence[FigureArtifact]:
        self._assert_output_path(output_directory)
        result.warnings.append(
            "Legacy Noseboom plots are browser-coupled; no plot files were "
            "invented by the adapter."
        )
        return ()

    def export_results(
        self,
        result: InstrumentResult,
        output_directory: Path,
        formats: Sequence[str],
    ) -> Sequence[OutputFile]:
        self._assert_output_path(output_directory)
        if self._last_export_source is None:
            raise RuntimeError("Noseboom processing must complete before export")
        outputs = []
        frequency = float(result.metadata.get("export_frequency_hz", 1.0))
        for format_name in formats:
            self._check_cancelled()
            suffix = "h5" if format_name.casefold() == "hdf" else format_name.casefold()
            frequency_label = (f"{frequency:g}Hz").replace(".", "p")
            safe_flight = "".join(
                value if value.isalnum() or value in "-_" else "_"
                for value in (self.flight_name.strip() or "Flight")
            )
            expected = (
                output_directory
                / safe_flight
                / "exports"
                / f"{safe_flight}_noseboom_export_{frequency_label}.{suffix}"
            )
            if expected.exists():
                raise FileExistsError(
                    f"Noseboom export already exists and was not overwritten: {expected}"
                )
            path, _ = self.bridge.export(
                output_directory,
                self.flight_name,
                self._last_export_source,
                frequency,
                format_name,
            )
            outputs.append(
                OutputFile(
                    path=Path(path),
                    role="noseboom_export",
                    size_bytes=Path(path).stat().st_size,
                )
            )
        result.output_files.extend(outputs)
        self._emit(100, "Noseboom outputs exported")
        return tuple(outputs)

    def cancel(self) -> None:
        self._cancelled.set()

    def report_progress(self, callback: ProgressCallback | None) -> None:
        self._progress_callback = callback

    def _apply_time_filter(self, data, options: Mapping[str, Any]):
        start = _datetime_option(options.get("analysis_start"))
        end = _datetime_option(options.get("analysis_end"))
        if start is None and end is None:
            return data
        if start is None or end is None or start >= end:
            raise ValueError("A valid analysis start and end are required")
        nanoseconds = data["time_ns"]
        start_ns = int(start.timestamp() * 1_000_000_000)
        end_ns = int(end.timestamp() * 1_000_000_000)
        filtered = data[(nanoseconds >= start_ns) & (nanoseconds <= end_ns)].copy()
        if filtered.empty:
            raise ValueError("Selected interval does not intersect Noseboom data")
        return filtered

    def _result_from_data(
        self,
        candidate: InputCandidate,
        data,
        status: ProcessingStatus,
        elapsed: timedelta,
        metadata: Mapping[str, Any],
    ) -> InstrumentResult:
        start = datetime.fromtimestamp(
            int(data["time_ns"].min()) / 1_000_000_000, tz=timezone.utc
        )
        end = datetime.fromtimestamp(
            int(data["time_ns"].max()) / 1_000_000_000, tz=timezone.utc
        )
        return InstrumentResult(
            instrument_id="noseboom",
            display_name="Noseboom",
            physical_group="NOSEBOOM",
            detection_status=DetectionStatus.READY,
            processing_status=status,
            source_files=[
                SourceFile(path=path, size_bytes=path.stat().st_size)
                for path in candidate.paths
            ],
            file_count=len(candidate.paths),
            original_start_time=start,
            original_end_time=end,
            utc_start_time=start,
            utc_end_time=end,
            progress=100.0,
            metadata=dict(metadata),
            elapsed_time=elapsed,
        )

    def _assert_output_path(self, path: Path) -> None:
        root = self.output_root.resolve(strict=False)
        target = Path(path).resolve(strict=False)
        try:
            target.relative_to(root)
        except ValueError as exc:
            raise ValueError(
                f"Noseboom output must remain below selected output folder: {root}"
            ) from exc
        target.mkdir(parents=True, exist_ok=True)

    def _emit(self, progress: float, phase: str) -> None:
        self._check_cancelled()
        if self._progress_callback:
            self._progress_callback(
                ProgressUpdate("noseboom", progress, phase, phase)
            )

    def _check_cancelled(self) -> None:
        if self._cancelled.is_set():
            raise RuntimeError("Noseboom processing was cancelled")

    def _log(self, level: LogLevel, message: str) -> None:
        if self.logger:
            self.logger.log(
                level, "noseboom-adapter", message, instrument="noseboom"
            )

    def _log_exception(self, message: str, exception: BaseException) -> None:
        if self.logger:
            self.logger.capture_exception(
                "noseboom-adapter",
                message,
                exception,
                instrument="noseboom",
            )


def _columns(path: Path) -> set[str]:
    # Normalized here, so detection, metadata and validation all see the same
    # unprefixed names whichever way the logger wrote the file.
    with Path(path).open("r", encoding="utf-8-sig", errors="replace", newline="") as stream:
        header = [value.strip() for value in next(csv.reader(stream), [])]
    return set(normalize_columns(header, source=Path(path).name))


def _datetime_option(value: Any) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        parsed = value
    else:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("Noseboom analysis times must include a timezone")
    return parsed.astimezone(timezone.utc)


def _finite_or_none(value):
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if number == number and abs(number) != float("inf") else None


def _map_payload(one_hz, frequency=None, spectra=None) -> dict[str, object]:
    required = {"plot_lat", "plot_lon"}
    if not required.issubset(one_hz.columns):
        return {"available": False, "reason": "No valid GPS route columns"}
    browser_columns = (
        "plot_lat", "plot_lon", "wind_mps", "wind_u_mps", "wind_v_mps",
        "wind_w_mps", "wind_dir_deg", "heading_deg", "altitude_m", "height_m",
        "terrain_m", "air_temp_degC", "rel_humidity_pct", "ground_speed_mps",
        "roll_deg", "straight", "straight_leg_id",
    )
    columns = [value for value in browser_columns if value in one_hz.columns]
    route = one_hz[columns].dropna(subset=["plot_lat", "plot_lon"])
    if route.empty:
        return {"available": False, "reason": "No finite GPS route samples"}

    map_step = max(1, (len(route) + 5999) // 6000)
    points = []
    numeric_columns = [
        value
        for value in columns
        if value not in {"plot_lat", "plot_lon", "straight", "straight_leg_id"}
    ]
    for timestamp, row in route.iloc[::map_step].iterrows():
        leg_id = _finite_or_none(row.get("straight_leg_id", 0))
        point = {
            "time": (
                timestamp.isoformat()
                if hasattr(timestamp, "isoformat")
                else str(timestamp)
            ),
            "lat": float(row["plot_lat"]),
            "lon": float(row["plot_lon"]),
            "straight": bool(row.get("straight", False)),
            "straight_leg_id": int(leg_id or 0),
        }
        point.update({
            name: _finite_or_none(row.get(name))
            for name in numeric_columns
        })
        points.append(point)

    histogram_columns = (
        "wind_mps", "wind_u_mps", "wind_v_mps", "wind_w_mps",
        "air_temp_degC", "rel_humidity_pct",
    )
    histogram_step = max(1, (len(one_hz) + 29999) // 30000)
    hist = {
        name: [
            number
            for number in (
                _finite_or_none(value)
                for value in one_hz[name].iloc[::histogram_step]
            )
            if number is not None
        ]
        for name in histogram_columns
        if name in one_hz.columns
    }

    frequency_rows = []
    if frequency is not None and hasattr(frequency, "iterrows"):
        frequency_step = max(1, (len(frequency) + 29999) // 30000)
        for _, row in frequency.iloc[::frequency_step].iterrows():
            value = _finite_or_none(row.get("frequency_hz"))
            if value is not None:
                frequency_rows.append({
                    "time": str(row.get("time", "")),
                    "frequency_hz": value,
                    "frequency_min_hz": _finite_or_none(
                        row.get("frequency_min_hz")
                    ),
                    "frequency_max_hz": _finite_or_none(
                        row.get("frequency_max_hz")
                    ),
                    "sample_count": int(
                        _finite_or_none(row.get("sample_count", 0)) or 0
                    ),
                })

    safe_spectra = {}
    for name, product in (spectra or {}).items():
        safe_spectra[str(name)] = {
            key: value
            for key, value in product.items()
            if key not in {"frequency_hz", "psd"}
        }
        safe_spectra[str(name)]["frequency_hz"] = [
            number
            for number in (
                _finite_or_none(value)
                for value in product.get("frequency_hz", [])
            )
            if number is not None
        ]
        safe_spectra[str(name)]["psd"] = [
            number
            for number in (
                _finite_or_none(value)
                for value in product.get("psd", [])
            )
            if number is not None
        ]

    altitude_step = max(1, (len(one_hz) + 11999) // 12000)
    altitude_profile = []
    for timestamp, row in one_hz.iloc[::altitude_step].iterrows():
        altitude_profile.append({
            "time": (
                timestamp.isoformat()
                if hasattr(timestamp, "isoformat")
                else str(timestamp)
            ),
            "gnss_msl_m": _finite_or_none(row.get("altitude_m")),
            "ins_ellipsoid_m": _finite_or_none(row.get("height_m")),
            "dtm_m": _finite_or_none(row.get("terrain_m")),
        })

    metrics = {
        int(row.get("leg", -1)): row
        for row in one_hz.attrs.get("straight_metrics", [])
        if _finite_or_none(row.get("leg")) is not None
    }
    straight_legs = []
    if "straight_leg_id" in one_hz.columns:
        for leg_value, group in one_hz.groupby("straight_leg_id"):
            leg_number = int(_finite_or_none(leg_value) or 0)
            if leg_number <= 0 or group.empty:
                continue
            coordinate_step = max(1, (len(group) + 599) // 600)
            wind_step = max(1, (len(group) + 239) // 240)
            coordinates = [
                [float(row["plot_lat"]), float(row["plot_lon"])]
                for _, row in group.iloc[::coordinate_step].iterrows()
                if (
                    _finite_or_none(row.get("plot_lat")) is not None
                    and _finite_or_none(row.get("plot_lon")) is not None
                )
            ]
            wind_samples = []
            for _, row in group.iloc[::wind_step].iterrows():
                direction = _finite_or_none(row.get("wind_dir_deg"))
                if direction is None:
                    wind_u = _finite_or_none(row.get("wind_u_mps"))
                    wind_v = _finite_or_none(row.get("wind_v_mps"))
                    if wind_u is not None and wind_v is not None:
                        direction = (
                            math.degrees(math.atan2(wind_u, wind_v)) + 360
                        ) % 360
                speed = _finite_or_none(row.get("wind_mps"))
                if direction is not None and speed is not None:
                    wind_samples.append({"dir": direction, "spd": speed})
            if not coordinates:
                continue
            metric = metrics.get(leg_number, {})
            duration = None
            if len(group) > 1:
                try:
                    duration = float(
                        (group.index[-1] - group.index[0]).total_seconds()
                    )
                except (AttributeError, TypeError):
                    duration = float(len(group) - 1)
            straight_legs.append({
                "id": leg_number,
                "coords": coordinates,
                "label": coordinates[len(coordinates) // 2],
                "duration_s": duration,
                "distance_km": _finite_or_none(metric.get("distance_km")),
                "mean_speed_mps": _finite_or_none(
                    metric.get("mean_speed_mps")
                ),
                "mean_wind_mps": (
                    _finite_or_none(group["wind_mps"].mean())
                    if "wind_mps" in group
                    else None
                ),
                "mean_heading_deg": _finite_or_none(
                    metric.get("mean_heading_deg")
                ),
                "heading_std_deg": _finite_or_none(
                    metric.get("median_heading_std_deg")
                ),
                "max_roll_deg": _finite_or_none(
                    metric.get("max_abs_roll_deg")
                ),
                "altitude_range_m": _finite_or_none(
                    metric.get("altitude_range_m")
                ),
                "max_vertical_speed_mps": _finite_or_none(
                    metric.get("max_abs_vertical_speed_mps")
                ),
                "windSamples": wind_samples,
            })

    start = one_hz.index[0] if len(one_hz) else None
    end = one_hz.index[-1] if len(one_hz) else None
    return {
        "available": True,
        "points": points,
        "sample_interval_seconds": map_step,
        "source": (
            "Unchanged legacy 1 Hz, frequency, spectra, and "
            "straight-flight routines"
        ),
        "hist": hist,
        "frequency": frequency_rows,
        "altitude_profile": altitude_profile,
        "spectra": safe_spectra,
        "straight_settings": dict(one_hz.attrs.get("straight_params", {})),
        "straight_metrics": list(
            one_hz.attrs.get("straight_metrics", [])
        ),
        "straight_legs": straight_legs,
        "time_bounds": {
            "start": (
                start.isoformat()
                if hasattr(start, "isoformat")
                else None
            ),
            "end": (
                end.isoformat()
                if hasattr(end, "isoformat")
                else None
            ),
        },
        "browser_limits": {
            "map_points": 6000,
            "histogram_samples_per_variable": 30000,
            "altitude_samples": 12000,
            "wind_rose_samples_per_leg": 240,
        },
    }
