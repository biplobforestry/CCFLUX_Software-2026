#!/usr/bin/env python
"""Scientifically traceable quicklook for Partector 2 Pro Influx CSV exports.

The script:
  * normalizes the duplicated Influx ``*_1``/``*_raw_value`` columns;
  * detects separate instrument sessions and removes logger-level duplicates;
  * applies transparent instrument/housekeeping quality-control flags;
  * integrates the eight dN/dlog10(D) channels in logarithmic diameter space;
  * makes a flight-oriented overview, size-distribution plot, QC table, and
    machine-readable summary.

This is descriptive quicklook analysis, not source apportionment.
"""

from __future__ import annotations

import argparse
import json
import math
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

import matplotlib

matplotlib.use("Agg")
import matplotlib.dates as mdates
import matplotlib.pyplot as plt
from matplotlib.colors import LogNorm
import numpy as np
import pandas as pd


SIZE_COLUMN_RE = re.compile(r"^n(?P<diameter>\d+(?:\.\d+)?)$", re.IGNORECASE)


@dataclass(frozen=True)
class QCConfig:
    """QC limits from the Naneos manual or conservative plausibility checks."""

    flow_min_lpm: float = 0.45
    flow_max_lpm: float = 0.55
    temperature_min_c: float = 0.0
    temperature_max_c: float = 40.0
    rh_min_percent: float = 10.0
    rh_max_percent: float = 90.0
    pressure_min_hpa: float = 700.0
    pressure_max_hpa: float = 1100.0
    number_min_cm3: float = 0.0
    number_max_cm3: float = 1.0e6
    diameter_min_nm: float = 10.0
    diameter_max_nm: float = 300.0
    ldsa_min_um2_cm3: float = 0.0
    ldsa_max_um2_cm3: float = 12000.0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Create a QC-aware Partector 2 Pro post-flight quicklook."
    )
    parser.add_argument("csv", type=Path, help="Influx-exported partector.csv")
    parser.add_argument(
        "-o",
        "--output-dir",
        type=Path,
        default=Path("partector_quicklook"),
        help="Output directory (default: ./partector_quicklook)",
    )
    parser.add_argument(
        "--session",
        default="longest",
        help=(
            "Session to plot: longest (default), latest, all, or a zero-based "
            "session number from sessions.csv"
        ),
    )
    parser.add_argument(
        "--session-gap-minutes",
        type=float,
        default=10.0,
        help="Timestamp gap that starts a new session (default: 10)",
    )
    parser.add_argument(
        "--trim-start-minutes",
        type=float,
        default=0.0,
        help="Explicitly exclude this many minutes at each session start",
    )
    parser.add_argument(
        "--flow-range",
        nargs=2,
        type=float,
        metavar=("MIN", "MAX"),
        default=(0.45, 0.55),
        help="Accepted flow range in L/min (default: 0.45 0.55)",
    )
    parser.add_argument(
        "--no-pressure-altitude",
        action="store_true",
        help="Do not show pressure-derived relative altitude.",
    )
    return parser


def canonicalize_columns(raw: pd.DataFrame) -> pd.DataFrame:
    """Select processed Influx fields and give them stable canonical names."""

    result: dict[str, pd.Series] = {}
    result["_time"] = raw["_time"] if "_time" in raw else pd.Series(dtype=object)

    for column in raw.columns:
        if column == "_time" or column.endswith("_raw_value"):
            continue
        canonical = column[:-2] if column.endswith("_1") else column
        # Processed ``*_1`` fields take precedence over unsuffixed fields.
        if canonical not in result or column.endswith("_1"):
            result[canonical] = raw[column]

    frame = pd.DataFrame(result)
    aliases = {
        # Influx appends ``_1`` to the source field ``time_``.
        "time_": "instrument_time_s",
        "number": "number_cm3",
        "diam": "mean_diameter_nm",
        "LDSA": "ldsa_um2_cm3",
        "surface": "surface_um2_cm3",
        "mass": "mass_ug_m3",
        "flow": "flow_lpm",
        "RH": "rh_percent",
        "T": "temperature_c",
        "P": "pressure_hpa",
        "bat": "battery_v",
        "Ipump": "pump_current_ma",
    }
    frame = frame.rename(columns=aliases)
    # Preserve the wall-clock value exactly as written in the CSV. The
    # trailing ISO timezone marker is removed without converting the clock.
    recorded_time = frame["_time"].astype("string").str.replace(
        r"(?:Z|[+-]\d{2}:\d{2})$", "", regex=True
    )
    frame["_time"] = pd.to_datetime(recorded_time, errors="coerce")
    for column in frame.columns:
        if column != "_time":
            frame[column] = pd.to_numeric(frame[column], errors="coerce")
    return frame


