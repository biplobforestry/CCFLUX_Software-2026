"""Noseboom quality control, following the campaign evaluation script.

The checks and their arithmetic come from NoseBoom_full_evaluation.py and are
kept as they were written there: the flow-uncertainty limit test, the wind
direction against heading and ground track, the vertical wind statistics, and
the comparison of wind speed and direction against the nearest airport's
METAR reports. Nothing recorded is altered; every series is read and reported.
"""
from __future__ import annotations

import json
import math
from collections.abc import Mapping, Sequence
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.parse import urlencode
from urllib.request import Request, urlopen

import numpy as np
import pandas as pd

# Airports around the campaign area, in Germany and the Netherlands. The one
# nearest the flight's median position is selected, and always named on the
# plot, so a reader can see whose observations the comparison used.
AIRPORTS: dict[str, tuple[str, float, float]] = {
    "EDDL": ("Duesseldorf Airport", 51.2895, 6.7668),
    "EDDK": ("Cologne Bonn Airport", 50.8659, 7.1427),
    "EDLW": ("Dortmund Airport", 51.5183, 7.6122),
    "EDDG": ("Muenster Osnabrueck International Airport", 52.1346, 7.6848),
    "EDLV": ("Weeze Airport", 51.6024, 6.1422),
    "EHEH": ("Eindhoven Airport", 51.4501, 5.3745),
    "EHBK": ("Maastricht Aachen Airport", 50.9117, 5.7701),
    "EHRD": ("Rotterdam The Hague Airport", 51.9569, 4.4372),
    "EHAM": ("Amsterdam Airport Schiphol", 52.3086, 4.7639),
    "EHGG": ("Groningen Airport Eelde", 53.1197, 6.5794),
}
METAR_API = "https://aviationweather.gov/api/data/metar"
MATCH_HALF_WINDOW_SECONDS = 150.0  # +/- 2.5 minutes around each report
VIEW_POINT_LIMIT = 4000


def _series(frame: pd.DataFrame, name: str) -> pd.Series:
    if name not in frame:
        return pd.Series(np.nan, index=frame.index, dtype=float)
    return pd.to_numeric(frame[name], errors="coerce")


def _thin(count: int, limit: int = VIEW_POINT_LIMIT) -> np.ndarray:
    """Even sample keeping both ends, so a browser series stays bounded."""
    if count <= limit:
        return np.arange(count)
    return np.unique(
        np.concatenate([np.linspace(0, count - 1, limit).astype(int), [0, count - 1]])
    )


def _clean(values: Any) -> list[float | None]:
    out: list[float | None] = []
    for value in np.asarray(values, dtype=float):
        out.append(None if not np.isfinite(value) else float(value))
    return out


def _times(stamps: Sequence[Any]) -> list[str]:
    return [
        pd.Timestamp(value).tz_convert("UTC").isoformat().replace("+00:00", "Z")
        if pd.notna(value) else None
        for value in stamps
    ]


def haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great-circle distance between two latitude/longitude points."""
    radius_km = 6371.0
    p1, p2 = np.radians([lat1, lat2])
    dlat = p2 - p1
    dlon = np.radians(lon2 - lon1)
    a = np.sin(dlat / 2) ** 2 + np.cos(p1) * np.cos(p2) * np.sin(dlon / 2) ** 2
    return float(2 * radius_km * np.arcsin(np.sqrt(a)))


def circular_difference_degrees(first: np.ndarray, second: np.ndarray) -> np.ndarray:
    return (first - second + 180.0) % 360.0 - 180.0


def circular_correlation(degrees_a: np.ndarray, degrees_b: np.ndarray) -> float:
    a = np.radians(np.asarray(degrees_a, dtype=float))
    b = np.radians(np.asarray(degrees_b, dtype=float))
    valid = np.isfinite(a) & np.isfinite(b)
    a, b = a[valid], b[valid]
    if a.size < 2:
        return float("nan")
    mean_a = np.arctan2(np.mean(np.sin(a)), np.mean(np.cos(a)))
    mean_b = np.arctan2(np.mean(np.sin(b)), np.mean(np.cos(b)))
    centered_a = np.sin(a - mean_a)
    centered_b = np.sin(b - mean_b)
    denominator = math.sqrt(float(np.sum(centered_a**2) * np.sum(centered_b**2)))
    if denominator == 0:
        return float("nan")
    return float(np.sum(centered_a * centered_b) / denominator)


def select_nearest_airport(
    latitude: pd.Series, longitude: pd.Series
) -> dict[str, Any] | None:
    """The configured airport nearest the flight's median position."""
    flight_lat = float(latitude.median()) if latitude.notna().any() else float("nan")
    flight_lon = float(longitude.median()) if longitude.notna().any() else float("nan")
    if not (np.isfinite(flight_lat) and np.isfinite(flight_lon)):
        return None
    ranked = sorted(
        (
            {
                "icao": icao,
                "name": name,
                "latitude": lat,
                "longitude": lon,
                "distance_km": haversine_km(flight_lat, flight_lon, lat, lon),
            }
            for icao, (name, lat, lon) in AIRPORTS.items()
        ),
        key=lambda item: item["distance_km"],
    )
    nearest = dict(ranked[0])
    nearest["flight_median_latitude"] = flight_lat
    nearest["flight_median_longitude"] = flight_lon
    nearest["alternatives"] = [
        {"icao": item["icao"], "name": item["name"],
         "distance_km": round(item["distance_km"], 2)}
        for item in ranked[1:6]
    ]
    return nearest


def relative_humidity_from_dewpoint(
    temperature_c: pd.Series, dewpoint_c: pd.Series
) -> pd.Series:
    """Relative humidity from temperature and dewpoint, by the Magnus equation."""
    numerator = np.exp((17.625 * dewpoint_c) / (243.04 + dewpoint_c))
    denominator = np.exp((17.625 * temperature_c) / (243.04 + temperature_c))
    return 100.0 * numerator / denominator


def fetch_metar(
    icao: str, start: datetime, end: datetime, *, timeout: float = 45.0,
    opener: Any = None,
) -> tuple[pd.DataFrame, dict[str, Any]]:
    """Recent METAR reports for one station, limited to the flight window."""
    now = datetime.now(timezone.utc)
    age_hours = max(0.0, (now - start).total_seconds() / 3600)
    hours = min(360, max(24, int(math.ceil(age_hours + 3))))
    url = METAR_API + "?" + urlencode({"ids": icao, "format": "json", "hours": hours})
    metadata: dict[str, Any] = {"url": url, "hours_requested": hours, "error": None}
    try:
        request = Request(url, headers={"User-Agent": "CC-FLUX-NoseBoom-QC/1.0"})
        with (opener or urlopen)(request, timeout=timeout) as response:
            payload = json.load(response)
    except Exception as exc:  # network, DNS, malformed payload
        metadata["error"] = f"{type(exc).__name__}: {exc}"
        return pd.DataFrame(), metadata

    rows = [
        {
            "time": pd.to_datetime(item.get("obsTime"), unit="s", utc=True),
            "station": item.get("icaoId", icao),
            "temperature_C": item.get("temp"),
            "dewpoint_C": item.get("dewp"),
            "wind_direction_deg": item.get("wdir"),
            "wind_speed_kt": item.get("wspd"),
            "raw_metar": item.get("rawOb", ""),
        }
        for item in (payload or [])
    ]
    metar = pd.DataFrame(rows)
    if metar.empty:
        metadata["error"] = "No METAR reports were returned for this station."
        return metar, metadata
    metar["time"] = pd.to_datetime(metar["time"], errors="coerce", utc=True)
    for column in ("temperature_C", "dewpoint_C", "wind_direction_deg", "wind_speed_kt"):
        metar[column] = pd.to_numeric(metar[column], errors="coerce")
    metar["relative_humidity_pct"] = relative_humidity_from_dewpoint(
        metar["temperature_C"], metar["dewpoint_C"]
    )
    metar["wind_speed_mps"] = metar["wind_speed_kt"] * 0.514444
    # The same half-open UTC interval the flight itself covers.
    metar = (
        metar.loc[(metar["time"] >= start) & (metar["time"] < end)]
        .sort_values("time")
        .drop_duplicates("time")
        .reset_index(drop=True)
    )
    metadata["reports_in_window"] = int(len(metar))
    return metar, metadata


