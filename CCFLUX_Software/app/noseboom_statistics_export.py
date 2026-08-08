"""Publication-quality Noseboom statistical figure export."""

from __future__ import annotations

from pathlib import Path
import threading
from typing import Callable

from core import figure_standard
from core.logging_manager import LogLevel, ProcessingLogManager


class NoseboomStatisticsExportManager:
    """Own one non-blocking publication export job."""

    def __init__(self, logger: ProcessingLogManager, on_complete: Callable[[list[Path]], None] | None = None) -> None:
        self._logger = logger
        self._on_complete = on_complete
        self._lock = threading.RLock()
        self._state: dict[str, object] = {"status": "idle", "progress": 0.0, "step": "No publication export is running", "files": [], "error": None}
        self._files: dict[str, Path] = {}

    def start(self, payload: dict[str, object], destination: Path, flight_name: str, formats: tuple[str, ...], dpi: int) -> dict[str, object]:
        with self._lock:
            if self._state["status"] == "running":
                raise RuntimeError("A Noseboom publication export is already running")
            self._state = {"status": "running", "progress": 1.0, "step": "Scheduling publication-quality figure rendering", "files": [], "error": None}
            self._files = {}
        self._logger.log(LogLevel.INFO, "noseboom-statistics-export", f"Started Noseboom publication export: formats={formats}, dpi={dpi}", instrument="noseboom", processing_step="statistics-export")
        threading.Thread(target=self._run, args=(dict(payload), Path(destination), flight_name, formats, dpi), daemon=True, name="noseboom-statistics-export").start()
        return self.snapshot()

    def _run(self, payload: dict[str, object], destination: Path, flight_name: str, formats: tuple[str, ...], dpi: int) -> None:
        try:
            outputs = export_noseboom_statistics(payload, destination, flight_name, formats, dpi, self._progress)
            with self._lock:
                self._files = {path.name: path for path in outputs}
                self._state = {"status": "complete", "progress": 100.0, "step": "Publication figures are ready", "files": [{"name": path.name, "url": "/api/noseboom/statistics/export/download/" + path.name} for path in outputs], "error": None}
            if self._on_complete:
                self._on_complete(outputs)
            self._logger.log(LogLevel.INFO, "noseboom-statistics-export", f"Completed Noseboom publication export with {len(outputs)} files", instrument="noseboom", processing_step="statistics-export")
        except Exception as exc:
            with self._lock:
                self._state = {"status": "failed", "progress": 100.0, "step": "Publication export failed", "files": [], "error": str(exc)}
            self._logger.capture_exception("noseboom-statistics-export", f"Noseboom publication export failed: {exc}", exc, instrument="noseboom", processing_step="statistics-export")

    def _progress(self, percent: float, step: str) -> None:
        with self._lock:
            self._state["progress"] = max(0.0, min(100.0, float(percent)))
            self._state["step"] = str(step)

    def snapshot(self) -> dict[str, object]:
        with self._lock:
            return {**self._state, "files": list(self._state["files"])}

    def file(self, name: str) -> Path:
        safe_name = Path(name).name
        if safe_name != name:
            raise ValueError("Invalid Noseboom export filename")
        with self._lock:
            path = self._files.get(safe_name)
        if path is None or not path.is_file():
            raise ValueError("Requested Noseboom publication export is unavailable")
        return path


HISTOGRAM_DEFINITIONS = (
    ("wind_mps", "Wind speed", "m s$^{-1}$", "#2a9d8f", "#0b5d56"),
    ("wind_u_mps", "Wind u component", "m s$^{-1}$", "#457b9d", "#173f5f"),
    ("wind_v_mps", "Wind v component", "m s$^{-1}$", "#7b2cbf", "#4a148c"),
    ("wind_w_mps", "Vertical wind component", "m s$^{-1}$", "#e76f51", "#9d291c"),
    ("air_temp_degC", "Air temperature", "°C", "#f4a261", "#b45f06"),
    ("rel_humidity_pct", "Relative humidity", "%", "#43aa8b", "#1b6b54"),
)


