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

from core import figure_standard

# The map obeys the same two rules as every other figure the project makes, and
# takes them from the one place they are written down rather than restating them.
FIGURE_WIDTH_INCHES = figure_standard.PAGE_WIDTH_INCHES
MINIMUM_FONT_POINTS = figure_standard.MINIMUM_FONT_POINTS
EXPORT_FORMATS = figure_standard.EXPORT_FORMATS
MEDIA_TYPES = figure_standard.MEDIA_TYPES
media_type = figure_standard.media_type
enforce_minimum_font = figure_standard.enforce_minimum_font

MINIMUM_DPI = 72
MAXIMUM_DPI = 1500

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

# A map is sized to the ground it covers, not to a column. Both numbers are
# ceilings: the figure is whatever the track's own shape needs inside them, so a
# square flight comes out square rather than being stretched to fill a wide
# frame. The extent is always the track plus a margin - a map that fills its
# frame by widening to a hundred kilometres of countryside the Zeppelin never
# flew over is a picture of the Rhineland, not of the flight.
MAP_MAXIMUM_WIDTH_INCHES = 7.0
MAP_MAXIMUM_HEIGHT_INCHES = 5.0
# Below this the basemap has no legible detail and the track is a scribble.
MINIMUM_MAP_INCHES = 2.4

# What has to fit around the drawn map, measured at nine point.
TITLE_HEIGHT_INCHES = 0.42
XAXIS_HEIGHT_INCHES = 0.52
YAXIS_WIDTH_INCHES = 0.62
# A colour bar and its label, on whichever side it goes.
COLOURBAR_WIDTH_INCHES = 0.78
COLOURBAR_HEIGHT_INCHES = 0.62
# Rotating a map is a real cost to a reader, so it has to buy something: a
# quarter more drawn map than the upright orientation manages.
ROTATION_GAIN = 1.25


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


def _tick_target(inches: float) -> int:
    """How many labels that many inches of axis can hold at nine point."""
    return max(2, min(7, int(round(inches * 1.4))))


def _scale_bar(
    axis, west: float, east: float, latitude: float, rotated: bool = False
) -> None:
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
    label = f"{length / 1000:g} km" if length >= 1000 else f"{length:g} m"
    # Bottom right, because the attribution has the bottom left and a scale bar
    # drawn through it made both unreadable.
    start = east - (east - west) * 0.04 - degrees
    if rotated:
        # Longitude runs up the page, so the bar does too - down the right-hand
        # edge, where neither the north arrow nor the attribution is.
        transform = _blend(axis, along="y")
        low = west + (east - west) * 0.04
        axis.plot(
            [0.955, 0.955], [low, low + degrees], transform=transform,
            color="#111111", linewidth=2.6, solid_capstyle="butt", zorder=6,
        )
        axis.text(
            0.937, low + degrees / 2, label, transform=transform,
            ha="center", va="center", rotation=90,
            fontsize=MINIMUM_FONT_POINTS, color="#111111", zorder=6,
            bbox={"boxstyle": "round,pad=0.18", "facecolor": "white",
                  "edgecolor": "none", "alpha": 0.75},
        )
        return
    transform = _blend(axis, along="x")
    axis.plot(
        [start, start + degrees], [0.055, 0.055], transform=transform,
        color="#111111", linewidth=2.6, solid_capstyle="butt", zorder=6,
    )
    axis.text(
        start + degrees / 2, 0.073, label, transform=transform,
        ha="center", va="bottom", fontsize=MINIMUM_FONT_POINTS, color="#111111",
        zorder=6,
        bbox={"boxstyle": "round,pad=0.18", "facecolor": "white",
              "edgecolor": "none", "alpha": 0.75},
    )


def _blend(axis, along: str = "x"):
    import matplotlib.transforms as transforms

    if along == "y":
        return transforms.blended_transform_factory(axis.transAxes, axis.transData)
    return transforms.blended_transform_factory(axis.transData, axis.transAxes)


