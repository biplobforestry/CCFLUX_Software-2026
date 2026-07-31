#!/usr/bin/env python3
"""Scientific full-record quicklook for paired Alphasense OPC-N3 CSV files.

The script never shifts timestamps, interpolates gaps, deletes zero readings, or
uses QC flags to remove observations. It supports logger exports in which bins
are either number concentration (#/cm3) or raw particles per sampling period.
"""

from __future__ import annotations

import argparse
import json
import re
from dataclasses import dataclass
from pathlib import Path

import matplotlib.colors as mcolors
import matplotlib.dates as mdates
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd

BIN_COUNT = 24
MAX_PARTICLE_RATE_S = 10_000.0
TEMPERATURE_LIMITS_C = (-10.0, 50.0)
RH_LIMITS_PERCENT = (0.0, 95.0)


@dataclass(frozen=True)
class SensorSpec:
    name: str
    suffix: str
    label: str
    pm1: str
    pm25: str
    pm10: str
    temperature: str
    rh: str


HBX4 = SensorSpec(
    name="HBX-4",
    suffix="X4",
    label="HBX-4 (without inlet)",
    pm1="opc_pm1_1",
    pm25="opc_pm2.5_1",
    pm10="opc_pm10_1",
    temperature="opc_temperature_degC",
    rh="RH_OPC_%",
)
HBX5 = SensorSpec(
    name="HBX-5",
    suffix="X5",
    label="HBX-5 (with inlet)",
    pm1="opc_pm1_in_1",
    pm25="opc_pm2.5_in_1",
    pm10="opc_pm10_in_1",
    temperature="opc_inlet_temp_degC",
    rh="RH_OPC_in_%",
)


def cli() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Create a full-record paired OPC-N3 quicklook without filling gaps."
    )
    p.add_argument("hbx4_csv", type=Path)
    p.add_argument("hbx5_csv", type=Path)
    p.add_argument("-o", "--output-dir", type=Path, default=Path("opc_n3_quicklook"))
    p.add_argument("--gap-seconds", type=float, default=10.0)
    p.add_argument(
        "--pair-tolerance-seconds",
        type=float,
        default=2.0,
        help="Nearest-time tolerance for comparison only; no interpolation.",
    )
    p.add_argument(
        "--hbx4-bin-units",
        choices=("auto", "number_cm3", "counts_per_period"),
        default="auto",
    )
    p.add_argument(
        "--hbx5-bin-units",
        choices=("auto", "number_cm3", "counts_per_period"),
        default="auto",
    )
    return p.parse_args()


def parse_recorded_time(values: pd.Series) -> pd.Series:
    """Strip timezone notation without converting the recorded wall-clock."""
    text = (
        values.astype("string")
        .str.strip()
        .str.replace(r"Z$", "", regex=True)
        .str.replace(r"[+-]\d{2}:\d{2}$", "", regex=True)
    )
    return pd.to_datetime(text, errors="coerce")


def median_interval_seconds(times: pd.Series) -> float | None:
    delta = times.diff().dt.total_seconds()
    delta = delta[(delta > 0) & np.isfinite(delta)]
    return float(delta.median()) if len(delta) else None


def integer_residual(values: np.ndarray) -> float | None:
    values = values[np.isfinite(values) & (values > 0)]
    if not len(values):
        return None
    return float(np.median(np.abs(values - np.rint(values))))


