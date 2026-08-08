"""A flight track drawn as a figure, not as a picture of a screen.

The map exports were a screenshot of the browser canvas wrapped in a PDF: one
raster, no fonts, and whatever size the window happened to be - the MIRO Rack
export measured 17.33 inches wide with no text in it at all. Nothing in it can
be read at a manuscript's column width, and nothing states what is plotted.

This draws the same track from the same numbers with matplotlib, so the result
carries what a reader needs: a titled axes framed in degrees of latitude and
longitude, a colour bar that names the quantity and its unit, a scale bar and a
north arrow for distance and orientation, and the basemap attribution. Text is
text, so it stays sharp at any zoom and can be searched and re-styled.

Two rules hold for every figure that leaves here, and both are checked on the
laid-out result rather than trusted from the settings: seven inches wide, so it
drops into a column without being rescaled, and nothing below nine point, which
is the smallest that still reads once that width is honoured.
"""

from __future__ import annotations

import math
import urllib.request
from pathlib import Path
from typing import Any, Sequence

FIGURE_WIDTH_INCHES = 7.0
MINIMUM_FONT_POINTS = 9.0
MINIMUM_DPI = 72
MAXIMUM_DPI = 1500
EXPORT_FORMATS = ("pdf", "png", "svg")
MEDIA_TYPES = {
    "pdf": "application/pdf",
    "png": "image/png",
    "svg": "image/svg+xml",
}


def media_type(image_format: str) -> str:
    """The content type a browser needs to display or download the figure."""
    return MEDIA_TYPES.get(str(image_format).casefold(), "application/octet-stream")

# The basemap the workspaces already draw, so an exported figure and the page it
# came from show the same ground.
TILE_URL = "https://tile.openstreetmap.org/{z}/{x}/{y}.png"
TILE_ATTRIBUTION = "© OpenStreetMap contributors"
TILE_PIXELS = 256
# A polite ceiling: a flight covering a degree needs a handful of tiles at the
# zoom that matches seven inches of paper, and asking a public tile server for
# hundreds to gain detail no reader can see would be rude as well as slow.
MAXIMUM_TILES = 64
TILE_TIMEOUT_SECONDS = 5
USER_AGENT = "CC-FLUX-PostFlightReview/1.0 (Forschungszentrum Juelich)"

# The height follows the track's own shape - a west-east transect is landscape,
# a north-south one is portrait - inside bounds that keep the figure on a page.
# The width is always seven inches: where the track does not fill it, the margin
# stays white rather than the figure being cropped narrower than a column.
MINIMUM_FIGURE_HEIGHT_INCHES = 3.2
MAXIMUM_FIGURE_HEIGHT_INCHES = 8.6
# Room for the title, the axis labels and the colour bar's own tick labels.
FURNITURE_HEIGHT_INCHES = 1.15


def _mercator_y(latitude: float) -> float:
    """Web Mercator northing, normalised so one unit is one unit of longitude.

    Plotting in degrees would stretch the basemap, because a degree of longitude
    is shorter than a degree of latitude everywhere but the equator. Working in
    this projection and labelling the axis in degrees keeps the tiles square and
    the ticks readable.
    """
    radians = math.radians(max(-85.05112878, min(85.05112878, latitude)))
    return math.degrees(math.log(math.tan(math.pi / 4 + radians / 2)))


def _latitude_from_mercator(y: float) -> float:
    return math.degrees(2 * math.atan(math.exp(math.radians(y))) - math.pi / 2)


def _tile_indices(latitude: float, longitude: float, zoom: int) -> tuple[float, float]:
    count = 2 ** zoom
    x = (longitude + 180.0) / 360.0 * count
    radians = math.radians(latitude)
    y = (1.0 - math.asinh(math.tan(radians)) / math.pi) / 2.0 * count
    return x, y


def choose_zoom(
    west: float, east: float, south: float, north: float, pixels_wide: float
) -> int:
    """The zoom whose tiles carry about the detail the figure can show.

    One level too high multiplies the downloads by four for detail that lands
    inside a printed pixel; one too low leaves the labels on the basemap coarse.
    """
    span = max(1e-9, east - west)
    for zoom in range(19, -1, -1):
        across = span / 360.0 * (2 ** zoom) * TILE_PIXELS
        tiles_x = span / 360.0 * (2 ** zoom)
        tiles_y = abs(
            _tile_indices(south, west, zoom)[1] - _tile_indices(north, west, zoom)[1]
        )
        if across <= pixels_wide * 1.6 and (tiles_x + 2) * (tiles_y + 2) <= MAXIMUM_TILES:
            return zoom
    return 0


