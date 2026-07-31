#!/usr/bin/env python3
"""Full-flight Gremsy gimbal quicklook with no time or signal filtering."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import matplotlib.dates as mdates
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
from scipy import signal

ACC = ["gimbal_acc_x_counts", "gimbal_acc_y_counts", "gimbal_acc_z_counts"]
GYRO = ["gimbal_gyro_x_counts", "gimbal_gyro_y_counts", "gimbal_gyro_z_counts"]
ANGLES = ["gimbal_pitch_deg", "gimbal_roll_deg", "gimbal_yaw_deg"]
ACC_COUNTS_PER_G = 8192.0
GYRO_COUNTS_PER_1000_DPS = 32768.0


def cli() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Evaluate the complete Gremsy CSV without selecting a time window or filtering signals."
    )
    parser.add_argument("csv", type=Path)
    parser.add_argument("-o", "--output-dir", type=Path, default=Path("gremsy_full_flight_quicklook"))
    parser.add_argument(
        "--gap-seconds",
        type=float,
        default=10.0,
        help="Mark separate acquisition sessions; no rows are removed.",
    )
    parser.add_argument(
        "--rms-seconds",
        type=float,
        default=30.0,
        help="Descriptive rolling-RMS window; raw data remain unchanged.",
    )
    parser.add_argument("--maneuver-threshold-dps", type=float, default=10.0)
    return parser.parse_args()


def recorded_time(values: pd.Series) -> pd.Series:
    """Remove timezone text without conversion, preserving the recorded clock."""
    text = (
        values.astype("string")
        .str.strip()
        .str.replace(r"Z$", "", regex=True)
        .str.replace(r"[+-]\d{2}:\d{2}$", "", regex=True)
    )
    return pd.to_datetime(text, errors="coerce")


def sample_rate(times: pd.Series) -> float:
    intervals = times.diff().dt.total_seconds()
    intervals = intervals[(intervals > 0) & np.isfinite(intervals)]
    return float(1.0 / intervals.median()) if len(intervals) else float("nan")


def load_csv(path: Path, gap_seconds: float) -> pd.DataFrame:
    df = pd.read_csv(path, low_memory=False)
    missing = [name for name in ["_time", *ACC, *GYRO] if name not in df]
    if missing:
        raise ValueError(f"Missing required columns: {', '.join(missing)}")
    df["recorded_time"] = recorded_time(df["_time"])
    bad_time = int(df["recorded_time"].isna().sum())
    if bad_time:
        raise ValueError(
            f"{bad_time} rows have invalid timestamps. Correct the source CSV; "
            "the script will not silently discard them."
        )
    df = df.sort_values("recorded_time", kind="stable").reset_index(drop=True)
    for name in [*ACC, *GYRO, *ANGLES]:
        if name in df:
            df[name] = pd.to_numeric(df[name], errors="coerce")

    intervals = df["recorded_time"].diff().dt.total_seconds()
    df["session_id"] = intervals.gt(gap_seconds).cumsum().astype(int) + 1
    df["gap_before_seconds"] = intervals.where(intervals.gt(gap_seconds))
    for axis, name in zip("xyz", ACC):
        df[f"acc_{axis}_g"] = df[name] / ACC_COUNTS_PER_G
    for axis, name in zip("xyz", GYRO):
        df[f"gyro_{axis}_dps"] = df[name] * 1000.0 / GYRO_COUNTS_PER_1000_DPS
    df["acc_norm_g"] = np.sqrt(sum(df[f"acc_{axis}_g"] ** 2 for axis in "xyz"))
    df["gyro_norm_dps"] = np.sqrt(sum(df[f"gyro_{axis}_dps"] ** 2 for axis in "xyz"))
    df["acc_deviation_g"] = df["acc_norm_g"] - 1.0
    return df


def describe_sessions(df: pd.DataFrame) -> pd.DataFrame:
    rows = []
    for session_id, group in df.groupby("session_id", sort=True):
        imu = group[[*ACC, *GYRO]]
        complete = imu.notna().all(axis=1)
        changed = imu.ne(imu.shift()).any(axis=1) & complete
        changed_times = group.loc[changed, "recorded_time"]
        elapsed = (group["recorded_time"].iloc[-1] - group["recorded_time"].iloc[0]).total_seconds()
        rows.append(
            {
                "session_id": int(session_id),
                "start_recorded_time": group["recorded_time"].iloc[0],
                "end_recorded_time": group["recorded_time"].iloc[-1],
                "duration_minutes": elapsed / 60.0,
                "rows": int(len(group)),
                "complete_imu_rows": int(complete.sum()),
                "unique_imu_states": int(changed.sum()),
                "logger_rate_hz": sample_rate(group["recorded_time"]),
                "imu_update_rate_hz": sample_rate(changed_times),
            }
        )
    return pd.DataFrame(rows)


def rolling_statistics(
    df: pd.DataFrame, sessions: pd.DataFrame, seconds: float, threshold: float
) -> pd.DataFrame:
    out = df.copy()
    out["acc_rms_g"] = np.nan
    out["gyro_rms_dps"] = np.nan
    for _, session_row in sessions.iterrows():
        mask = out["session_id"].eq(int(session_row["session_id"]))
        rate = float(session_row["logger_rate_hz"])
        count = max(3, round(rate * seconds)) if np.isfinite(rate) else 3
        minimum = max(2, count // 3)
        out.loc[mask, "acc_rms_g"] = (
            out.loc[mask, "acc_deviation_g"]
            .pow(2).rolling(count, center=True, min_periods=minimum).mean().pow(0.5)
        )
        out.loc[mask, "gyro_rms_dps"] = (
            out.loc[mask, "gyro_norm_dps"]
            .pow(2).rolling(count, center=True, min_periods=minimum).mean().pow(0.5)
        )
    out["maneuver_flag"] = out["gyro_norm_dps"] > threshold
    return out


def full_flight_asd(
    df: pd.DataFrame, sessions: pd.DataFrame, column: str
) -> tuple[np.ndarray, np.ndarray]:
    """Duration-weighted Welch ASD using every acquisition session."""
    spectra = []
    for _, session_row in sessions.iterrows():
        values = df.loc[
            df["session_id"].eq(int(session_row["session_id"])), column
        ].dropna().to_numpy(dtype=float)
        rate = float(session_row["logger_rate_hz"])
        if len(values) < 16 or not np.isfinite(rate) or rate <= 0:
            continue
        nperseg = min(len(values), max(64, round(rate * 600)))
        frequency, power = signal.welch(
            values,
            fs=rate,
            window="hann",
            nperseg=nperseg,
            noverlap=nperseg // 2,
            detrend="linear",
            scaling="density",
        )
        spectra.append((frequency, power, len(values)))
    if not spectra:
        return np.array([]), np.array([])

    grid = np.linspace(0.0, max(item[0][-1] for item in spectra), 512)
    numerator = np.zeros_like(grid)
    denominator = np.zeros_like(grid)
    for frequency, power, weight in spectra:
        valid = grid <= frequency[-1]
        numerator[valid] += np.interp(grid[valid], frequency, power) * weight
        denominator[valid] += weight
    power = np.divide(
        numerator, denominator, out=np.full_like(grid, np.nan), where=denominator > 0
    )
    return grid, np.sqrt(power)


def full_flight_spectrogram(
    df: pd.DataFrame, sessions: pd.DataFrame, column: str
) -> list[tuple[pd.DatetimeIndex, np.ndarray, np.ndarray]]:
    """Unfiltered time-frequency PSD, calculated separately for every session."""
    results = []
    for _, session_row in sessions.iterrows():
        group = df.loc[df["session_id"].eq(int(session_row["session_id"]))]
        valid = group[column].notna()
        values = group.loc[valid, column].to_numpy(dtype=float)
        times = group.loc[valid, "recorded_time"]
        rate = float(session_row["logger_rate_hz"])
        if len(values) < 16 or not np.isfinite(rate) or rate <= 0:
            continue
        nperseg = min(len(values), max(32, round(rate * 300)))
        noverlap = min(nperseg - 1, round(0.75 * nperseg))
        frequency, seconds, power = signal.spectrogram(
            values,
            fs=rate,
            window="hann",
            nperseg=nperseg,
            noverlap=noverlap,
            detrend="linear",
            scaling="density",
            mode="psd",
        )
        center_times = pd.DatetimeIndex(
            times.iloc[0] + pd.to_timedelta(seconds, unit="s")
        )
        power_db = 10.0 * np.log10(np.maximum(power, np.finfo(float).tiny))
        results.append((center_times, frequency, power_db))
    return results


def no_gap_bridge(df: pd.DataFrame, column: str) -> np.ndarray:
    values = df[column].to_numpy(dtype=float, copy=True)
    starts = df["session_id"].ne(df["session_id"].shift()).to_numpy()
    starts[0] = False
    values[starts] = np.nan
    return values


def rms(values: pd.Series) -> float | None:
    data = values.to_numpy(dtype=float)
    data = data[np.isfinite(data)]
    return float(np.sqrt(np.mean(data**2))) if len(data) else None


def dominant(
    spectrum: tuple[np.ndarray, np.ndarray], reliable_upper: float
) -> float | None:
    frequency, amplitude = spectrum
    valid = (
        (frequency > 0)
        & (frequency <= reliable_upper)
        & np.isfinite(amplitude)
    )
    if not valid.any():
        return None
    return float(frequency[valid][np.argmax(amplitude[valid])])


def time_axes(axes: list[plt.Axes]) -> None:
    for ax in axes:
        locator = mdates.AutoDateLocator(minticks=4, maxticks=9)
        ax.xaxis.set_major_locator(locator)
        ax.xaxis.set_major_formatter(mdates.ConciseDateFormatter(locator))
        ax.tick_params(axis="x", labelbottom=True)
        ax.set_xlabel("Recorded time (timezone removed; no conversion)")
        ax.grid(True, alpha=0.25)


def make_plot(
    df: pd.DataFrame,
    summary: dict,
    acc_asd: tuple[np.ndarray, np.ndarray],
    gyro_asd: tuple[np.ndarray, np.ndarray],
    acc_spectrogram: list[tuple[pd.DatetimeIndex, np.ndarray, np.ndarray]],
    png: Path,
    pdf: Path,
) -> None:
    plt.style.use("seaborn-v0_8-whitegrid")
    fig, axes = plt.subplots(3, 2, figsize=(16, 12), constrained_layout=True)
    time = df["recorded_time"]
    colors = ["#0072B2", "#D55E00", "#009E73"]

    ax = axes[0, 0]
    for axis, color in zip("xyz", colors):
        ax.plot(time, no_gap_bridge(df, f"acc_{axis}_g"), lw=0.7, color=color, label=axis.upper())
    ax.plot(time, no_gap_bridge(df, "acc_norm_g"), lw=0.9, color="black", label="norm")
    ax.set(title="All recorded RAW_IMU acceleration", ylabel="Acceleration (g)")
    ax.legend(ncol=4, fontsize=8)

    ax = axes[0, 1]
    for axis, color in zip("xyz", colors):
        ax.plot(time, no_gap_bridge(df, f"gyro_{axis}_dps"), lw=0.7, color=color, label=axis.upper())
    ax.plot(time, no_gap_bridge(df, "gyro_norm_dps"), lw=0.9, color="black", label="norm")
    threshold = summary["configuration"]["maneuver_threshold_dps"]
    ax.axhline(threshold, color="#CC79A7", ls="--", lw=1, label=f"motion flag {threshold:g}")
    ax.set(title="All recorded RAW_IMU angular rate", ylabel="Angular rate (deg/s)")
    ax.legend(ncol=3, fontsize=8)

    ax = axes[1, 0]
    ax.plot(time, no_gap_bridge(df, "acc_deviation_g"), lw=0.75, color="#0072B2", label="|a| − 1 g (unfiltered)")
    ax.plot(time, no_gap_bridge(df, "acc_rms_g"), lw=1.2, color="#D55E00", label=f"{summary['configuration']['rms_seconds']:g} s RMS")
    ax.set(title="Unfiltered acceleration deviation", ylabel="Acceleration (g)")
    ax.legend(fontsize=8)

    ax = axes[1, 1]
    ax.plot(time, no_gap_bridge(df, "gyro_norm_dps"), lw=0.75, color="#009E73", label="gyro magnitude (unfiltered)")
    ax.plot(time, no_gap_bridge(df, "gyro_rms_dps"), lw=1.2, color="#D55E00", label=f"{summary['configuration']['rms_seconds']:g} s RMS")
    ax.set(title="Unfiltered angular motion", ylabel="Angular rate (deg/s)")
    ax.legend(fontsize=8)

    ax = axes[2, 0]
    finite_db = [
        values[np.isfinite(values)]
        for _, _, values in acc_spectrogram
        if np.isfinite(values).any()
    ]
    mesh = None
    if finite_db:
        pooled = np.concatenate(finite_db)
        vmin, vmax = np.percentile(pooled, [5, 99])
        if vmax <= vmin:
            vmax = vmin + 1.0
        for centers, frequency, power_db in acc_spectrogram:
            mesh = ax.pcolormesh(
                centers,
                frequency,
                power_db,
                shading="auto",
                cmap="viridis",
                vmin=vmin,
                vmax=vmax,
            )
        fig.colorbar(mesh, ax=ax, label="Acceleration PSD (dB re g²/Hz)")
    else:
        ax.text(
            0.5,
            0.5,
            "Insufficient data for spectrogram",
            ha="center",
            va="center",
            transform=ax.transAxes,
        )
    reliable_spectrogram = summary["sampling"]["effective_update_nyquist_hz"]
    if reliable_spectrogram is not None:
        ax.axhline(
            reliable_spectrogram,
            color="white",
            ls="--",
            lw=1,
            label="effective update Nyquist",
        )
        ax.legend(fontsize=8)
    ax.set(title="Acceleration spectrogram (unfiltered input)", ylabel="Frequency (Hz)")

    ax = axes[2, 1]
    fa, aa = acc_asd
    fg, ag = gyro_asd
    ma = (fa > 0) & np.isfinite(aa) & (aa > 0)
    mg = (fg > 0) & np.isfinite(ag) & (ag > 0)
    l1 = ax.semilogy(fa[ma], aa[ma], color="#0072B2", label="Acceleration ASD")
    ax.set(xlabel="Frequency (Hz)", ylabel="Acceleration ASD (g/√Hz)", title="Welch ASD using all acquisition sessions")
    ax2 = ax.twinx()
    l2 = ax2.semilogy(fg[mg], ag[mg], color="#D55E00", label="Angular-rate ASD")
    ax2.set_ylabel("Angular-rate ASD ((deg/s)/√Hz)")
    nyquist = summary["sampling"]["effective_update_nyquist_hz"]
    extra = []
    if nyquist is not None:
        extra = [ax.axvline(nyquist, color="0.35", ls="--", lw=1, label="effective update Nyquist")]
        right = ax.get_xlim()[1]
        if nyquist < right:
            ax.axvspan(nyquist, right, color="0.5", alpha=0.10)
    lines = l1 + l2 + extra
    ax.legend(lines, [line.get_label() for line in lines], fontsize=8)
    ax.grid(True, which="both", alpha=0.25)

    time_axes([axes[0, 0], axes[0, 1], axes[1, 0], axes[1, 1], axes[2, 0]])
    dataset = summary["dataset"]
    fig.suptitle(
        "Inertial Measurement Analyzer\n"
        f"{dataset['start_recorded_time']} to {dataset['end_recorded_time']} | "
        f"{dataset['elapsed_hours']:.2f} h elapsed | {dataset['rows_evaluated']} rows | "
        f"{dataset['sessions']} acquisition sessions",
        fontsize=14,
    )
    fig.savefig(png, dpi=180)
    fig.savefig(pdf)
    plt.close(fig)


def finite(value: float | None) -> float | None:
    return float(value) if value is not None and np.isfinite(value) else None


def write_method(path: Path, summary: dict) -> None:
    dataset = summary["dataset"]
    sampling = summary["sampling"]
    path.write_text(
        f"""# Gremsy full-flight quicklook