def infer_bin_units(
    raw_bins: np.ndarray,
    sample_volume_cm3: np.ndarray,
    requested: str,
) -> tuple[str, dict]:
    positive = np.isfinite(raw_bins) & (raw_bins > 0)
    volume_matrix = np.broadcast_to(sample_volume_cm3[:, None], raw_bins.shape)
    score_counts = integer_residual(raw_bins[positive])
    score_concentration = integer_residual(
        (raw_bins * volume_matrix)[positive]
    )
    evidence = {
        "positive_bin_cells": int(positive.sum()),
        "median_integer_residual_if_counts": score_counts,
        "median_integer_residual_if_number_concentration": score_concentration,
    }
    if requested != "auto":
        return requested, evidence
    if not positive.any():
        return "number_cm3", {
            **evidence,
            "auto_note": "All bins are zero; units cannot be identified from quantization.",
        }
    concentration_clear = (
        score_concentration is not None
        and score_concentration <= 0.02
        and (score_counts is None or score_concentration < 0.5 * score_counts)
    )
    counts_clear = (
        score_counts is not None
        and score_counts <= 0.02
        and (
            score_concentration is None
            or score_counts < 0.5 * score_concentration
        )
    )
    if concentration_clear:
        return "number_cm3", evidence
    if counts_clear:
        return "counts_per_period", evidence
    raise ValueError(
        "Bin units are ambiguous. Specify --hbx4-bin-units or "
        "--hbx5-bin-units after confirming the logger format."
    )


def required_columns(spec: SensorSpec) -> list[str]:
    return [
        "_time",
        *[f"Bin{i}_{spec.suffix}_1" for i in range(BIN_COUNT)],
        f"SFR_{spec.suffix}_1",
        f"SP_{spec.suffix}_1",
        f"RejectGlitch_{spec.suffix}_1",
        f"RejectRatio_{spec.suffix}_1",
        f"Laser_status_{spec.suffix}_1",
        spec.pm1,
        spec.pm25,
        spec.pm10,
        spec.temperature,
        spec.rh,
    ]