def sensor_means_at_report_times(
    sensor_time: pd.Series,
    values: np.ndarray,
    report_times: Sequence[Any],
    *,
    circular: bool = False,
) -> tuple[np.ndarray, np.ndarray]:
    """Mean sensor value inside +/-2.5 min of each report, as the script does."""
    means = np.full(len(report_times), np.nan)
    counts = np.zeros(len(report_times), dtype=int)
    if not len(report_times) or sensor_time.empty:
        return means, counts
    sensor_ns = sensor_time.astype("int64").to_numpy()
    half_window_ns = int(MATCH_HALF_WINDOW_SECONDS * 1_000_000_000)
    for index, report_time in enumerate(report_times):
        report_ns = int(pd.Timestamp(report_time).value)
        selected = np.abs(sensor_ns - report_ns) <= half_window_ns
        window = values[selected]
        window = window[np.isfinite(window)]
        counts[index] = len(window)
        if not len(window):
            continue
        if circular:
            radians = np.radians(window)
            means[index] = float(
                np.degrees(np.arctan2(np.mean(np.sin(radians)), np.mean(np.cos(radians))))
                % 360.0
            )
        else:
            means[index] = float(np.mean(window))
    return means, counts


# Column names as the Noseboom CSV carries them once the NoseBoom_ prefix is
# removed, matching the campaign evaluation script.
COLUMNS = {
    "alpha": "Airflow_FlowUncert_alpha_deg",
    "beta": "Airflow_FlowUncert_beta_deg",
    "latitude": "INS_Filter_LLHPos_Latitude_deg",
    "longitude": "INS_Filter_LLHPos_Longitude_deg",
    "wind_speed": "WIND_vWind_m/s",
    "wind_direction": "WIND_dir_deg",
    "vertical_wind": "WIND_vWind_z_m/s",
    "ned_north": "INS_Filter_vNED_North_m/s",
    "ned_east": "INS_Filter_vNED_East_m/s",
    "ned_down": "INS_Filter_vNED_Down_m/s",
}
HEADING_CANDIDATES = (
    "INS_Filter_EulerAngles_Yaw_rad",
    "INS_Filter_EulerAngles_Yaw_deg",
    "INS_Filter_Heading_deg",
)


def flow_uncertainty(frame: pd.DataFrame, times: pd.Series) -> dict[str, Any]:
    """Alpha and beta over time; both at 90 degrees means outside the limits."""
    alpha = _series(frame, COLUMNS["alpha"])
    beta = _series(frame, COLUMNS["beta"])
    both_90 = np.isclose(alpha, 90.0) & np.isclose(beta, 90.0)
    keep = _thin(len(frame))
    return {
        "available": bool(alpha.notna().any() or beta.notna().any()),
        "time": _times(times.iloc[keep]),
        "alpha": _clean(alpha.iloc[keep]),
        "beta": _clean(beta.iloc[keep]),
        "samples_at_limit": int(both_90.sum()),
        "percentage_at_limit": float(100 * both_90.mean()) if len(frame) else 0.0,
        "alpha_min": float(alpha.min()) if alpha.notna().any() else None,
        "alpha_max": float(alpha.max()) if alpha.notna().any() else None,
        "beta_min": float(beta.min()) if beta.notna().any() else None,
        "beta_max": float(beta.max()) if beta.notna().any() else None,
    }


