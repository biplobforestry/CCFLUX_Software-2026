"""The Source Investigation as a figure: the rows, the region, and the wind.

What is on the screen is three things a reader needs together - the gas rows a
feature was spotted on, the ground it was over, and the direction the air came
from - so the export is those three, not a picture of the browser.

Everything obeys the campaign standard: seven inches wide, nothing below nine
point. The map obeys the map standard instead, sized to its own track inside
seven by five, because a map stretched to a column width stops being a map of
the flight.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Mapping, Sequence

import numpy as np

from core import figure_standard, scientific_map
from app import source_investigation as engine

# The rows carry their own colours from the page, so an exported figure and the
# screen it was read from agree. This is only what a row falls back to.
FALLBACK_COLOURS = (
    "#1756d1", "#d1471a", "#159447", "#8931ef", "#c79a00", "#0e7773",
)


def render(
    rows: Mapping[str, Any],
    analysis: Mapping[str, Any] | None,
    layout: Sequence[Mapping[str, Any]],
    destination: Path,
    stem: str,
    *,
    flight_name: str = "",
    formats: Sequence[str] | None = None,
    dpi: int = 600,
    cache_directory: Path | None = None,
) -> list[Path]:
    """Write the gas rows, and - when a region was chosen - the wind and map."""
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    chosen = tuple(
        str(item).strip().casefold() for item in (formats or ("pdf",))
        if str(item).strip()
    ) or ("pdf",)
    unknown = [item for item in chosen if item not in figure_standard.EXPORT_FORMATS]
    if unknown:
        raise ValueError(
            "Unsupported export format(s): " + ", ".join(sorted(set(unknown)))
        )
    destination = Path(destination)
    destination.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []

    with plt.rc_context(figure_standard.rc_parameters()):
        figure = _rows_figure(plt, rows, layout, flight_name)
        for suffix in chosen:
            path = destination / f"{stem}_rows.{suffix}"
            figure.savefig(path, format=suffix, dpi=dpi)
            written.append(path)
        plt.close(figure)

        if analysis:
            figure = _wind_figure(plt, analysis, flight_name)
            for suffix in chosen:
                path = destination / f"{stem}_wind.{suffix}"
                figure.savefig(path, format=suffix, dpi=dpi)
                written.append(path)
            plt.close(figure)

            track = analysis.get("track") or {}
            if track.get("available"):
                written.extend(
                    _region_map(track, analysis, destination, f"{stem}_map",
                                flight_name, chosen, dpi, cache_directory)
                )
    return written


def _rows_figure(plt, rows: Mapping[str, Any], layout, flight_name: str):
    """The stacked gas rows, as they were laid out on the page."""
    import pandas as pd

    times = pd.to_datetime(list(rows.get("time") or []), format="ISO8601")
    plans = [dict(item) for item in (layout or []) if item.get("left") or item.get("right")]
    if not plans:
        # Nothing chosen: draw whatever gas the flight has, so an export is
        # never an empty page.
        first = next(
            (name for name in engine.GAS_CHANNELS if name in rows.get("series", {})),
            None,
        )
        plans = [{"left": [{"key": first}] if first else [], "right": []}]
    height = min(
        figure_standard.MAXIMUM_HEIGHT_INCHES,
        max(2.6, 1.05 + 1.85 * len(plans)),
    )
    figure, axes = plt.subplots(
        len(plans), 1, sharex=True, constrained_layout=True,
        figsize=(figure_standard.PAGE_WIDTH_INCHES, height),
        squeeze=False,
    )
    series = rows.get("series") or {}
    envelope = rows.get("envelope") or {}
    for index, (plan, cell) in enumerate(zip(plans, axes.ravel())):
        twin = None
        for side in ("left", "right"):
            entries = list(plan.get(side) or [])
            if not entries:
                continue
            target = cell
            if side == "right":
                twin = cell.twinx()
                twin.grid(False)
                target = twin
            units: list[str] = []
            for order, entry in enumerate(entries):
                key = str(entry.get("key") or "")
                values = series.get(key)
                if not values:
                    continue
                colour = entry.get("colour") or FALLBACK_COLOURS[
                    order % len(FALLBACK_COLOURS)
                ]
                width = float(entry.get("width") or 1.1)
                label = engine.CHANNEL_LABELS.get(key, key)
                units.append(engine.CHANNEL_UNITS.get(key, ""))
                band = envelope.get(key)
                if band:
                    # The excursion every drawn point stands for. Without it a
                    # decimated record hides the spike it was opened to find.
                    target.fill_between(
                        times, _numeric(band["low"]), _numeric(band["high"]),
                        color=colour, alpha=0.18, linewidth=0, zorder=1,
                        rasterized=True,
                    )
                target.plot(
                    times, _numeric(values), color=colour, linewidth=width,
                    label=label, zorder=3, rasterized=True,
                )
            target.set_ylabel(_axis_label(entries, units))
        handles, labels = cell.get_legend_handles_labels()
        if twin is not None:
            extra = twin.get_legend_handles_labels()
            handles, labels = handles + extra[0], labels + extra[1]
        if handles:
            cell.legend(
                handles, labels, loc="upper left", ncol=min(3, len(handles)),
                handlelength=1.2, columnspacing=0.9, borderpad=0.3,
                framealpha=0.85,
            )
        cell.grid(True, alpha=0.25)
        if index == len(plans) - 1:
            cell.set_xlabel("Recorded time (UTC)")
    smoothing = rows.get("smoothing") or {}
    # Three short lines rather than one long one, which ran off both edges of a
    # seven-inch page.
    figure.suptitle(
        f"{flight_name} · Source investigation".strip(" ·")
        + f"\n{smoothing.get('method', 'none')} smoothing, "
        f"{smoothing.get('seconds', 0)} s · shaded band is the raw excursion"
        f"\n{rows.get('shown', 0):,} of {rows.get('samples', 0):,} samples drawn"
    )
    figure_standard.finalise(figure)
    return figure


def _axis_label(entries, units) -> str:
    names = [
        engine.CHANNEL_LABELS.get(str(entry.get("key") or ""), "")
        for entry in entries
    ]
    distinct = [unit for unit in dict.fromkeys(units) if unit]
    unit = distinct[0] if len(distinct) == 1 else ", ".join(distinct)
    joined = ", ".join(name for name in names if name)
    return f"{joined} ({unit})" if unit else joined


def _numeric(values) -> np.ndarray:
    return np.array(
        [np.nan if value is None else float(value) for value in values],
        dtype=float,
    )


def _wind_figure(plt, analysis: Mapping[str, Any], flight_name: str):
    """The wind rose over the region, and what else was measured in it."""
    rose = analysis.get("windrose")
    statistics = analysis.get("statistics") or {}
    figure = plt.figure(
        figsize=(figure_standard.PAGE_WIDTH_INCHES, 4.2), constrained_layout=True
    )
    grid = figure.add_gridspec(1, 2, width_ratios=(1.0, 0.85))
    polar = figure.add_subplot(grid[0, 0], projection="polar")
    if rose and rose.get("samples"):
        _draw_rose(polar, rose)
    else:
        polar.text(0.5, 0.5, "No wind record\nover this region",
                   transform=polar.transAxes, ha="center", va="center")
        polar.set_axis_off()
    table = figure.add_subplot(grid[0, 1])
    _draw_summary(table, analysis, statistics)
    region = analysis.get("region") or {}
    figure.suptitle(
        f"{flight_name} · selected region".strip(" ·")
        + f"\n{str(region.get('start', ''))[:19]} to "
        f"{str(region.get('end', ''))[:19]} · "
        f"{region.get('seconds', 0):.0f} s · {region.get('samples', 0):,} samples"
    )
    figure_standard.finalise(figure)
    return figure


def _draw_rose(axis, rose: Mapping[str, Any]) -> None:
    """Sixteen sectors, stacked by wind speed band."""
    import matplotlib

    petals = list(rose.get("petals") or [])
    edges = list(rose.get("speed_edges") or [])
    width = np.deg2rad(360.0 / max(1, len(petals)))
    # Compass convention: north at the top, clockwise, which is how a rose is
    # read and the opposite of the mathematical default.
    axis.set_theta_zero_location("N")
    axis.set_theta_direction(-1)
    total = max(1, int(rose.get("samples") or 1))
    colours = matplotlib.colormaps["viridis"](
        np.linspace(0.15, 0.95, max(1, len(edges)))
    )
    bottom = np.zeros(len(petals))
    angles = np.deg2rad([petal["centre_deg"] for petal in petals])
    for band_index in range(len(edges)):
        heights = np.array([
            petal["bands"][band_index]["count"] / total * 100.0
            for petal in petals
        ])
        low = edges[band_index]
        high = edges[band_index + 1] if band_index + 1 < len(edges) else None
        axis.bar(
            angles, heights, width=width * 0.92, bottom=bottom,
            color=colours[band_index], edgecolor="white", linewidth=0.4,
            label=f"{low:g}-{high:g}" if high is not None else f"> {low:g}",
        )
        bottom += heights
    axis.set_xticks(np.deg2rad(np.arange(0, 360, 45)))
    axis.set_xticklabels(["N", "NE", "E", "SE", "S", "SW", "W", "NW"])
    axis.set_title(f"Wind rose · {rose.get('convention', '')}", pad=12)
    axis.legend(
        title="m s$^{-1}$", loc="upper left", bbox_to_anchor=(-0.22, 1.06),
        handlelength=1.0, borderpad=0.3, framealpha=0.85, fontsize=9,
    )


def _draw_summary(axis, analysis: Mapping[str, Any], statistics) -> None:
    axis.set_axis_off()
    lines: list[str] = []
    for key, label, unit in (
        ("wind_direction", "Wind from", "°"),
        ("wind_speed", "Wind speed", "m/s"),
        ("track", "Track", "°"),
        ("ground_speed", "Ground speed", "m/s"),
        ("altitude", "Altitude", "m"),
    ):
        value = statistics.get(key) or {}
        if not value.get("available"):
            continue
        if "label" in value:
            lines.append(f"{label}: {value['mean']:.0f}{unit} ({value['label']})")
        else:
            lines.append(
                f"{label}: {value['mean']:.1f} {unit} "
                f"({value['minimum']:.1f}–{value['maximum']:.1f})"
            )
    enhanced = [
        (name, entry) for name, entry in (analysis.get("enhancements") or {}).items()
        if entry.get("available") and entry.get("enhancement") is not None
    ]
    enhanced.sort(key=lambda item: -(item[1].get("enhancement") or 0.0))
    if enhanced:
        lines.append("")
        lines.append("Enhancement above the flight background:")
        for name, entry in enhanced[:6]:
            # The background is stated, not implied: an instrument whose noise
            # sits below zero gives an enhancement larger than the peak, and
            # "+606 ppb (peak 602)" reads as an error until the -4 is visible.
            lines.append(
                f"  {entry['label']}: peak {entry['maximum']:.4g} "
                f"− {entry['background']:.3g} = "
                f"+{entry['enhancement']:.4g} {entry['unit']}"
            )
    if not lines:
        lines = ["No navigation or gas summary for this region."]
    axis.text(
        0.0, 1.0, "\n".join(lines), transform=axis.transAxes,
        ha="left", va="top", linespacing=1.5, family="monospace",
    )
    # Said on the figure, not only on the page: a filter applied silently and
    # then read off is how a plume becomes a different size than it was.
    axis.text(
        0.0, 0.0, "Computed from the raw record inside the region.",
        transform=axis.transAxes, ha="left", va="bottom", color="#555555",
    )


def _region_map(
    track: Mapping[str, Any],
    analysis: Mapping[str, Any],
    destination: Path,
    stem: str,
    flight_name: str,
    formats: Sequence[str],
    dpi: int,
    cache_directory: Path | None,
) -> list[Path]:
    """The whole flight, with the selected region marked on it.

    Context is most of what places a source: which leg the feature was on, and
    whether the same ground was passed earlier without seeing it.
    """
    whole = list(track.get("track") or [])
    marked = list(track.get("region") or [])
    if len(whole) < 2:
        return []
    latitudes = [float(point["lat"]) for point in whole]
    longitudes = [float(point["lon"]) for point in whole]
    inside = {
        (round(float(point["lat"]), 6), round(float(point["lon"]), 6))
        for point in marked
    }
    # One as the value, so the region reads as the highlighted part of the
    # track rather than as a second, unrelated line.
    values = [
        1.0 if (round(lat, 6), round(lon, 6)) in inside else 0.0
        for lat, lon in zip(latitudes, longitudes)
    ]
    region = analysis.get("region") or {}
    return scientific_map.render_track_map(
        latitudes, longitudes, values, destination, stem,
        title=f"{flight_name} · selected region".strip(" ·"),
        subtitle=f"{str(region.get('start', ''))[:19]} to "
                 f"{str(region.get('end', ''))[:19]}",
        value_label="Selected region (1) against the rest of the flight (0)",
        colormap="coolwarm",
        formats=formats,
        dpi=dpi,
        cache_directory=cache_directory,
    )
