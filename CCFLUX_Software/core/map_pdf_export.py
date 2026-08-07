"""Turn a browser-composed map image into a print-resolution PDF.

The browser draws what the operator can actually see - the tiles, the coloured
track, the legend - and sends it here as a PNG. Rasterising in the page and
converting on the server keeps one rendering of the map, so the exported PDF
carries the same picture that was reviewed on screen.
"""
from __future__ import annotations

import base64
import binascii
import io
import re
from datetime import datetime, timezone

from PIL import Image, UnidentifiedImageError

MAXIMUM_ENCODED_BYTES = 64_000_000
MINIMUM_SIZE = (800, 500)
MAXIMUM_SIZE = (10_000, 10_000)


def decode_map_png(data_url: str) -> Image.Image:
    """Return the opaque RGB image carried by a PNG data URL."""
    if not data_url.startswith("data:image/png;base64,"):
        raise ValueError("Map export must contain a PNG image")
    encoded = data_url.split(",", 1)[1]
    if len(encoded) > MAXIMUM_ENCODED_BYTES:
        raise ValueError("Map export image is too large")
    try:
        raw = base64.b64decode(encoded, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise ValueError("Map export image is not valid base64 data") from exc
    try:
        image = Image.open(io.BytesIO(raw))
        image.load()
    except (OSError, UnidentifiedImageError) as exc:
        raise ValueError("Map export image is not a readable PNG") from exc
    if image.format != "PNG":
        raise ValueError("Map export image must use PNG format")
    width, height = image.size
    if not (
        MINIMUM_SIZE[0] <= width <= MAXIMUM_SIZE[0]
        and MINIMUM_SIZE[1] <= height <= MAXIMUM_SIZE[1]
    ):
        raise ValueError(
            "Map export image dimensions are outside the supported range"
        )
    if image.mode in {"RGBA", "LA"}:
        # A map drawn with transparency would print as black without a page
        # behind it, so the transparent parts become white paper.
        background = Image.new("RGB", image.size, "white")
        background.paste(image.convert("RGB"), mask=image.getchannel("A"))
        return background
    return image.convert("RGB")


def safe_file_stem(value: str, fallback: str = "Flight") -> str:
    stem = re.sub(r"[^A-Za-z0-9._-]+", "_", str(value)).strip("._")
    return stem or fallback


FIGURE_WIDTH_INCHES = 7.0
MINIMUM_EXPORT_DPI = 72
MAXIMUM_EXPORT_DPI = 1500
EXPORT_FORMATS = ("pdf", "png", "svg")
EXPORT_MEDIA_TYPES = {
    "pdf": "application/pdf",
    "png": "image/png",
    "svg": "image/svg+xml",
}


def render_map_figure(
    data_url: str,
    *,
    flight_name: str,
    map_name: str,
    subject: str,
    filename_tag: str,
    image_format: str = "pdf",
    dpi: int = 300,
) -> tuple[str, bytes, str, float]:
    """Return a seven-inch wide map figure, its name, type and true resolution.

    The requested resolution is a ceiling, not a promise: the browser composes
    the map at the pixels it can actually draw, and enlarging that would invent
    detail the tiles never carried. The image is downscaled if it exceeds the
    ceiling and otherwise kept, and the physical width is declared as seven
    inches either way, so the printed figure is the width a manuscript column
    expects and the resolution reported is the one that was really achieved.
    """
    suffix = str(image_format).casefold()
    if suffix not in EXPORT_FORMATS:
        raise ValueError("Choose PDF, PNG, or SVG for the map export")
    resolution = int(dpi)
    if not MINIMUM_EXPORT_DPI <= resolution <= MAXIMUM_EXPORT_DPI:
        raise ValueError(
            f"DPI must be between {MINIMUM_EXPORT_DPI} and {MAXIMUM_EXPORT_DPI}"
        )
    image = decode_map_png(data_url)
    ceiling = round(FIGURE_WIDTH_INCHES * resolution)
    if image.width > ceiling:
        height = max(1, round(image.height * ceiling / image.width))
        image = image.resize((ceiling, height), Image.LANCZOS)
    effective = image.width / FIGURE_WIDTH_INCHES
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    filename = (
        f"{safe_file_stem(flight_name)}_{filename_tag}_{stamp}"
        f"_{round(effective)}dpi.{suffix}"
    )
    buffer = io.BytesIO()
    if suffix == "svg":
        # The map is a raster; an SVG of it is that raster placed at the right
        # physical size, which is what a vector wrapper can honestly carry.
        raw = io.BytesIO()
        image.save(raw, format="PNG")
        encoded = base64.b64encode(raw.getvalue()).decode("ascii")
        inches_high = image.height / effective
        drawing = (
            '<?xml version="1.0" encoding="UTF-8"?>\n'
            f'<svg xmlns="http://www.w3.org/2000/svg" '
            f'xmlns:xlink="http://www.w3.org/1999/xlink" '
            f'width="{FIGURE_WIDTH_INCHES:g}in" height="{inches_high:.4f}in" '
            f'viewBox="0 0 {image.width} {image.height}">\n'
            f"  <title>{_escaped(flight_name)} - {_escaped(map_name)}</title>\n"
            f"  <desc>{_escaped(subject)}</desc>\n"
            f'  <image x="0" y="0" width="{image.width}" height="{image.height}" '
            f'xlink:href="data:image/png;base64,{encoded}"/>\n'
            "</svg>\n"
        )
        return filename, drawing.encode("utf-8"), EXPORT_MEDIA_TYPES[suffix], effective
    if suffix == "png":
        image.save(buffer, format="PNG", dpi=(effective, effective))
    else:
        image.save(
            buffer, format="PDF", resolution=effective, quality=95,
            title=f"{flight_name} - {map_name}",
            author="Biplob Dey - Forschungszentrum Jülich GmbH",
            subject=subject,
        )
    return filename, buffer.getvalue(), EXPORT_MEDIA_TYPES[suffix], effective


def _escaped(value: str) -> str:
    return (
        str(value).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    )


def render_map_pdf(
    data_url: str,
    *,
    flight_name: str,
    map_name: str,
    subject: str,
    filename_tag: str,
) -> tuple[str, bytes]:
    """Return a timestamped file name and the PDF bytes for a map image."""
    image = decode_map_png(data_url)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    filename = f"{safe_file_stem(flight_name)}_{filename_tag}_{stamp}.pdf"
    buffer = io.BytesIO()
    image.save(
        buffer,
        format="PDF",
        resolution=300.0,
        quality=95,
        title=f"{flight_name} - {map_name}",
        author="Biplob Dey - Forschungszentrum Jülich GmbH",
        subject=subject,
    )
    return filename, buffer.getvalue()