def load_sensor(
    path: Path,
    spec: SensorSpec,
    gap_seconds: float,
    requested_bin_units: str,
) -> tuple[pd.DataFrame, dict]:
    df = pd.read_csv(path, low_memory=False)
    missing = [column for column in required_columns(spec) if column not in df]
    if missing:
        raise ValueError(f"{spec.name}: missing columns: {', '.join(missing)}")

    df["recorded_time"] = parse_recorded_time(df["_time"])
    bad_time = int(df["recorded_time"].isna().sum())
    if bad_time:
        raise ValueError(
            f"{spec.name}: {bad_time} rows have invalid timestamps. "
            "Correct the source rather than silently dropping them."
        )
    df = df.sort_values("recorded_time", kind="stable").reset_index(drop=True)
    processed = [
        column
        for column in df
        if not column.endswith("_raw_value")
        and column not in {"_time", "recorded_time"}
    ]
    for column in processed:
        df[column] = pd.to_numeric(df[column], errors="coerce")

    intervals = df["recorded_time"].diff().dt.total_seconds()
    df["session_id"] = intervals.gt(gap_seconds).cumsum().astype(int) + 1
    df["gap_before_seconds"] = intervals.where(intervals.gt(gap_seconds))

    bins = [f"Bin{i}_{spec.suffix}_1" for i in range(BIN_COUNT)]
    sfr = f"SFR_{spec.suffix}_1"
    sp = f"SP_{spec.suffix}_1"
    volume = df[sfr] * df[sp]
    raw_bin_matrix = df[bins].to_numpy(dtype=float)
    unit_mode, unit_evidence = infer_bin_units(
        raw_bin_matrix, volume.to_numpy(dtype=float), requested_bin_units
    )
    if unit_mode == "counts_per_period":
        concentration = raw_bin_matrix / volume.to_numpy(dtype=float)[:, None]
    else:
        concentration = raw_bin_matrix.copy()

    nc_columns = [f"bin{index}_number_cm3" for index in range(BIN_COUNT)]
    concentration_frame = pd.DataFrame(
        concentration, columns=nc_columns, index=df.index
    )
    df = pd.concat([df, concentration_frame], axis=1).copy()
    df["total_number_cm3"] = df[nc_columns].sum(axis=1, min_count=1)
    df["particle_rate_s"] = df["total_number_cm3"] * df[sfr]
    df["implied_particles_period"] = df["particle_rate_s"] * df[sp]
    df["all_bins_zero"] = df[nc_columns].fillna(0).eq(0).all(axis=1)
    df["active_bin_count"] = df[nc_columns].gt(0).sum(axis=1)

    qc_columns = [
        *bins,
        sfr,
        sp,
        spec.pm1,
        spec.pm25,
        spec.pm10,
        spec.temperature,
        spec.rh,
    ]
    df["qc_missing_critical"] = df[qc_columns].isna().any(axis=1)
    df["qc_negative_value"] = df[
        [*bins, spec.pm1, spec.pm25, spec.pm10]
    ].lt(0).any(axis=1)
    df["qc_nonpositive_sample_volume"] = volume.le(0) | volume.isna()
    df["qc_pm_order"] = ~(
        (df[spec.pm1] <= df[spec.pm25])
        & (df[spec.pm25] <= df[spec.pm10])
    )
    df["qc_particle_rate"] = df["particle_rate_s"] > MAX_PARTICLE_RATE_S
    df["qc_temperature_range"] = ~df[spec.temperature].between(
        *TEMPERATURE_LIMITS_C, inclusive="both"
    )
    df["qc_rh_range"] = ~df[spec.rh].between(
        *RH_LIMITS_PERCENT, inclusive="both"
    )
    flags = [column for column in df if column.startswith("qc_")]
    df["qc_any"] = df[flags].any(axis=1)
    df["qc_flags"] = df[flags].apply(
        lambda row: ";".join(
            name.removeprefix("qc_") for name, value in row.items() if value
        ),
        axis=1,
    )

    elapsed = (
        df["recorded_time"].iloc[-1] - df["recorded_time"].iloc[0]
    ).total_seconds()
    session_duration = 0.0
    for _, group in df.groupby("session_id"):
        session_duration += (
            group["recorded_time"].iloc[-1] - group["recorded_time"].iloc[0]
        ).total_seconds()
    metadata = {
        "sensor": spec.name,
        "label": spec.label,
        "input_csv": str(path.resolve()),
        "rows": int(len(df)),
        "start_recorded_time": df["recorded_time"].iloc[0].isoformat(sep=" "),
        "end_recorded_time": df["recorded_time"].iloc[-1].isoformat(sep=" "),
        "elapsed_hours": elapsed / 3600.0,
        "acquisition_hours": session_duration / 3600.0,
        "acquisition_fraction_of_elapsed": (
            session_duration / elapsed if elapsed > 0 else None
        ),
        "sessions": int(df["session_id"].nunique()),
        "median_interval_seconds": median_interval_seconds(df["recorded_time"]),
        "largest_gap_seconds": float(intervals.max()),
        "bin_units_used": unit_mode,
        "bin_unit_evidence": unit_evidence,
        "zero_bin_row_fraction": float(df["all_bins_zero"].mean()),
        "qc_flagged_fraction": float(df["qc_any"].mean()),
        "pm1_zero_fraction": float(df[spec.pm1].eq(0).mean()),
        "pm25_zero_fraction": float(df[spec.pm25].eq(0).mean()),
        "pm10_zero_fraction": float(df[spec.pm10].eq(0).mean()),
        "sfr_ml_s": describe(df[sfr]),
        "sampling_period_s": describe(df[sp]),
        "laser_status_raw": describe(df[f"Laser_status_{spec.suffix}_1"]),
        "reject_glitch_nonzero_fraction": float(
            df[f"RejectGlitch_{spec.suffix}_1"].gt(0).mean()
        ),
        "reject_ratio_nonzero_fraction": float(
            df[f"RejectRatio_{spec.suffix}_1"].gt(0).mean()
        ),
    }
    return df, metadata


def describe(values: pd.Series) -> dict:
    x = pd.to_numeric(values, errors="coerce")
    return {
        "valid": int(x.notna().sum()),
        "min": finite(x.min()),
        "median": finite(x.median()),
        "max": finite(x.max()),
    }


