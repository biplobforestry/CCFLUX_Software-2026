"""Adapter around the existing AirFloX/SIF scientific implementation."""

from __future__ import annotations
import json
import threading
import time
import warnings
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Mapping, Sequence
import numpy as np
import pandas as pd

from core.detector import InputCandidate
from core.enums import DetectionStatus, ProcessingStatus
from core.logging_manager import LogLevel, ProcessingLogManager
from core.models import FigureArtifact, InstrumentDescriptor, InstrumentResult, OutputFile, ProgressUpdate, SourceFile
from core.scanner import ScanIndex
from core.time_manager import TimeRange, TimezoneState
from instruments.base.interface import InstrumentBase, ProgressCallback
from .legacy_bridge import LegacySifBridge

HEADER_TOKENS = ("IT_WR[us]=", "IT_VEG[us]=", "GPS_TIME_UTC=", "GPS_date=")


@dataclass(slots=True)
class LoadedSif:
    candidate: InputCandidate
    mode_files: dict[str, tuple[Path, ...]]


class SifAdapter(InstrumentBase):
    descriptor = InstrumentDescriptor("sif", "Solar-Induced Fluorescence / FLOX", "HATCHBOX", frozenset({"detection", "metadata", "quicklook", "export"}), True)

    def __init__(self, *, output_root: Path, flight_name: str, bridge=None, logger: ProcessingLogManager | None = None):
        self.output_root, self.flight_name = Path(output_root), flight_name
        self.bridge, self.logger = bridge or LegacySifBridge(), logger
        self._callback: ProgressCallback | None = None
        self._cancelled = threading.Event()
        self._products: dict[str, dict[str, Any]] = {}

    def detect(self, scan_index: ScanIndex) -> Sequence[InputCandidate]:
        paths = [entry.path for entry in scan_index.entries if entry.is_file and entry.path.suffix.casefold() == ".csv" and _is_raw(entry.path)]
        return (InputCandidate("sif", tuple(paths), 0.95, "AirFloX spectral blocks and required metadata tokens"),) if paths else ()

    def inspect_metadata(self, candidate):
        modes = _classify(candidate.paths)
        return {"file_count": len(candidate.paths), "full_files": [str(p) for p in modes["FULL"]], "fluo_files": [str(p) for p in modes["FLUO"]], "raw_format": "semicolon-delimited AirFloX multi-row spectral blocks", "timestamp_basis": "GPS_TIME_UTC and GPS_date repaired by existing parser"}

    def extract_time_range(self, candidate):
        values = []
        for mode, files in _classify(candidate.paths).items():
            if not files: continue
            calibration, _ = self.bridge.essentials(mode)
            count = len(self.bridge.module.read_full_calibration(calibration))
            for path in files:
                try:
                    raw = self.bridge.module.read_drox_full(
                        path,
                        count,
                        drop_e500_zero=(mode == "FLUO"),
                        drop_zero_gps=False,
                    )
                    values.extend(v for v in self.bridge.module.get_gps_utc(raw) if not pd.isna(v))
                except Exception:
                    continue
        return TimeRange(min(values) if values else None, max(values) if values else None, min(values) if values else None, max(values) if values else None, TimezoneState.EXPLICIT_UTC, "seconds", "GPS_TIME_UTC + GPS_date through get_gps_utc")

    def validate(self, candidate):
        errors, warnings, modes = [], [], _classify(candidate.paths)
        if not candidate.paths: errors.append("No AirFloX raw spectral CSV files were selected.")
        for mode in ("FULL", "FLUO"):
            if modes[mode]:
                try: self.bridge.essentials(mode)
                except Exception as exc: errors.append(f"{mode}: {exc}")
        coverage = self.extract_time_range(candidate) if candidate.paths else TimeRange()
        if candidate.paths and coverage.utc_start is None: errors.append("No valid SIF GPS timestamps were extracted.")
        if not modes["FULL"] or not modes["FLUO"]: warnings.append("Only one AirFloX mode is present; available mode will be processed.")
        return InstrumentResult("sif", "Solar-Induced Fluorescence / FLOX", "HATCHBOX", DetectionStatus.FAILED if errors else DetectionStatus.WARNING if warnings else DetectionStatus.READY, source_files=_sources(candidate.paths), file_count=len(candidate.paths), original_start_time=coverage.original_start, original_end_time=coverage.original_end, utc_start_time=coverage.utc_start, utc_end_time=coverage.utc_end, warnings=warnings, errors=errors, metadata=dict(self.inspect_metadata(candidate)) if candidate.paths else {})

    def load(self, candidate):
        validation = self.validate(candidate)
        if validation.errors: raise ValueError("; ".join(validation.errors))
        self._check(); self._emit(10, "AirFloX files and essentials validated")
        return LoadedSif(candidate, _classify(candidate.paths))

    def process_quicklook(self, loaded, options: Mapping[str, Any]):
        started = time.monotonic()
        self._products = {}
        # An operator may supply their own calibration or vegetation-index file;
        # anything not supplied stays on the bundled CAL_FROG/Indices_ICOS set.
        essentials_overrides = {
            key: options.get(key)
            for key in ("calibration_full", "calibration_fluo", "indices_file")
            if options.get(key)
        }
        for mode in ("FULL", "FLUO"):
            calibration, indices = self.bridge.essentials(mode, essentials_overrides)
            self._emit(
                4,
                f"{mode} calibration {Path(calibration).name}, "
                f"indices {Path(indices).name}",
            )
        selected_modes = tuple(
            mode for mode in options.get("modes", ("FULL", "FLUO"))
            if mode in {"FULL", "FLUO"}
        )
        raw_min_kb = float(options.get("raw_min_kb", 100.0))
        if raw_min_kb < 0:
            raise ValueError("SIF raw-file minimum size cannot be negative")
        retained_paths: list[Path] = []
        skipped_paths: list[Path] = []
        selected_files: dict[str, tuple[Path, ...]] = {}
        minimum_bytes = raw_min_kb * 1024
        for mode, files in loaded.mode_files.items():
            retained = tuple(
                path for path in files if path.stat().st_size >= minimum_bytes
            )
            skipped = tuple(path for path in files if path not in retained)
            selected_files[mode] = retained
            retained_paths.extend(retained)
            skipped_paths.extend(skipped)
            if mode in selected_modes and files and not retained:
                raise ValueError(
                    f"All {mode} raw files are smaller than "
                    f"{raw_min_kb:g} KB. Lower the SIF raw-file size filter."
                )
        if not retained_paths:
            raise ValueError(
                f"No SIF raw files meet the {raw_min_kb:g} KB size filter."
            )
        self._emit(
            6,
            f"Raw-file filter {raw_min_kb:g} KB: "
            f"{len(retained_paths)} retained, {len(skipped_paths)} skipped",
        )
        position_mode = str(
            options.get(
                "position_mode",
                "uav_airship" if options.get("flight_root") else "tower",
            )
        )
        telemetry_log = None
        if position_mode == "uav_airship":
            flight_root = options.get("flight_root")
            if flight_root is None:
                raise ValueError(
                    "SIF UAV/Airship processing requires the active Flight Folder "
                    "to prepare Gimbal + Noseboom navigation."
                )
            self._emit(14, "Preparing Gimbal attitude and Noseboom position")
            telemetry_log = self.bridge.module.prepare_sif_log_from_hatchbox(
                Path(flight_root),
                self.output_root,
                custom_log=(
                    Path(options["telemetry_log"])
                    if options.get("telemetry_log")
                    else None
                ),
                altitude_filter=bool(options.get("altitude_filter", False)),
                max_position_gap_sec=float(
                    options.get("max_position_gap_seconds", 0.2)
                ),
            )
            self._emit(22, "Gimbal and Noseboom navigation ready")
        for index, mode in enumerate(selected_modes):
            files = selected_files.get(mode, ())
            if not files: continue
            self._check()
            mode_start = 28 + index * 31
            self._emit(mode_start, f"Combining {mode} spectral files")
            try:
                raw_path = self._combined_input(mode, files)
                calibration, indices = self.bridge.essentials(mode, essentials_overrides)
                self._emit(
                    mode_start + 6,
                    f"Calibrating {mode} radiance and reflectance",
                )
                result = (
                    self.bridge.module.process_full(
                        raw_path,
                        calibration,
                        indices,
                        bool(options.get("apply_nonlinearity_correction", False)),
                        bool(options.get("spectral_shift_correction", False)),
                        retain_zero_gps=position_mode == "uav_airship",
                    )
                    if mode == "FULL"
                    else self.bridge.module.process_fluo(
                        raw_path,
                        calibration,
                        indices,
                        bool(options.get("apply_nonlinearity_correction", False)),
                        False,
                        retain_zero_gps=position_mode == "uav_airship",
                    )
                )
                self._emit(
                    mode_start + 13,
                    (
                        f"Calculating {mode} vegetation indices"
                        if mode == "FULL"
                        else "Calculating FLUO vegetation indices and SIF iFLD"
                    ),
                )
                if telemetry_log is not None:
                    metadata, matched = self.bridge.module.match_data(
                        result["out"], telemetry_log
                    )
                    metadata = _refresh_sza_from_position(
                        metadata, self.bridge.module
                    )
                    if bool(options.get("drop_unmatched_telemetry", True)):
                        metadata, result = _select_rows(metadata, result, matched)
                    valid = _valid_positions(metadata)
                else:
                    metadata, valid = self.bridge.module.apply_static_position(
                        result["out"],
                        options.get("static_lat"),
                        options.get("static_lon"),
                        options.get("static_alt"),
                    )
                    metadata = _refresh_sza_from_position(
                        metadata, self.bridge.module
                    )
                metadata, spectra = self.bridge.module.apply_time_window(
                    metadata,
                    result,
                    options.get("analysis_start"),
                    options.get("analysis_end"),
                )
                valid = _valid_positions(metadata)
                if bool(options.get("drop_invalid_spectral_rows", False)):
                    spectral_valid = _valid_spectral_rows(spectra)
                    metadata, spectra = _select_rows(
                        metadata, spectra, spectral_valid
                    )
                    valid = _valid_positions(metadata)
                self._emit(mode_start + 22, f"Preparing {mode} exports and map data")
                self._products[mode] = {
                    "raw": raw_path,
                    "metadata": metadata,
                    "spectra": spectra,
                    "valid": valid,
                }
            except Exception as exc:
                if self.logger: self.logger.capture_exception("sif-adapter", "SIF quicklook failed", exc, instrument="sif")
                raise
        if not self._products: raise ValueError("No selected SIF mode could be processed")
        all_times = pd.concat([pd.to_datetime(p["metadata"]["datetime [UTC]"], utc=True) for p in self._products.values()]).dropna()
        self._emit(100, "SIF quicklook complete")
        if self.logger: self.logger.log(LogLevel.SUCCESS, "sif-adapter", "SIF quicklook completed", instrument="sif")
        return InstrumentResult("sif", "Solar-Induced Fluorescence / FLOX", "HATCHBOX", DetectionStatus.READY, ProcessingStatus.COMPLETE, _sources(retained_paths), len(retained_paths), all_times.min().to_pydatetime(), all_times.max().to_pydatetime(), all_times.min().to_pydatetime(), all_times.max().to_pydatetime(), progress=100.0, metadata={"processed_modes": sorted(self._products), "rows_by_mode": {mode: len(p["metadata"]) for mode, p in self._products.items()}, "raw_file_filter_kb": raw_min_kb, "retained_raw_files": [str(path) for path in retained_paths], "skipped_raw_files": [{"path": str(path), "size_kb": round(path.stat().st_size / 1024, 1)} for path in skipped_paths], "scientific_source": str(self.bridge.source_path), "essentials_directory": str(self.bridge.essentials("FULL", essentials_overrides)[0].parent), "calibration_files": {m: str(self.bridge.essentials(m, essentials_overrides)[0]) for m in sorted(self._products)}, "index_file": str(self.bridge.essentials("FULL", essentials_overrides)[1]), "position_mode": position_mode, "telemetry_log": str(telemetry_log) if telemetry_log else None, "options": _json_safe(dict(options))}, elapsed_time=timedelta(seconds=time.monotonic() - started))

    def process_detailed(self, loaded, options): return self.process_quicklook(loaded, options)

    def create_plots(self, result, output_directory):
        self._assert_output(output_directory)
        return ()

    def export_browser_data(self, result, output_directory):
        """Persist responsive FULL/FLUO plots and georeferenced map records."""
        self._assert_output(output_directory)
        if not self._products:
            raise RuntimeError("SIF quicklook must complete before browser export")
        modes = {}
        for mode, product in self._products.items():
            metadata = product["metadata"].copy()
            sample = metadata.iloc[_representative_indices(len(metadata), 5000)]
            numeric_columns = [
                column
                for column in sample.columns
                if column not in {"ID", "Lat", "Lon", "Alt", "datetime [UTC]"}
                and pd.to_numeric(sample[column], errors="coerce").notna().any()
            ]
            series = {
                column: _finite_values(
                    pd.to_numeric(sample[column], errors="coerce")
                )
                for column in numeric_columns
            }
            spectra = product["spectra"]
            modes[mode] = {
                "row_count": int(len(metadata)),
                "time": [
                    None if pd.isna(value) else value.isoformat()
                    for value in pd.to_datetime(
                        sample["datetime [UTC]"], utc=True, errors="coerce"
                    )
                ],
                "latitude": _finite_values(
                    pd.to_numeric(sample.get("Lat"), errors="coerce")
                ),
                "longitude": _finite_values(
                    pd.to_numeric(sample.get("Lon"), errors="coerce")
                ),
                "altitude_m": _finite_values(
                    pd.to_numeric(sample.get("Alt"), errors="coerce")
                ),
                "variables": series,
                "variable_names": numeric_columns,
                "spectra": _spectral_summary(spectra),
            }
        payload = {
            "schema": "ccflux-sif-browser-v1",
            "available": True,
            "flight_id": self.flight_name,
            "instrument_id": "sif",
            "time_basis": "UTC; AirFloX matched to Gimbal attitude and Noseboom position",
            "summary": _json_safe(result.metadata),
            # Which of the columns are vegetation indices, taken from the index
            # definition file this run actually used. The map offers these, and
            # reading them here means an operator's own index list works as well
            # as the bundled one - a name the page had never heard of would
            # otherwise be silently absent from the map.
            "index_names": _index_names(result.metadata.get("index_file")),
            "modes": modes,
        }
        path = Path(output_directory) / "sif_browser.json"
        if path.exists():
            raise FileExistsError(f"Output exists and was not overwritten: {path}")
        path.write_text(
            json.dumps(payload, indent=2, allow_nan=False),
            encoding="utf-8",
        )
        output = OutputFile(path, "browser_payload", size_bytes=path.stat().st_size)
        result.output_files.append(output)
        return output

    def export_results(self, result, output_directory, formats):
        self._assert_output(output_directory)
        if {v.casefold() for v in formats} - {"csv", "gis"}: raise ValueError("SIF exports support CSV and GIS")
        outputs = []
        for mode, p in self._products.items():
            mode_dir = output_directory / mode; mode_dir.mkdir(parents=True, exist_ok=True)
            raw, m, r, valid = p["raw"], p["metadata"], p["spectra"], p["valid"]
            targets = [mode_dir / f"Incoming_radiance_{mode}_{raw.stem}.csv", mode_dir / f"Reflected_radiance_{mode}_{raw.stem}.csv", mode_dir / f"Reflectance_{mode}_{raw.stem}.csv", mode_dir / f"ALL_INDEX_AIRFLOX_{mode}_{raw.stem}.csv"]
            if any(path.exists() for path in targets): raise FileExistsError("One or more SIF outputs already exist")
            written = [self.bridge.module.write_spectra(r["wl"], r["E"], valid, m["datetime [UTC]"], targets[0]), self.bridge.module.write_spectra(r["wl"], r["L"], valid, m["datetime [UTC]"], targets[1]), self.bridge.module.write_spectra(r["wl"], r["Ref"], valid, m["datetime [UTC]"], targets[2]), self.bridge.module.write_r_table(m, targets[3])]
            if "gis" in {v.casefold() for v in formats}: written += self.bridge.module.export_gis(m, raw, mode_dir)
            outputs.extend(OutputFile(Path(path), "sif_product", size_bytes=Path(path).stat().st_size) for path in written)
        result.output_files.extend(outputs); return tuple(outputs)

    def cancel(self): self._cancelled.set()
    def report_progress(self, callback): self._callback = callback
    def _combined_input(self, mode, files):
        if len(files) == 1: return files[0]
        path = self.output_root / "_combined" / f"{self.flight_name}_{mode}.CSV"
        if path.exists(): raise FileExistsError(f"SIF combined input exists: {path}")
        return self.bridge.module.concat(files, path)
    def _emit(self, progress, phase):
        self._check()
        if self._callback: self._callback(ProgressUpdate("sif", progress, phase, phase))
    def _check(self):
        if self._cancelled.is_set(): raise RuntimeError("SIF processing was cancelled")
    def _assert_output(self, path):
        root, target = self.output_root.resolve(strict=False), Path(path).resolve(strict=False)
        try: target.relative_to(root)
        except ValueError as exc: raise ValueError(f"SIF output must remain below {root}") from exc
        target.mkdir(parents=True, exist_ok=True)


