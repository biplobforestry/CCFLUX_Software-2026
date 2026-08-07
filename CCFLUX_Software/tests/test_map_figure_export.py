"""A map exported for a manuscript is seven inches wide, whatever the format.

The requested resolution is a ceiling, not a promise: the browser composes the
map at the pixels the tiles carry, and enlarging that would invent detail. The
figure is downscaled if it exceeds the ceiling and its physical width is
declared as seven inches either way.
"""

import base64
import io

import pytest
from PIL import Image

from core.map_pdf_export import (
    EXPORT_FORMATS,
    FIGURE_WIDTH_INCHES,
    MAXIMUM_EXPORT_DPI,
    render_map_figure,
)


def _data_url(width: int = 1600, height: int = 900) -> str:
    image = Image.new("RGB", (width, height), (40, 90, 140))
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return "data:image/png;base64," + base64.b64encode(buffer.getvalue()).decode()


def _render(**overrides):
    request = {
        "flight_name": "Flight_CCT0803",
        "map_name": "FLIR apparent land-surface temperature map",
        "subject": "Georeferenced FLIR apparent land-surface temperature",
        "filename_tag": "flir_temperature_map",
    }
    request.update(overrides)
    return render_map_figure(request.pop("data_url", _data_url()), **request)


def test_the_three_offered_formats_are_pdf_png_and_svg():
    assert set(EXPORT_FORMATS) == {"pdf", "png", "svg"}


def test_the_highest_resolution_offered_is_1500_dpi():
    assert MAXIMUM_EXPORT_DPI == 1500
    with pytest.raises(ValueError, match="DPI must be between"):
        _render(dpi=1501)


def test_an_unknown_format_is_refused():
    with pytest.raises(ValueError, match="PDF, PNG, or SVG"):
        _render(image_format="tiff")


@pytest.mark.parametrize("suffix", sorted(EXPORT_FORMATS))
def test_every_format_is_written_and_named(suffix):
    filename, content, media_type, effective = _render(image_format=suffix, dpi=300)

    assert filename.endswith(f".{suffix}")
    assert "Flight_CCT0803" in filename and "flir_temperature_map" in filename
    assert content and media_type
    # The name states the resolution actually achieved, not the one asked for.
    assert f"{round(effective)}dpi" in filename


def test_a_png_declares_the_seven_inch_width():
    _, content, _, effective = _render(image_format="png", dpi=1500)

    with Image.open(io.BytesIO(content)) as image:
        assert image.width / effective == pytest.approx(FIGURE_WIDTH_INCHES)
        # PNG stores the density as a rational, so it is not bit-exact.
        assert image.info["dpi"][0] == pytest.approx(effective, rel=1e-4)


def test_a_capture_is_never_enlarged_to_reach_the_requested_dpi():
    """Upscaling would invent detail the tiles never carried."""
    _, content, _, effective = _render(image_format="png", dpi=1500)

    with Image.open(io.BytesIO(content)) as image:
        assert image.width == 1600
    assert effective == pytest.approx(1600 / FIGURE_WIDTH_INCHES)


def test_a_capture_wider_than_the_ceiling_is_reduced_to_it():
    _, content, _, effective = _render(
        data_url=_data_url(4000, 2000), image_format="png", dpi=150
    )

    with Image.open(io.BytesIO(content)) as image:
        assert image.width == round(FIGURE_WIDTH_INCHES * 150)
        assert image.height == round(2000 * image.width / 4000)
    assert effective == pytest.approx(150)


def test_an_svg_places_the_map_at_seven_inches():
    _, content, media_type, _ = _render(image_format="svg")
    drawing = content.decode("utf-8")

    assert media_type == "image/svg+xml"
    assert f'width="{FIGURE_WIDTH_INCHES:g}in"' in drawing
    assert 'xlink:href="data:image/png;base64,' in drawing
    assert "Flight_CCT0803" in drawing


def test_a_pdf_is_a_pdf():
    _, content, media_type, _ = _render(image_format="pdf")

    assert content.startswith(b"%PDF")
    assert media_type == "application/pdf"


def test_the_flight_name_cannot_escape_the_file_name():
    filename, _, _, _ = _render(flight_name="../../etc/passwd")

    assert "/" not in filename and ".." not in filename