def require_columns(frame: pd.DataFrame, required: Iterable[str]) -> None:
    missing = sorted(set(required) - set(frame.columns))
    if missing:
        raise ValueError("Required columns are missing: " + ", ".join(missing))


def find_size_columns(frame: pd.DataFrame) -> tuple[list[str], np.ndarray]:
    pairs: list[tuple[float, str]] = []
    for column in frame.columns:
        match = SIZE_COLUMN_RE.match(column)
        if match:
            pairs.append((float(match.group("diameter")), column))
    pairs.sort()
    if len(pairs) != 8:
        raise ValueError(
            f"Expected 8 Partector Pro size channels, found {len(pairs)}: "
            + ", ".join(name for _, name in pairs)
        )
    return [name for _, name in pairs], np.asarray([d for d, _ in pairs])


def assign_sessions(frame: pd.DataFrame, gap_minutes: float) -> pd.DataFrame:
    """Segment before sorting so instrument-clock resets remain detectable."""

    data = frame.loc[frame["_time"].notna()].copy()
    data["_source_row"] = data.index
    data = data.sort_values("_time", kind="stable").reset_index(drop=True)
    timestamp_gap = data["_time"].diff().dt.total_seconds()
    instrument_reset = data["instrument_time_s"].diff() < 0
    new_session = (
        timestamp_gap.isna()
        | (timestamp_gap > gap_minutes * 60.0)
        | instrument_reset.fillna(False)
    )
    data["session"] = new_session.cumsum().astype(int) - 1
    data["logger_duplicate"] = data.duplicated(
        subset=["session", "instrument_time_s"], keep="last"
    )
    return data


def apply_qc(
    data: pd.DataFrame,
    size_columns: list[str],
    config: QCConfig,
    trim_start_minutes: float,
) -> pd.DataFrame:
    """Create independent QC flags and a combined validity flag."""

    d = data.copy()
    d["qc_timestamp"] = d["_time"].notna()
    d["qc_error"] = d["error"].eq(0)
    d["qc_flow"] = d["flow_lpm"].between(
        config.flow_min_lpm, config.flow_max_lpm, inclusive="both"
    )
    d["qc_temperature"] = d["temperature_c"].between(
        config.temperature_min_c, config.temperature_max_c, inclusive="both"
    )
    d["qc_rh"] = d["rh_percent"].between(
        config.rh_min_percent, config.rh_max_percent, inclusive="both"
    )
    d["qc_pressure"] = d["pressure_hpa"].between(
        config.pressure_min_hpa, config.pressure_max_hpa, inclusive="both"
    )
    d["qc_number"] = d["number_cm3"].between(
        config.number_min_cm3, config.number_max_cm3, inclusive="both"
    )
    d["qc_diameter"] = d["mean_diameter_nm"].between(
        config.diameter_min_nm, config.diameter_max_nm, inclusive="both"
    )
    d["qc_ldsa"] = d["ldsa_um2_cm3"].between(
        config.ldsa_min_um2_cm3, config.ldsa_max_um2_cm3, inclusive="both"
    )
    d["qc_size_distribution"] = (
        d[size_columns].notna().all(axis=1) & d[size_columns].ge(0).all(axis=1)
    )

    start = d.groupby("session")["_time"].transform("min")
    elapsed_min = (d["_time"] - start).dt.total_seconds() / 60.0
    d["qc_user_trim"] = elapsed_min >= trim_start_minutes
    d["qc_unique_instrument_record"] = ~d["logger_duplicate"]

    qc_columns = [
        "qc_timestamp",
        "qc_error",
        "qc_flow",
        "qc_temperature",
        "qc_rh",
        "qc_pressure",
        "qc_number",
        "qc_diameter",
        "qc_ldsa",
        "qc_size_distribution",
        "qc_user_trim",
        "qc_unique_instrument_record",
    ]
    d["qc_valid"] = d[qc_columns].all(axis=1)
    failed = []
    for _, row in d[qc_columns].iterrows():
        names = [name.removeprefix("qc_") for name in qc_columns if not bool(row[name])]
        failed.append(";".join(names))
    d["qc_failed"] = failed
    return d