def _north_arrow(axis, rotated: bool = False) -> None:
    """Which way north is - the whole reason a map may be turned at all.

    Kept inside the top-right corner rather than the top-left, where it used to
    sit on the first latitude label.
    """
    if rotated:
        # A quarter turn anticlockwise puts north to the left.
        tail, head = (0.135, 0.93), (0.035, 0.93)
    else:
        tail, head = (0.965, 0.79), (0.965, 0.90)
    axis.annotate(
        "N", xy=head, xytext=tail,
        xycoords="axes fraction", textcoords="axes fraction",
        ha="center", va="center", fontsize=MINIMUM_FONT_POINTS + 1,
        fontweight="bold", color="#111111", zorder=6,
        arrowprops={"arrowstyle": "-|>", "color": "#111111", "linewidth": 1.5},
        bbox={"boxstyle": "circle,pad=0.16", "facecolor": "white",
              "edgecolor": "none", "alpha": 0.75},
    )


def rc_parameters() -> dict[str, Any]:
    """The campaign settings, with the grid the basemap supplies turned off."""
    parameters = figure_standard.rc_parameters()
    # A map's ground is the basemap; a graticule over it would be a second grid.
    parameters["axes.grid"] = False
    return parameters


def plan_layout(
    span_x: float,
    span_y: float,
    has_colourbar: bool,
    *,
    allow_rotation: bool = True,
    maximum_width: float = MAP_MAXIMUM_WIDTH_INCHES,
    maximum_height: float = MAP_MAXIMUM_HEIGHT_INCHES,
) -> dict[str, Any]:
    """Decide the figure size, the colour bar's side, and whether to rotate.

    The map keeps the ground's true proportions, so the only question is how
    large it can be drawn inside the budget and which way round. Three things
    are chosen together because they trade against each other:

    * A colour bar on the right takes width; one underneath takes height. It
      goes on whichever side the layout has slack, which is the side that
      leaves the drawn map larger.
    * A track taller than it is wide cannot fill a landscape budget upright.
      Turned a quarter, it can - so the orientation that draws more map wins,
      but only by a clear margin, because north not being up costs a reader
      something and a marginal gain is not worth it.
    * Everything else - title, tick labels, axis names - is fixed furniture
      subtracted before the map is fitted, so the map never overruns it.
    """
    span_x = max(float(span_x), 1e-9)
    span_y = max(float(span_y), 1e-9)
    best: dict[str, Any] | None = None
    orientations = (False, True) if allow_rotation else (False,)
    for rotated in orientations:
        # A quarter turn swaps which way the ground is long.
        width_span = span_y if rotated else span_x
        height_span = span_x if rotated else span_y
        aspect = height_span / width_span
        for side in ("right", "bottom") if has_colourbar else ("none",):
            available_width = maximum_width - YAXIS_WIDTH_INCHES - (
                COLOURBAR_WIDTH_INCHES if side == "right" else 0.0
            )
            available_height = maximum_height - TITLE_HEIGHT_INCHES - (
                XAXIS_HEIGHT_INCHES
            ) - (COLOURBAR_HEIGHT_INCHES if side == "bottom" else 0.0)
            if available_width <= 0 or available_height <= 0:
                continue
            map_width = min(available_width, available_height / aspect)
            map_height = map_width * aspect
            candidate = {
                "rotated": rotated,
                "colourbar": side,
                "map_width": map_width,
                "map_height": map_height,
                "area": map_width * map_height,
                "figure_width": map_width + YAXIS_WIDTH_INCHES + (
                    COLOURBAR_WIDTH_INCHES if side == "right" else 0.0
                ),
                "figure_height": map_height + TITLE_HEIGHT_INCHES
                + XAXIS_HEIGHT_INCHES
                + (COLOURBAR_HEIGHT_INCHES if side == "bottom" else 0.0),
            }
            if best is None:
                best = candidate
                continue
            # Rotation has to clear the bar; a different colour bar side does
            # not, because neither orientation of the bar costs the reader
            # anything.
            threshold = (
                best["area"] * ROTATION_GAIN
                if candidate["rotated"] != best["rotated"] and candidate["rotated"]
                else best["area"]
            )
            if candidate["area"] > threshold:
                best = candidate
    if best is None:  # pragma: no cover - only if the budget is degenerate
        raise ValueError("No map fits inside the given page budget")
    if min(best["map_width"], best["map_height"]) < MINIMUM_MAP_INCHES:
        # A very elongated track: let it run to the budget rather than shrink
        # the long side to keep a short one legible.
        best["too_thin"] = True
    return best


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
    allow_rotation: bool = True,
) -> list[Path]:
    """One flight track, drawn to the campaign's figure standard.

    The extent is the track and a margin, never the window someone happened to
    leave a browser map on, and the page is sized to the ground's own
    proportions inside seven inches by five.
    """
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
    layout = plan_layout(
        east - west, y_high - y_low, colour is not None,
        allow_rotation=allow_rotation,
    )

    written: list[Path] = []
    destination = Path(destination)
    destination.mkdir(parents=True, exist_ok=True)

    with plt.rc_context(rc_parameters()):
        figure, axis = plt.subplots(
            figsize=(layout["figure_width"], layout["figure_height"]),
            constrained_layout=True,
        )
        zoom = choose_zoom(
            west, east, south, north,
            layout["map_width"] * min(resolution, 300),
        )
        mosaic, extent = (None, None)
        if cache_directory is not None:
            mosaic, extent = basemap(west, east, south, north, zoom,
                                     Path(cache_directory))
        # A quarter turn anticlockwise, applied to the ground and everything
        # drawn on it: east goes up the page and north goes to the left. It is a
        # rotation, not a transpose - a transpose would mirror the map and swap
        # east for west, which is worse than a badly proportioned figure.
        rotated = bool(layout["rotated"])

        def place(x, y):
            return (-np.asarray(y), np.asarray(x)) if rotated else (x, y)

        if mosaic is not None:
            picture = np.asarray(mosaic)
            left, right, bottom, top = extent
            if rotated:
                picture = np.rot90(picture, k=1)
                left, right, bottom, top = -top, -bottom, left, right
            axis.imshow(
                picture, extent=(left, right, bottom, top), origin="upper",
                interpolation="bilinear", zorder=0,
            )
        else:
            axis.set_facecolor("#f2f2ef")

        points = np.column_stack(
            place(longitude, np.array([_mercator_y(value) for value in latitude]))
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
            # Outside the map, on the side the layout left room on. Drawn over
            # the map it hides the ground it is describing.
            horizontal = layout["colourbar"] == "bottom"
            bar = figure.colorbar(
                collection, ax=axis, pad=0.02, fraction=0.05,
                location="bottom" if horizontal else "right",
                orientation="horizontal" if horizontal else "vertical",
            )
            if value_label:
                bar.set_label(value_label)
            bar.outline.set_linewidth(0.8)
        else:
            axis.add_collection(
                LineCollection(segments, colors="#1565c0", linewidths=2.2, zorder=3)
            )

        # Tick density from the drawn size, not a fixed count: six labels on a
        # one-inch axis is a smear, and an elongated map has one of each.
        # Rotated, latitude runs along the wide axis and longitude up the short
        # one; upright it is the other way about.
        longitude_inches = layout["map_height"] if rotated else layout["map_width"]
        latitude_inches = layout["map_width"] if rotated else layout["map_height"]
        longitude_ticks = _degree_ticks(west, east, _tick_target(longitude_inches))
        latitude_ticks = _degree_ticks(south, north, _tick_target(latitude_inches))
        merc_ticks = [_mercator_y(value) for value in latitude_ticks]
        if rotated:
            axis.set_xlim(-y_high, -y_low)
            axis.set_ylim(west, east)
            axis.set_xticks([-value for value in merc_ticks])
            axis.set_xticklabels([f"{value:g}" for value in latitude_ticks])
            axis.set_yticks(longitude_ticks)
            axis.set_xlabel("Latitude (°)")
            axis.set_ylabel("Longitude (°)")
        else:
            axis.set_xlim(west, east)
            axis.set_ylim(y_low, y_high)
            axis.set_xticks(longitude_ticks)
            axis.set_yticks(merc_ticks)
            axis.set_yticklabels([f"{value:g}" for value in latitude_ticks])
            axis.set_xlabel("Longitude (°)")
            axis.set_ylabel("Latitude (°)")
        # The ground keeps its proportions; the box was sized for them, so this
        # neither stretches the map nor leaves the frame part empty.
        axis.set_aspect("equal", adjustable="box")
        axis.tick_params(direction="out", length=3.5, width=0.8)

        _scale_bar(axis, west, east, float(latitude.mean()), rotated=rotated)
        _north_arrow(axis, rotated=rotated)
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
