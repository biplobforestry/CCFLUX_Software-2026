"""Where did that come from? The gas record, the ground under it, and the wind.

The Trace Gas Investigation answers whether a signal is real or the instrument's
own. This answers the next question: a real enhancement having been found, what
was the Zeppelin over, and what was the air doing when it arrived.

That question needs three things on one page and they are the three parts here.

* **The gases, read together.** Ten MIRO species on however many stacked rows an
  operator wants, each row with its own left and right scale, because a plume is
  recognised by which species move together - NO with NO2 and CO is combustion,
  NH3 with CH4 is agricultural - and species on separate figures cannot be
  compared at a glance. The instrument's own housekeeping is offered on the same
  axes, so a feature that is really the cell warming is visible as such rather
  than mistaken for air.
* **A region, chosen by eye.** The operator draws a box over the interesting
  stretch. Everything below is computed over exactly that interval.
* **The wind over that region.** A wind rose says which direction the air came
  from, which is the whole of source attribution: an enhancement met while the
  wind was from the south-west did not come from the north-east.

Smoothing is display only. The raw record is what every number here is computed
from, and the page says so, because a filter that is applied silently and then
integrated over is how a plume becomes a different size than it was.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any, Mapping, Sequence

import numpy as np
import pandas as pd

# The ten species the MIRO writes, in the order the instrument reports them.
GAS_CHANNELS = (
    "CO wet", "N2O wet", "H2O wet", "NO wet", "NO2 wet",
    "CH4 wet", "SO2 wet", "NH3 wet", "O3 wet", "CO2 wet",
)
# Offered on the same axes as the gases. A rise that follows the cell
# temperature is the instrument, and the only way to see that is to be able to
# put the two on one panel.
HOUSEKEEPING_CHANNELS = ("T Cell C", "Outside T", "Laser housing T", "p Cell")

# The MIRO switches between ambient air and zero air on a solenoid, and what it
# reports while that valve is over is the calibration, not the atmosphere. Left
# in, Flight_CC0807 showed CO reaching tens of thousands of ppb - a number no
# reader would believe and none should be shown. Ambient is valve state 0.
VALVE_COLUMN = "VValve 0"
AMBIENT_VALVE_STATE = 0
# The cell does not clear the instant the valve returns. The validated analysis
# discards this much of each ambient episode, and so does this page, or the
# decay from zero air is drawn as a plume.
SETTLE_SECONDS = 30.0

# What the navigation record contributes. Altitude first because it is the one
# an operator reaches for; the rest describe the air the sample was taken in.
NAVIGATION_CHANNELS = (
    "altitude", "ground_speed_mps", "wind_mps", "wind_dir_deg", "heading_deg",
    "wind_u_mps", "wind_v_mps", "wind_w_mps", "air_temp_degC",
    "rel_humidity_pct",
)

CHANNEL_LABELS = {
    "CO wet": "CO", "N2O wet": "N2O", "H2O wet": "H2O", "NO wet": "NO",
    "NO2 wet": "NO2", "CH4 wet": "CH4", "SO2 wet": "SO2", "NH3 wet": "NH3",
    "O3 wet": "O3", "CO2 wet": "CO2",
    "T Cell C": "Cell temperature", "Outside T": "Outside temperature",
    "Laser housing T": "Laser housing temperature", "p Cell": "Cell pressure",
    "altitude": "Altitude", "ground_speed_mps": "Ground speed",
    "wind_mps": "Wind speed", "wind_dir_deg": "Wind direction",
    "heading_deg": "Track",
    "wind_u_mps": "Wind east component", "wind_v_mps": "Wind north component",
    "wind_w_mps": "Vertical wind",
    "air_temp_degC": "Air temperature", "rel_humidity_pct": "Relative humidity",
}
# The MIRO writes mole fractions. Everything downstream of it reports ppb, ppm
# or percent, and the conversion is the legacy module's gas_unit_scale. Plotting
# the stored numbers under these labels would be wrong by a factor of a billion,
# so the scale is applied on the way in and this table is only the naming.
GAS_UNIT_SCALE = {
    "H2O wet": ("%", 100.0),
    "CO2 wet": ("ppm", 1e6),
    "CH4 wet": ("ppm", 1e6),
}
DEFAULT_GAS_UNIT_SCALE = ("ppb", 1e9)


def gas_unit_scale(column: str, miro_module: Any = None) -> tuple[str, float]:
    """The instrument module's own conversion, asked for by preference.

    The fallback exists so this module can be tested without loading the legacy
    package; a test pins the two against each other so they cannot drift.
    """
    if miro_module is not None and hasattr(miro_module, "gas_unit_scale"):
        return miro_module.gas_unit_scale(column)
    return GAS_UNIT_SCALE.get(column, DEFAULT_GAS_UNIT_SCALE)


CHANNEL_UNITS = {
    "CO wet": "ppb", "N2O wet": "ppb", "H2O wet": "%", "NO wet": "ppb",
    "NO2 wet": "ppb", "CH4 wet": "ppm", "SO2 wet": "ppb", "NH3 wet": "ppb",
    "O3 wet": "ppb", "CO2 wet": "ppm",
    "T Cell C": "degC", "Outside T": "degC", "Laser housing T": "degC",
    "p Cell": "mbar",
    "altitude": "m", "ground_speed_mps": "m/s", "wind_mps": "m/s",
    "wind_dir_deg": "deg", "heading_deg": "deg",
    "wind_u_mps": "m/s", "wind_v_mps": "m/s", "wind_w_mps": "m/s",
    "air_temp_degC": "degC", "rel_humidity_pct": "%",
}
# Bearings. They are plotted like anything else but must never be smoothed,
# averaged or interpolated as plain numbers.
CIRCULAR_CHANNELS = frozenset({"wind_dir_deg", "heading_deg"})

SMOOTHING_METHODS = ("savgol", "moving", "spline", "none")
DEFAULT_SMOOTHING = "savgol"
# A polynomial fitted over a moving window keeps a plume's height and width
# where a moving average lowers and widens it - but only for a feature wider
# than the window. Measured on Flight_CC0806, a one-second NO2 spike of 75.6 ppb
# came through a 15 s window at 6.7 ppb, under 9% of itself. Five seconds is the
# default because it takes the sample-to-sample noise off a 1 Hz record and
# leaves anything a Zeppelin can fly through intact; the envelope behind the
# line carries the true excursion at every width.
DEFAULT_SMOOTHING_SECONDS = 5
DEFAULT_POLYNOMIAL_ORDER = 2
MAXIMUM_SMOOTHING_SECONDS = 900

# More than this and a browser stops drawing a line and starts drawing a smear,
# well before it stops being fast.
MAXIMUM_ROW_POINTS = 6000
MAXIMUM_ROWS = 8
DEFAULT_ROWS = 3

# Sixteen sectors is the convention a wind rose is read in: enough to place a
# source, few enough that each sector holds samples to count.
WINDROSE_SECTORS = 16
# Bin edges in m/s. The top bin is open, so a gust is counted rather than lost.
WINDROSE_SPEED_EDGES = (0.0, 2.0, 4.0, 6.0, 8.0, 10.0)


class SourceInvestigationError(ValueError):
    """Something the operator can act on, phrased for them rather than a log."""


@dataclass(frozen=True, slots=True)
class Filters:
    start: pd.Timestamp | None
    end: pd.Timestamp | None
    rows: int
    smoothing: str
    smoothing_seconds: int
    polynomial_order: int


@dataclass(frozen=True, slots=True)
class Region:
    start: pd.Timestamp
    end: pd.Timestamp


def channel_catalogue(
    miro_data: pd.DataFrame | None, navigation: pd.DataFrame | None
) -> list[dict[str, Any]]:
    """Every channel this flight can actually draw, with what it is measured in.

    Built from the data rather than from the constant lists, so a flight whose
    MIRO wrote nine species offers nine and not a tenth that draws an empty
    axis.
    """
    catalogue: list[dict[str, Any]] = []
    for group, names, source in (
        ("Trace gas", GAS_CHANNELS, miro_data),
        ("Instrument", HOUSEKEEPING_CHANNELS, miro_data),
        ("Navigation", NAVIGATION_CHANNELS, navigation),
    ):
        for name in names:
            if source is None or name not in getattr(source, "columns", ()):
                continue
            values = pd.to_numeric(source[name], errors="coerce")
            if not values.notna().any():
                continue
            catalogue.append({
                "key": name,
                "label": CHANNEL_LABELS.get(name, name),
                "unit": CHANNEL_UNITS.get(name, ""),
                "group": group,
                "circular": name in CIRCULAR_CHANNELS,
            })
    return catalogue


def parse_filters(request: Mapping[str, Any]) -> Filters:
    rows = int(request.get("rows") or DEFAULT_ROWS)
    if not 1 <= rows <= MAXIMUM_ROWS:
        raise SourceInvestigationError(
            f"Choose between 1 and {MAXIMUM_ROWS} rows."
        )
    method = str(request.get("smoothing") or DEFAULT_SMOOTHING).strip().casefold()
    if method not in SMOOTHING_METHODS:
        raise SourceInvestigationError(
            "Smoothing must be one of: " + ", ".join(SMOOTHING_METHODS)
        )
    seconds = int(request.get("smoothing_seconds") or DEFAULT_SMOOTHING_SECONDS)
    if not 1 <= seconds <= MAXIMUM_SMOOTHING_SECONDS:
        raise SourceInvestigationError(
            f"The smoothing window must be 1 to {MAXIMUM_SMOOTHING_SECONDS} seconds."
        )
    order = int(request.get("polynomial_order") or DEFAULT_POLYNOMIAL_ORDER)
    if not 1 <= order <= 5:
        raise SourceInvestigationError("The polynomial order must be 1 to 5.")
    return Filters(
        start=_stamp(request.get("start")),
        end=_stamp(request.get("end")),
        rows=rows,
        smoothing=method,
        smoothing_seconds=seconds,
        polynomial_order=order,
    )


def parse_region(request: Mapping[str, Any]) -> Region:
    start = _stamp(request.get("region_start"))
    end = _stamp(request.get("region_end"))
    if start is None or end is None:
        raise SourceInvestigationError(
            "Select a region on a gas row before asking where it was."
        )
    if end < start:
        start, end = end, start
    if start == end:
        raise SourceInvestigationError(
            "The selected region has no duration; drag across the feature."
        )
    return Region(start=start, end=end)


def _stamp(value: Any) -> pd.Timestamp | None:
    if value in (None, ""):
        return None
    stamp = pd.to_datetime(value, errors="coerce")
    if pd.isna(stamp):
        return None
    # Every clock in this project is naive UTC by the time it reaches here, and
    # comparing a tz-aware bound against a naive index raises rather than
    # silently misaligning.
    return stamp.tz_localize(None) if stamp.tzinfo is not None else stamp


def smooth_values(
    times: pd.Series,
    values: np.ndarray,
    method: str,
    seconds: int,
    order: int,
    circular: bool = False,
) -> np.ndarray:
    """The displayed curve. The raw values are what the numbers come from.

    A bearing is smoothed through its components, because a filter run over
    degrees turns a wind holding steady at north into one swinging to south and
    back every time it crosses 360.
    """
    if method == "none" or len(values) < 3:
        return values
    if circular:
        radians = np.deg2rad(values)
        across = smooth_values(times, np.sin(radians), method, seconds, order)
        along = smooth_values(times, np.cos(radians), method, seconds, order)
        return np.rad2deg(np.arctan2(across, along)) % 360

    window = _window_samples(times, seconds)
    if window < 3:
        return values
    finite = np.isfinite(values)
    if not finite.any():
        return values
    if method == "savgol":
        smoothed = _savitzky_golay(values, finite, window, order)
    elif method == "moving":
        smoothed = _moving_average(values, window)
    elif method == "spline":
        smoothed = _spline(values, finite, window)
    else:
        return values
    return _within_measured_range(smoothed, values[finite])


def _within_measured_range(smoothed: np.ndarray, measured: np.ndarray) -> np.ndarray:
    """A drawn curve may not leave the range the instrument reported.

    A polynomial fitted across a sharp spike rings on both sides of it, and the
    undershoot goes below everything measured: on Flight_CC0806, Savitzky-Golay
    put 718 CO values and the spline 1 442 under the record's own minimum, and
    on Flight_CC0807 the spline reached -500 ppb. A negative concentration is
    not a smoothed measurement, it is an artefact of the filter, and a reader
    comparing this page against the workspace sees two different flights.

    Clamping is a floor and a ceiling, not a fix for ringing inside the range;
    what it guarantees is that nothing is drawn that was never measured.
    """
    if not len(measured):
        return smoothed
    return np.clip(smoothed, float(np.min(measured)), float(np.max(measured)))


def _window_samples(times: pd.Series, seconds: int) -> int:
    """How many samples that many seconds is, on this record's own spacing.

    The spacing is taken through total_seconds rather than astype("int64"),
    which returns whatever unit the column happens to be stored in. The
    campaign files parse to nanoseconds and worked; a microsecond column made
    a fifteen-second window come out as 15 001 samples, and a rolling mean
    wider than the flight returns nothing at all.
    """
    stamps = pd.to_datetime(pd.Series(times), errors="coerce").dropna()
    if len(stamps) < 2:
        return 0
    deltas = stamps.diff().dt.total_seconds().dropna()
    step = float(np.median(deltas)) if len(deltas) else 0.0
    if not np.isfinite(step) or step <= 0:
        return 0
    window = int(round(seconds / step))
    # An even window has no centre sample, and the filters below are centred.
    return window + 1 if window % 2 == 0 else window


def _savitzky_golay(
    values: np.ndarray, finite: np.ndarray, window: int, order: int
) -> np.ndarray:
    from scipy.signal import savgol_filter

    if window <= order + 1:
        window = order + 2 + (order % 2)
    if window % 2 == 0:
        window += 1
    filled = _fill_for_filtering(values, finite)
    if window > len(filled):
        return values
    smoothed = savgol_filter(filled, window, order, mode="interp")
    # A gap in the record stays a gap: the filter must not draw a line across
    # a stretch where the instrument reported nothing.
    return np.where(finite, smoothed, np.nan)


def _moving_average(values: np.ndarray, window: int) -> np.ndarray:
    series = pd.Series(values, dtype=float)
    smoothed = series.rolling(window, center=True, min_periods=max(1, window // 3)).mean()
    return np.where(np.isfinite(values), smoothed.to_numpy(), np.nan)


def _spline(values: np.ndarray, finite: np.ndarray, window: int) -> np.ndarray:
    """A smoothing spline, expressed as a cubic through decimated knots.

    Offered because it is the smoothest of the three; it can overshoot between
    knots, which is why it is not the default.
    """
    from scipy.interpolate import make_interp_spline

    index = np.arange(len(values), dtype=float)
    usable = index[finite]
    if len(usable) < 4:
        return values
    step = max(1, window // 2)
    knots = usable[::step]
    if len(knots) < 4:
        knots = usable
    spline = make_interp_spline(
        knots, np.interp(knots, usable, values[finite]), k=min(3, len(knots) - 1)
    )
    return np.where(finite, spline(index), np.nan)


def _fill_for_filtering(values: np.ndarray, finite: np.ndarray) -> np.ndarray:
    """Bridge gaps so the filter runs, then the caller puts the gaps back."""
    index = np.arange(len(values), dtype=float)
    return np.interp(index, index[finite], values[finite])


def _decimate(count: int, limit: int = MAXIMUM_ROW_POINTS) -> int:
    return max(1, (count + limit - 1) // limit)


def combined_frame(
    miro_data: pd.DataFrame | None,
    navigation: pd.DataFrame | None,
    *,
    miro_module: Any = None,
) -> pd.DataFrame:
    """One time-indexed table holding whatever this flight has, in real units.

    The two records run on different clocks and different rates, so navigation
    is matched to the nearest MIRO sample within a second rather than joined on
    equality, which would drop almost everything.
    """
    if miro_data is None or not len(miro_data):
        raise SourceInvestigationError(
            "Process MIRO in the MIRO Rack workspace before opening the Source "
            "Investigation; it is the record this page is built on."
        )
    columns = [
        name for name in (*GAS_CHANNELS, *HOUSEKEEPING_CHANNELS)
        if name in miro_data.columns
    ]
    carried = [*columns, VALVE_COLUMN] if VALVE_COLUMN in miro_data.columns else columns
    frame = miro_data[["timestamp", *carried]].dropna(subset=["timestamp"]).copy()
    frame["timestamp"] = pd.to_datetime(frame["timestamp"], errors="coerce")
    frame = frame.dropna(subset=["timestamp"]).sort_values("timestamp")
    # Mole fractions to the units every label on the page states.
    for name in columns:
        if name in GAS_CHANNELS:
            _, scale = gas_unit_scale(name, miro_module)
            frame[name] = pd.to_numeric(frame[name], errors="coerce") * scale
    frame = _ambient_only(frame, columns)
    if navigation is not None and len(navigation):
        wanted = [
            name for name in NAVIGATION_CHANNELS if name in navigation.columns
        ]
        if wanted:
            nav = navigation[["timestamp", *wanted]].dropna(subset=["timestamp"]).copy()
            nav["timestamp"] = pd.to_datetime(nav["timestamp"], errors="coerce")
            nav = nav.dropna(subset=["timestamp"]).sort_values("timestamp")
            frame = pd.merge_asof(
                frame, nav, on="timestamp", direction="nearest",
                tolerance=pd.Timedelta(seconds=1),
            )
    return frame.reset_index(drop=True)


def _ambient_only(frame: pd.DataFrame, gases: Sequence[str]) -> pd.DataFrame:
    """Blank the gases wherever the instrument was not sampling the atmosphere.

    The valve episodes are left as gaps rather than dropped, so the time axis
    stays continuous and the reader can see that the record was interrupted
    instead of finding two ambient stretches joined into one apparent trend.

    The samples are counted and reported, because a page that silently removes
    a third of a flight is worse than one that never removed anything.
    """
    if VALVE_COLUMN not in frame.columns:
        frame.attrs["ambient"] = {"available": False}
        return frame
    valve = pd.to_numeric(frame[VALVE_COLUMN], errors="coerce").round()
    valve = valve.where(valve.isin([0, 1])).ffill().bfill()
    ambient = valve.eq(AMBIENT_VALVE_STATE)
    if not ambient.any():
        # Every sample is calibration: blanking them all would leave an empty
        # page with no reason given, so the record is left alone and flagged.
        frame.attrs["ambient"] = {
            "available": True, "kept": 0, "removed": int(len(frame)),
            "note": "Every MIRO sample in this flight is a zero-air episode.",
        }
        return frame
    # The cell clears over seconds, not instantly, so the start of each ambient
    # episode still holds the decay from zero air.
    episode = valve.ne(valve.shift()).cumsum()
    since = frame["timestamp"] - frame.groupby(episode)["timestamp"].transform("first")
    settled = ambient & (since >= pd.Timedelta(seconds=SETTLE_SECONDS))
    removed = int((~settled).sum())
    blanked = frame.copy()
    for name in gases:
        if name in GAS_CHANNELS:
            blanked.loc[~settled, name] = np.nan
    blanked.attrs["ambient"] = {
        "available": True,
        "kept": int(settled.sum()),
        "removed": removed,
        "settle_seconds": SETTLE_SECONDS,
        "note": (
            f"{removed:,} samples are zero-air calibration or the "
            f"{SETTLE_SECONDS:g} s settling after it, and carry no atmospheric "
            "value. They are left as gaps rather than joined over."
        ),
    }
    return blanked


def wind_direction_from_components(frame: pd.DataFrame) -> pd.Series | None:
    """Where the wind came from, derived when the instrument's own is absent.

    resample_navigation dropped WIND_dir_deg for every flight processed before
    it was added to the circular columns, so a project on disk can carry the
    components and not the bearing. Meteorological convention: the direction
    the wind is coming from, which is the one a source is attributed with.
    """
    if not {"wind_u_mps", "wind_v_mps"}.issubset(frame.columns):
        return None
    east = pd.to_numeric(frame["wind_u_mps"], errors="coerce")
    north = pd.to_numeric(frame["wind_v_mps"], errors="coerce")
    if not (east.notna().any() and north.notna().any()):
        return None
    return (np.degrees(np.arctan2(-east, -north)) % 360.0).rename("wind_dir_deg")


def build_rows(frame: pd.DataFrame, filters: Filters) -> dict[str, Any]:
    """The plotted record: every channel, on the window, at the display rate."""
    window = frame
    if filters.start is not None:
        window = window[window["timestamp"] >= filters.start]
    if filters.end is not None:
        window = window[window["timestamp"] <= filters.end]
    if window.empty:
        raise SourceInvestigationError(
            "No MIRO samples fall inside the selected time filter."
        )
    step = _decimate(len(window))
    full_times = window["timestamp"]
    shown_index = np.arange(0, len(window), step)
    times = full_times.iloc[shown_index]
    series: dict[str, list[float | None]] = {}
    raw: dict[str, list[float | None]] = {}
    envelope: dict[str, dict[str, list[float | None]]] = {}
    for name in window.columns:
        if name in ("timestamp", VALVE_COLUMN):
            continue
        values = pd.to_numeric(window[name], errors="coerce").to_numpy(dtype=float)
        if not np.isfinite(values).any():
            continue
        # Smoothed at the record's own rate and decimated afterwards. The other
        # way round, the window is a count of surviving samples rather than the
        # seconds it is labelled with, and it filters a signal that has already
        # been aliased by the subsampling.
        smoothed = smooth_values(
            full_times, values, filters.smoothing, filters.smoothing_seconds,
            filters.polynomial_order, name in CIRCULAR_CHANNELS,
        )
        series[name] = _jsonable(smoothed[shown_index])
        raw[name] = _jsonable(values[shown_index])
        if name not in CIRCULAR_CHANNELS:
            envelope[name] = _envelope(values, step, len(shown_index))
    return {
        "time": [value.isoformat() for value in times],
        "series": series,
        "raw": raw,
        # Every sample's excursion, kept even where it fell between two drawn
        # points. A one-second plume is one sample in forty-three thousand, and
        # taking every eighth would throw it away on the page whose whole
        # purpose is finding it.
        "envelope": envelope,
        "samples": int(len(window)),
        "shown": int(len(shown_index)),
        "decimation": int(step),
        "ambient": dict(frame.attrs.get("ambient") or {"available": False}),
        "smoothing": {
            "method": filters.smoothing,
            "seconds": filters.smoothing_seconds,
            "polynomial_order": filters.polynomial_order,
            # Said plainly, because a filter applied silently and then read off
            # is how a plume becomes a different size than it was.
            "note": "Smoothing affects the drawn curve only. Every number "
                    "reported for a selected region is computed from the raw "
                    "record.",
        },
    }


def _jsonable(values: np.ndarray) -> list[float | None]:
    return [None if not np.isfinite(value) else float(value) for value in values]


def _envelope(
    values: np.ndarray, step: int, buckets: int
) -> dict[str, list[float | None]]:
    """The lowest and highest raw sample each drawn point stands for.

    Drawn as a band behind the line, this is what makes a decimated record safe
    to read: a spike that fell between two plotted samples is still visible as
    the band reaching up to it.
    """
    if step <= 1:
        return {"low": _jsonable(values), "high": _jsonable(values)}
    padded = np.full(buckets * step, np.nan)
    padded[: len(values)] = values
    blocks = padded.reshape(buckets, step)
    import warnings

    with warnings.catch_warnings():
        # A bucket the instrument reported nothing in is an expected gap, not a
        # fault worth a line of console output per channel per export.
        warnings.simplefilter("ignore", RuntimeWarning)
        low = np.nanmin(blocks, axis=1)
        high = np.nanmax(blocks, axis=1)
    return {"low": _jsonable(low), "high": _jsonable(high)}


def wrap_degrees(value: float) -> float:
    """A bearing in [0, 360), with north reported as 0 rather than 360.

    The mean of 359 and 1 comes out of arctan2 as a hair under zero, and a
    plain modulo turns that into 360.0 - which is not a bearing, sorts after
    every other direction, and reads as a different number from the north it is.
    """
    wrapped = float(value) % 360.0
    return 0.0 if wrapped >= 360.0 - 1e-9 else wrapped


def circular_mean(degrees: np.ndarray) -> float:
    finite = degrees[np.isfinite(degrees)]
    if not len(finite):
        return float("nan")
    radians = np.deg2rad(finite)
    return wrap_degrees(np.degrees(np.arctan2(np.sin(radians).mean(),
                                              np.cos(radians).mean())))


def windrose(directions: np.ndarray, speeds: np.ndarray) -> dict[str, Any]:
    """Sixteen sectors by speed band, as a fraction of the samples in the region.

    The counts are what a rose is read from; the fractions are what makes two
    regions of different length comparable.
    """
    finite = np.isfinite(directions) & np.isfinite(speeds)
    directions, speeds = directions[finite], speeds[finite]
    width = 360.0 / WINDROSE_SECTORS
    petals = []
    for sector in range(WINDROSE_SECTORS):
        centre = sector * width
        # Centred on the compass point, so the north petal spans 348.75 to 11.25
        # rather than starting at north.
        offset = (directions - centre + 180.0) % 360.0 - 180.0
        inside = np.abs(offset) <= width / 2.0
        bands = []
        for index, low in enumerate(WINDROSE_SPEED_EDGES):
            high = (
                WINDROSE_SPEED_EDGES[index + 1]
                if index + 1 < len(WINDROSE_SPEED_EDGES) else math.inf
            )
            band = inside & (speeds >= low) & (speeds < high)
            bands.append({
                "from": low,
                "to": None if math.isinf(high) else high,
                "count": int(band.sum()),
            })
        petals.append({
            "centre_deg": centre,
            "label": compass_label(centre),
            "count": int(inside.sum()),
            "bands": bands,
        })
    total = int(len(directions))
    for petal in petals:
        petal["fraction"] = petal["count"] / total if total else 0.0
    return {
        "sectors": WINDROSE_SECTORS,
        "speed_edges": list(WINDROSE_SPEED_EDGES),
        "petals": petals,
        "samples": total,
        "convention": "Direction the wind is coming from",
    }


COMPASS = ("N", "NNE", "NE", "ENE", "E", "ESE", "SE", "SSE",
           "S", "SSW", "SW", "WSW", "W", "WNW", "NW", "NNW")


def compass_label(degrees: float) -> str:
    return COMPASS[int(round(degrees / 22.5)) % 16]


def _statistic(values: np.ndarray, circular: bool = False) -> dict[str, Any]:
    finite = values[np.isfinite(values)]
    if not len(finite):
        return {"available": False}
    if circular:
        mean = circular_mean(finite)
        return {
            "available": True, "mean": mean, "label": compass_label(mean),
            "samples": int(len(finite)),
        }
    return {
        "available": True,
        "mean": float(np.mean(finite)),
        "median": float(np.median(finite)),
        "minimum": float(np.min(finite)),
        "maximum": float(np.max(finite)),
        "samples": int(len(finite)),
    }


def investigate_region(
    frame: pd.DataFrame, region: Region
) -> dict[str, Any]:
    """Everything the page shows below the gas rows, over one chosen interval.

    Computed on the raw record inside the region, never on the drawn curve.
    """
    inside = frame[
        (frame["timestamp"] >= region.start) & (frame["timestamp"] <= region.end)
    ]
    if inside.empty:
        raise SourceInvestigationError(
            "The selected region holds no MIRO samples."
        )
    directions = None
    derived = False
    if "wind_dir_deg" in inside.columns:
        directions = pd.to_numeric(inside["wind_dir_deg"], errors="coerce")
        if not directions.notna().any():
            directions = None
    if directions is None:
        computed = wind_direction_from_components(inside)
        if computed is not None:
            directions, derived = computed, True

    speeds = (
        pd.to_numeric(inside["wind_mps"], errors="coerce")
        if "wind_mps" in inside.columns else None
    )
    rose = None
    if directions is not None and speeds is not None:
        rose = windrose(directions.to_numpy(float), speeds.to_numpy(float))
        rose["derived_direction"] = derived

    track = (
        pd.to_numeric(inside["heading_deg"], errors="coerce").to_numpy(float)
        if "heading_deg" in inside.columns else np.array([])
    )
    statistics = {
        "wind_direction": (
            _statistic(directions.to_numpy(float), circular=True)
            if directions is not None else {"available": False}
        ),
        "wind_speed": _statistic(
            speeds.to_numpy(float) if speeds is not None else np.array([])
        ),
        "track": _statistic(track, circular=True),
        "ground_speed": _statistic(
            pd.to_numeric(inside["ground_speed_mps"], errors="coerce").to_numpy(float)
            if "ground_speed_mps" in inside.columns else np.array([])
        ),
        "altitude": _statistic(
            pd.to_numeric(inside["altitude"], errors="coerce").to_numpy(float)
            if "altitude" in inside.columns else np.array([])
        ),
    }
    enhancements = {}
    for name in GAS_CHANNELS:
        if name not in inside.columns:
            continue
        values = pd.to_numeric(inside[name], errors="coerce").to_numpy(float)
        outside = pd.to_numeric(frame[name], errors="coerce").to_numpy(float)
        summary = _statistic(values)
        if summary["available"]:
            background = np.nanpercentile(outside[np.isfinite(outside)], 10) \
                if np.isfinite(outside).any() else float("nan")
            # Against the flight's own tenth percentile, which is the closest
            # thing to a background this page can state without a second site.
            summary["background"] = _finite_or_none(background)
            summary["enhancement"] = _finite_or_none(summary["maximum"] - background)
            summary["unit"] = CHANNEL_UNITS.get(name, "")
            summary["label"] = CHANNEL_LABELS.get(name, name)
        enhancements[name] = summary
    return {
        "region": {
            "start": region.start.isoformat(),
            "end": region.end.isoformat(),
            "seconds": float((region.end - region.start).total_seconds()),
            "samples": int(len(inside)),
        },
        "windrose": rose,
        "statistics": statistics,
        "enhancements": enhancements,
        "note": "Every value is computed from the raw record inside the "
                "region. Smoothing is applied to the drawn curve only.",
    }


def _finite_or_none(value: float) -> float | None:
    return float(value) if np.isfinite(value) else None


def region_track(
    navigation: pd.DataFrame | None, region: Region
) -> dict[str, Any]:
    """The flight track, with the region marked on it.

    The whole track is returned rather than the region alone: which leg the
    feature was on, and whether the Zeppelin passed the same ground earlier
    without seeing it, is most of what places a source.
    """
    if navigation is None or not len(navigation):
        return {"available": False,
                "reason": "No processed Noseboom navigation is available."}
    if not {"lat", "lon", "timestamp"}.issubset(navigation.columns):
        return {"available": False,
                "reason": "The navigation record carries no positions."}
    nav = navigation.dropna(subset=["lat", "lon", "timestamp"]).copy()
    nav["timestamp"] = pd.to_datetime(nav["timestamp"], errors="coerce")
    nav = nav.dropna(subset=["timestamp"]).sort_values("timestamp")
    if nav.empty:
        return {"available": False, "reason": "No finite positions."}
    inside = (nav["timestamp"] >= region.start) & (nav["timestamp"] <= region.end)
    step = _decimate(len(nav), 4000)
    shown = nav.iloc[::step]
    marked = nav[inside]
    if marked.empty:
        return {
            "available": False,
            "reason": "The selected region falls outside the navigation record, "
                      "so the position at that moment is not known.",
        }
    return {
        "available": True,
        "track": [
            {"lat": float(row.lat), "lon": float(row.lon)}
            for row in shown.itertuples()
        ],
        "region": [
            {"lat": float(row.lat), "lon": float(row.lon),
             "time": row.timestamp.isoformat()}
            for row in marked.iloc[::_decimate(len(marked), 2000)].itertuples()
        ],
        "bounds": {
            "north": float(marked["lat"].max()), "south": float(marked["lat"].min()),
            "east": float(marked["lon"].max()), "west": float(marked["lon"].min()),
        },
        "points": int(len(marked)),
    }