def _fetch_tile(zoom: int, x: int, y: int, cache: Path):
    from PIL import Image

    path = cache / str(zoom) / str(x) / f"{y}.png"
    if not path.is_file():
        path.parent.mkdir(parents=True, exist_ok=True)
        request = urllib.request.Request(
            TILE_URL.format(z=zoom, x=x, y=y), headers={"User-Agent": USER_AGENT}
        )
        with urllib.request.urlopen(request, timeout=TILE_TIMEOUT_SECONDS) as response:
            path.write_bytes(response.read())
    return Image.open(path).convert("RGB")


def basemap(
    west: float, east: float, south: float, north: float, zoom: int, cache: Path
):
    """The tile mosaic covering the view, and the extent it really spans.

    Returns (image, extent) or (None, None) when the tiles cannot be had. A
    figure without its basemap is still a correct figure - the track, the axes
    and the colour bar are all drawn from the data - so a machine with no
    network gets a plain background rather than no export.
    """
    from PIL import Image

    cache = Path(cache)
    left, top = _tile_indices(north, west, zoom)
    right, bottom = _tile_indices(south, east, zoom)
    x0, x1 = int(math.floor(left)), int(math.floor(right))
    y0, y1 = int(math.floor(top)), int(math.floor(bottom))
    count = 2 ** zoom
    columns = list(range(x0, x1 + 1))
    rows = list(range(y0, y1 + 1))
    if not columns or not rows or len(columns) * len(rows) > MAXIMUM_TILES:
        return None, None
    mosaic = Image.new("RGB", (len(columns) * TILE_PIXELS, len(rows) * TILE_PIXELS),
                       (245, 245, 242))
    fetched = 0
    for column, x in enumerate(columns):
        for row, y in enumerate(rows):
            if not 0 <= y < count:
                continue
            try:
                tile = _fetch_tile(zoom, x % count, y, cache)
            except Exception:
                continue
            mosaic.paste(tile, (column * TILE_PIXELS, row * TILE_PIXELS))
            fetched += 1
    if not fetched:
        return None, None
    extent = (
        columns[0] / count * 360.0 - 180.0,
        (columns[-1] + 1) / count * 360.0 - 180.0,
        _mercator_y(_tile_latitude(rows[-1] + 1, zoom)),
        _mercator_y(_tile_latitude(rows[0], zoom)),
    )
    return mosaic, extent


def _tile_latitude(y: float, zoom: int) -> float:
    n = math.pi - 2.0 * math.pi * y / (2 ** zoom)
    return math.degrees(math.atan(math.sinh(n)))


def _degree_ticks(low: float, high: float, target: int = 6) -> list[float]:
    """Tick positions a reader would choose: 0.05, 0.1, 0.2, 0.5 of a degree."""
    span = max(1e-9, high - low)
    raw = span / max(1, target)
    magnitude = 10 ** math.floor(math.log10(raw))
    for multiple in (1, 2, 2.5, 5, 10):
        step = multiple * magnitude
        if step >= raw:
            break
    first = math.ceil(low / step) * step
    ticks = []
    value = first
    while value <= high + step * 1e-6:
        ticks.append(round(value, 10))
        value += step
    return ticks


def _scale_bar(axis, west: float, east: float, latitude: float) -> None:
    """A bar whose length is a round number of kilometres at this latitude."""
    metres_per_degree = 111_320.0 * math.cos(math.radians(latitude))
    span_metres = (east - west) * metres_per_degree
    target = span_metres / 4.0
    magnitude = 10 ** math.floor(math.log10(max(1.0, target)))
    for multiple in (1, 2, 5, 10):
        length = multiple * magnitude
        if length >= target:
            break
    if length > span_metres * 0.8:
        length = magnitude
    degrees = length / metres_per_degree
    x0 = west + (east - west) * 0.04
    y = 0.055
    axis.plot(
        [x0, x0 + degrees], [y, y], transform=_blend(axis),
        color="#111111", linewidth=2.6, solid_capstyle="butt", zorder=6,
    )
    label = f"{length / 1000:g} km" if length >= 1000 else f"{length:g} m"
    axis.text(
        x0 + degrees / 2, y + 0.018, label, transform=_blend(axis),
        ha="center", va="bottom", fontsize=MINIMUM_FONT_POINTS, color="#111111",
        zorder=6,
        bbox={"boxstyle": "round,pad=0.18", "facecolor": "white",
              "edgecolor": "none", "alpha": 0.75},
    )


