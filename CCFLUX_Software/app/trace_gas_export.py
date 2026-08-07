"""Publication-quality Trace Gas Investigation figures.

Seven inches wide so the figure drops into a manuscript column without being
rescaled by a typesetter, and nothing drawn below nine point, which is the
smallest size that still reads once that width is honoured. Both are checked
after the figure is laid out, not merely requested, because constrained_layout
shrinks tick labels to make room and a figure can leave here below the floor it
was configured with.
"""

from __future__ import annotations

import math
from pathlib import Path
from typing import Any, Mapping, Sequence

# A figure wider than a manuscript column has to be shrunk, which takes the type
# below the size it was checked at.
FIGURE_WIDTH_INCHES = 7.0
MINIMUM_FONT_POINTS = 9.0
MINIMUM_DPI = 72
MAXIMUM_DPI = 1500
EXPORT_FORMATS = ("pdf", "png", "svg")

VIEW_TITLES = {
    "overview": "Trace gas investigation",
    "series": "Species and driver over time",
    "scatter": "Species against driver",
    "matrix": "Driver sensitivity matrix",
}
SPECIES_COLOUR = "#0072B2"
DRIVER_COLOUR = "#D55E00"
FIT_COLOUR = "#009E73"
REFERENCE_COLOUR = "#CC79A7"


def _rc() -> dict[str, Any]:
    """Every text size at or above the floor, before anything is drawn."""
    return {
        "font.size": MINIMUM_FONT_POINTS,
        "axes.titlesize": MINIMUM_FONT_POINTS + 1.0,
        "axes.labelsize": MINIMUM_FONT_POINTS,
        "xtick.labelsize": MINIMUM_FONT_POINTS,
        "ytick.labelsize": MINIMUM_FONT_POINTS,
        "legend.fontsize": MINIMUM_FONT_POINTS,
        "figure.titlesize": MINIMUM_FONT_POINTS + 2.0,
        "axes.grid": True,
        "grid.alpha": 0.3,
        "grid.linewidth": 0.5,
        "axes.axisbelow": True,
        "savefig.bbox": "tight",
        "savefig.pad_inches": 0.03,
    }


def resolve_formats(requested: Sequence[str] | None) -> tuple[str, ...]:
    chosen = tuple(
        str(value).strip().lower() for value in (requested or ()) if str(value).strip()
    )
    unknown = [value for value in chosen if value not in EXPORT_FORMATS]
    if unknown:
        raise ValueError(
            "Unsupported export format(s): "
            + ", ".join(sorted(set(unknown)))
            + ". Choose from " + ", ".join(EXPORT_FORMATS)
        )
    return chosen or ("pdf",)


def resolve_dpi(value: Any) -> int:
    try:
        dpi = int(value)
    except (TypeError, ValueError):
        dpi = 600
    return max(MINIMUM_DPI, min(MAXIMUM_DPI, dpi))


def _numeric(values: Sequence[Any]) -> "list[float]":
    return [float("nan") if value is None else float(value) for value in values]


def _pairs(x: Sequence[Any], y: Sequence[Any]):
    import numpy as np

    a = np.asarray(_numeric(x), dtype=float)
    b = np.asarray(_numeric(y), dtype=float)
    mask = np.isfinite(a) & np.isfinite(b)
    return a[mask], b[mask]


def _species_entry(payload: Mapping[str, Any], name: str | None) -> Mapping[str, Any]:
    species = payload.get("species") or []
    if not species:
        raise ValueError("The investigation produced no species to plot.")
    for entry in species:
        if entry.get("name") == name:
            return entry
    return species[0]


def _driver_info(payload: Mapping[str, Any], name: str | None) -> Mapping[str, Any]:
    drivers = payload.get("drivers") or []
    if not drivers:
        raise ValueError("The investigation produced no drivers to plot against.")
    for driver in drivers:
        if driver.get("name") == name:
            return driver
    return drivers[0]


def _label(entry: Mapping[str, Any]) -> str:
    unit = entry.get("unit")
    return f"{entry.get('label', entry.get('name'))}" + (f" [{unit}]" if unit else "")


def _draw_series(axis, payload, entry, driver, mdates):
    import numpy as np

    series = payload["series"]
    times = [np.datetime64(value) for value in series["time"]]
    axis.plot(times, _numeric(series.get(entry["name"], [])),
              color=SPECIES_COLOUR, linewidth=0.9, label=_label(entry))
    axis.set_ylabel(_label(entry), color=SPECIES_COLOUR)
    axis.tick_params(axis="y", labelcolor=SPECIES_COLOUR)
    axis.set_xlabel("Time (UTC)")
    axis.xaxis.set_major_formatter(mdates.DateFormatter("%H:%M"))
    twin = axis.twinx()
    twin.plot(times, _numeric(series.get(driver["name"], [])),
              color=DRIVER_COLOUR, linewidth=0.9)
    unit = driver.get("unit")
    twin.set_ylabel(
        f"{driver.get('label')}" + (f" [{unit}]" if unit else ""), color=DRIVER_COLOUR
    )
    twin.tick_params(axis="y", labelcolor=DRIVER_COLOUR)
    twin.grid(False)


