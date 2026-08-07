"""Every MicaSense axis carries its unit, and the workspace exports as figures.

Three things were wrong or missing on the page:

  * no axis carried a unit on several panels, and the light-sensor attitude
    carried the wrong one - the DLS writes yaw, pitch and roll in radians and
    the adapter stores them raw, but the axis said degrees, so a level sensor
    read as tilted by a factor of 57;
  * the flight track was bare degrees with no ground beneath it; and
  * there was no way to get the figures out at publication size.
"""

from __future__ import annotations

import base64
import io
import zipfile
from pathlib import Path

import pytest

pytest.importorskip("PIL")
from PIL import Image

from core.map_pdf_export import (
    FIGURE_MINIMUM_FONT_POINTS,
    FIGURE_WIDTH_INCHES,
    MAXIMUM_REPORT_FIGURES,
    render_figure_report,
)

ASSETS = Path(__file__).resolve().parents[1] / "app" / "assets"
SCRIPT = (ASSETS / "micasense.js").read_text(encoding="utf-8")
MARKUP = (ASSETS / "micasense.html").read_text(encoding="utf-8")


class TestEveryAxisCarriesItsUnit:
    def test_the_light_sensor_attitude_is_radians_not_degrees(self):
        """The defect: the DLS writes radians and nothing converts them."""
        assert "'Angle [rad, as recorded]'" in SCRIPT
        assert "'Angle [deg]'" not in SCRIPT

    @pytest.mark.parametrize("label", [
        "'Seconds per trigger [s]'",       # cadence
        "'Normalized sharpness [–]'",      # sharpness is dimensionless
        "'ISO speed [–]'",                 # so is ISO
        "'Exposure time [s]'",
        "'Altitude [m]'",
        "'Irradiance [W/m²/nm]'",
        "'Temperature [°C]'",
        "'Elevation [rad, as recorded]'",
        "'Captures [count]'",
        "'Bands in the capture [count]'",
        "'Image number [–]'",
        "'GPS altitude [m]'",              # the track colour bar
    ])
    def test_the_unit_is_stated(self, label):
        assert label in SCRIPT, f"{label} is missing from the MicaSense panels"

    def test_time_axes_name_their_scale(self):
        assert "'Capture time [UTC]'" in SCRIPT
        assert "'Capture time (UTC)'" not in SCRIPT

    def test_no_axis_title_is_left_bare(self):
        """Any axis title that is a plain word without a bracketed unit."""
        import re

        allowed = {"Capture outcome"}
        titles = re.findall(r"layout\('[^']*','([^']*)','([^']*)'", SCRIPT)
        bare = [
            value
            for pair in titles for value in pair
            if value and "[" not in value and value not in allowed
        ]
        assert not bare, f"axis titles with no unit: {bare}"


class TestTheTrackHasGroundBeneathIt:
    def test_the_base_map_is_openstreetmap(self):
        assert "'scattermap'" in SCRIPT
        assert "style: 'open-street-map'" in SCRIPT

    def test_it_falls_back_when_the_map_cannot_render(self):
        """No WebGL or no tiles must leave the measurement visible."""
        assert "trackTraceAndLayout(rows, false)" in SCRIPT
        assert ".catch(" in SCRIPT

    def test_the_flat_fallback_still_carries_units(self):
        assert "'Longitude [°E]', 'Latitude [°N]'" in SCRIPT


class TestTheExportControls:
    def test_all_three_formats_are_offered(self):
        for value in ('value="pdf"', 'value="png"', 'value="svg"'):
            assert value in MARKUP, value

    def test_the_button_is_wired(self):
        assert 'id="exportFiguresBtn"' in MARKUP
        assert "$('exportFiguresBtn').onclick = exportFigures" in SCRIPT

    def test_the_page_declares_the_print_geometry(self):
        assert "const FIGURE_WIDTH_INCHES = 7" in SCRIPT
        assert "const MINIMUM_FONT_POINTS = 9" in SCRIPT

    def test_every_panel_is_listed_for_export(self):
        """Eleven figures; a panel added without a name would be dropped."""
        import re

        listed = re.findall(r"\['(\w+Plot)', '([a-z_]+)'\]", SCRIPT)
        drawn = set(re.findall(r"Plotly\.react\('(\w+Plot)'", SCRIPT))
        assert len(listed) == 11
        assert {name for name, _ in listed} == drawn

    def test_type_is_sized_in_points_not_screen_pixels(self):
        """A 13 px label in a 700 px panel prints at 6.5 pt."""
        assert "const perPoint = dpi / 72" in SCRIPT
        assert "base: 9.5" in SCRIPT
        for key in ("title:", "axisTitle:", "tick:", "legend:", "colorbar:"):
            assert key in SCRIPT, key

    def test_the_declared_minimum_matches_the_backend(self):
        assert FIGURE_MINIMUM_FONT_POINTS == 9.0
        assert FIGURE_WIDTH_INCHES == 7.0