def finite(value: float | int | None) -> float | None:
    return float(value) if value is not None and np.isfinite(value) else None


def pair_sensors(
    hbx4: pd.DataFrame,
    hbx5: pd.DataFrame,
    tolerance_seconds: float,
) -> pd.DataFrame:
    left = hbx4[
        ["recorded_time", HBX4.pm1, HBX4.pm25, HBX4.pm10, "total_number_cm3"]
    ].rename(
        columns={
            "recorded_time": "time_hbx4",
            HBX4.pm1: "pm1_hbx4",
            HBX4.pm25: "pm25_hbx4",
            HBX4.pm10: "pm10_hbx4",
            "total_number_cm3": "number_hbx4_cm3",
        }
    )
    right = hbx5[
        ["recorded_time", HBX5.pm1, HBX5.pm25, HBX5.pm10, "total_number_cm3"]
    ].rename(
        columns={
            "recorded_time": "time_hbx5",
            HBX5.pm1: "pm1_hbx5",
            HBX5.pm25: "pm25_hbx5",
            HBX5.pm10: "pm10_hbx5",
            "total_number_cm3": "number_hbx5_cm3",
        }
    )
    paired = pd.merge_asof(
        left.sort_values("time_hbx4"),
        right.sort_values("time_hbx5"),
        left_on="time_hbx4",
        right_on="time_hbx5",
        direction="nearest",
        tolerance=pd.Timedelta(seconds=tolerance_seconds),
    ).dropna(subset=["time_hbx5"])
    paired["time_difference_seconds"] = (
        paired["time_hbx5"] - paired["time_hbx4"]
    ).dt.total_seconds()
    paired = (
        paired.assign(_absolute_time_difference=paired["time_difference_seconds"].abs())
        .sort_values(["_absolute_time_difference", "time_hbx4"])
        .drop_duplicates("time_hbx5", keep="first")
        .drop(columns="_absolute_time_difference")
        .sort_values("time_hbx4")
        .reset_index(drop=True)
    )
    for metric in ("pm1", "pm25", "pm10", "number"):
        left_name = f"{metric}_hbx4" + ("_cm3" if metric == "number" else "")
        right_name = f"{metric}_hbx5" + ("_cm3" if metric == "number" else "")
        paired[f"{metric}_difference_hbx5_minus_hbx4"] = (
            paired[right_name] - paired[left_name]
        )
        paired[f"{metric}_ratio_hbx5_over_hbx4"] = np.divide(
            paired[right_name],
            paired[left_name],
            out=np.full(len(paired), np.nan),
            where=paired[left_name].to_numpy(dtype=float) != 0,
        )
    return paired


def comparison_summary(paired: pd.DataFrame) -> dict:
    result = {
        "matched_rows": int(len(paired)),
        "matching_method": "nearest timestamp within tolerance; no interpolation",
    }
    for metric in ("pm1", "pm25", "pm10", "number"):
        suffix = "_cm3" if metric == "number" else ""
        x = paired[f"{metric}_hbx4{suffix}"]
        y = paired[f"{metric}_hbx5{suffix}"]
        finite_pair = x.notna() & y.notna()
        nonzero_pair = finite_pair & x.ne(0) & y.ne(0)
        result[metric] = {
            "finite_pairs": int(finite_pair.sum()),
            "both_nonzero_pairs": int(nonzero_pair.sum()),
            "median_difference_hbx5_minus_hbx4": finite(
                (y[finite_pair] - x[finite_pair]).median()
            ),
            "median_ratio_hbx5_over_hbx4_nonzero": finite(
                (y[nonzero_pair] / x[nonzero_pair]).median()
            ),
            "pearson_r_nonzero": (
                finite(x[nonzero_pair].corr(y[nonzero_pair]))
                if nonzero_pair.sum() >= 3
                else None
            ),
        }
    return result