def export_noseboom_statistics(
    payload: dict[str, object],
    destination: Path,
    flight_name: str,
    formats: tuple[str, ...],
    dpi: int,
    progress,
) -> list[Path]:
    """Render two bounded 7 × 6 inch scientific figure layouts."""
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    import numpy as np

    valid_formats = tuple(dict.fromkeys(value.lower() for value in formats))
    if not valid_formats or any(value not in {"pdf", "svg", "png"} for value in valid_formats):
        raise ValueError("Choose at least one of PDF, SVG, or PNG")
    if not 72 <= int(dpi) <= 1200:
        raise ValueError("DPI must be between 72 and 1200")

    destination = Path(destination)
    destination.mkdir(parents=True, exist_ok=True)
    bounds = payload.get("time_bounds") or {}
    browser_points = payload.get("points") or []
    fallback_start = browser_points[0].get("time") if browser_points else None
    fallback_end = browser_points[-1].get("time") if browser_points else None
    start = str(bounds.get("start") or fallback_start or "Unavailable")
    end = str(bounds.get("end") or fallback_end or "Unavailable")
    outputs: list[Path] = []
    # The campaign standard, in the serif the Noseboom figures are written in.
    # Sizes come from the standard rather than being restated here, so raising
    # the floor once raises it for this figure too.
    style = {
        **figure_standard.rc_parameters(),
        "font.family": "Times New Roman",
        "figure.facecolor": "white",
        "axes.facecolor": "white",
        "grid.alpha": 0.24,
    }

    with plt.rc_context(style):
        progress(10, "Preparing histogram data")
        fig, axes = plt.subplots(3, 2, figsize=(7, 6), constrained_layout=False)
        hist = payload.get("hist") or {}
        for axis, (key, title, unit, bar_color, curve_color) in zip(axes.flat, HISTOGRAM_DEFINITIONS):
            values = np.asarray(hist.get(key, ()), dtype=float)
            values = values[np.isfinite(values)]
            if values.size:
                counts, edges, _ = axis.hist(
                    values, bins=42, color=bar_color, alpha=0.78,
                    edgecolor="#263238", linewidth=0.45, label="Observed frequency",
                )
                centers = 0.5 * (edges[:-1] + edges[1:])
                offsets = np.arange(-4, 5, dtype=float)
                kernel = np.exp(-0.5 * (offsets / 1.35) ** 2)
                kernel /= kernel.sum()
                curve = np.convolve(counts, kernel, mode="same")
                axis.plot(
                    centers, curve, color=curve_color, linewidth=1.35,
                    label="Frequency distribution curve",
                )
                axis.legend(loc="best", frameon=False)
            else:
                axis.text(0.5, 0.5, "No valid samples", ha="center", va="center", transform=axis.transAxes)
            axis.set_title(title)
            axis.set_xlabel(f"{title} [{unit}]")
            axis.set_ylabel("Count")
            axis.tick_params(direction="out")
        fig.suptitle(flight_name, fontsize=10, y=0.985)
        fig.text(0.5, 0.008, f"Start Time: {start}    End Time: {end}", ha="center")
        fig.subplots_adjust(left=0.105, right=0.985, top=0.94, bottom=0.105, hspace=0.72, wspace=0.34)
        figure_standard.finalise(fig)
        for index, output_format in enumerate(valid_formats):
            progress(20 + 15 * index / max(1, len(valid_formats)), f"Writing histogram summary ({output_format.upper()})")
            path = _available_path(destination / f"{_safe_name(flight_name)}_noseboom_histogram_summary.{output_format}")
            fig.savefig(path, format=output_format, dpi=dpi, facecolor="white")
            outputs.append(path)
        plt.close(fig)

        progress(55, "Preparing frequency, altitude, and spectra")
        fig = plt.figure(figsize=(7, 6), constrained_layout=False)
        grid = fig.add_gridspec(2, 2, height_ratios=(1, 1.14))
        frequency_axis = fig.add_subplot(grid[0, 0])
        altitude_axis = fig.add_subplot(grid[0, 1])
        spectra_axis = fig.add_subplot(grid[1, :])

        frequency = payload.get("frequency") or []
        fy = np.asarray([row.get("frequency_hz", np.nan) for row in frequency], dtype=float)
        fx = np.arange(fy.size)
        if fy.size:
            frequency_axis.plot(fx, fy, color="#1565c0", linewidth=0.9)
        frequency_axis.set_title("Acquisition frequency")
        frequency_axis.set_xlabel("One-second frequency bin")
        frequency_axis.set_ylabel("Frequency [Hz]")

        altitude = payload.get("altitude_profile") or [
            {
                "gnss_msl_m": row.get("altitude_m"),
                "ins_ellipsoid_m": row.get("height_m"),
                "dtm_m": row.get("terrain_m"),
            }
            for row in browser_points
        ]
        ax = np.arange(len(altitude))
        gnss = np.asarray([row.get("gnss_msl_m", np.nan) for row in altitude], dtype=float)
        ins = np.asarray([row.get("ins_ellipsoid_m", np.nan) for row in altitude], dtype=float)
        dtm = np.asarray([row.get("dtm_m", np.nan) for row in altitude], dtype=float)
        finite_dtm = dtm[np.isfinite(dtm)]
        finite_flight = np.concatenate((gnss[np.isfinite(gnss)], ins[np.isfinite(ins)]))
        if finite_dtm.size:
            terrain_floor = max(0.0, float(np.nanpercentile(finite_dtm, 2)) - 20.0)
            altitude_axis.fill_between(
                ax, terrain_floor, dtm, where=np.isfinite(dtm),
                color="#588157", alpha=0.28, label="DTM terrain",
            )
            altitude_axis.plot(ax, dtm, color="#386641", linewidth=0.8)
        if np.isfinite(gnss).any():
            altitude_axis.plot(ax, gnss, color="#d62828", linewidth=1.0, label="GNSS MSL")
        if np.isfinite(ins).any():
            altitude_axis.plot(ax, ins, color="#5e3c99", linewidth=1.0, label="INS ellipsoid")
        limits = []
        if finite_dtm.size:
            limits.extend((terrain_floor - 20.0, float(np.nanpercentile(finite_dtm, 98)) + 30.0))
        if finite_flight.size:
            limits.extend((float(np.nanpercentile(finite_flight, 2)) - 20.0, float(np.nanpercentile(finite_flight, 98)) + 30.0))
        if limits and max(limits) > min(limits):
            altitude_axis.set_ylim(min(limits), max(limits))
        altitude_axis.set_title("Altitude profile")
        altitude_axis.set_xlabel("Time sample")
        altitude_axis.set_ylabel("Height [m]")
        if altitude_axis.get_legend_handles_labels()[0]:
            altitude_axis.legend(loc="best")

        spectra = payload.get("spectra") or {}
        _plot_spectrum(spectra_axis, spectra.get("wind_mps"), "Total wind speed", "#111111")
        _plot_spectrum(spectra_axis, spectra.get("wind_w_mps"), "Vertical wind component", "#2c7fb8")
        reference = spectra.get("wind_mps") or spectra.get("wind_w_mps") or {}
        frequencies = np.asarray(reference.get("frequency_hz", ()), dtype=float)
        powers = np.asarray(reference.get("psd", ()), dtype=float)
        valid = np.flatnonzero(np.isfinite(frequencies) & np.isfinite(powers) & (frequencies > 0) & (powers > 0))
        if valid.size:
            candidates = valid[frequencies[valid] >= 0.02]
            anchor = int(candidates[0] if candidates.size else valid[0])
            ref_x = np.geomspace(max(0.01, frequencies[valid].min()), frequencies[valid].max(), 300)
            ref_y = powers[anchor] * (ref_x / frequencies[anchor]) ** (-5.0 / 3.0)
            spectra_axis.loglog(ref_x, ref_y, color="#777777", linestyle="--", linewidth=1.1, label=r"$f^{-5/3}$ reference")
        spectra_axis.set_title("Noseboom wind power spectrum")
        spectra_axis.set_xlabel("Frequency [Hz]")
        spectra_axis.set_ylabel(r"PSD [(m s$^{-1}$)$^2$ Hz$^{-1}$]")
        if spectra_axis.get_legend_handles_labels()[0]:
            spectra_axis.legend(loc="best")
        fig.suptitle(flight_name, fontsize=10, y=0.985)
        fig.subplots_adjust(left=0.105, right=0.985, top=0.94, bottom=0.105, hspace=0.52, wspace=0.36)
        figure_standard.finalise(fig)
        for index, output_format in enumerate(valid_formats):
            progress(70 + 24 * index / max(1, len(valid_formats)), f"Writing scientific overview ({output_format.upper()})")
            path = _available_path(destination / f"{_safe_name(flight_name)}_noseboom_frequency_altitude_spectra.{output_format}")
            fig.savefig(path, format=output_format, dpi=dpi, facecolor="white")
            outputs.append(path)
        plt.close(fig)

        qc = payload.get("quality_control") or {}
        if qc.get("available"):
            progress(95, "Preparing quality control figure")
            outputs.extend(
                _render_quality_control(
                    qc, destination, flight_name, valid_formats, dpi, progress
                )
            )

    progress(100, "Publication figures are ready")
    return outputs