def _figure(width: int, height: int, colour: str = "white") -> str:
    image = Image.new("RGB", (width, height), colour)
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return "data:image/png;base64," + base64.b64encode(buffer.getvalue()).decode()


def _figures(count: int, dpi: int = 150) -> list[dict[str, str]]:
    width = round(FIGURE_WIDTH_INCHES * dpi)
    return [
        {"name": f"panel_{index}", "image": _figure(width, round(width * 0.63))}
        for index in range(count)
    ]


class TestTheReportFile:
    def test_eleven_figures_become_one_pdf(self):
        name, content, media_type, dpi = render_figure_report(
            _figures(11), flight_name="Flight_CC0806",
            report_name="MicaSense", subject="test",
            filename_tag="micasense_figures", image_format="pdf", dpi=150,
        )
        assert media_type == "application/pdf"
        assert content.startswith(b"%PDF")
        assert name.endswith(".pdf") and "Flight_CC0806" in name and "150dpi" in name
        assert dpi == 150.0
        # One page per figure.
        assert content.count(b"/Type /Page\n") == 11 or content.count(b"/Page") >= 11

    def test_each_pdf_page_is_seven_inches_wide(self):
        _, content, _, _ = render_figure_report(
            _figures(3), flight_name="F", report_name="M", subject="s",
            filename_tag="t", image_format="pdf", dpi=150,
        )
        # PDF user units are 1/72 inch, so seven inches is 504.
        assert b"504" in content

    def test_png_and_svg_come_back_as_a_zip_of_figures(self):
        for image_format, suffix in (("png", ".png"), ("svg", ".svg")):
            name, content, media_type, _ = render_figure_report(
                _figures(11), flight_name="F", report_name="M", subject="s",
                filename_tag="t", image_format=image_format, dpi=150,
            )
            assert media_type == "application/zip" and name.endswith(".zip")
            with zipfile.ZipFile(io.BytesIO(content)) as archive:
                names = archive.namelist()
            assert len(names) == 11
            assert all(item.endswith(suffix) for item in names)
            # Ordered, so the file list reads in panel order.
            assert names == sorted(names)

    def test_an_svg_declares_seven_inches(self):
        _, content, _, _ = render_figure_report(
            _figures(1), flight_name="F", report_name="M", subject="s",
            filename_tag="t", image_format="svg", dpi=150,
        )
        with zipfile.ZipFile(io.BytesIO(content)) as archive:
            drawing = archive.read(archive.namelist()[0]).decode("utf-8")
        assert 'width="7in"' in drawing

    def test_a_figure_of_another_size_is_fitted_to_the_page(self):
        odd = [{"name": "odd", "image": _figure(1000, 620)}]
        _, content, _, _ = render_figure_report(
            odd, flight_name="F", report_name="M", subject="s",
            filename_tag="t", image_format="png", dpi=150,
        )
        with zipfile.ZipFile(io.BytesIO(content)) as archive:
            image = Image.open(io.BytesIO(archive.read(archive.namelist()[0])))
        assert image.width == round(FIGURE_WIDTH_INCHES * 150)

    def test_nothing_captured_says_so(self):
        with pytest.raises(ValueError, match="No figure could be captured"):
            render_figure_report(
                [], flight_name="F", report_name="M", subject="s",
                filename_tag="t", image_format="pdf",
            )

    def test_an_unknown_format_is_refused(self):
        with pytest.raises(ValueError, match="PDF, PNG, or SVG"):
            render_figure_report(
                _figures(1), flight_name="F", report_name="M", subject="s",
                filename_tag="t", image_format="tiff",
            )

    def test_too_many_figures_are_refused(self):
        with pytest.raises(ValueError, match="At most"):
            render_figure_report(
                _figures(MAXIMUM_REPORT_FIGURES + 1), flight_name="F",
                report_name="M", subject="s", filename_tag="t",
            )


def test_the_route_is_registered():
    server = (Path(__file__).parents[1] / "app" / "server.py").read_text(encoding="utf-8")
    backend = (Path(__file__).parents[1] / "app" / "scan_backend.py").read_text(
        encoding="utf-8"
    )
    assert '"/api/micasense/figures/export"' in server
    assert "export_micasense_figures" in server
    assert "def export_micasense_figures" in backend