def plot_by_session(
    ax: plt.Axes,
    df: pd.DataFrame,
    column: str,
    **kwargs,
) -> None:
    first = True
    label = kwargs.pop("label", None)
    for _, group in df.groupby("session_id", sort=True):
        ax.plot(
            group["recorded_time"],
            group[column],
            label=label if first else None,
            **kwargs,
        )
        first = False


def time_axis(ax: plt.Axes) -> None:
    locator = mdates.AutoDateLocator(minticks=4, maxticks=8)
    ax.xaxis.set_major_locator(locator)
    ax.xaxis.set_major_formatter(mdates.ConciseDateFormatter(locator))
    ax.tick_params(axis="x", labelbottom=True)
    ax.set_xlabel("Recorded time (timezone removed; no conversion)")
    ax.grid(True, alpha=0.25)


def concentration_limits(datasets: list[pd.DataFrame]) -> tuple[float, float]:
    positive = np.concatenate(
        [
            df[[f"bin{i}_number_cm3" for i in range(BIN_COUNT)]]
            .to_numpy(dtype=float)
            .ravel()
            for df in datasets
        ]
    )
    positive = positive[np.isfinite(positive) & (positive > 0)]
    if not len(positive):
        return 1e-6, 1.0
    low, high = np.percentile(positive, [2, 99])
    return max(float(low), 1e-9), max(float(high), float(low) * 1.01)


def bin_heatmap(
    fig: plt.Figure,
    ax: plt.Axes,
    df: pd.DataFrame,
    title: str,
    norm: mcolors.LogNorm,
) -> None:
    columns = [f"bin{i}_number_cm3" for i in range(BIN_COUNT)]
    mesh = None
    for _, group in df.groupby("session_id", sort=True):
        if len(group) < 2:
            continue
        centers = mdates.date2num(group["recorded_time"])
        middle = (centers[:-1] + centers[1:]) / 2
        edges = np.r_[
            centers[0] - (middle[0] - centers[0]),
            middle,
            centers[-1] + (centers[-1] - middle[-1]),
        ]
        values = group[columns].to_numpy(dtype=float).T
        values = np.ma.masked_invalid(values)
        values = np.ma.masked_less_equal(values, 0)
        mesh = ax.pcolormesh(
            edges,
            np.arange(BIN_COUNT + 1) - 0.5,
            values,
            cmap="viridis",
            norm=norm,
            shading="flat",
        )
    ax.set(title=title, ylabel="OPC-N3 software bin index")
    ax.set_yticks([0, 4, 8, 12, 16, 20, 23])
    if mesh is not None:
        fig.colorbar(mesh, ax=ax, label="Number concentration (#/cm³)")


def diagnostics_panel(
    ax: plt.Axes,
    df: pd.DataFrame,
    spec: SensorSpec,
    metadata: dict,
) -> None:
    sfr = f"SFR_{spec.suffix}_1"
    sp = f"SP_{spec.suffix}_1"
    plot_by_session(ax, df, sfr, color="#0072B2", lw=0.8, label="SFR (mL/s)")
    plot_by_session(ax, df, sp, color="#D55E00", lw=0.8, label="SP (s)")
    ax.set_ylabel("SFR (mL/s) / sampling period (s)")
    ax2 = ax.twinx()
    plot_by_session(
        ax2, df, spec.temperature, color="#009E73", lw=0.8, label="Temperature (°C)"
    )
    plot_by_session(
        ax2, df, spec.rh, color="#CC79A7", lw=0.8, label="Internal RH (%)"
    )
    ax2.set_ylabel("Temperature (°C) / internal RH (%)")
    lines = [
        line
        for line in ax.get_lines() + ax2.get_lines()
        if not line.get_label().startswith("_")
    ]
    ax.legend(lines, [line.get_label() for line in lines], ncol=2, fontsize=7)
    ax.set_title(
        f"{spec.label}: diagnostics | zero-bin rows "
        f"{metadata['zero_bin_row_fraction']:.1%}, QC flags "
        f"{metadata['qc_flagged_fraction']:.1%}"
    )


