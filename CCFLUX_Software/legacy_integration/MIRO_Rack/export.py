"""Publication-quality figure export for the MIRO/Picarro dashboard."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
import re
from typing import Callable, Iterable

import matplotlib

matplotlib.use("Agg")
import matplotlib.dates as mdates
import matplotlib.pyplot as plt
from matplotlib.backends.backend_pdf import PdfPages
import numpy as np
import pandas as pd

import miro
import picarro

try:
    # The dashboard executes this module in-process, where core is importable.
    from core import figure_standard
except ModuleNotFoundError:  # Run straight from a shell in another directory.
    import sys

    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
    from core import figure_standard


FORMATS = ("pdf", "png", "svg")
Progress = Callable[[float, str], None] | None


def _flight_no(params: dict) -> str:
    """Return a compact display title while preserving the user's wording."""
    return " ".join(str((params or {}).get("flight_no") or "").split())[:80]


def _filename_component(value: str) -> str:
    """Convert a flight label to a Windows-safe filename component."""
    component = re.sub(r"[^A-Za-z0-9._-]+", "_", value).strip(" ._")[:80]
    if not component:
        return "Flight"
    reserved = {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)), *(f"LPT{i}" for i in range(1, 10))}
    if component.upper() in reserved:
        component = f"Flight_{component}"
    return component


def _notify(progress: Progress, fraction: float, message: str) -> None:
    if progress:
        progress(float(fraction), message)


def _timestamp_text(value) -> str:
    timestamp = pd.to_datetime(value, errors="coerce")
    return timestamp.strftime("%m-%d-%Y %H:%M:%S") if not pd.isna(timestamp) else "Unavailable"


def _figure_footer(fig: plt.Figure, start, end, center: str = "") -> None:
    fig.text(0.11, 0.018, f"Start: {_timestamp_text(start)}", ha="left", va="bottom")
    if center:
        fig.text(0.50, 0.018, center, ha="center", va="bottom")
    fig.text(0.98, 0.018, f"End: {_timestamp_text(end)}", ha="right", va="bottom")

def _style() -> None:
    # The width was already right; the text was not. Tick labels and legends
    # were set at seven point, which is below what the campaign standard says
    # can be read at seven inches. Sizes now come from the standard, in the
    # serif and the embeddable font types this export has always used.
    plt.rcParams.update(
        {
            **figure_standard.rc_parameters(),
            "font.family": "Times New Roman",
            "pdf.fonttype": 42,
            "ps.fonttype": 42,
            "svg.fonttype": "none",
            "axes.linewidth": 0.7,
            "xtick.major.width": 0.6,
            "ytick.major.width": 0.6,
            "axes.grid": False,
        }
    )


def _validate(output_directory: str | Path, formats: Iterable[str], dpi: int) -> tuple[Path, list[str], int]:
    output = Path(output_directory).expanduser().resolve()
    if not output.is_dir():
        raise FileNotFoundError(f"Output directory does not exist: {output}")
    selected = list(dict.fromkeys(str(item).lower().lstrip(".") for item in formats))
    invalid = [item for item in selected if item not in FORMATS]
    if invalid or not selected:
        raise ValueError("Choose at least one export format: PDF, PNG, or SVG.")
    dpi = int(dpi)
    if dpi < 72 or dpi > 2000:
        raise ValueError("DPI must be between 72 and 2000.")
    return output, selected, dpi


def _save(fig: plt.Figure, output: Path, stem: str, formats: list[str], dpi: int, progress: Progress) -> list[str]:
    paths: list[str] = []
    for index, extension in enumerate(formats, start=1):
        path = output / f"{stem}.{extension}"
        _notify(progress, 0.78 + 0.20 * (index - 1) / max(1, len(formats)), f"Export: writing {path.name}")
        fig.savefig(path, format=extension, dpi=dpi, facecolor="white", bbox_inches=None)
        paths.append(str(path))
    plt.close(fig)
    return paths


def _with_error(value: float, error: float, unit: str = "") -> str:
    """A fitted number and its standard error, short enough for the panel."""
    text = f"{value:.4g}"
    if np.isfinite(error):
        text += f" ± {error:.3g}"
    return f"{text} {unit}".strip()