def _blend(axis):
    import matplotlib.transforms as transforms

    return transforms.blended_transform_factory(axis.transData, axis.transAxes)


def _north_arrow(axis) -> None:
    axis.annotate(
        "N", xy=(0.965, 0.90), xytext=(0.965, 0.79),
        xycoords="axes fraction", textcoords="axes fraction",
        ha="center", va="center", fontsize=MINIMUM_FONT_POINTS + 1,
        fontweight="bold", color="#111111", zorder=6,
        arrowprops={"arrowstyle": "-|>", "color": "#111111", "linewidth": 1.5},
    )


def enforce_minimum_font(figure) -> None:
    """Raise anything the layout engine shrank back to the floor.

    constrained_layout is free to shrink tick labels to make room, so a figure
    configured at nine point can still leave here below it.
    """
    figure.canvas.draw()
    for artist in figure.findobj(match=lambda item: hasattr(item, "get_fontsize")):
        try:
            size = float(artist.get_fontsize())
        except (TypeError, ValueError):
            continue
        if size < MINIMUM_FONT_POINTS:
            artist.set_fontsize(MINIMUM_FONT_POINTS)


def rc_parameters() -> dict[str, Any]:
    """Every text size at or above the floor, before anything is drawn."""
    return {
        "font.size": MINIMUM_FONT_POINTS,
        "axes.titlesize": MINIMUM_FONT_POINTS + 2.0,
        "axes.labelsize": MINIMUM_FONT_POINTS + 1.0,
        "xtick.labelsize": MINIMUM_FONT_POINTS,
        "ytick.labelsize": MINIMUM_FONT_POINTS,
        "legend.fontsize": MINIMUM_FONT_POINTS,
        "figure.titlesize": MINIMUM_FONT_POINTS + 3.0,
        "axes.linewidth": 0.8,
        # Deliberately not "tight". Tight crops to the ink, so a portrait map
        # came out 6.02 inches wide instead of seven and no longer matched the
        # column it was drawn for. constrained_layout already fits the furniture
        # inside the figure, so the saved size is the size that was asked for.
        "savefig.bbox": None,
        "savefig.pad_inches": 0.0,
    }