def make_plot(
    hbx4: pd.DataFrame,
    hbx5: pd.DataFrame,
    meta4: dict,
    meta5: dict,
    output_png: Path,
    output_pdf: Path,
) -> None:
    plt.style.use("seaborn-v0_8-whitegrid")
    fig, axes = plt.subplots(4, 2, figsize=(17, 16), constrained_layout=True)
    colors = {"PM1": "#0072B2", "PM2.5": "#D55E00", "PM10": "#009E73"}
    for ax, df, spec in zip(axes[0], [hbx4, hbx5], [HBX4, HBX5]):
        for label, column in [
            ("PM1", spec.pm1),
            ("PM2.5", spec.pm25),
            ("PM10", spec.pm10),
        ]:
            plot_by_session(
                ax, df, column, color=colors[label], lw=0.8, label=label
            )
        ax.set_yscale("symlog", linthresh=0.01)
        ax.set_title(f"{spec.label}: reported mass concentration")
        ax.set_ylabel("Mass concentration (µg/m³)")
        ax.legend(ncol=3, fontsize=8)

    for ax, df, spec in zip(axes[1], [hbx4, hbx5], [HBX4, HBX5]):
        plot_by_session(
            ax,
            df,
            "total_number_cm3",
            color="#0072B2",
            lw=0.8,
            label="Total number concentration",
        )
        ax.set_yscale("symlog", linthresh=0.01)
        ax.set_title(f"{spec.label}: all 24 bins")
        ax.set_ylabel("Number concentration (#/cm³)")
        ax.legend(fontsize=8)

    vmin, vmax = concentration_limits([hbx4, hbx5])
    norm = mcolors.LogNorm(vmin=vmin, vmax=vmax)
    bin_heatmap(
        fig, axes[2, 0], hbx4, f"{HBX4.label}: bin-resolved concentration", norm
    )
    bin_heatmap(
        fig, axes[2, 1], hbx5, f"{HBX5.label}: bin-resolved concentration", norm
    )

    diagnostics_panel(axes[3, 0], hbx4, HBX4, meta4)
    diagnostics_panel(axes[3, 1], hbx5, HBX5, meta5)
    for ax in axes.ravel():
        time_axis(ax)

    start = min(hbx4["recorded_time"].iloc[0], hbx5["recorded_time"].iloc[0])
    end = max(hbx4["recorded_time"].iloc[-1], hbx5["recorded_time"].iloc[-1])
    elapsed = (end - start).total_seconds() / 3600.0
    fig.suptitle(
        "OPC-N3 Inlet Comparison Quicklook\n"
        f"{start.isoformat(sep=' ')} to {end.isoformat(sep=' ')} | "
        f"{elapsed:.2f} h elapsed | gaps preserved | no interpolation",
        fontsize=15,
    )
    fig.savefig(output_png, dpi=180)
    fig.savefig(output_pdf)
    plt.close(fig)


