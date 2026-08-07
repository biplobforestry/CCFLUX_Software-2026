"""Publication-quality INS Gimbal figure export.

One page of the workspace exports as one bounded figure. The width is fixed at
seven inches so the figure drops into a manuscript column without rescaling,
and nothing is drawn below eight point, which is the smallest size that still
reads after that width is honoured.
"""

from __future__ import annotations

from pathlib import Path
import threading
from typing import Callable

from core.logging_manager import LogLevel, ProcessingLogManager

# A figure wider than a manuscript column has to be shrunk by the typesetter,
# which takes the type below the size it was checked at. Seven inches is the
# width, eight point the floor, and 1500 the highest resolution offered.
FIGURE_WIDTH_INCHES = 7.0
MINIMUM_FONT_POINTS = 8
MAXIMUM_DPI = 1500
MINIMUM_DPI = 72
EXPORT_FORMATS = ("pdf", "png", "svg")

VIEW_TITLES = {
    "overview": "Recorded RAW_IMU acceleration and angular rate",
    "motion": "Unfiltered motion diagnostics",
    "frequency": "Acceleration spectrogram and amplitude spectral density",
}
AXIS_COLOURS = {
    "x": "#0072B2", "y": "#D55E00", "z": "#009E73",
    "norm": "#111827", "rms": "#CC79A7",
}


class InsGimbalExportManager:
    """Own one non-blocking INS Gimbal figure export job."""

    def __init__(
        self,
        logger: ProcessingLogManager,
        on_complete: Callable[[list[Path]], None] | None = None,
    ) -> None:
        self._logger = logger
        self._on_complete = on_complete
        self._lock = threading.RLock()
        self._state: dict[str, object] = {
            "status": "idle", "progress": 0.0,
            "step": "No INS Gimbal export is running", "files": [], "error": None,
        }
        self._files: dict[str, Path] = {}

    def start(
        self,
        payload: dict[str, object],
        destination: Path,
        flight_name: str,
        view: str,
        formats: tuple[str, ...],
        dpi: int,
    ) -> dict[str, object]:
        with self._lock:
            if self._state["status"] == "running":
                raise RuntimeError("An INS Gimbal export is already running")
            self._state = {
                "status": "running", "progress": 1.0,
                "step": "Scheduling publication-quality figure rendering",
                "files": [], "error": None,
            }
            self._files = {}
        self._logger.log(
            LogLevel.INFO, "ins-gimbal-export",
            f"Started INS Gimbal export: view={view}, formats={formats}, dpi={dpi}",
            instrument="ins_gimbal", processing_step="figure-export",
        )
        threading.Thread(
            target=self._run,
            args=(dict(payload), Path(destination), flight_name, view, formats, dpi),
            daemon=True, name="ins-gimbal-export",
        ).start()
        return self.snapshot()

    def _run(
        self, payload: dict[str, object], destination: Path, flight_name: str,
        view: str, formats: tuple[str, ...], dpi: int,
    ) -> None:
        try:
            outputs = export_ins_gimbal_figure(
                payload, destination, flight_name, view, formats, dpi, self._progress
            )
            with self._lock:
                self._files = {path.name: path for path in outputs}
                self._state = {
                    "status": "complete", "progress": 100.0,
                    "step": "Publication figures are ready",
                    "files": [
                        {
                            "name": path.name,
                            "url": "/api/ins-gimbal/export/download/" + path.name,
                        }
                        for path in outputs
                    ],
                    "error": None,
                }
            if self._on_complete:
                self._on_complete(outputs)
            self._logger.log(
                LogLevel.INFO, "ins-gimbal-export",
                f"Completed INS Gimbal export with {len(outputs)} files",
                instrument="ins_gimbal", processing_step="figure-export",
            )
        except Exception as exc:
            with self._lock:
                self._state = {
                    "status": "failed", "progress": 100.0,
                    "step": "Publication export failed", "files": [],
                    "error": str(exc),
                }
            self._logger.capture_exception(
                "ins-gimbal-export", f"INS Gimbal export failed: {exc}", exc,
                instrument="ins_gimbal", processing_step="figure-export",
            )

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
            raise ValueError("Invalid INS Gimbal export filename")
        with self._lock:
            path = self._files.get(safe_name)
        if path is None or not path.is_file():
            raise ValueError("Requested INS Gimbal publication export is unavailable")
        return path