def _is_raw(path):
    try: text = path.read_text(encoding="utf-8-sig", errors="replace")[:65536]
    except OSError: return False
    return all(token in text for token in HEADER_TOKENS) and all(f"\n{label};" in "\n" + text for label in ("WR", "VEG", "WR2", "DC_WR", "DC_VEG"))

def _classify(paths):
    result = {"FULL": [], "FLUO": []}
    for path in paths:
        text = path.read_text(encoding="utf-8-sig", errors="replace")[:8192].upper()
        mode = "FLUO" if "AIRFLOX FLUO" in text or path.parent.name.upper() == "FLUO" else "FULL"
        result[mode].append(path)
    return {key: tuple(value) for key, value in result.items()}

def _sources(paths): return [SourceFile(path, size_bytes=path.stat().st_size) for path in paths]


def _valid_positions(frame):
    return (
        pd.to_datetime(frame["datetime [UTC]"], errors="coerce").notna()
        & pd.to_numeric(frame.get("Lat"), errors="coerce").notna()
        & pd.to_numeric(frame.get("Lon"), errors="coerce").notna()
    ).to_numpy()


def _refresh_sza_from_position(frame, science_module):
    """Recalculate SZA after Gimbal/Noseboom or static positioning is applied."""
    result = frame.copy()
    timestamps = pd.to_datetime(
        result["datetime [UTC]"], utc=True, errors="coerce"
    )
    latitude = pd.to_numeric(result.get("Lat"), errors="coerce").to_numpy(float)
    longitude = pd.to_numeric(result.get("Lon"), errors="coerce").to_numpy(float)
    valid = (
        timestamps.notna().to_numpy()
        & np.isfinite(latitude)
        & np.isfinite(longitude)
    )
    values = np.full(len(result), np.nan, dtype=float)
    if valid.any():
        values[valid] = science_module.zenith(
            [value.to_pydatetime() for value in timestamps[valid]],
            longitude[valid],
            latitude[valid],
        )
    result["SZA"] = values
    return result