def write_readme(path: Path, summary: dict) -> None:
    path.write_text(
        f"""# OPC-N3 paired-sensor quicklook

This quicklook evaluates the complete HBX-4 and HBX-5 files. It does not
interpolate gaps, replace zeros, resample time, shift timestamps, or remove
QC-flagged observations.

## Instrument basis

- Alphasense OPC-N3: 24 software bins spanning 0.35-40 µm spherical-equivalent
  diameter for refractive index 1.5.
- Typical total flow: 5.5 L/min; typical sample flow: 280 mL/min.
- Histogram period: 1-30 s; maximum particle count rate: 10,000 particles/s.
- Temperature specification: -10 to 50 °C.
- RH specification: 0-95% non-condensing.

The supplied datasheet does not provide the individual 24 bin boundaries, so
the figure uses bin index rather than inventing diameters or dN/dlogDp.

## Bin units

HBX-4 bins were interpreted as
`{summary['hbx4']['bin_units_used']}` and HBX-5 bins as
`{summary['hbx5']['bin_units_used']}`. In automatic mode, the decision is tested
using particle-count quantization: number concentration × SFR × sampling period
must recover integer particle counts.

## Quality-control policy

QC flags annotate missing critical values, negative measurements, non-positive
sample volume, inconsistent PM ordering, rates above the documented maximum,
and temperature/RH outside specifications. Flags never delete data. All-zero
particle rows are reported separately and remain zero because they may reflect
clean air, inlet state, acquisition state, or another physical condition.
Laser status and rejection diagnostics are summarized without undocumented
pass/fail thresholds.

HBX-4 and HBX-5 comparison rows use nearest timestamps within
{summary['configuration']['pair_tolerance_seconds']:g} s. This is matching, not
interpolation.

## Scientific limitations

An inlet-effect conclusion requires valid co-located measurements, synchronized
sampling, documented inlet geometry and flow, and preferably pre/post-flight
zero and reference checks. Internal OPC temperature/RH are not necessarily
ambient values. Optical sizing and mass estimates depend on refractive index,
particle density/shape and humidity.

Sources:
- Local manufacturer datasheet: alphasense_opc-n3_datasheet_en_1.pdf
- Nurowska et al. (2023), https://doi.org/10.5194/amt-16-2415-2023
- Alphasense OPC-N3 product specification, https://www.alphasense.com/
""",
        encoding="utf-8",
    )


def main() -> None:
    args = cli()
    if args.gap_seconds <= 0 or args.pair_tolerance_seconds <= 0:
        raise ValueError("Gap and pairing tolerances must be positive.")
    output = args.output_dir.resolve()
    output.mkdir(parents=True, exist_ok=True)

    hbx4, meta4 = load_sensor(
        args.hbx4_csv.resolve(),
        HBX4,
        args.gap_seconds,
        args.hbx4_bin_units,
    )
    hbx5, meta5 = load_sensor(
        args.hbx5_csv.resolve(),
        HBX5,
        args.gap_seconds,
        args.hbx5_bin_units,
    )
    paired = pair_sensors(hbx4, hbx5, args.pair_tolerance_seconds)
    summary = {
        "method": {
            "time_handling": "timezone suffix removed without conversion or shift",
            "gap_handling": "preserved; sessions split only for plotting",
            "zero_handling": "preserved as observed zero",
            "interpolation": "none",
            "qc_policy": "flag only; no removal",
            "bin_diameter_axis": "bin index because individual boundaries were not documented",
        },
        "configuration": {
            "gap_seconds": args.gap_seconds,
            "pair_tolerance_seconds": args.pair_tolerance_seconds,
        },
        "hbx4": meta4,
        "hbx5": meta5,
        "paired_comparison": comparison_summary(paired),
    }

    hbx4.to_csv(
        output / "hbx4_evaluated.csv",
        index=False,
        date_format="%Y-%m-%d %H:%M:%S.%f",
    )
    hbx5.to_csv(
        output / "hbx5_evaluated.csv",
        index=False,
        date_format="%Y-%m-%d %H:%M:%S.%f",
    )
    paired.to_csv(
        output / "paired_comparison.csv",
        index=False,
        date_format="%Y-%m-%d %H:%M:%S.%f",
    )
    (output / "summary.json").write_text(
        json.dumps(summary, indent=2, allow_nan=False), encoding="utf-8"
    )
    write_readme(output / "README.md", summary)
    make_plot(
        hbx4,
        hbx5,
        meta4,
        meta5,
        output / "opc_n3_inlet_comparison_quicklook.png",
        output / "opc_n3_inlet_comparison_quicklook.pdf",
    )
    print(json.dumps(summary, indent=2, allow_nan=False))
    print(f"\nOutputs: {output}")


if __name__ == "__main__":
    main()