def _paired_minute_data(mdata: pd.DataFrame, pdata: pd.DataFrame, gas: str, params: dict) -> pd.DataFrame:
    mseries = miro.comparison_series(
        mdata, f"{gas} wet", params.get("miro_start"), params.get("miro_end"), 30.0
    )
    pseries = picarro.comparison_series(
        pdata, gas, params.get("picarro_start"), params.get("picarro_end")
    )
    mminute = mseries.resample("1min").mean().rename("miro")
    pminute = pseries.resample("1min").mean().rename("picarro")
    return pd.concat([mminute, pminute], axis=1, join="inner").dropna()


def comparison_figure(mdata: pd.DataFrame, pdata: pd.DataFrame, params: dict, progress: Progress = None) -> plt.Figure:
    _style()
    flight_no = _flight_no(params)
    # Two rows rather than three panels with insets. The rolling correlation
    # used to be a histogram drawn a third of the size of its host panel, whose
    # own labels were set at seven and eight point; at two inches of panel there
    # is no size at which an inset carries readable axes. Given a row of its own
    # it is a panel like any other, and everything on the page is nine point.
    fig, axes = plt.subplots(
        2, 3, figsize=(figure_standard.PAGE_WIDTH_INCHES, 5.6),
        constrained_layout=False,
    )
    units = {"CO2": "ppm", "CH4": "ppm", "H2O": "%"}
    paired_starts, paired_ends = [], []
    for index, (axis, gas) in enumerate(zip(axes[0], ("CO2", "CH4", "H2O")), start=1):
        _notify(progress, 0.08 + index * 0.18, f"Export: preparing {gas} comparison")
        joined = _paired_minute_data(mdata, pdata, gas, params)
        if len(joined) < 3:
            raise ValueError(f"Not enough paired one-minute {gas} values for export.")
        paired_starts.append(joined.index.min())
        paired_ends.append(joined.index.max())
        x = joined.picarro.to_numpy(float)
        y = joined.miro.to_numpy(float)
        slope, intercept = np.polyfit(x, y, 1)
        predicted = slope * x + intercept
        residuals = y - predicted
        residual_sum_squares = float(np.sum(residuals**2))
        denominator = float(np.sum((y - y.mean()) ** 2))
        r_squared = 1.0 - residual_sum_squares / denominator if denominator > 0 else np.nan
        slope_se = intercept_se = np.nan
        x_sum_squares = float(np.sum((x - x.mean()) ** 2))
        if len(x) > 2 and x_sum_squares > 0:
            residual_variance = residual_sum_squares / (len(x) - 2)
            slope_se = float(np.sqrt(residual_variance / x_sum_squares))
            intercept_se = float(
                np.sqrt(residual_variance * (1.0 / len(x) + x.mean() ** 2 / x_sum_squares))
            )
        axis.scatter(
            x,
            y,
            s=6,
            c="#8931ef",
            edgecolors="#159447",
            linewidths=0.45,
            alpha=0.82,
            rasterized=False,
        )
        fit_x = np.array([x.min(), x.max()])
        axis.plot(fit_x, slope * fit_x + intercept, color="#d62728", linewidth=1.0)
        # Four significant figures, not three decimals. A CO2 offset of
        # 733.796 +/- 51.200 ppm set at nine point is wider than the two-inch
        # panel it sits in, and ran across its neighbour.
        statistics = (
            f"slope {_with_error(slope, slope_se)}\n"
            f"offset {_with_error(intercept, intercept_se, units[gas])}\n"
            + rf"$R^2$ = {r_squared:.3f}"
        )
        axis.text(
            0.04,
            0.95,
            statistics,
            transform=axis.transAxes,
            ha="left",
            va="top",
            linespacing=1.25,
            bbox={"facecolor": "white", "edgecolor": "none", "alpha": 0.78, "pad": 1.5},
        )
        axis.set_title(gas, fontweight="bold")
        axis.set_xlabel(f"Picarro {gas} ({units[gas]})", color="#159447")
        axis.set_ylabel(f"MIRO {gas} ({units[gas]})", color="#8931ef")
        axis.tick_params(axis="x", direction="out", length=2.5, colors="#159447")
        axis.tick_params(axis="y", direction="out", length=2.5, colors="#8931ef")
        axis.grid(True, linewidth=0.35, alpha=0.32)

        below = axes[1][index - 1]
        rolling = joined.picarro.rolling("30min", min_periods=15).corr(joined.miro)
        values = rolling.replace([np.inf, -np.inf], np.nan).dropna().clip(-1, 1).to_numpy(float)
        if values.size:
            counts, edges = np.histogram(values, bins=min(24, max(8, int(round(np.sqrt(values.size))))))
            centers = (edges[:-1] + edges[1:]) / 2
            kernel_x = np.arange(-3, 4, dtype=float)
            kernel = np.exp(-0.5 * (kernel_x / 1.2) ** 2)
            kernel /= kernel.sum()
            smooth = np.convolve(counts.astype(float), kernel, mode="same")
            below.bar(centers, counts, width=np.diff(edges) * 0.9, color="#64c7bd", edgecolor="none", alpha=0.65)
            below.plot(centers, smooth, color="#087f78", linewidth=1.0)
            correlation_min = float(values.min())
            correlation_max = float(values.max())
            if np.isclose(correlation_min, correlation_max):
                padding = max(0.01, abs(correlation_min) * 0.02)
                correlation_min = max(-1.0, correlation_min - padding)
                correlation_max = min(1.0, correlation_max + padding)
                if np.isclose(correlation_min, correlation_max):
                    correlation_min, correlation_max = -1.0, 1.0
            below.set_xlim(correlation_min, correlation_max)
        else:
            below.text(0.5, 0.5, "No 30-minute window\nheld enough pairs",
                       transform=below.transAxes, ha="center", va="center")
            below.set_xlim(-1.0, 1.0)
        below.set_title(f"{gas}: 30-min rolling $r$")
        below.set_xlabel("Pearson r")
        below.set_ylabel("Windows")
        below.tick_params(direction="out", length=2.5)
        below.grid(True, linewidth=0.35, alpha=0.32)
        below.spines["top"].set_visible(False)
        below.spines["right"].set_visible(False)
    if flight_no:
        fig.suptitle(flight_no, fontweight="bold", y=0.985)
    _figure_footer(fig, min(paired_starts), max(paired_ends))
    fig.subplots_adjust(
        left=0.10, right=0.985, bottom=0.115,
        top=0.895 if flight_no else 0.945, wspace=0.42, hspace=0.52,
    )
    figure_standard.finalise(fig)
    return fig