def logarithmic_bin_edges(centers_nm: np.ndarray) -> np.ndarray:
    """Return bin edges midway between centers in log10 diameter."""

    log_centers = np.log10(centers_nm)
    edges = np.empty(len(centers_nm) + 1)
    edges[1:-1] = (log_centers[:-1] + log_centers[1:]) / 2.0
    edges[0] = log_centers[0] - (edges[1] - log_centers[0])
    edges[-1] = log_centers[-1] + (log_centers[-1] - edges[-2])
    return 10.0**edges


def integrate_size_distribution(
    data: pd.DataFrame,
    size_columns: list[str],
    centers_nm: np.ndarray,
) -> pd.DataFrame:
    """Integrate dN/dlog10(D) using logarithmic bin widths."""

    d = data.copy()
    edges = logarithmic_bin_edges(centers_nm)
    log_edges = np.log10(edges)
    widths = np.diff(log_edges)
    values = d[size_columns].to_numpy(float)
    d["n_8bin_cm3"] = np.nansum(values * widths[None, :], axis=1)

    bands = [(10.0, 30.0), (30.0, 50.0), (50.0, 100.0), (100.0, 300.0)]
    for low, high in bands:
        overlap = np.maximum(
            0.0,
            np.minimum(log_edges[1:], math.log10(high))
            - np.maximum(log_edges[:-1], math.log10(low)),
        )
        d[f"n_{int(low)}_{int(high)}_cm3"] = np.nansum(
            values * overlap[None, :], axis=1
        )

    denominator = d["number_cm3"].where(d["number_cm3"] > 0)
    d["size_integral_to_reported_ratio"] = d["n_8bin_cm3"] / denominator
    return d


def session_table(data: pd.DataFrame) -> pd.DataFrame:
    rows = []
    for session, group in data.groupby("session", sort=True):
        valid = group[group["qc_valid"]]
        rows.append(
            {
                "session": int(session),
                "start_time": group["_time"].min().isoformat(),
                "end_time": group["_time"].max().isoformat(),
                "duration_min": (
                    group["_time"].max() - group["_time"].min()
                ).total_seconds()
                / 60.0,
                "raw_rows": int(len(group)),
                "unique_instrument_rows": int((~group["logger_duplicate"]).sum()),
                "valid_rows": int(len(valid)),
                "valid_fraction_of_unique": (
                    float(len(valid) / max(1, (~group["logger_duplicate"]).sum()))
                ),
            }
        )
    return pd.DataFrame(rows)


def choose_sessions(table: pd.DataFrame, selector: str) -> list[int]:
    if table.empty:
        raise ValueError("No sessions were found")
    normalized = selector.lower()
    if normalized == "all":
        return table["session"].astype(int).tolist()
    eligible = table.loc[table["valid_rows"] > 0]
    if eligible.empty:
        raise ValueError("No session contains a valid record after QC")
    if normalized == "longest":
        row = eligible.sort_values(
            ["duration_min", "valid_rows"], ascending=False
        ).iloc[0]
        return [int(row["session"])]
    if normalized == "latest":
        return [int(eligible.sort_values("end_time").iloc[-1]["session"])]
    try:
        requested = int(selector)
    except ValueError as exc:
        raise ValueError(
            "--session must be longest, latest, all, or an integer"
        ) from exc
    if requested not in set(table["session"].astype(int)):
        raise ValueError(f"Session {requested} does not exist")
    return [requested]


def pressure_relative_altitude_m(pressure_hpa: pd.Series) -> pd.Series:
    """Standard-atmosphere relative height referenced to max session pressure."""

    reference = float(pressure_hpa.max())
    return 44330.0 * (1.0 - (pressure_hpa / reference) ** 0.1903)