def direction_heading_track(frame: pd.DataFrame, times: pd.Series) -> dict[str, Any]:
    """Wind direction against the airship heading and its ground track."""
    north = _series(frame, COLUMNS["ned_north"]).to_numpy(float)
    east = _series(frame, COLUMNS["ned_east"]).to_numpy(float)
    down = _series(frame, COLUMNS["ned_down"]).to_numpy(float)
    track = np.degrees(np.arctan2(east, north)) % 360.0
    ground_speed = np.sqrt(north**2 + east**2)
    speed_3d = np.sqrt(north**2 + east**2 + down**2)

    heading_column = next((name for name in HEADING_CANDIDATES if name in frame), None)
    if heading_column is None:
        heading = np.full(len(frame), np.nan)
    else:
        raw = _series(frame, heading_column).to_numpy(float)
        heading = (np.degrees(raw) if heading_column.endswith("_rad") else raw) % 360.0
    direction = _series(frame, COLUMNS["wind_direction"]).to_numpy(float) % 360.0

    keep = _thin(len(frame))
    return {
        "available": bool(np.isfinite(direction).any()),
        "heading_source": heading_column,
        "time": _times(times.iloc[keep]),
        "wind_direction": _clean(direction[keep]),
        "heading": _clean(heading[keep]),
        "track": _clean(track[keep]),
        "mean_ground_speed_mps": float(np.nanmean(ground_speed)) if len(frame) else None,
        "mean_3d_speed_mps": float(np.nanmean(speed_3d)) if len(frame) else None,
        "wind_track_correlation": circular_correlation(direction, track),
        "wind_heading_correlation": circular_correlation(direction, heading),
    }


def vertical_wind(frame: pd.DataFrame, times: pd.Series) -> dict[str, Any]:
    """Vertical wind with the ten-minute running mean the script plots."""
    vertical = _series(frame, COLUMNS["vertical_wind"])
    if not vertical.notna().any():
        return {"available": False}
    rolling = (
        pd.Series(vertical.to_numpy(), index=pd.DatetimeIndex(times))
        .rolling("10min", center=True, min_periods=1)
        .mean()
    )
    keep = _thin(len(frame))
    return {
        "available": True,
        "time": _times(times.iloc[keep]),
        "vertical_wind": _clean(vertical.iloc[keep]),
        "rolling_mean": _clean(rolling.iloc[keep]),
        "mean": float(vertical.mean()),
        "median": float(vertical.median()),
        "standard_deviation": float(vertical.std(ddof=0)),
        "minimum": float(vertical.min()),
        "maximum": float(vertical.max()),
    }


def _validation(
    frame: pd.DataFrame,
    times: pd.Series,
    metar: pd.DataFrame,
    airport: Mapping[str, Any] | None,
    *,
    column: str,
    metar_column: str,
    circular: bool,
) -> dict[str, Any]:
    values = _series(frame, column).to_numpy(float)
    if circular:
        values = values % 360.0
    keep = _thin(len(frame))
    payload: dict[str, Any] = {
        "available": bool(np.isfinite(values).any()),
        "airport": dict(airport) if airport else None,
        "time": _times(times.iloc[keep]),
        "noseboom": _clean(values[keep]),
        "report_time": [],
        "report_value": [],
        "matched_noseboom": [],
        "bias": None,
        "mae": None,
        "matched_reports": 0,
        "match_window_minutes": MATCH_HALF_WINDOW_SECONDS / 60.0,
    }
    if metar.empty or metar_column not in metar:
        return payload
    reported = pd.to_numeric(metar[metar_column], errors="coerce").to_numpy(float)
    matched, _counts = sensor_means_at_report_times(
        times, values, metar["time"], circular=circular
    )
    valid = np.isfinite(matched) & np.isfinite(reported)
    if circular:
        error = circular_difference_degrees(matched[valid], reported[valid])
    else:
        error = matched[valid] - reported[valid]
    payload.update(
        {
            "report_time": _times(metar["time"]),
            "report_value": _clean(reported),
            "matched_noseboom": _clean(matched),
            "bias": float(np.mean(error)) if error.size else None,
            "mae": float(np.mean(np.abs(error))) if error.size else None,
            "matched_reports": int(valid.sum()),
        }
    )
    return payload