def _downsample_series(series: pd.Series, maximum: int = 50000) -> pd.Series:
    if len(series) <= maximum:
        return series
    indices = np.unique(np.linspace(0, len(series) - 1, maximum).astype(int))
    return series.iloc[indices]


def picarro_figure(pdata: pd.DataFrame, params: dict, progress: Progress = None) -> plt.Figure:
    _style()
    flight_no = _flight_no(params)
    fig, axes = plt.subplots(
        3, 1, figsize=(figure_standard.PAGE_WIDTH_INCHES, 6.4), sharex=True,
        constrained_layout=False,
    )
    units = {"CO2": "ppm", "CH4": "ppm", "H2O": "%"}
    series_starts, series_ends = [], []
    for index, (axis, gas) in enumerate(zip(axes, ("CO2", "CH4", "H2O")), start=1):
        _notify(progress, 0.10 + index * 0.19, f"Export: preparing Picarro {gas}")
        series = picarro.comparison_series(
            pdata, gas, params.get("picarro_start"), params.get("picarro_end")
        ).dropna()
        if series.empty:
            raise ValueError(f"No Picarro {gas} values are available in the selected timeframe.")
        series_starts.append(series.index.min())
        series_ends.append(series.index.max())
        display = _downsample_series(series)
        # Fifty thousand points of vector geometry per trace made a PDF slow to
        # open for detail no reader can resolve; the axes and labels stay text.
        axis.plot(display.index, display.to_numpy(float), color="#159447",
                  linewidth=0.55, rasterized=True)
        axis.set_title(f"Picarro {gas}", loc="left", fontweight="bold", pad=2)
        axis.set_ylabel(f"{gas} ({units[gas]})")
        axis.tick_params(direction="out", length=2.5)
        axis.grid(True, linewidth=0.35, alpha=0.32)
    axes[-1].set_xlabel("Recorded time")
    # Six labels at most across seven inches: two lines of date and time at nine
    # point need about an inch apiece.
    locator = mdates.AutoDateLocator(minticks=3, maxticks=6)
    axes[-1].xaxis.set_major_locator(locator)
    axes[-1].xaxis.set_major_formatter(mdates.DateFormatter("%m-%d\n%H:%M"))
    if flight_no:
        fig.suptitle(flight_no, fontweight="bold", y=0.985)
    _figure_footer(fig, min(series_starts), max(series_ends))
    fig.subplots_adjust(left=0.115, right=0.985, bottom=0.135,
                        top=0.925 if flight_no else 0.96, hspace=0.30)
    figure_standard.finalise(fig)
    return fig