def _qc_times(values):
    import numpy as np
    import pandas as pd

    return pd.to_datetime(pd.Series(list(values or ())), errors="coerce", utc=True)


def _render_quality_control(
    qc: dict, destination: Path, flight_name: str,
    formats: tuple[str, ...], dpi: int, progress,
) -> list[Path]:
    """The five QC panels, in the three rows the workspace shows them in."""
    import matplotlib.dates as mdates
    import matplotlib.pyplot as plt
    import numpy as np

    # Three rows: the flow uncertainty across the width, then two pairs. The
    # width stays at seven inches so the figure drops into a manuscript column
    # without rescaling, which would shrink the labels below eight point.
    figure = plt.figure(figsize=(7, 8.4), constrained_layout=False)
    grid = figure.add_gridspec(3, 2, hspace=0.62, wspace=0.32)
    flow_axis = figure.add_subplot(grid[0, :])
    axes = [
        flow_axis,
        figure.add_subplot(grid[1, 0]),
        figure.add_subplot(grid[1, 1]),
        figure.add_subplot(grid[2, 0]),
        figure.add_subplot(grid[2, 1]),
    ]

    flow = qc.get("flow_uncertainty") or {}
    times = _qc_times(flow.get("time"))
    flow_axis.plot(times, np.asarray(flow.get("alpha", ()), dtype=float), linewidth=0.8, label="alpha")
    flow_axis.plot(times, np.asarray(flow.get("beta", ()), dtype=float), linewidth=0.8,
                   linestyle="--", label="beta")
    flow_axis.set_title(
        "(a) Flow-uncertainty angles · "
        f"{flow.get('samples_at_limit', 0):,} at the 90 deg limit"
    )
    flow_axis.set_ylabel("Uncertainty [deg]")

    direction = qc.get("direction_heading_track") or {}
    times = _qc_times(direction.get("time"))
    axis = axes[1]
    for key, label in (("wind_direction", "Wind"), ("heading", "Heading"), ("track", "Track")):
        axis.plot(times, np.asarray(direction.get(key, ()), dtype=float), linewidth=0.7, label=label)
    axis.set_title("(b) Direction, heading and track")
    axis.set_ylabel("Direction [deg]")
    axis.set_ylim(0, 360)
    axis.set_yticks([0, 90, 180, 270, 360])

    vertical = qc.get("vertical_wind") or {}
    times = _qc_times(vertical.get("time"))
    axis = axes[2]
    axis.plot(times, np.asarray(vertical.get("vertical_wind", ()), dtype=float),
              color="0.6", linewidth=0.5, alpha=0.6, label="Instantaneous")
    axis.plot(times, np.asarray(vertical.get("rolling_mean", ()), dtype=float),
              color="tab:red", linewidth=1.0, label="10-minute mean")
    axis.set_title(f"(c) Vertical wind · mean {vertical.get('mean', float('nan')):+.3f} m/s")
    axis.set_ylabel(r"Vertical wind [m s$^{-1}$]")

    for index, (key, letter, quantity, unit, limits) in enumerate((
        ("wind_speed_validation", "d", "Wind speed", r"[m s$^{-1}$]", None),
        ("wind_direction_validation", "e", "Wind direction", "[deg]", (0, 360)),
    )):
        section = qc.get(key) or {}
        axis = axes[3 + index]
        axis.plot(_qc_times(section.get("time")),
                  np.asarray(section.get("noseboom", ()), dtype=float),
                  linewidth=0.7, label="Noseboom")
        airport = section.get("airport") or {}
        reports = list(section.get("report_time") or ())
        if reports:
            axis.plot(_qc_times(reports),
                      np.asarray(section.get("report_value", ()), dtype=float),
                      "o", markersize=3,
                      label=f"{airport.get('icao', '')} {airport.get('name', '')}".strip())
        bias = section.get("bias")
        axis.set_title(
            f"({letter}) {quantity} vs METAR"
            + (f" · bias {bias:+.2f}" if isinstance(bias, (int, float)) else " · no report")
        )
        axis.set_ylabel(f"{quantity} {unit}")
        if limits:
            axis.set_ylim(*limits)
            axis.set_yticks([0, 90, 180, 270, 360])

    for axis in axes:
        axis.set_xlabel("UTC time")
        axis.xaxis.set_major_formatter(mdates.DateFormatter("%H:%M"))
        axis.grid(alpha=0.24)
        if axis.get_legend_handles_labels()[0]:
            axis.legend(loc="best", frameon=False)

    airport = (qc.get("metar") or {}).get("airport") or {}
    figure.suptitle(
        f"{flight_name} - Noseboom quality control"
        + (f" - reference {airport.get('icao')} {airport.get('name')}" if airport else ""),
        fontsize=10, y=0.992,
    )
    figure.subplots_adjust(left=0.105, right=0.985, top=0.935, bottom=0.06)
    figure_standard.finalise(figure)
    outputs: list[Path] = []
    for index, output_format in enumerate(formats):
        progress(95 + 4 * index / max(1, len(formats)),
                 f"Writing quality control figure ({output_format.upper()})")
        path = _available_path(
            destination / f"{_safe_name(flight_name)}_noseboom_quality_control.{output_format}"
        )
        figure.savefig(path, format=output_format, dpi=dpi, facecolor="white")
        outputs.append(path)
    plt.close(figure)
    return outputs


def _plot_spectrum(axis, product, label: str, color: str) -> None:
    import numpy as np

    if not product:
        return
    x = np.asarray(product.get("frequency_hz", ()), dtype=float)
    y = np.asarray(product.get("psd", ()), dtype=float)
    valid = np.isfinite(x) & np.isfinite(y) & (x > 0) & (y > 0)
    if valid.any():
        axis.loglog(x[valid], y[valid], color=color, linewidth=1.0, label=label)


def _available_path(path: Path) -> Path:
    """Avoid silently replacing an earlier scientific export."""
    if not path.exists():
        return path
    for number in range(2, 1000):
        candidate = path.with_name(
            f"{path.stem}_{number:02d}{path.suffix}"
        )
        if not candidate.exists():
            return candidate
    raise FileExistsError(
        f"No available publication-export filename remains for {path.name}"
    )

def _safe_name(value: str) -> str:
    cleaned = "".join(char if char.isalnum() or char in "-_" else "_" for char in str(value))
    return cleaned.strip("_") or "Flight"