def _draw_scatter(axis, payload, entry, driver):
    import numpy as np

    series = payload["series"]
    x, y = _pairs(series.get(driver["name"], []), series.get(entry["name"], []))
    axis.scatter(x, y, s=5, alpha=0.5, color=SPECIES_COLOUR, edgecolors="none")
    fit = (entry.get("drivers") or {}).get(driver["name"]) or {}
    slope, intercept = fit.get("slope"), fit.get("intercept")
    if x.size and slope is not None and intercept is not None:
        edge = np.array([x.min(), x.max()], dtype=float)
        axis.plot(edge, slope * edge + intercept, color=FIT_COLOUR, linewidth=1.4)
        r_squared = fit.get("r_squared")
        percent = fit.get("percent_per_unit")
        text = f"slope {slope:.4g} {entry.get('unit','')}/{driver.get('unit','unit')}"
        if percent is not None:
            text += f"\n{percent:.3g} %/{driver.get('unit','unit')}"
        if r_squared is not None:
            text += f"\n$R^2$ = {r_squared:.3f}"
        axis.text(0.03, 0.97, text, transform=axis.transAxes, va="top", ha="left",
                  fontsize=MINIMUM_FONT_POINTS,
                  bbox={"boxstyle": "round,pad=0.3", "facecolor": "white",
                        "edgecolor": "#bbbbbb", "alpha": 0.85})
    unit = driver.get("unit")
    axis.set_xlabel(f"{driver.get('label')}" + (f" [{unit}]" if unit else ""))
    axis.set_ylabel(_label(entry))


def _draw_reference(axis, payload, entry):
    import numpy as np

    series = payload["series"]
    reference = series.get(f"ref::{entry['name']}")
    if reference is None or not entry.get("reference"):
        axis.text(0.5, 0.5, "No reference analyser for this species",
                  transform=axis.transAxes, ha="center", va="center",
                  fontsize=MINIMUM_FONT_POINTS, color="#555555")
        axis.set_xticks([])
        axis.set_yticks([])
        return
    x, y = _pairs(reference, series.get(entry["name"], []))
    axis.scatter(x, y, s=5, alpha=0.45, color=REFERENCE_COLOUR, edgecolors="none",
                 label=f"as measured ($R^2$ = {entry['reference'].get('r_squared', float('nan')):.3f})")
    detrended = entry.get("reference_detrended") or {}
    slope_per_unit = detrended.get("slope_per_unit")
    intercept = detrended.get("intercept")
    driver_name = detrended.get("driver")
    if slope_per_unit is not None and intercept is not None and driver_name:
        drive = np.asarray(_numeric(series.get(driver_name, [])), dtype=float)
        values = np.asarray(_numeric(series.get(entry["name"], [])), dtype=float)
        ref = np.asarray(_numeric(reference), dtype=float)
        mask = np.isfinite(drive) & np.isfinite(values) & np.isfinite(ref)
        # Exactly the correction the server scored: the whole fit of the
        # difference, intercept included, so the plotted cloud is the one the
        # quoted R2 belongs to.
        corrected = values[mask] - (slope_per_unit * drive[mask] + intercept)
        axis.scatter(ref[mask], corrected, s=5, alpha=0.5, color=FIT_COLOUR,
                     edgecolors="none",
                     label=f"drift removed ($R^2$ = {detrended.get('r_squared', float('nan')):.3f})")
    if x.size:
        edge = np.array([min(x.min(), y.min()), max(x.max(), y.max())], dtype=float)
        axis.plot(edge, edge, color="#666666", linewidth=1.0, linestyle=":", label="1:1")
    axis.set_xlabel(f"{entry.get('reference_label', 'Reference')} "
                    f"[{entry.get('unit','')}]".strip())
    axis.set_ylabel(f"MIRO {_label(entry)}")
    axis.legend(loc="lower right", framealpha=0.85)


