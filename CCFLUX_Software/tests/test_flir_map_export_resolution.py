"""The exported thermal map carries real map detail, not a stretched screen.

The export built a canvas at twice the container size but drew the on-screen
tiles into it at their CSS size, so a 900 px wide map became an 1800 px image
holding 900 px of actual road. The file then declared 1800/7 = 257 DPI for
something with about 129 DPI of map in it, and it printed soft.

Real resolution comes from a deeper tile zoom: one zoom level is one doubling of
genuine tile pixels. Everything else - track, frames, legend - is drawn in that
same pixel space.
"""

from __future__ import annotations

import base64
import io
import re
from pathlib import Path

import pytest

pytest.importorskip("PIL")
from PIL import Image

from core.map_pdf_export import FIGURE_WIDTH_INCHES, render_map_figure

SCRIPT = (Path(__file__).resolve().parents[1] / "app" / "assets" / "flir.js").read_text(
    encoding="utf-8"
)


class TestTheBaseMapIsRedrawnNotStretched:
    def test_tiles_are_fetched_for_the_export(self):
        assert "async function drawTiles(" in SCRIPT
        assert "tile.openstreetmap.org/${plan.zoom}/" in SCRIPT

    def test_the_zoom_is_chosen_to_reach_the_requested_width(self):
        assert "function tilePlan(targetWidth)" in SCRIPT
        assert "if(width>=targetWidth)break" in SCRIPT
        assert "EXPORT_WIDTH_INCHES*(Number(dpi)||300)" in SCRIPT

    def test_the_old_stretch_is_no_longer_the_primary_path(self):
        """context.scale(2,2) over screen tiles added pixels, not detail."""
        assert "context.scale(2,2)" not in SCRIPT
        # It survives only as the offline fallback.
        assert "function drawScreenTiles(" in SCRIPT
        assert "if(!await drawTiles(context,plan))drawScreenTiles(context,plan)" in SCRIPT

    def test_vectors_are_drawn_in_the_tile_pixel_space(self):
        """latLngToContainerPoint is screen space and would misplace the track."""
        assert "map.project([point.latitude,point.longitude],plan.zoom)" in SCRIPT
        body = SCRIPT[SCRIPT.index("async function composeMapImage("):]
        body = body[: body.index("\n  // Laid out in points")]
        assert "latLngToContainerPoint" not in body

    def test_the_request_is_bounded(self):
        """A deeper zoom must not ask the tile server for the world."""
        assert "MAX_EXPORT_TILES=480" in SCRIPT
        assert "MAX_EXPORT_PIXELS=8000" in SCRIPT
        assert "if(tiles>MAX_EXPORT_TILES)break" in SCRIPT
        assert "if(width>MAX_EXPORT_PIXELS||height>MAX_EXPORT_PIXELS)break" in SCRIPT

    def test_tiles_are_requested_without_tainting_the_canvas(self):
        assert "image.crossOrigin='anonymous'" in SCRIPT

    def test_a_missing_tile_leaves_paper_rather_than_failing(self):
        assert "image.onerror=()=>done()" in SCRIPT

    def test_the_composition_is_awaited(self):
        """It fetches tiles now, so a synchronous call would export a blank page."""
        assert "const image=await composeMapImage(dpi)" in SCRIPT
        assert "image:composeMapImage()" not in SCRIPT


class TestTheLegendStaysLegible:
    def test_it_is_laid_out_in_points(self):
        assert "const pt=width/(EXPORT_WIDTH_INCHES*72)" in SCRIPT

    def test_no_type_is_smaller_than_nine_point(self):
        body = SCRIPT[SCRIPT.index("function drawMapExportLegend("):]
        body = body[: body.index("\n  async function exportMapFigure")]
        sizes = [float(value) for value in re.findall(r"\$\{([\d.]+)\*pt\}px Arial", body)]
        assert sizes, "the legend declares no type size in points"
        assert min(sizes) >= 9.0, f"legend type falls to {min(sizes)} pt"

    def test_the_fixed_pixel_sizes_are_gone(self):
        """At 2100 px the old 13 px title printed at 4.5 pt."""
        assert "'700 13px Arial'" not in SCRIPT
        assert "context.font='11px Arial'" not in SCRIPT
        assert "context.font='10px Arial'" not in SCRIPT


def _png(width: int, height: int) -> str:
    buffer = io.BytesIO()
    Image.new("RGB", (width, height), "white").save(buffer, format="PNG")
    return "data:image/png;base64," + base64.b64encode(buffer.getvalue()).decode()


class TestTheFileReportsTheResolutionItReallyHas:
    def test_a_screen_sized_map_is_reported_as_what_it_is(self):
        """The old path: 1800 px over seven inches is 257 DPI, not 300."""
        name, _, _, effective = render_map_figure(
            _png(1800, 1100), flight_name="F", map_name="M", subject="s",
            filename_tag="flir_temperature_map", image_format="pdf", dpi=300,
        )
        assert round(effective) == 257
        assert "257dpi" in name

    def test_a_deeper_composition_reaches_the_requested_resolution(self):
        """3600 px of real tiles downscales to a true 300 DPI page."""
        _, _, _, effective = render_map_figure(
            _png(3600, 2200), flight_name="F", map_name="M", subject="s",
            filename_tag="flir_temperature_map", image_format="pdf", dpi=300,
        )
        assert effective == pytest.approx(300.0)

    def test_the_declared_width_is_the_manuscript_width(self):
        assert FIGURE_WIDTH_INCHES == 7.0

    @pytest.mark.parametrize("dpi,pixels", [(150, 1050), (300, 2100), (600, 4200)])
    def test_the_browser_target_matches_the_server_ceiling(self, dpi, pixels):
        """What tilePlan aims for is what render_map_figure keeps."""
        assert round(FIGURE_WIDTH_INCHES * dpi) == pixels