def miro_figure(
    result: dict,
    params: dict,
    page_number: int | None = None,
    total_pages: int | None = None,
    pdf_page: bool = False,
) -> plt.Figure:
    """Build one publication page containing the four MIRO quick-look plots."""
    _style()
    gas = str(result["gas"])
    unit = str(result["unit"])
    cutoff = int(result["smooth_seconds"])
    flight_no = _flight_no(params)
    fig, axes = plt.subplots(
        2, 2, figsize=(figure_standard.PAGE_WIDTH_INCHES, 6.2),
        constrained_layout=False,
    )
    ambient_axis, residual_axis, allan_axis, psd_axis = axes.flat

    times = pd.to_datetime(result["series"]["time"], errors="coerce")
    ambient = np.asarray([np.nan if value is None else value for value in result["series"]["ambient"]], dtype=float)
    residual = np.asarray([np.nan if value is None else value for value in result["series"]["residual"]], dtype=float)
    ambient_axis.plot(times, ambient, color="#145ee8", linewidth=0.45,
                      rasterized=True)
    ambient_axis.set_title("Ambient concentration", loc="left", fontweight="bold")
    ambient_axis.set_ylabel(f"{gas} ({unit})")
    residual_axis.plot(times, residual, color="#145ee8", linewidth=0.45,
                       rasterized=True)
    residual_axis.axhline(0.0, color="#666666", linewidth=0.55, linestyle="--")
    residual_axis.set_title(f"Residual after {cutoff} s detrending", loc="left",
                            fontweight="bold")
    residual_axis.set_ylabel(f"Residual ({unit})")
    for axis in (ambient_axis, residual_axis):
        axis.set_xlabel("Recorded time")
        # Four labels across a three-inch panel: two lines of date and time at
        # nine point run to about half an inch each. Asking for fewer left the
        # locator free to place a single tick, which states almost nothing.
        locator = mdates.AutoDateLocator(minticks=3, maxticks=4)
        axis.xaxis.set_major_locator(locator)
        axis.xaxis.set_major_formatter(mdates.DateFormatter("%m-%d\n%H:%M"))

    allan = result["allan"]
    tau = np.asarray(allan.get("tau", []), dtype=float)
    deviation = np.asarray(allan.get("ambient", []), dtype=float)
    valid = np.isfinite(tau) & np.isfinite(deviation) & (tau > 0) & (deviation > 0)
    if valid.any():
        allan_axis.loglog(tau[valid], deviation[valid], color="#145ee8", marker="o", markersize=2.0, linewidth=0.65)
    ref_tau = tau
    ref_dev = np.asarray(allan.get("white_noise", []), dtype=float)
    ref_valid = np.isfinite(ref_tau) & np.isfinite(ref_dev) & (ref_tau > 0) & (ref_dev > 0)
    if ref_valid.any():
        allan_axis.loglog(ref_tau[ref_valid], ref_dev[ref_valid], color="#444444", linestyle="--", linewidth=0.65, label=r"$\tau^{-1/2}$ reference")
        allan_axis.legend(frameon=False, loc="best")
    allan_axis.set_title("Allan deviation of ambient data", loc="left", fontweight="bold")
    allan_axis.set_xlabel(r"Averaging time, $\tau$ (s)")
    allan_axis.set_ylabel(f"Allan deviation ({unit})")

    frequency = np.asarray(result["psd"].get("frequency", []), dtype=float)
    power = np.asarray(result["psd"].get("power", []), dtype=float)
    psd_valid = np.isfinite(frequency) & np.isfinite(power) & (frequency > 0) & (power > 0)
    if psd_valid.any():
        psd_axis.loglog(frequency[psd_valid], power[psd_valid], color="#8931ef", linewidth=0.65)
    psd_axis.set_title("Residual power spectral density", loc="left",
                       fontweight="bold")
    psd_axis.set_xlabel("Frequency (Hz)")
    psd_axis.set_ylabel(f"PSD ({unit}$^2$ Hz$^{{-1}}$)")

    for axis in axes.flat:
        axis.grid(True, linewidth=0.35, alpha=0.32)
        axis.tick_params(direction="out", length=2.5)
    if pdf_page:
        page_title = f"{gas}_{flight_no}" if flight_no else gas
        fig.suptitle(page_title, fontweight="bold", y=0.985)
        valid_times = times[~pd.isna(times)]
        start = valid_times.min() if len(valid_times) else pd.NaT
        end = valid_times.max() if len(valid_times) else pd.NaT
        _figure_footer(fig, start, end, f"Page {page_number} of {total_pages}")
    elif flight_no:
        fig.suptitle(f"{flight_no} - {gas}", fontweight="bold", y=0.985)
    fig.subplots_adjust(
        left=0.125,
        right=0.98,
        bottom=0.135 if pdf_page else 0.115,
        top=0.925 if (pdf_page or flight_no) else 0.96,
        wspace=0.34,
        hspace=0.42,
    )
    figure_standard.finalise(fig)
    return fig