def _draw_matrix(axis, payload, driver):
    """The sensitivity table as a figure, so it can travel with the plots."""
    axis.axis("off")
    name = driver["name"]
    unit = driver.get("unit") or "unit"
    rows = []
    for entry in payload.get("species") or []:
        fit = (entry.get("drivers") or {}).get(name) or {}

        def cell(value, digits=3):
            if value is None or not isinstance(value, (int, float)) or not math.isfinite(value):
                return "--"
            return f"{value:.{digits}g}"

        rows.append([
            entry.get("label", ""), entry.get("unit", ""),
            cell(entry.get("mean"), 5), cell(entry.get("sd")),
            cell(fit.get("slope")), cell(fit.get("percent_per_unit")),
            cell(fit.get("r_squared")), cell(fit.get("partial_slope")),
            "yes" if fit.get("confounded") else "",
        ])
    if not rows:
        return
    columns = ["Species", "Unit", "Mean", "SD", f"Slope/{unit}",
               f"%/{unit}", "$R^2$", f"Partial/{unit}", "Conf."]
    # Auto widths size to the cells and ignore the header, so "Slope/degC" ran
    # into the column beside it. Width follows the widest of the two, and the
    # mathtext R-squared is measured as the two characters it prints as.
    widths = []
    for index, heading in enumerate(columns):
        printed = 2 if heading.startswith("$") else len(heading)
        longest = max([printed] + [len(row[index]) for row in rows])
        widths.append(longest + 2)
    total = float(sum(widths))
    table = axis.table(cellText=rows, colLabels=columns, loc="center",
                       cellLoc="right", colLoc="right",
                       colWidths=[width / total for width in widths])
    table.auto_set_font_size(False)
    table.set_fontsize(MINIMUM_FONT_POINTS)
    table.scale(1.0, 1.3)
    for (row, _column), cell in table.get_celld().items():
        cell.set_edgecolor("#cccccc")
        cell.set_linewidth(0.4)
        if row == 0:
            cell.set_facecolor("#e8eef2")
            cell.set_text_props(fontweight="bold")


def _enforce_minimum_font(figure) -> None:
    """Raise anything the layout engine shrank back to the floor.

    constrained_layout is free to shrink tick labels to make room, so a figure
    configured at nine point can still leave here below it. Checked after
    layout rather than trusted from the rc settings.
    """
    figure.canvas.draw()
    for text in figure.findobj(match=lambda artist: hasattr(artist, "get_fontsize")):
        try:
            size = float(text.get_fontsize())
        except (TypeError, ValueError):
            continue
        if size < MINIMUM_FONT_POINTS:
            text.set_fontsize(MINIMUM_FONT_POINTS)


def render(
    payload: Mapping[str, Any],
    destination: Path,
    stem: str,
    *,
    view: str = "overview",
    species: str | None = None,
    driver: str | None = None,
    formats: Sequence[str] | None = None,
    dpi: int = 600,
) -> list[Path]:
    """One figure, written once per requested format."""
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.dates as mdates
    import matplotlib.pyplot as plt

    if view not in VIEW_TITLES:
        raise ValueError(
            f"Unknown figure: {view}. Choose from " + ", ".join(sorted(VIEW_TITLES))
        )
    chosen_formats = resolve_formats(formats)
    resolution = resolve_dpi(dpi)
    entry = _species_entry(payload, species)
    driver_info = _driver_info(payload, driver)
    destination = Path(destination)
    destination.mkdir(parents=True, exist_ok=True)

    written: list[Path] = []
    with plt.rc_context(_rc()):
        if view == "overview":
            figure, axes = plt.subplots(
                3, 1, figsize=(FIGURE_WIDTH_INCHES, 8.6), constrained_layout=True
            )
            _draw_series(axes[0], payload, entry, driver_info, mdates)
            _draw_scatter(axes[1], payload, entry, driver_info)
            _draw_reference(axes[2], payload, entry)
        elif view == "series":
            figure, axis = plt.subplots(
                figsize=(FIGURE_WIDTH_INCHES, 3.4), constrained_layout=True
            )
            _draw_series(axis, payload, entry, driver_info, mdates)
        elif view == "scatter":
            figure, axis = plt.subplots(
                figsize=(FIGURE_WIDTH_INCHES, 4.2), constrained_layout=True
            )
            _draw_scatter(axis, payload, entry, driver_info)
        else:
            height = min(9.5, 1.1 + 0.32 * max(1, len(payload.get("species") or [])))
            figure, axis = plt.subplots(
                figsize=(FIGURE_WIDTH_INCHES, height), constrained_layout=True
            )
            _draw_matrix(axis, payload, driver_info)

        window = payload.get("window") or {}
        # Only the ISO date/time separator, not every T - a global replace also
        # ate the one in "UTC".
        readable = lambda stamp: str(stamp or "").replace("T", " ", 1)
        subtitle = (
            f"{readable(window.get('start'))} to {readable(window.get('end'))} UTC · "
            f"{window.get('samples','?')} samples at "
            f"{window.get('resolution_seconds','?')} s"
        )
        figure.suptitle(f"{VIEW_TITLES[view]} — {entry.get('label','')}\n{subtitle}")
        _enforce_minimum_font(figure)

        for suffix in chosen_formats:
            path = destination / f"{stem}_{view}.{suffix}"
            figure.savefig(path, format=suffix, dpi=resolution)
            written.append(path)
        plt.close(figure)
    return written