def percentile_text(series: pd.Series) -> dict[str, float | None]:
    clean = series.replace([np.inf, -np.inf], np.nan).dropna()
    if clean.empty:
        return {"p16": None, "median": None, "p84": None}
    q = clean.quantile([0.16, 0.5, 0.84])
    return {"p16": float(q.loc[0.16]), "median": float(q.loc[0.5]), "p84": float(q.loc[0.84])}


def create_summary(
    raw_count: int,
    data: pd.DataFrame,
    selected: pd.DataFrame,
    sessions: pd.DataFrame,
    selected_ids: list[int],
    centers_nm: np.ndarray,
    config: QCConfig,
    args: argparse.Namespace,
) -> dict:
    valid = selected[selected["qc_valid"]]
    failure_counts = {
        column.removeprefix("qc_"): int((~selected[column]).sum())
        for column in selected.columns
        if column.startswith("qc_") and column not in {"qc_valid", "qc_failed"}
    }
    return {
        "input_file": str(args.csv.resolve()),
        "raw_rows": raw_count,
        "parsed_rows": int(len(data)),
        "selected_sessions": selected_ids,
        "selected_rows": int(len(selected)),
        "selected_valid_rows": int(len(valid)),
        "selected_qc_pass_fraction": float(len(valid) / max(1, len(selected))),
        "qc_failure_counts": failure_counts,
        "qc_limits": config.__dict__,
        "session_gap_minutes": args.session_gap_minutes,
        "trim_start_minutes": args.trim_start_minutes,
        "size_channel_centers_nm": centers_nm.tolist(),
        "integration_note": (
            "The eight channels are dN/dlog10(D). Integrals use log10 bin "
            "widths with edges at geometric midpoints; partial mode bins are "
            "allocated by logarithmic overlap."
        ),
        "metrics_valid_records": {
            "number_cm3": percentile_text(valid["number_cm3"]),
            "ldsa_um2_cm3": percentile_text(valid["ldsa_um2_cm3"]),
            "mean_diameter_nm": percentile_text(valid["mean_diameter_nm"]),
            "mass_ug_m3": percentile_text(valid["mass_ug_m3"]),
            "n_8bin_cm3": percentile_text(valid["n_8bin_cm3"]),
            "size_integral_to_reported_ratio": percentile_text(
                valid["size_integral_to_reported_ratio"]
            ),
        },
        "sessions": sessions.to_dict(orient="records"),
        "interpretation_limits": [
            "The quicklook is descriptive; it does not identify emission sources.",
            "N50-100 and N100-300 are size-range proxies, not measured CCN.",
            "Pressure-derived relative altitude is diagnostic, not aircraft altitude.",
            "Partector mass is PM0.3 and depends on density/morphology assumptions.",
            "Manual typical accuracies are about +/-30% for LDSA, number and diameter, and +/-50% for PM0.3 mass.",
        ],
    }


def format_time_axis(ax: plt.Axes) -> None:
    locator = mdates.AutoDateLocator(minticks=3, maxticks=8)
    formatter = mdates.ConciseDateFormatter(locator)
    ax.xaxis.set_major_locator(locator)
    ax.xaxis.set_major_formatter(formatter)


def finite_positive_limits(values: np.ndarray) -> tuple[float, float]:
    finite = values[np.isfinite(values) & (values > 0)]
    if finite.size == 0:
        return 1.0, 10.0
    low = max(float(np.nanpercentile(finite, 5)), np.finfo(float).tiny)
    high = float(np.nanpercentile(finite, 99))
    if high <= low:
        high = low * 10.0
    return low, high