def build_qc_payload(
    frame: pd.DataFrame,
    *,
    time_column: str = "_time",
    fetch: Any = fetch_metar,
) -> dict[str, Any]:
    """Every QC section the Noseboom workspace shows, from one loaded window."""
    times = pd.to_datetime(frame[time_column], errors="coerce", utc=True)
    frame = frame.loc[times.notna()].reset_index(drop=True)
    times = times.loc[times.notna()].reset_index(drop=True)
    if frame.empty:
        return {"available": False, "message": "The selected interval holds no dated Noseboom record."}

    airport = select_nearest_airport(
        _series(frame, COLUMNS["latitude"]), _series(frame, COLUMNS["longitude"])
    )
    metar, metar_meta = pd.DataFrame(), {"error": "No airport could be selected."}
    if airport is not None:
        metar, metar_meta = fetch(
            airport["icao"],
            times.iloc[0].to_pydatetime(),
            (times.iloc[-1] + pd.Timedelta(seconds=1)).to_pydatetime(),
        )
    return {
        "available": True,
        "schema": "ccflux-noseboom-qc-v1",
        "record_count": int(len(frame)),
        "flow_uncertainty": flow_uncertainty(frame, times),
        "direction_heading_track": direction_heading_track(frame, times),
        "vertical_wind": vertical_wind(frame, times),
        "wind_speed_validation": _validation(
            frame, times, metar, airport,
            column=COLUMNS["wind_speed"], metar_column="wind_speed_mps", circular=False,
        ),
        "wind_direction_validation": _validation(
            frame, times, metar, airport,
            column=COLUMNS["wind_direction"], metar_column="wind_direction_deg", circular=True,
        ),
        "metar": {
            "airport": dict(airport) if airport else None,
            "reports": int(len(metar)),
            "error": metar_meta.get("error"),
            "source": "AviationWeather.gov METAR",
        },
    }


# The legacy browser loader reduces a delivery to the columns it plots, so the
# QC columns never reach it. Reading them here keeps that loader untouched.
QC_SOURCE_COLUMNS = (
    "Airflow_UTCcorr_Nanoseconds_ns",
    *COLUMNS.values(),
    *HEADING_CANDIDATES,
)


def load_qc_window(
    paths: Sequence[Any],
    start_ns: int,
    end_ns: int,
    *,
    progress: Any = None,
    chunk_size: int = 250_000,
) -> pd.DataFrame:
    """Stream the QC columns for one interval out of the Noseboom delivery."""
    from core.text_encoding import detect_encoding
    from core.noseboom_columns import normalize_column_name

    time_column = "Airflow_UTCcorr_Nanoseconds_ns"
    frames: list[pd.DataFrame] = []
    for path in paths:
        encoding = detect_encoding(path)
        header = pd.read_csv(path, nrows=0, encoding=encoding)
        mapping = {normalize_column_name(name): name for name in header.columns}
        wanted = [mapping[name] for name in QC_SOURCE_COLUMNS if name in mapping]
        if mapping.get(time_column) is None:
            continue
        for raw in pd.read_csv(
            path, usecols=wanted, encoding=encoding,
            low_memory=False, chunksize=chunk_size,
        ):
            raw = raw.rename(columns=normalize_column_name)
            stamps = pd.to_numeric(raw[time_column], errors="coerce")
            finite = stamps.dropna()
            if not finite.empty and float(finite.min()) > end_ns:
                break
            selected = raw.loc[(stamps >= start_ns) & (stamps <= end_ns)]
            if not selected.empty:
                frames.append(selected)
            if progress:
                progress(min(90.0, 5.0 + 85.0 * len(frames) / 40.0), "Reading QC columns")
    if not frames:
        return pd.DataFrame()
    frame = pd.concat(frames, ignore_index=True)
    frame["_time"] = pd.to_datetime(
        pd.to_numeric(frame[time_column], errors="coerce"), unit="ns", utc=True
    )
    return frame.sort_values("_time").reset_index(drop=True)
