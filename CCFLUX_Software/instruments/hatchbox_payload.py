"""Compact, auditable browser payloads for Hatchbox aerosol instruments."""

from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Any, Iterable

import numpy as np
import pandas as pd

from core.browser_payload import write_text_atomic


def write_json_atomic(path: Path, payload: dict[str, Any]) -> Path:
    """Write finite JSON atomically so project reload never sees a partial file."""
    return write_text_atomic(
        Path(path),
        json.dumps(_json_value(payload), indent=2, allow_nan=False),
    )


def opc_sensor_payload(
    data: pd.DataFrame,
    spec: Any,
    metadata: dict[str, Any],
    *,
    flight_id: str,
    instrument_id: str,
) -> dict[str, Any]:
    """Prepare a responsive OPC payload without altering the evaluated record."""
    ordered = data.sort_values("recorded_time", kind="stable").reset_index(drop=True)
    indices = _representative_indices(
        len(ordered), 6000, ordered.get("session_id")
    )
    sample = ordered.iloc[indices]
    bin_columns = [f"bin{index}_number_cm3" for index in range(24)]
    heat_indices = _representative_indices(
        len(ordered), 1800, ordered.get("session_id")
    )
    heat = ordered.iloc[heat_indices]
    return {
        "schema": "ccflux-opc-browser-v1",
        "available": True,
        "flight_id": flight_id,
        "instrument_id": instrument_id,
        "sensor": spec.name,
        "label": spec.label,
        "time_basis": "UTC as recorded by the campaign instrument",
        "science_policy": {
            "sorting": "Stable chronological sort by valid recorded timestamp",
            "gaps": "Preserved; no interpolation",
            "zeros": "Preserved as observations",
            "qc": "Flags annotate records and do not remove them",
        },
        "summary": metadata,
        "series": {
            "time": _times(sample["recorded_time"]),
            "session": _column(sample, "session_id"),
            "pm1": _column(sample, spec.pm1),
            "pm25": _column(sample, spec.pm25),
            "pm10": _column(sample, spec.pm10),
            "number": _column(sample, "total_number_cm3"),
            "temperature": _column(sample, spec.temperature),
            "rh": _column(sample, spec.rh),
            "flow": _column(sample, f"SFR_{spec.suffix}_1"),
            "sampling_period": _column(sample, f"SP_{spec.suffix}_1"),
            "laser": _column(sample, f"Laser_status_{spec.suffix}_1"),
            "reject_ratio": _column(sample, f"RejectRatio_{spec.suffix}_1"),
            "active_bins": _column(sample, "active_bin_count"),
            "all_bins_zero": _column(sample, "all_bins_zero"),
            "qc_any": _column(sample, "qc_any"),
        },
        "heatmap": {
            "time": _times(heat["recorded_time"]),
            "session": _column(heat, "session_id"),
            "bin_index": list(range(24)),
            "z": [
                _column(heat, column)
                for column in bin_columns
            ],
        },
        "distribution": _distribution(ordered, bin_columns, list(range(24))),
    }


def partector_payload(
    selected: pd.DataFrame,
    sessions: pd.DataFrame,
    size_columns: list[str],
    centers_nm: np.ndarray,
    summary: dict[str, Any],
    *,
    flight_id: str,
) -> dict[str, Any]:
    """Prepare valid scientific traces and auditable QC context for Partector."""
    ordered = selected.sort_values("_time", kind="stable").reset_index(drop=True)
    valid = ordered.loc[ordered["qc_valid"]].copy()
    indices = _representative_indices(len(valid), 6000, valid.get("session"))
    sample = valid.iloc[indices]
    heat_indices = _representative_indices(len(valid), 1800, valid.get("session"))
    heat = valid.iloc[heat_indices]
    metric_columns = (
        "number_cm3",
        "ldsa_um2_cm3",
        "mean_diameter_nm",
        "mass_ug_m3",
        "n_8bin_cm3",
        "n_10_30_cm3",
        "n_30_50_cm3",
        "n_50_100_cm3",
        "n_100_300_cm3",
        "flow_lpm",
        "temperature_c",
        "rh_percent",
        "pressure_hpa",
        "battery_v",
        "pump_current_ma",
        "size_integral_to_reported_ratio",
    )
    return {
        "schema": "ccflux-partector-browser-v1",
        "available": True,
        "flight_id": flight_id,
        "instrument_id": "partector",
        "time_basis": "UTC as recorded by the campaign instrument",
        "science_policy": {
            "sorting": "Stable chronological sort by recorded timestamp",
            "sessions": "Split at instrument-clock resets or configured gaps",
            "duplicates": "One logger record retained per session/instrument clock",
            "size_integration": "dN/dlog10(D) integrated in log10 diameter",
            "interpolation": "None",
            "qc": "Invalid records remain in QC exports but are excluded from figures",
        },
        "summary": summary,
        "sessions": sessions.to_dict(orient="records"),
        "series": {
            "time": _times(sample["_time"]),
            "session": _column(sample, "session"),
            **{
                column: _column(sample, column)
                for column in metric_columns
                if column in sample
            },
        },
        "heatmap": {
            "time": _times(heat["_time"]),
            "diameter_nm": [float(value) for value in centers_nm],
            "z": [_column(heat, column) for column in size_columns],
        },
        "distribution": _distribution(
            valid, size_columns, [float(value) for value in centers_nm]
        ),
    }