def publication_style() -> dict[str, object]:
    """Matplotlib settings that hold the eight point floor."""
    return {
        "font.family": "Times New Roman",
        "font.size": MINIMUM_FONT_POINTS,
        "axes.titlesize": MINIMUM_FONT_POINTS + 1,
        "axes.labelsize": MINIMUM_FONT_POINTS,
        "xtick.labelsize": MINIMUM_FONT_POINTS,
        "ytick.labelsize": MINIMUM_FONT_POINTS,
        "legend.fontsize": MINIMUM_FONT_POINTS,
        "figure.titlesize": MINIMUM_FONT_POINTS + 2,
        "figure.facecolor": "white",
        "axes.facecolor": "white",
        "axes.grid": True,
        "grid.alpha": 0.24,
        "lines.linewidth": 0.7,
    }


def validate_request(
    formats: tuple[str, ...], dpi: int
) -> tuple[tuple[str, ...], int]:
    """Reject a request the figure limits cannot honour."""
    chosen = tuple(dict.fromkeys(str(value).lower() for value in formats))
    if not chosen or any(value not in EXPORT_FORMATS for value in chosen):
        raise ValueError("Choose at least one of PDF, PNG, or SVG")
    resolution = int(dpi)
    if not MINIMUM_DPI <= resolution <= MAXIMUM_DPI:
        raise ValueError(f"DPI must be between {MINIMUM_DPI} and {MAXIMUM_DPI}")
    return chosen, resolution


def export_ins_gimbal_figure(
    payload: dict[str, object],
    destination: Path,
    flight_name: str,
    view: str,
    formats: tuple[str, ...],
    dpi: int,
    progress,
) -> list[Path]:
    """Render one seven-inch figure for the named workspace page."""
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.dates as mdates
    import matplotlib.pyplot as plt
    import numpy as np

    chosen, resolution = validate_request(formats, dpi)
    if view not in VIEW_TITLES:
        raise ValueError(f"Unknown INS Gimbal view: {view}")
    destination = Path(destination)
    destination.mkdir(parents=True, exist_ok=True)

    series = payload.get("series") or {}
    times = _times(series.get("time") or ())
    sessions = np.asarray(series.get("session") or (), dtype=float)
    summary = payload.get("summary") or {}
    configuration = summary.get("configuration") or {}
    outputs: list[Path] = []

    def value(key):
        return np.asarray(series.get(key) or (), dtype=float)

    def draw_time_axis(axis):
        axis.xaxis.set_major_formatter(mdates.DateFormatter("%H:%M"))
        axis.set_xlabel("Recorded UTC")

    def draw_by_session(axis, key, label, colour, width=0.7):
        """One line per acquisition session, so gaps are never bridged."""
        data = value(key)
        if data.size != len(times):
            return
        first = True
        for identifier in _session_ids(sessions):
            inside = sessions == identifier if sessions.size else np.ones(
                data.size, dtype=bool
            )
            axis.plot(
                [times[index] for index in np.flatnonzero(inside)],
                data[inside], color=colour, linewidth=width,
                label=label if first else None,
            )
            first = False

    with plt.rc_context(publication_style()):
        progress(12, f"Preparing the {view} figure")
        if view == "frequency":
            figure, axes = plt.subplots(
                2, 1, figsize=(FIGURE_WIDTH_INCHES, 6.4), constrained_layout=True
            )
            _draw_spectrogram(axes[0], payload, summary, np, mdates)
            _draw_asd(axes[1], payload, summary, np)
        else:
            figure, axes = plt.subplots(
                2, 1, figsize=(FIGURE_WIDTH_INCHES, 5.6), sharex=True,
                constrained_layout=True,
            )
            if view == "overview":
                for key, label, colour in (
                    ("acc_x_g", "X", AXIS_COLOURS["x"]),
                    ("acc_y_g", "Y", AXIS_COLOURS["y"]),
                    ("acc_z_g", "Z", AXIS_COLOURS["z"]),
                    ("acc_norm_g", "Norm", AXIS_COLOURS["norm"]),
                ):
                    draw_by_session(axes[0], key, label, colour)
                axes[0].set_ylabel("Acceleration [g]")
                axes[0].set_title("All recorded RAW_IMU acceleration")
                for key, label, colour in (
                    ("gyro_x_dps", "X", AXIS_COLOURS["x"]),
                    ("gyro_y_dps", "Y", AXIS_COLOURS["y"]),
                    ("gyro_z_dps", "Z", AXIS_COLOURS["z"]),
                    ("gyro_norm_dps", "Norm", AXIS_COLOURS["norm"]),
                ):
                    draw_by_session(axes[1], key, label, colour)
                threshold = float(configuration.get("maneuver_threshold_dps") or 10.0)
                axes[1].axhline(
                    threshold, color=AXIS_COLOURS["rms"], linewidth=1.0,
                    linestyle="--", label=f"Motion flag {threshold:g} deg/s",
                )
                axes[1].set_ylabel("Angular rate [deg/s]")
                axes[1].set_title("All recorded RAW_IMU angular rate")
            else:
                seconds = float(configuration.get("rms_seconds") or 30.0)
                draw_by_session(
                    axes[0], "acc_deviation_g", "|a| − 1 g (unfiltered)",
                    AXIS_COLOURS["x"],
                )
                draw_by_session(
                    axes[0], "acc_rms_g", f"{seconds:g} s RMS", AXIS_COLOURS["y"], 1.1
                )
                axes[0].set_ylabel("Acceleration [g]")
                axes[0].set_title("Unfiltered acceleration deviation")
                draw_by_session(
                    axes[1], "gyro_norm_dps", "Gyro magnitude (unfiltered)",
                    AXIS_COLOURS["z"],
                )
                draw_by_session(
                    axes[1], "gyro_rms_dps", f"{seconds:g} s RMS",
                    AXIS_COLOURS["y"], 1.1,
                )
                axes[1].set_ylabel("Angular rate [deg/s]")
                axes[1].set_title("Unfiltered angular motion")
            for axis in axes:
                axis.legend(
                    loc="best", frameon=False, fontsize=MINIMUM_FONT_POINTS, ncol=2
                )
            draw_time_axis(axes[1])

        progress(58, "Labelling the figure")
        dataset = summary.get("dataset") or {}
        start = _stamp_text(dataset.get("start_recorded_time") or _first(times))
        end = _stamp_text(dataset.get("end_recorded_time") or _last(times))
        # The interval belongs in the title. Written along the bottom edge it
        # landed on the x-axis label, which constrained_layout does not reserve
        # space for.
        figure.suptitle(
            f"{flight_name} · {VIEW_TITLES[view]}\n"
            f"Recorded UTC {start} to {end}"
        )

        stem = f"{_safe_name(flight_name)}_ins_gimbal_{view}"
        for index, suffix in enumerate(chosen):
            target = _available_path(destination / f"{stem}.{suffix}")
            figure.savefig(target, dpi=resolution, format=suffix)
            outputs.append(target)
            progress(
                62 + 36 * (index + 1) / len(chosen), f"Wrote {target.name}"
            )
        plt.close(figure)

    progress(100, "Publication figures are ready")
    return outputs


