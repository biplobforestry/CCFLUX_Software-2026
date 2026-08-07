"""Attribute trace-gas variation to instrument state, altitude and time.

Flight_CC0806 is the reason this exists. MIRO CO2 correlated with the Picarro at
R2 = 0.05 while H2O, through the identical readers and clock, reached 0.996 - so
the disagreement was never in the software. The MIRO minus Picarro difference
tracked the MIRO cell temperature at +0.959 and +6.2 ppm per degree, and the
cell warmed 7.7 degrees over the flight: about 48 ppm of drift laid over an
atmospheric signal whose standard deviation was 3.45 ppm.

Finding that took a day of scripting. This module makes it a page.

Two design decisions are worth stating, because they are what separates this
from a wall of scatter plots:

* **Altitude and temperature covary.** A Zeppelin climbs as the day warms, so a
  single regression of a gas on cell temperature also carries the vertical
  gradient, and one on altitude carries the drift. Every driver is therefore
  reported twice: alone, and as a partial coefficient from a joint fit against
  all available drivers at once. Where the two disagree, the simple slope is
  confounded, and the page says so.
* **Species without a reference are still diagnosable.** Only CO2, CH4 and H2O
  have a Picarro to compare against. The rest - CO, N2O, NO, NO2, SO2, NH3, O3 -
  cannot be scored for accuracy, but their dependence on instrument state is
  measurable regardless, and a species that follows the cell temperature is
  suspect whether or not a second analyser is there to prove it.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any, Mapping, Sequence

import numpy as np
import pandas as pd

# Averaging windows offered on the page. Sensitivities are estimated on averaged
# samples, because instrument noise at the native rate inflates the scatter
# without adding information about a drift that takes hours to develop.
RESOLUTIONS = (1, 10, 60, 300)
DEFAULT_RESOLUTION = 60
# More points than this and a browser scatter stops being readable before it
# stops being fast.
MAXIMUM_SERIES_POINTS = 4000

REFERENCE_GASES = {"CO2 wet": "CO2", "CH4 wet": "CH4", "H2O wet": "H2O"}

DRIVER_LABELS = {
    "T Cell C": "Cell temperature",
    "Outside T": "Outside temperature",
    "Laser housing T": "Laser housing temperature",
    "p Cell": "Cell pressure",
    "altitude": "Altitude",
    "elapsed_h": "Time since start",
}
DRIVER_UNITS = {
    "T Cell C": "degC", "Outside T": "degC", "Laser housing T": "degC",
    "p Cell": "mbar", "altitude": "m", "elapsed_h": "h",
}
INSTRUMENT_DRIVERS = ("T Cell C", "Outside T", "Laser housing T", "p Cell")


def _finite(*arrays: np.ndarray) -> np.ndarray:
    mask = np.ones(len(arrays[0]), dtype=bool)
    for array in arrays:
        mask &= np.isfinite(array)
    return mask


@dataclass(frozen=True, slots=True)
class Fit:
    """One ordinary least-squares line, with what is needed to judge it."""

    slope: float
    intercept: float
    r_squared: float
    standard_error: float
    samples: int

    @property
    def t_statistic(self) -> float:
        if not self.standard_error or not math.isfinite(self.standard_error):
            return float("nan")
        return self.slope / self.standard_error

    def as_dict(self) -> dict[str, float | int]:
        return {
            "slope": _clean(self.slope),
            "intercept": _clean(self.intercept),
            "r_squared": _clean(self.r_squared),
            "standard_error": _clean(self.standard_error),
            "t_statistic": _clean(self.t_statistic),
            "samples": int(self.samples),
        }


def _clean(value: float) -> float | None:
    """JSON has no NaN; a missing statistic is null, not a silent zero."""
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def straight_line(x: np.ndarray, y: np.ndarray) -> Fit:
    """Least-squares y on x, with the standard error of the slope."""
    mask = _finite(x, y)
    x, y = x[mask], y[mask]
    count = len(x)
    if count < 3:
        return Fit(float("nan"), float("nan"), float("nan"), float("nan"), count)
    spread = float(np.sum((x - x.mean()) ** 2))
    if spread <= 0:
        return Fit(float("nan"), float("nan"), float("nan"), float("nan"), count)
    slope, intercept = np.polyfit(x, y, 1)
    residual = y - (slope * x + intercept)
    residual_sum = float(np.sum(residual ** 2))
    total = float(np.sum((y - y.mean()) ** 2))
    r_squared = 1.0 - residual_sum / total if total > 0 else float("nan")
    standard_error = float("nan")
    if count > 2:
        standard_error = math.sqrt(residual_sum / (count - 2) / spread)
    return Fit(float(slope), float(intercept), r_squared, standard_error, count)


# Condition number of the scaled design above which the drivers are so nearly
# the same line that the joint fit can move weight between them freely: the
# individual coefficients stop meaning anything even though the fit as a whole
# is sound. It happens easily here, because a cell that warms steadily through a
# flight is very nearly a clock. A hundred is the usual threshold for severe
# collinearity, and it matches what these records do: a design at 93, with cell
# temperature and time correlated at 0.94, still recovered a planted 6.0 ppm/degC
# to four decimal places, while an exactly dependent one goes straight to 1e15.
COLLINEARITY_LIMIT = 100.0


def joint_fit(
    drivers: Mapping[str, np.ndarray], y: np.ndarray
) -> tuple[dict[str, Fit], float, float]:
    """Partial coefficients from one fit against every driver at once.

    A Zeppelin climbs as the day warms, so a gas regressed on cell temperature
    alone also carries the vertical gradient. Solving for all drivers together
    gives each one's effect holding the others fixed, which is the number that
    says whether a drift is thermal or atmospheric.

    Returns the coefficients, the fit's R2, and the condition number of the
    scaled design. The last one is not decoration: a cell that warms steadily
    is nearly a linear function of time, and where that is so the split between
    the two is arbitrary and must not be read as physics.
    """
    # A driver that never moves carries no information and, once centred, is a
    # column of zeros that makes the whole design singular. The MIRO holds its
    # cell at a fixed pressure, so this is the ordinary case, not a rare one:
    # left in, it marked every joint fit unusable.
    names = [
        name for name in drivers
        if np.isfinite(drivers[name]).any()
        and float(np.nanstd(drivers[name])) > 0.0
    ]
    if not names:
        return {}, float("nan"), float("nan")
    columns = [drivers[name] for name in names]
    mask = _finite(y, *columns)
    if int(mask.sum()) < len(names) + 3:
        return {}, float("nan"), float("nan")
    design = np.column_stack([column[mask] for column in columns] + [np.ones(int(mask.sum()))])
    target = y[mask]
    # Centre and scale so a metre of altitude and a degree of temperature are
    # not compared at wildly different magnitudes in the same normal equations.
    centre = design[:, :-1].mean(axis=0)
    scale = design[:, :-1].std(axis=0)
    scale[scale == 0] = 1.0
    scaled = np.column_stack([(design[:, :-1] - centre) / scale, design[:, -1]])
    try:
        condition = float(np.linalg.cond(scaled[:, :-1]))
    except np.linalg.LinAlgError:
        condition = float("inf")
    try:
        solution, *_ = np.linalg.lstsq(scaled, target, rcond=None)
    except np.linalg.LinAlgError:
        return {}, float("nan"), condition
    predicted = scaled @ solution
    residual = target - predicted
    total = float(np.sum((target - target.mean()) ** 2))
    r_squared = 1.0 - float(np.sum(residual ** 2)) / total if total > 0 else float("nan")
    degrees = len(target) - len(names) - 1
    variance = float(np.sum(residual ** 2)) / degrees if degrees > 0 else float("nan")
    try:
        covariance = np.linalg.pinv(scaled.T @ scaled) * variance
        errors = np.sqrt(np.clip(np.diag(covariance), 0.0, None))
    except np.linalg.LinAlgError:
        errors = np.full(len(names) + 1, float("nan"))
    results: dict[str, Fit] = {}
    for index, name in enumerate(names):
        # Undo the scaling so the coefficient is per real unit again.
        results[name] = Fit(
            slope=float(solution[index] / scale[index]),
            intercept=float("nan"),
            r_squared=float("nan"),
            standard_error=float(errors[index] / scale[index]),
            samples=int(mask.sum()),
        )
    return results, r_squared, condition


def agreement(reference: np.ndarray, measured: np.ndarray) -> dict[str, float | None]:
    """How a species compares against a second analyser measuring the same air."""
    mask = _finite(reference, measured)
    x, y = reference[mask], measured[mask]
    if len(x) < 3:
        return {"samples": int(len(x))}
    fit = straight_line(x, y)
    difference = y - x
    return {
        "samples": int(len(x)),
        "r_squared": _clean(fit.r_squared),
        "slope": _clean(fit.slope),
        "intercept": _clean(fit.intercept),
        "bias": _clean(float(np.mean(difference))),
        "bias_sd": _clean(float(np.std(difference, ddof=1))) if len(x) > 1 else None,
        "rmse": _clean(float(np.sqrt(np.mean(difference ** 2)))),
        "reference_sd": _clean(float(np.std(x, ddof=1))) if len(x) > 1 else None,
        "measured_sd": _clean(float(np.std(y, ddof=1))) if len(y) > 1 else None,
    }


@dataclass
class Filters:
    start: pd.Timestamp | None = None
    end: pd.Timestamp | None = None
    altitude_min: float | None = None
    altitude_max: float | None = None
    resolution_seconds: int = DEFAULT_RESOLUTION
    stable_ambient_only: bool = True
    applied: list[str] = field(default_factory=list)


def parse_filters(request: Mapping[str, Any] | None) -> Filters:
    request = dict(request or {})
    resolution = int(request.get("resolution_seconds") or DEFAULT_RESOLUTION)
    if resolution not in RESOLUTIONS:
        raise ValueError(
            "Averaging window must be one of: "
            + ", ".join(f"{value} s" for value in RESOLUTIONS)
        )

    def moment(key: str) -> pd.Timestamp | None:
        raw = request.get(key)
        if raw in (None, ""):
            return None
        stamp = pd.to_datetime(raw, errors="coerce")
        if pd.isna(stamp):
            raise ValueError(f"{key} is not a readable date and time: {raw!r}")
        return stamp.tz_localize(None) if stamp.tzinfo is not None else stamp

    def number(key: str) -> float | None:
        raw = request.get(key)
        if raw in (None, ""):
            return None
        try:
            return float(raw)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"{key} is not a number: {raw!r}") from exc

    filters = Filters(
        start=moment("start"), end=moment("end"),
        altitude_min=number("altitude_min"), altitude_max=number("altitude_max"),
        resolution_seconds=resolution,
        stable_ambient_only=bool(request.get("stable_ambient_only", True)),
    )
    if filters.start is not None and filters.end is not None and filters.start > filters.end:
        raise ValueError("The start of the window is after its end.")
    if (
        filters.altitude_min is not None
        and filters.altitude_max is not None
        and filters.altitude_min > filters.altitude_max
    ):
        raise ValueError("The lowest altitude is above the highest.")
    return filters


def _averaged(frame: pd.DataFrame, seconds: int) -> pd.DataFrame:
    if frame.empty:
        return frame
    indexed = frame.set_index("timestamp").sort_index()
    numeric = indexed.select_dtypes(include=[np.number])
    if seconds <= 1:
        return numeric
    return numeric.resample(f"{seconds}s").mean()


def build_frame(
    miro_data: pd.DataFrame,
    picarro_data: pd.DataFrame | None,
    navigation: pd.DataFrame | None,
    filters: Filters,
    *,
    miro_module,
    picarro_module,
) -> tuple[pd.DataFrame, dict[str, Any]]:
    """One table: every MIRO species, the instrument state, altitude, references."""
    notes: list[str] = []
    frame = miro_data
    if filters.stable_ambient_only:
        prepared, *_ = miro_module._stable_ambient_frame(miro_data, 30.0)
        frame = prepared.loc[prepared["valid_ambient"]]
        notes.append(
            "Zeroing cycles and the 30 s after each are excluded; MIRO's "
            "valve-0 output is already background-corrected and is not "
            "zero-subtracted again."
        )
    gases = [
        column for column in miro_module.GAS_COLUMNS
        if column in frame and frame[column].notna().any()
    ]
    housekeeping = [
        column for column in miro_module.HOUSEKEEPING_COLUMNS if column in frame
    ]
    if not housekeeping:
        notes.append(
            "This MIRO delivery carries no cell temperature or pressure "
            "columns, so instrument-state dependence cannot be evaluated."
        )
    keep = ["timestamp", *gases, *housekeeping]
    miro_frame = _averaged(frame[keep], filters.resolution_seconds)
    # Report every species in the unit the rest of the workspace uses.
    units: dict[str, str] = {}
    for gas in gases:
        unit, scale = miro_module.gas_unit_scale(gas)
        miro_frame[gas] = miro_frame[gas] * scale
        units[gas] = unit
    joined = miro_frame

    if picarro_data is not None and not picarro_data.empty:
        reference_columns = {}
        for gas, short in REFERENCE_GASES.items():
            column = f"{short}_sync"
            if column in picarro_data:
                reference_columns[column] = f"ref::{gas}"
        if reference_columns:
            reference = _averaged(
                picarro_data[["timestamp", *reference_columns]], filters.resolution_seconds
            ).rename(columns=reference_columns)
            joined = joined.join(reference, how="outer")
            notes.append(
                "Picarro provides a reference for CO2, CH4 and H2O only; the "
                "remaining species are evaluated against instrument state alone."
            )
    else:
        notes.append("No Picarro data is loaded, so no species has a reference.")

    if navigation is not None and not navigation.empty and "altitude" in navigation:
        altitude = _averaged(
            navigation[["timestamp", "altitude"]], filters.resolution_seconds
        )
        joined = joined.join(altitude, how="left")
    else:
        notes.append(
            "No Noseboom altitude is available, so altitude dependence and the "
            "altitude filter are unavailable."
        )

    joined = joined.sort_index()
    # An averaging window that caught only a zeroing cycle produces a row of
    # NaN. Counting those as samples overstated the record and made the stable
    # ambient filter look as though it had changed nothing.
    measured = [column for column in joined.columns if column in gases]
    if measured:
        joined = joined.dropna(subset=measured, how="all")
    if filters.start is not None:
        joined = joined.loc[joined.index >= filters.start]
    if filters.end is not None:
        joined = joined.loc[joined.index <= filters.end]
    if "altitude" in joined:
        if filters.altitude_min is not None:
            joined = joined.loc[joined["altitude"] >= filters.altitude_min]
        if filters.altitude_max is not None:
            joined = joined.loc[joined["altitude"] <= filters.altitude_max]
    if joined.empty:
        raise ValueError("No samples remain after the filters that were applied.")
    elapsed = (joined.index - joined.index[0]).total_seconds() / 3600.0
    joined["elapsed_h"] = np.asarray(elapsed, dtype=float)
    return joined, {"gases": gases, "units": units, "notes": notes,
                    "housekeeping": housekeeping}


def investigate(
    miro_data: pd.DataFrame,
    picarro_data: pd.DataFrame | None,
    navigation: pd.DataFrame | None,
    filters: Filters,
    *,
    miro_module,
    picarro_module,
) -> dict[str, Any]:
    joined, context = build_frame(
        miro_data, picarro_data, navigation, filters,
        miro_module=miro_module, picarro_module=picarro_module,
    )
    drivers = [
        name for name in (*INSTRUMENT_DRIVERS, "altitude", "elapsed_h")
        if name in joined and joined[name].notna().any()
    ]
    driver_values = {name: joined[name].to_numpy(float) for name in drivers}

    species: list[dict[str, Any]] = []
    for gas in context["gases"]:
        values = joined[gas].to_numpy(float)
        finite = values[np.isfinite(values)]
        if len(finite) < 3:
            continue
        mean = float(np.mean(finite))
        entry: dict[str, Any] = {
            "name": gas,
            "label": str(gas).replace(" wet", ""),
            "unit": context["units"].get(gas, ""),
            "samples": int(len(finite)),
            "mean": _clean(mean),
            "sd": _clean(float(np.std(finite, ddof=1))) if len(finite) > 1 else None,
            "minimum": _clean(float(np.min(finite))),
            "maximum": _clean(float(np.max(finite))),
            "drivers": {},
        }
        partial, joint_r2, condition = joint_fit(driver_values, values)
        entry["joint_r_squared"] = _clean(joint_r2)
        entry["driver_condition_number"] = _clean(condition)
        # With a near-singular design the fit is still sound but the split
        # between drivers is not, so the partial columns are marked rather than
        # quietly believed.
        reliable = bool(math.isfinite(condition) and condition <= COLLINEARITY_LIMIT)
        entry["partial_reliable"] = reliable
        for name in drivers:
            fit = straight_line(driver_values[name], values)
            record = fit.as_dict()
            record["label"] = DRIVER_LABELS.get(name, name)
            record["driver_unit"] = DRIVER_UNITS.get(name, "")
            # The number a reader actually wants: how much of the species, in
            # percent of its own mean, one unit of the driver moves.
            record["percent_per_unit"] = (
                _clean(fit.slope / mean * 100.0) if mean else None
            )
            if name in partial:
                record["partial_slope"] = _clean(partial[name].slope)
                record["partial_standard_error"] = _clean(partial[name].standard_error)
                record["partial_percent_per_unit"] = (
                    _clean(partial[name].slope / mean * 100.0) if mean else None
                )
                record["partial_reliable"] = reliable
                # A simple slope that reverses or halves once the other drivers
                # are held fixed was measuring them, not this one. Only claimed
                # where the drivers are distinct enough for the joint fit to
                # apportion them at all.
                simple, joint_slope = fit.slope, partial[name].slope
                record["confounded"] = bool(
                    reliable and math.isfinite(simple) and math.isfinite(joint_slope)
                    and (simple == 0 or abs(joint_slope - simple) > 0.5 * abs(simple))
                )
            entry["drivers"][name] = record

        reference_column = f"ref::{gas}"
        if reference_column in joined:
            reference_values = joined[reference_column].to_numpy(float)
            entry["reference"] = agreement(reference_values, values)
            entry["reference_label"] = f"Picarro {REFERENCE_GASES[gas]}"
            # The disagreement, not the species, is what a drift shows up in.
            # Regressing the species itself against cell temperature also
            # removes the real covariance - the cell warms through the day and
            # so does the air - and on H2O that turned an honest R2 of 0.996
            # into 0.597 by deleting signal. The difference carries only what
            # the two analysers disagree about, which is the drift.
            difference = values - reference_values
            entry["difference_drivers"] = {}
            for name in drivers:
                fit = straight_line(driver_values[name], difference)
                record = fit.as_dict()
                record["label"] = DRIVER_LABELS.get(name, name)
                record["driver_unit"] = DRIVER_UNITS.get(name, "")
                record["percent_per_unit"] = (
                    _clean(fit.slope / mean * 100.0) if mean else None
                )
                entry["difference_drivers"][name] = record
            best = max(
                (name for name in drivers if name in INSTRUMENT_DRIVERS),
                key=lambda name: entry["difference_drivers"][name].get("r_squared") or 0.0,
                default=None,
            )
            if best is not None:
                fit = straight_line(driver_values[best], difference)
                if math.isfinite(fit.slope):
                    corrected = values - (fit.slope * driver_values[best] + fit.intercept)
                    entry["reference_detrended"] = agreement(reference_values, corrected)
                    entry["reference_detrended"]["driver"] = best
                    entry["reference_detrended"]["driver_label"] = DRIVER_LABELS.get(
                        best, best
                    )
                    # Both halves of the fit, so a plot can reproduce exactly
                    # what was scored here. The intercept carries the constant
                    # offset between the two analysers, and removing it is what
                    # puts the corrected cloud on the 1:1 line instead of
                    # leaving it parked at the old bias.
                    entry["reference_detrended"]["slope_per_unit"] = _clean(fit.slope)
                    entry["reference_detrended"]["intercept"] = _clean(fit.intercept)
                    entry["reference_detrended"]["driver_r_squared"] = _clean(
                        fit.r_squared
                    )
        species.append(entry)

    notes = list(context["notes"])
    if species and not any(entry.get("partial_reliable") for entry in species):
        notes.append(
            "The recorded drivers are too nearly the same line over this window "
            "for a joint fit to apportion them - a cell that warms steadily is "
            "close to a clock - so the partial columns are not reported as "
            "reliable. Narrowing the window, or filtering to one altitude band, "
            "separates them."
        )
    return {
        "species": species,
        "drivers": [
            {"name": name, "label": DRIVER_LABELS.get(name, name),
             "unit": DRIVER_UNITS.get(name, "")}
            for name in drivers
        ],
        "series": _series_payload(joined, context, drivers),
        "window": {
            "start": joined.index[0].isoformat(),
            "end": joined.index[-1].isoformat(),
            "samples": int(len(joined)),
            "resolution_seconds": filters.resolution_seconds,
            "stable_ambient_only": filters.stable_ambient_only,
            "altitude_min": _clean(
                float(joined["altitude"].min()) if "altitude" in joined else float("nan")
            ),
            "altitude_max": _clean(
                float(joined["altitude"].max()) if "altitude" in joined else float("nan")
            ),
        },
        "notes": notes,
    }


def _series_payload(
    joined: pd.DataFrame, context: Mapping[str, Any], drivers: Sequence[str]
) -> dict[str, Any]:
    step = max(1, math.ceil(len(joined) / MAXIMUM_SERIES_POINTS))
    sampled = joined.iloc[::step]
    payload: dict[str, Any] = {
        "time": [stamp.isoformat() for stamp in sampled.index],
        "decimation": step,
    }
    for column in sampled.columns:
        values = pd.to_numeric(sampled[column], errors="coerce")
        payload[str(column)] = [
            None if not math.isfinite(value) else round(float(value), 6)
            for value in values.to_numpy(float)
        ]
    return payload