def _valid_spectral_rows(spectra):
    matrices = [
        np.asarray(spectra[name], dtype=float)
        for name in ("E", "L", "Ref")
        if name in spectra
    ]
    if not matrices:
        return np.zeros(0, dtype=bool)
    valid = np.ones(matrices[0].shape[1], dtype=bool)
    for matrix in matrices:
        valid &= np.isfinite(matrix).any(axis=0)
    return valid


def _select_rows(metadata, spectra, keep):
    mask = np.asarray(keep, dtype=bool)
    selected = metadata.loc[mask].reset_index(drop=True)
    result = dict(spectra)
    for name in ("E", "L", "Ref"):
        if name in result:
            result[name] = np.asarray(result[name])[:, mask]
    return selected, result


def _representative_indices(length, maximum):
    if length <= maximum:
        return np.arange(length, dtype=int)
    return np.unique(np.linspace(0, length - 1, maximum, dtype=int))



def _index_names(index_file: object) -> list[str]:
    """Index names from a semicolon-separated index definition file.

    The file is the same one the retrieval read, so the names match the columns
    it produced. An unreadable or unexpected file yields nothing rather than
    raising: the map falls back to offering every variable, which is a worse
    view but not a broken one.
    """
    if not index_file:
        return []
    path = Path(str(index_file))
    if not path.is_file():
        return []
    try:
        lines = path.read_text(encoding="utf-8-sig", errors="replace").splitlines()
    except OSError:
        return []
    names: list[str] = []
    for line in lines[1:]:                      # the first line names the columns
        name = line.split(";", 1)[0].strip().strip('"')
        if name and name not in names:
            names.append(name)
    return names

def _finite_values(series):
    if series is None:
        return []
    return [
        None if pd.isna(value) or not np.isfinite(float(value)) else float(value)
        for value in series
    ]


def _spectral_summary(spectra):
    wavelength = np.asarray(spectra.get("wl", ()), dtype=float)
    index = _representative_indices(len(wavelength), 900)
    payload = {"wavelength_nm": _finite_values(wavelength[index])}
    for key, label in (("E", "incoming"), ("L", "reflected"), ("Ref", "reflectance")):
        matrix = np.asarray(spectra.get(key, ()), dtype=float)
        if matrix.ndim != 2 or matrix.shape[0] != len(wavelength):
            payload[label] = []
            continue
        with np.errstate(invalid="ignore"), warnings.catch_warnings():
            warnings.simplefilter("ignore", RuntimeWarning)
            payload[label] = _finite_values(
                np.nanmedian(matrix[index, :], axis=1)
            )
    return payload


def _json_safe(value):
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, (datetime, pd.Timestamp)):
        return value.isoformat()
    if isinstance(value, Mapping):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(item) for item in value]
    if isinstance(value, np.generic):
        return value.item()
    return value