The complete CSV is evaluated: {dataset['rows_evaluated']} rows covering
{dataset['elapsed_hours']:.2f} elapsed hours. No one-hour selection, band-pass,
low-pass, high-pass, smoothing, or interpolation is applied to recorded channels.

The rolling RMS is a descriptive statistic. Welch ASD uses a Hann window, 50%
overlap and linear detrending separately inside each acquisition session; session
PSDs are duration-weighted. This estimates a spectrum but does not filter the data.

Gaps over {summary['configuration']['gap_seconds']:g} seconds mark separate
acquisition sessions, preventing false lines or spectra through missing time
without removing rows. Median logging rate is
{sampling['median_logger_rate_hz']:.4g} Hz and effective RAW_IMU update Nyquist is
{sampling['effective_update_nyquist_hz']:.4g} Hz. Content above this effective
Nyquist is shown transparently but is not scientifically resolved.

Timezone suffix text is removed without conversion or time shifting.

This single-point low-rate quicklook is not mount transmissibility. A controlled
isolation test requires synchronized accelerometers on both sides of the mount,
rotor-speed reference, and camera image-quality measurements.
""",
        encoding="utf-8",
    )


def main() -> None:
    args = cli()
    if args.gap_seconds <= 0 or args.rms_seconds <= 0:
        raise ValueError("Gap seconds and RMS seconds must be positive.")
    output = args.output_dir.resolve()
    output.mkdir(parents=True, exist_ok=True)

    data = load_csv(args.csv.resolve(), args.gap_seconds)
    sessions = describe_sessions(data)
    data = rolling_statistics(data, sessions, args.rms_seconds, args.maneuver_threshold_dps)
    acc_asd = full_flight_asd(data, sessions, "acc_deviation_g")
    gyro_asd = full_flight_asd(data, sessions, "gyro_norm_dps")
    acc_spectrogram = full_flight_spectrogram(data, sessions, "acc_deviation_g")
    logger_rate = float(sessions["logger_rate_hz"].median())
    update_rate = float(sessions["imu_update_rate_hz"].median())
    update_nyquist = update_rate / 2.0
    elapsed_hours = (data["recorded_time"].iloc[-1] - data["recorded_time"].iloc[0]).total_seconds() / 3600.0

    summary = {
        "input_csv": str(args.csv.resolve()),
        "dataset": {
            "start_recorded_time": data["recorded_time"].iloc[0].isoformat(sep=" "),
            "end_recorded_time": data["recorded_time"].iloc[-1].isoformat(sep=" "),
            "elapsed_hours": elapsed_hours,
            "rows_in_csv": int(len(data)),
            "rows_evaluated": int(len(data)),
            "sessions": int(len(sessions)),
            "time_window_selection": "none; complete CSV evaluated",
            "signal_filter": "none",
        },
        "sampling": {
            "median_logger_rate_hz": finite(logger_rate),
            "logger_nyquist_hz": finite(logger_rate / 2.0),
            "median_imu_update_rate_hz": finite(update_rate),
            "effective_update_nyquist_hz": finite(update_nyquist),
        },
        "configuration": {
            "gap_seconds": args.gap_seconds,
            "rms_seconds": args.rms_seconds,
            "maneuver_threshold_dps": args.maneuver_threshold_dps,
        },
        "metrics": {
            "unfiltered_acceleration_deviation_rms_g": rms(data["acc_deviation_g"]),
            "unfiltered_acceleration_deviation_peak_abs_g": finite(data["acc_deviation_g"].abs().max()),
            "unfiltered_angular_rate_rms_dps": rms(data["gyro_norm_dps"]),
            "unfiltered_angular_rate_peak_dps": finite(data["gyro_norm_dps"].max()),
            "dominant_acceleration_frequency_below_update_nyquist_hz": dominant(acc_asd, update_nyquist),
            "dominant_angular_rate_frequency_below_update_nyquist_hz": dominant(gyro_asd, update_nyquist),
            "maneuver_fraction": finite(data["maneuver_flag"].mean()),
        },
        "limitations": [
            "Single-point IMU quicklook; it is not mount transmissibility.",
            "Intentional gimbal motion can overlap measured vibration.",
            "Frequencies above effective RAW_IMU update Nyquist are unresolved.",
        ],
    }

    sessions.to_csv(output / "sessions.csv", index=False, date_format="%Y-%m-%d %H:%M:%S.%f")
    data.to_csv(output / "evaluated_full_flight.csv", index=False, date_format="%Y-%m-%d %H:%M:%S.%f")
    (output / "summary.json").write_text(json.dumps(summary, indent=2, allow_nan=False), encoding="utf-8")
    write_method(output / "README.md", summary)
    make_plot(
        data,
        summary,
        acc_asd,
        gyro_asd,
        acc_spectrogram,
        output / "gremsy_full_flight_quicklook.png",
        output / "gremsy_full_flight_quicklook.pdf",
    )
    print(json.dumps(summary, indent=2, allow_nan=False))
    print(f"\nOutputs: {output}")


if __name__ == "__main__":
    main()
