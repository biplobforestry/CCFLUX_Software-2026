"""One definition of what a CC-FLUX figure is, for every plot the project makes.

The campaign's figures were authored at whatever size each script happened to
choose: the OPC quicklook at seventeen inches by sixteen, the Partector at
13.2 by 9, the Mapview export at 17.33 with no text in it at all. Dropped into a
report they are rescaled by whoever is writing it, and a rescaled figure carries
rescaled text - the OPC panel labels, set at seven point on a seventeen-inch
sheet, arrive at under three point in a manuscript column.

So two rules hold for every figure that leaves this project:

* seven inches wide, so it drops into a column at its authored size and its
  text arrives at the size it was set in;
* nothing below nine point, which is the smallest that still reads there.

Both are checked on the laid-out figure rather than trusted from the settings,
because constrained_layout is free to shrink tick labels to make room and will
happily take a figure configured at nine point below it. `finalise` is the
guarantee and is cheap to call; authoring at `PAGE_WIDTH_INCHES` in the first
place is what makes it a formality rather than a rescue.

Height is deliberately not fixed. A three-panel time series and a single
scatter want different shapes, and forcing one aspect on both wastes paper or
crowds the axes. It is bounded only so a figure still fits on a page beside its
caption.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Iterable

# The width of a single-column figure in the journals this campaign publishes
# in, and of the report template the flights are written up in.
PAGE_WIDTH_INCHES = 7.0
MINIMUM_FONT_POINTS = 9.0
# A caption needs room beneath the figure on the same page.
MAXIMUM_HEIGHT_INCHES = 9.0
MINIMUM_HEIGHT_INCHES = 2.0

# Enough for a printed raster to hold the detail of the vector original without
# producing files too large to mail.
RASTER_DPI = 200

EXPORT_FORMATS = ("pdf", "png", "svg")
MEDIA_TYPES = {
    "pdf": "application/pdf",
    "png": "image/png",
    "svg": "image/svg+xml",
}


def media_type(image_format: str) -> str:
    """The content type a browser needs to display or download the figure."""
    return MEDIA_TYPES.get(str(image_format).casefold(), "application/octet-stream")


def rc_parameters(
    minimum_points: float = MINIMUM_FONT_POINTS,
    *,
    dpi: int = RASTER_DPI,
) -> dict[str, Any]:
    """Every text size at or above the floor, before anything is drawn.

    Sizes are set relative to the floor rather than as absolutes, so raising the
    floor for a poster raises the whole hierarchy with it and keeps a title
    looking like a title.
    """
    return {
        "font.size": minimum_points,
        "axes.titlesize": minimum_points + 1.0,
        "axes.labelsize": minimum_points + 0.5,
        "xtick.labelsize": minimum_points,
        "ytick.labelsize": minimum_points,
        "legend.fontsize": minimum_points,
        "legend.title_fontsize": minimum_points,
        "figure.titlesize": minimum_points + 2.0,
        "axes.linewidth": 0.8,
        "axes.grid": True,
        "grid.alpha": 0.25,
        "grid.linewidth": 0.5,
        "lines.linewidth": 1.1,
        "legend.frameon": True,
        "legend.framealpha": 0.9,
        "legend.borderpad": 0.4,
        "figure.dpi": 100,
        "savefig.dpi": dpi,
        "savefig.facecolor": "white",
        # Deliberately not "tight". Tight crops to the ink, so a portrait map
        # came out 6.02 inches wide instead of seven and no longer matched the
        # column it was drawn for. constrained_layout already fits the furniture
        # inside the figure, so the saved size is the size that was asked for.
        "savefig.bbox": None,
        "savefig.pad_inches": 0.0,
    }


def enforce_minimum_font(
    figure, minimum_points: float = MINIMUM_FONT_POINTS
) -> list[float]:
    """Raise anything the layout engine shrank back to the floor.

    Returns the sizes it found, so a caller that wants to assert on them does
    not have to walk the figure again.
    """
    figure.canvas.draw()
    sizes: list[float] = []
    for artist in figure.findobj(match=lambda item: hasattr(item, "get_fontsize")):
        try:
            size = float(artist.get_fontsize())
        except (TypeError, ValueError):
            continue
        if size < minimum_points:
            artist.set_fontsize(minimum_points)
            size = minimum_points
        sizes.append(size)
    return sizes


def fit_to_page(figure, width_inches: float = PAGE_WIDTH_INCHES) -> None:
    """Bring the figure to the page width, keeping its proportions.

    A producer that already authored at the page width gets nothing done to it.
    One that did not is scaled rather than cropped: cropping to the ink is what
    turned a portrait map into 6.02 inches and broke the very guarantee the
    width exists to give.

    Scaling only moves the paper. Text is set in points and stays where it was
    set, so a figure squeezed from seventeen inches keeps readable labels but
    crowds them - which is why the producers are authored at the page width and
    this is the backstop, not the mechanism.
    """
    current_width, current_height = figure.get_size_inches()
    if current_width <= 0:
        return
    height = current_height * (width_inches / current_width)
    height = min(MAXIMUM_HEIGHT_INCHES, max(MINIMUM_HEIGHT_INCHES, height))
    figure.set_size_inches(width_inches, height)


def finalise(
    figure,
    *,
    width_inches: float = PAGE_WIDTH_INCHES,
    minimum_points: float = MINIMUM_FONT_POINTS,
) -> None:
    """Both rules, applied to the laid-out figure, in the order that works.

    The width is set first because changing it re-runs the layout, and the
    layout is what shrinks the text that then has to be raised.
    """
    fit_to_page(figure, width_inches)
    enforce_minimum_font(figure, minimum_points)


def save(
    figure,
    paths: Path | str | Iterable[Path | str],
    *,
    dpi: int = RASTER_DPI,
    width_inches: float = PAGE_WIDTH_INCHES,
    minimum_points: float = MINIMUM_FONT_POINTS,
    **savefig_arguments: Any,
) -> list[Path]:
    """Write the figure to every path asked for, to the standard.

    Both rules are applied once, before the first write, so the PDF and the PNG
    of one figure are the same figure and not two slightly different ones.
    """
    if isinstance(paths, (str, Path)):
        paths = [paths]
    written: list[Path] = []
    finalise(figure, width_inches=width_inches, minimum_points=minimum_points)
    savefig_arguments.setdefault("facecolor", "white")
    savefig_arguments.setdefault("bbox_inches", None)
    savefig_arguments.setdefault("pad_inches", 0.0)
    for value in paths:
        path = Path(value)
        path.parent.mkdir(parents=True, exist_ok=True)
        figure.savefig(path, dpi=dpi, **savefig_arguments)
        written.append(path)
    return written