def _draw_spectrogram(axis, payload, summary, np, mdates) -> None:
    """One pcolormesh per acquisition session, on the shared colour limits."""
    spectrogram = payload.get("spectrogram") or {}
    limits = spectrogram.get("color_limits_db") or [None, None]
    mesh = None
    for session in spectrogram.get("sessions") or ():
        stamps = _times(session.get("time") or ())
        frequency = np.asarray(session.get("frequency_hz") or (), dtype=float)
        power = np.asarray(session.get("power_db_g2_hz") or (), dtype=float)
        if not stamps or not frequency.size or not power.size:
            continue
        mesh = axis.pcolormesh(
            np.asarray(mdates.date2num(stamps)), frequency, power,
            cmap="viridis", shading="nearest",
            vmin=limits[0] if limits[0] is not None else None,
            vmax=limits[1] if limits[1] is not None else None,
        )
    axis.xaxis.set_major_locator(mdates.AutoDateLocator())
    axis.xaxis.set_major_formatter(mdates.DateFormatter("%H:%M"))
    axis.set_xlabel("Recorded UTC")
    axis.set_ylabel("Frequency [Hz]")
    axis.set_title("Acceleration spectrogram · unfiltered input")
    axis.grid(False)
    if mesh is not None:
        bar = axis.figure.colorbar(mesh, ax=axis, pad=0.015)
        bar.set_label("Acceleration PSD [dB re g$^2$/Hz]")
        bar.ax.tick_params(labelsize=MINIMUM_FONT_POINTS)
    nyquist = _number((summary.get("sampling") or {}).get("effective_update_nyquist_hz"))
    if nyquist is not None:
        axis.axhline(nyquist, color="white", linewidth=1.0, linestyle="--")
        axis.annotate(
            f"Effective update Nyquist {nyquist:,.3f} Hz",
            xy=(0.995, nyquist), xycoords=("axes fraction", "data"),
            ha="right", va="bottom", fontsize=MINIMUM_FONT_POINTS,
            color="#172431",
            bbox={"facecolor": "white", "alpha": 0.8, "edgecolor": "none", "pad": 1.5},
        )