def plot_quicklook(
    selected: pd.DataFrame,
    size_columns: list[str],
    centers_nm: np.ndarray,
    selected_ids: list[int],
    output_dir: Path,
    show_pressure_altitude: bool,
) -> None:
    valid = selected[selected["qc_valid"]].sort_values("_time").copy()
    if valid.empty:
        raise ValueError("No valid rows are available for plotting")

    plt.rcParams.update(
        {
            "font.size": 9,
            "axes.titlesize": 11,
            "axes.labelsize": 9,
            "legend.fontsize": 8,
            "figure.dpi": 130,
            "savefig.dpi": 180,
        }
    )
    figure = plt.figure(figsize=(13.2, 9.0), constrained_layout=True)
    grid = figure.add_gridspec(3, 2, height_ratios=[1.0, 1.15, 1.0])
    ax_number = figure.add_subplot(grid[0, 0])
    ax_ldsa = figure.add_subplot(grid[0, 1])
    ax_size = figure.add_subplot(grid[1, :])
    ax_modes = figure.add_subplot(grid[2, 0])
    ax_house = figure.add_subplot(grid[2, 1])

    time = valid["_time"]
    ax_number.plot(time, valid["number_cm3"], color="#165D8A", lw=1.0)
    ax_number.set_yscale("log")
    ax_number.set_ylabel(r"Number (# cm$^{-3}$)")
    ax_number.set_title("Particle number concentration")
    ax_number.grid(alpha=0.25)

    ax_ldsa.plot(time, valid["ldsa_um2_cm3"], color="#C35A18", lw=1.0, label="LDSA")
    ax_ldsa.set_ylabel(r"LDSA ($\mu$m$^2$ cm$^{-3}$)")
    ax_ldsa.set_title("Lung-deposited surface area")
    ax_ldsa.grid(alpha=0.25)
    diameter_axis = ax_ldsa.twinx()
    diameter_axis.plot(
        time,
        valid["mean_diameter_nm"],
        color="#4D4D4D",
        alpha=0.65,
        lw=0.8,
        label="Mean diameter",
    )
    diameter_axis.set_ylabel("Mean diameter (nm)")

    z = valid[size_columns].to_numpy(float).T
    low, high = finite_positive_limits(z)
    edges = logarithmic_bin_edges(centers_nm)
    time_edges = _time_edges(time)
    mesh = ax_size.pcolormesh(
        time_edges,
        edges,
        np.ma.masked_less_equal(z, 0),
        shading="flat",
        cmap="viridis",
        norm=LogNorm(vmin=low, vmax=high),
    )
    ax_size.set_yscale("log")
    ax_size.set_ylim(edges[0], edges[-1])
    ax_size.set_ylabel("Mobility-equivalent diameter (nm)")
    ax_size.set_title(r"Partector Pro size distribution, dN/dlog$_{10}$(D)")
    figure.colorbar(
        mesh, ax=ax_size, pad=0.01, label=r"dN/dlog$_{10}$(D) (# cm$^{-3}$)"
    )

    mode_specs = [
        ("n_10_30_cm3", "10-30 nm", "#482878"),
        ("n_30_50_cm3", "30-50 nm", "#3E8F8C"),
        ("n_50_100_cm3", "50-100 nm", "#7DB928"),
        ("n_100_300_cm3", "100-300 nm", "#ECA52C"),
    ]
    for column, label, color in mode_specs:
        ax_modes.plot(time, valid[column], label=label, lw=0.9, color=color)
    ax_modes.set_yscale("symlog", linthresh=1.0)
    ax_modes.set_ylabel(r"Integrated number (# cm$^{-3}$)")
    ax_modes.set_title("Log-bin integrated size bands")
    ax_modes.grid(alpha=0.25)
    ax_modes.legend(ncol=2)

    ax_house.plot(time, valid["flow_lpm"], color="#165D8A", lw=0.9, label="Flow (L/min)")
    ax_house.plot(
        time, valid["rh_percent"] / 100.0, color="#4B9F59", lw=0.9, label="RH / 100"
    )
    ax_house.set_ylabel("Flow; relative RH")
    ax_house.set_title("Housekeeping and pressure profile")
    ax_house.grid(alpha=0.25)
    house_right = ax_house.twinx()
    if show_pressure_altitude:
        altitude_parts = []
        for _, group in valid.groupby("session"):
            altitude_parts.append(
                pd.Series(
                    pressure_relative_altitude_m(group["pressure_hpa"]).to_numpy(),
                    index=group.index,
                )
            )
        altitude = pd.concat(altitude_parts).sort_index()
        house_right.plot(
            valid["_time"],
            altitude.reindex(valid.index),
            color="#7A4EAB",
            lw=0.9,
            label="Pressure-relative altitude",
        )
        house_right.set_ylabel("Relative pressure altitude (m)")
    else:
        house_right.plot(
            time, valid["pressure_hpa"], color="#7A4EAB", lw=0.9, label="Pressure"
        )
        house_right.set_ylabel("Pressure (hPa)")
    handles, labels = ax_house.get_legend_handles_labels()
    handles2, labels2 = house_right.get_legend_handles_labels()
    ax_house.legend(handles + handles2, labels + labels2, loc="best")

    for axis in [ax_number, ax_ldsa, ax_size, ax_modes, ax_house]:
        format_time_axis(axis)
        axis.set_xlabel("Recorded time")
        for label in axis.get_xticklabels():
            label.set_rotation(15)
            label.set_ha("right")

    ids = ", ".join(str(i) for i in selected_ids)
    start = valid["_time"].min().isoformat()
    end = valid["_time"].max().isoformat()
    figure.suptitle(
        f"Partector 2 Pro post-flight quicklook - session(s) {ids}\n"
        f"{start} to {end} | valid n={len(valid)}",
        fontsize=14,
        fontweight="bold",
    )
    figure.savefig(output_dir / "partector_quicklook.png", bbox_inches="tight")
    figure.savefig(output_dir / "partector_quicklook.pdf", bbox_inches="tight")
    plt.close(figure)