def render_track_map(
    latitudes: Sequence[float],
    longitudes: Sequence[float],
    values: Sequence[float] | None,
    destination: Path,
    stem: str,
    *,
    title: str,
    value_label: str = "",
    subtitle: str = "",
    colormap: str = "viridis",
    formats: Sequence[str] = ("pdf",),
    dpi: int = 300,
    cache_directory: Path | None = None,
    log_scale: bool = False,
) -> list[Path]:
    """One flight track, drawn to the campaign's figure standard."""
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    import numpy as np
    from matplotlib.collections import LineCollection
    from matplotlib.colors import LogNorm, Normalize

    chosen = tuple(str(item).strip().casefold() for item in formats if str(item).strip())
    unknown = [item for item in chosen if item not in EXPORT_FORMATS]
    if unknown:
        raise ValueError(
            "Unsupported export format(s): " + ", ".join(sorted(set(unknown)))
            + ". Choose from " + ", ".join(EXPORT_FORMATS)
        )
    chosen = chosen or ("pdf",)
    resolution = max(MINIMUM_DPI, min(MAXIMUM_DPI, int(dpi)))

    latitude = np.asarray(latitudes, dtype=float)
    longitude = np.asarray(longitudes, dtype=float)
    good = np.isfinite(latitude) & np.isfinite(longitude)
    if values is not None:
        colour = np.asarray(values, dtype=float)
        good &= np.isfinite(colour)
    else:
        colour = None
    if int(good.sum()) < 2:
        raise ValueError("A track needs at least two positioned samples to draw")
    latitude, longitude = latitude[good], longitude[good]
    colour = colour[good] if colour is not None else None

    # A margin, so the track never runs into the frame, and a floor so a hover
    # does not become a map of one building.
    pad_x = max((longitude.max() - longitude.min()) * 0.06, 0.004)
    pad_y = max((latitude.max() - latitude.min()) * 0.08, 0.003)
    west, east = longitude.min() - pad_x, longitude.max() + pad_x
    south, north = latitude.min() - pad_y, latitude.max() + pad_y

    y_low, y_high = _mercator_y(south), _mercator_y(north)
    aspect = (y_high - y_low) / max(1e-9, east - west)
    height = max(
        MINIMUM_FIGURE_HEIGHT_INCHES,
        min(
            MAXIMUM_FIGURE_HEIGHT_INCHES,
            FIGURE_WIDTH_INCHES * aspect + FURNITURE_HEIGHT_INCHES,
        ),
    )

    written: list[Path] = []
    destination = Path(destination)
    destination.mkdir(parents=True, exist_ok=True)

    with plt.rc_context(rc_parameters()):
        figure, axis = plt.subplots(
            figsize=(FIGURE_WIDTH_INCHES, height), constrained_layout=True
        )
        zoom = choose_zoom(
            west, east, south, north, FIGURE_WIDTH_INCHES * min(resolution, 300)
        )
        mosaic, extent = (None, None)
        if cache_directory is not None:
            mosaic, extent = basemap(west, east, south, north, zoom,
                                     Path(cache_directory))
        if mosaic is not None:
            axis.imshow(
                np.asarray(mosaic), extent=extent, origin="upper",
                interpolation="bilinear", zorder=0,
            )
        else:
            axis.set_facecolor("#f2f2ef")

        points = np.column_stack(
            [longitude, [_mercator_y(value) for value in latitude]]
        )
        segments = np.stack([points[:-1], points[1:]], axis=1)
        if colour is not None:
            midpoint = (colour[:-1] + colour[1:]) / 2.0
            finite = midpoint[np.isfinite(midpoint)]
            positive = finite[finite > 0]
            norm = (
                LogNorm(vmin=positive.min(), vmax=positive.max())
                if log_scale and positive.size
                else Normalize(vmin=finite.min(), vmax=finite.max())
                if finite.size else None
            )
            collection = LineCollection(
                segments, cmap=colormap, norm=norm, linewidths=2.8,
                capstyle="round", zorder=3,
            )
            collection.set_array(midpoint)
            axis.add_collection(collection)
            bar = figure.colorbar(collection, ax=axis, pad=0.015, fraction=0.045)
            if value_label:
                bar.set_label(value_label)
            bar.outline.set_linewidth(0.8)
        else:
            axis.add_collection(
                LineCollection(segments, colors="#1565c0", linewidths=2.2, zorder=3)
            )

        axis.set_xlim(west, east)
        axis.set_ylim(y_low, y_high)
        axis.set_aspect("equal", adjustable="box")

        axis.set_xticks(_degree_ticks(west, east))
        latitude_ticks = _degree_ticks(south, north)
        axis.set_yticks([_mercator_y(value) for value in latitude_ticks])
        axis.set_yticklabels([f"{value:g}" for value in latitude_ticks])
        axis.set_xlabel("Longitude (°)")
        axis.set_ylabel("Latitude (°)")
        axis.tick_params(direction="out", length=3.5, width=0.8)

        _scale_bar(axis, west, east, float(latitude.mean()))
        _north_arrow(axis)
        axis.text(
            0.012, 0.012, TILE_ATTRIBUTION if mosaic is not None
            else "Basemap unavailable; track drawn from recorded positions",
            transform=axis.transAxes, ha="left", va="bottom",
            fontsize=MINIMUM_FONT_POINTS, color="#333333", zorder=6,
            bbox={"boxstyle": "round,pad=0.2", "facecolor": "white",
                  "edgecolor": "none", "alpha": 0.72},
        )
        axis.set_title(title if not subtitle else f"{title}\n{subtitle}")
        enforce_minimum_font(figure)

        for suffix in chosen:
            path = destination / f"{stem}.{suffix}"
            figure.savefig(path, format=suffix, dpi=resolution)
            written.append(path)
        plt.close(figure)
    return written