def _draw_asd(axis, payload, summary, np) -> None:
    """Both amplitude spectral densities, each on its own decade axis."""
    asd = payload.get("asd") or {}
    acceleration = asd.get("acceleration") or {}
    angular = asd.get("angular_rate") or {}
    left = np.asarray(acceleration.get("frequency_hz") or (), dtype=float)
    left_amplitude = np.asarray(
        acceleration.get("amplitude_g_sqrt_hz") or (), dtype=float
    )
    if left.size and left.size == left_amplitude.size:
        axis.plot(
            left, left_amplitude, color=AXIS_COLOURS["x"], linewidth=0.9,
            label="Acceleration ASD",
        )
    axis.set_yscale("log")
    axis.set_xlabel("Frequency [Hz]")
    axis.set_ylabel("Acceleration ASD [g/$\\sqrt{\\mathrm{Hz}}$]")
    axis.tick_params(axis="y", colors=AXIS_COLOURS["x"])
    axis.yaxis.label.set_color(AXIS_COLOURS["x"])
    axis.set_title(
        "Welch amplitude spectral density · "
        "duration-weighted over every acquisition session"
    )
    right = np.asarray(angular.get("frequency_hz") or (), dtype=float)
    right_amplitude = np.asarray(
        angular.get("amplitude_dps_sqrt_hz") or (), dtype=float
    )
    twin = axis.twinx()
    twin.grid(False)
    if right.size and right.size == right_amplitude.size:
        twin.plot(
            right, right_amplitude, color=AXIS_COLOURS["y"], linewidth=0.9,
            label="Angular-rate ASD",
        )
    twin.set_yscale("log")
    twin.set_ylabel("Angular-rate ASD [(deg/s)/$\\sqrt{\\mathrm{Hz}}$]")
    twin.tick_params(axis="y", colors=AXIS_COLOURS["y"])
    twin.yaxis.label.set_color(AXIS_COLOURS["y"])
    nyquist = _number((summary.get("sampling") or {}).get("effective_update_nyquist_hz"))
    if nyquist is not None:
        axis.axvline(nyquist, color="#555555", linewidth=0.8, linestyle="--")
    handles = axis.get_legend_handles_labels()[0] + twin.get_legend_handles_labels()[0]
    labels = axis.get_legend_handles_labels()[1] + twin.get_legend_handles_labels()[1]
    if handles:
        axis.legend(
            handles, labels, loc="best", frameon=False,
            fontsize=MINIMUM_FONT_POINTS,
        )


def _session_ids(sessions):
    """Session identifiers in the order they were recorded, gaps preserved."""
    seen: list[float] = []
    for value in sessions:
        if value == value and value not in seen:  # NaN is never a session
            seen.append(value)
    return seen or [None]


def _times(values):
    from datetime import datetime

    stamps = []
    for value in values:
        text = str(value).replace("Z", "+00:00")
        try:
            stamp = datetime.fromisoformat(text)
        except ValueError:
            stamps.append(None)
            continue
        stamps.append(stamp.replace(tzinfo=None))
    return stamps


def _first(times):
    return next((str(stamp) for stamp in times if stamp is not None), None)


def _last(times):
    return next((str(stamp) for stamp in reversed(times) if stamp is not None), None)


def _stamp_text(value) -> str:
    """A recorded time to the second; the sub-second digits say nothing here."""
    if value is None:
        return "Unavailable"
    text = str(value).replace("T", " ").replace("Z", "")
    return text.split(".", 1)[0].strip() or "Unavailable"


def _number(value):
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if number == number else None


def _available_path(path: Path) -> Path:
    """Never overwrite an export the operator may already have cited."""
    if not path.exists():
        return path
    for index in range(2, 1000):
        candidate = path.with_name(f"{path.stem}_{index}{path.suffix}")
        if not candidate.exists():
            return candidate
    raise ValueError(f"Too many existing exports named like {path.name}")


def _safe_name(value: str) -> str:
    keep = [
        character if character.isalnum() or character in {"-", "_"} else "_"
        for character in str(value)
    ]
    return "".join(keep).strip("_") or "flight"