def _miro_export_stem(gas: str, flight_component: str, stamp: str) -> str:
    gas_component = _filename_component(gas)
    return f"MIRO_{gas_component}_{flight_component}_{stamp}" if flight_component else f"MIRO_{gas_component}_{stamp}"


def _export_miro_figures(
    output: Path,
    formats: list[str],
    dpi: int,
    mdata: pd.DataFrame,
    params: dict,
    stamp: str,
    flight_component: str,
    progress: Progress,
) -> list[str]:
    gases = list(miro.GAS_COLUMNS)
    missing = [gas for gas in gases if gas not in mdata.columns]
    if missing:
        raise ValueError("MIRO export is missing compound columns: " + ", ".join(missing))
    paths: list[str] = []
    pdf_path = output / (f"MIRO_all_compounds_{flight_component}_{stamp}.pdf" if flight_component else f"MIRO_all_compounds_{stamp}.pdf")
    pdf = PdfPages(pdf_path) if "pdf" in formats else None
    try:
        for index, gas in enumerate(gases, start=1):
            _notify(progress, 0.04 + 0.88 * (index - 1) / len(gases), f"MIRO export: analyzing {gas} ({index}/{len(gases)})")
            result = miro.analyze(
                mdata,
                gas,
                float(params.get("smooth_seconds", 300)),
                params.get("miro_start"),
                params.get("miro_end"),
                30.0,
            )
            fig = miro_figure(
                result,
                params,
                page_number=index,
                total_pages=len(gases),
                pdf_page=True,
            )
            if pdf is not None:
                pdf.savefig(fig, dpi=dpi, facecolor="white", bbox_inches=None)
            stem = _miro_export_stem(gas, flight_component, stamp)
            for extension in formats:
                if extension == "pdf":
                    continue
                path = output / f"{stem}.{extension}"
                fig.savefig(path, format=extension, dpi=dpi, facecolor="white", bbox_inches=None)
                paths.append(str(path))
            plt.close(fig)
        if pdf is not None:
            paths.insert(0, str(pdf_path))
    finally:
        if pdf is not None:
            pdf.close()
    _notify(progress, 0.98, "MIRO export: finalizing files")
    return paths


def export_figures(
    scope: str,
    output_directory: str | Path,
    formats: Iterable[str],
    dpi: int,
    mdata: pd.DataFrame | None,
    pdata: pd.DataFrame | None,
    params: dict,
    progress: Progress = None,
) -> list[str]:
    output, selected, dpi = _validate(output_directory, formats, dpi)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    flight_no = _flight_no(params)
    flight_component = _filename_component(flight_no) if flight_no else ""
    if scope == "miro":
        if mdata is None:
            raise RuntimeError("MIRO data are required for MIRO export.")
        return _export_miro_figures(output, selected, dpi, mdata, params, stamp, flight_component, progress)
    if scope == "comparison":
        if mdata is None or pdata is None:
            raise RuntimeError("MIRO and Picarro data are required for comparison export.")
        fig = comparison_figure(mdata, pdata, params, progress)
        stem = f"MIRO_Picarro_comparison_{flight_component}_{stamp}" if flight_component else f"MIRO_Picarro_comparison_{stamp}"
    elif scope == "picarro":
        if pdata is None:
            raise RuntimeError("Picarro data are required for Picarro export.")
        fig = picarro_figure(pdata, params, progress)
        stem = f"Picarro_timeseries_{flight_component}_{stamp}" if flight_component else f"Picarro_timeseries_{stamp}"
    else:
        raise ValueError("Unknown export section.")
    return _save(fig, output, stem, selected, dpi, progress)