def _time_edges(time: pd.Series) -> np.ndarray:
    """Create pcolormesh edges for irregular datetimes."""

    numeric = mdates.date2num(pd.to_datetime(time).to_numpy())
    if len(numeric) == 1:
        half = 0.5 / 86400.0
        return np.array([numeric[0] - half, numeric[0] + half])
    midpoint = (numeric[:-1] + numeric[1:]) / 2.0
    first = numeric[0] - (midpoint[0] - numeric[0])
    last = numeric[-1] + (numeric[-1] - midpoint[-1])
    return np.concatenate(([first], midpoint, [last]))


def write_methodology(
    output: Path,
    input_path: Path,
    selected_ids: list[int],
    summary: dict,
) -> None:
    metrics = summary["metrics_valid_records"]
    text = f"""# Partector 2 Pro quicklook

Input: `{input_path.resolve()}`

Selected session(s): {", ".join(map(str, selected_ids))}

## Result

- QC-valid records: {summary["selected_valid_rows"]} of {summary["selected_rows"]} selected rows.
- Number concentration median [P16, P84]: {metrics["number_cm3"]["median"]:.3g} [{metrics["number_cm3"]["p16"]:.3g}, {metrics["number_cm3"]["p84"]:.3g}] #/cm3.
- LDSA median [P16, P84]: {metrics["ldsa_um2_cm3"]["median"]:.3g} [{metrics["ldsa_um2_cm3"]["p16"]:.3g}, {metrics["ldsa_um2_cm3"]["p84"]:.3g}] um2/cm3.
- Mean diameter median [P16, P84]: {metrics["mean_diameter_nm"]["median"]:.3g} [{metrics["mean_diameter_nm"]["p16"]:.3g}, {metrics["mean_diameter_nm"]["p84"]:.3g}] nm.

## Scientific method

1. Preserve the `_time` wall-clock value exactly as written; remove the trailing timezone marker without converting the clock, and retain the original source-row index.
2. Split sessions when the instrument clock resets or the timestamp gap exceeds the configured threshold.
3. Remove logger-level replication by retaining one record per session and instrument-time value.
4. Flag records independently for non-zero instrument error, flow, temperature, RH, pressure, number, diameter, LDSA, and size-distribution validity. No interpolation is used.
5. Treat the eight channels as dN/dlog10(D), as specified by Naneos. Integrate in log10 diameter using bin edges at geometric midpoints. Size bands that cut a bin receive the logarithmic overlap fraction.
6. Summarize skewed aerosol distributions with the median and 16th/84th percentiles, following the robust percentile emphasis used by Asmi et al. (2011).
7. Show the raw time resolution. No smoothing, hypothesis testing, or source attribution is performed.

## Interpretation limits

- The Partector Pro has eight channels from 10 to 300 nm. Therefore, plotted N50-100 and N100-300 bands are not the N50 and N100 (integrated to 500 nm) defined by Asmi et al. (2011), and they are not measured CCN.
- The Cai et al. (2020) source-apportionment workflow is not applied. It used 106 SMPS bins, explicit uncertainty estimates, 15-minute averages, event exclusions, and independent chemical/tracer measurements. An eight-bin single-flight quicklook cannot support equivalent source attribution.
- Pressure-derived height is a standard-atmosphere relative diagnostic referenced to the maximum session pressure. It is not GPS or aircraft altitude.
- The manual reports typical accuracy of about +/-30% for LDSA, number, and mean diameter and +/-50% for PM0.3 mass. Mass also depends on particle density, morphology, and distribution assumptions.
- The manual specifies 0-40 C, 10-90% RH non-condensing, and 0-3000 m operating height. Data outside configured QC limits are retained in `qc_records.csv` but excluded from plots/statistics.

## References

- Asmi, A. et al. (2011), *Number size distributions and seasonality of submicron particles in Europe 2008-2009*, Atmospheric Chemistry and Physics, 11, 5505-5538, doi:10.5194/acp-11-5505-2011.
- Cai, J. et al. (2020), *Size-segregated particle number and mass concentrations from different emission sources in urban Beijing*, Atmospheric Chemistry and Physics, 20, 12721-12740, doi:10.5194/acp-20-12721-2020.
- Naneos particle solutions (2023), *Partector 2 Aerosol Dosimeter: Data File description*, revision A.
- Naneos particle solutions (2024), *Partector 2 operation manual*, revision ZB.
"""
    output.write_text(text, encoding="utf-8")