def ins_gimbal_payload(
    data: pd.DataFrame,
    sessions: pd.DataFrame,
    summary: dict[str, Any],
    spectra: tuple[Any, Any, Any],
    *,
    flight_id: str,
) -> dict[str, Any]:
    """Prepare responsive Gremsy RAW_IMU plots without changing calculations."""
    ordered = data.sort_values("recorded_time", kind="stable").reset_index(drop=True)
    indices = _representative_indices(len(ordered), 7000, ordered.get("session_id"))
    sample = ordered.iloc[indices]
    series_columns = (
        "acc_x_g", "acc_y_g", "acc_z_g", "acc_norm_g", "acc_deviation_g",
        "gyro_x_dps", "gyro_y_dps", "gyro_z_dps", "gyro_norm_dps",
        "acc_rms_g", "gyro_rms_dps", "maneuver_flag",
        "gimbal_pitch_deg", "gimbal_roll_deg", "gimbal_yaw_deg",
    )
    acc_asd, gyro_asd, spectrograms = spectra
    spectrogram_payload = []
    finite_power = []
    for centers, frequency, power_db in spectrograms:
        time_index = _representative_indices(len(centers), 420)
        frequency_index = _representative_indices(len(frequency), 320)
        selected_power = np.asarray(power_db)[np.ix_(frequency_index, time_index)]
        finite = selected_power[np.isfinite(selected_power)]
        if finite.size:
            finite_power.append(finite)
        spectrogram_payload.append({
            "time": _times(pd.DatetimeIndex(centers)[time_index]),
            "frequency_hz": _values(np.asarray(frequency)[frequency_index]),
            "power_db_g2_hz": _json_value(selected_power),
        })
    color_limits = [None, None]
    if finite_power:
        pooled = np.concatenate(finite_power)
        lower, upper = np.percentile(pooled, [5, 99])
        if upper <= lower:
            upper = lower + 1.0
        color_limits = [float(lower), float(upper)]
    return {
        "schema": "ccflux-ins-gimbal-browser-v1",
        "available": True,
        "flight_id": flight_id,
        "instrument_id": "ins_gimbal",
        "time_basis": "UTC as recorded by the CC-FLUX campaign instrument",
        "science_policy": {
            "sorting": "Stable chronological sort by recorded UTC timestamp",
            "sessions": "Split only at configured acquisition gaps",
            "signal_filter": "None; rolling RMS is descriptive and raw signals remain unchanged",
            "spectra": "Duration-weighted Welch ASD across every acquisition session",
            "visualization": "Representative browser samples only; exported evaluated data retain full resolution",
        },
        "summary": summary,
        "sessions": sessions.to_dict(orient="records"),
        "series": {
            "time": _times(sample["recorded_time"]),
            "session": _column(sample, "session_id"),
            **{
                column: _column(sample, column)
                for column in series_columns
                if column in sample
            },
        },
        "spectrogram": {
            "sessions": spectrogram_payload,
            "color_limits_db": color_limits,
        },
        "asd": {
            "acceleration": {
                "frequency_hz": _values(np.asarray(acc_asd[0])),
                "amplitude_g_sqrt_hz": _values(np.asarray(acc_asd[1])),
            },
            "angular_rate": {
                "frequency_hz": _values(np.asarray(gyro_asd[0])),
                "amplitude_dps_sqrt_hz": _values(np.asarray(gyro_asd[1])),
            },
        },
    }
def combine_opc_payloads(
    hbx4_payload: dict[str, Any],
    hbx5_payload: dict[str, Any],
    *,
    flight_id: str,
) -> dict[str, Any]:
    """Combine the two independently processed OPC sensor payloads."""
    return {
        "schema": "ccflux-opc-combined-browser-v2",
        "available": True,
        "flight_id": flight_id,
        "time_basis": "UTC as recorded by the campaign instruments",
        "sensors": {"hbx4": hbx4_payload, "hbx5": hbx5_payload},
        "science_policy": (
            "HBX-4 and HBX-5 remain independent instruments with separate interactive "
            "axes; no inlet/no-inlet intercomparison is calculated."
        ),
    }

def _distribution(
    data: pd.DataFrame, columns: Iterable[str], x_values: list[float | int]
) -> dict[str, Any]:
    matrix = data[list(columns)].apply(pd.to_numeric, errors="coerce")
    return {
        "x": x_values,
        "p16": _values(matrix.quantile(0.16).to_numpy()),
        "median": _values(matrix.quantile(0.50).to_numpy()),
        "p84": _values(matrix.quantile(0.84).to_numpy()),
    }


def _representative_indices(
    length: int, maximum: int, groups: pd.Series | None = None
) -> np.ndarray:
    if length <= maximum:
        return np.arange(length, dtype=int)
    selected = set(np.linspace(0, length - 1, maximum, dtype=int).tolist())
    if groups is not None and len(groups) == length:
        values = groups.reset_index(drop=True)
        changes = np.flatnonzero(values.ne(values.shift()).to_numpy())
        for index in changes:
            selected.add(max(0, int(index) - 1))
            selected.add(int(index))
    return np.asarray(sorted(selected), dtype=int)


def _column(data: pd.DataFrame, name: str) -> list[Any]:
    if name not in data:
        return [None] * len(data)
    return _values(data[name].to_numpy())


def _values(values: Iterable[Any]) -> list[Any]:
    return [_json_value(value) for value in values]


def _times(values: Iterable[Any]) -> list[str | None]:
    output: list[str | None] = []
    for value in values:
        if pd.isna(value):
            output.append(None)
        else:
            output.append(pd.Timestamp(value).isoformat())
    return output


def _json_value(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(key): _json_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_value(item) for item in value]
    if isinstance(value, np.ndarray):
        return [_json_value(item) for item in value.tolist()]
    if isinstance(value, (pd.Timestamp,)):
        return value.isoformat()
    if isinstance(value, np.generic):
        value = value.item()
    if isinstance(value, float) and not math.isfinite(value):
        return None
    if pd.isna(value):
        return None
    return value
