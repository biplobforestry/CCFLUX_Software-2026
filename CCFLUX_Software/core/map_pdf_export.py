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