def main() -> int:
    args = build_parser().parse_args()
    if not args.csv.is_file():
        raise FileNotFoundError(args.csv)
    if args.session_gap_minutes <= 0:
        raise ValueError("--session-gap-minutes must be positive")
    if args.trim_start_minutes < 0:
        raise ValueError("--trim-start-minutes cannot be negative")
    if args.flow_range[0] >= args.flow_range[1]:
        raise ValueError("--flow-range MIN must be less than MAX")

    raw = pd.read_csv(args.csv, low_memory=False)
    frame = canonicalize_columns(raw)
    required = [
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
    ]
    require_columns(frame, required)
    size_columns, centers_nm = find_size_columns(frame)

    data = assign_sessions(frame, args.session_gap_minutes)
    config = QCConfig(
        flow_min_lpm=args.flow_range[0],
        flow_max_lpm=args.flow_range[1],
    )
    data = apply_qc(data, size_columns, config, args.trim_start_minutes)
    data = integrate_size_distribution(data, size_columns, centers_nm)
    sessions = session_table(data)
    selected_ids = choose_sessions(sessions, args.session)
    selected = data[data["session"].isin(selected_ids)].copy()

    args.output_dir.mkdir(parents=True, exist_ok=True)
    sessions.to_csv(args.output_dir / "sessions.csv", index=False)
    data.to_csv(args.output_dir / "qc_records.csv", index=False)
    data[data["qc_valid"]].to_csv(args.output_dir / "cleaned_valid_records.csv", index=False)

    summary = create_summary(
        len(raw),
        data,
        selected,
        sessions,
        selected_ids,
        centers_nm,
        config,
        args,
    )
    (args.output_dir / "summary.json").write_text(
        json.dumps(summary, indent=2, allow_nan=False), encoding="utf-8"
    )
    write_methodology(
        args.output_dir / "README.md", args.csv, selected_ids, summary
    )
    plot_quicklook(
        selected,
        size_columns,
        centers_nm,
        selected_ids,
        args.output_dir,
        show_pressure_altitude=not args.no_pressure_altitude,
    )

    print(f"Quicklook written to: {args.output_dir.resolve()}")
    print(sessions.to_string(index=False))
    print(f"Selected session(s): {selected_ids}")
    print(
        f"Valid selected records: {int(selected['qc_valid'].sum())} / {len(selected)}"
    )
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        raise SystemExit(2)
