"""Every exported map carries a scale bar, a north arrow and coordinates.

A map figure that leaves the screen has to stand on its own: without a scale
nobody can say how far apart two plumes were, without an orientation mark nobody
can say which way the airship flew, and without coordinates the figure cannot be
tied to anything else in the campaign.

The FLIR thermal map and the OPC and Partector size maps carried none of the
three. The MIRO Rack map already drew a north arrow, a scale bar and its corner
coordinates, so it needed only the graticule.
"""

from __future__ import annotations

import math
import re
from pathlib import Path

import pytest

ASSETS = Path(__file__).resolve().parents[1] / "app" / "assets"
FURNITURE = (ASSETS / "map_furniture.js").read_text(encoding="utf-8")


def read(name: str) -> str:
    return (ASSETS / name).read_text(encoding="utf-8")


class TestTheSharedFurnitureExists:
    def test_it_draws_all_three(self):
        assert "function drawScaleBar(" in FURNITURE
        assert "function drawNorthArrow(" in FURNITURE
        assert "function drawGraticule(" in FURNITURE

    def test_it_is_published_for_the_map_pages(self):
        assert "global.CCFLUXMapFurniture" in FURNITURE
        for name in ("draw", "graticule", "metresPerPixel"):
            assert re.search(rf"\b{name}\b", FURNITURE), name

    def test_type_never_falls_below_nine_point(self):
        assert "const MINIMUM_POINTS = 9" in FURNITURE
        sizes = [
            float(value)
            for value in re.findall(r"\$\{([\d.]+) \* pt\}px Arial", FURNITURE)
        ]
        assert all(size >= 9 for size in sizes), sizes
        # The rest are expressed as MINIMUM_POINTS or MINIMUM_POINTS + n.
        assert "MINIMUM_POINTS * pt}px Arial" in FURNITURE

    def test_it_is_laid_out_against_the_seven_inch_width(self):
        assert "const FIGURE_WIDTH_INCHES = 7" in FURNITURE
        assert "options.width / (FIGURE_WIDTH_INCHES * 72)" in FURNITURE

    def test_labels_are_readable_over_any_tile(self):
        """Dark text on a light roof and light text on a dark park both vanish."""
        assert "function halo(" in FURNITURE
        assert "context.strokeStyle = PAPER" in FURNITURE

    def test_it_degrades_rather_than_throwing(self):
        assert "if (!context || !options || !options.width || !options.height) return null" in FURNITURE


class TestTheScaleBarIsHonest:
    """The bar is a ground distance, so it has to come from the projection."""

    def test_metres_per_pixel_narrows_with_latitude(self):
        """Web Mercator stretches with latitude; a pixel covers less ground."""
        assert "Math.cos(latitude * Math.PI / 180)" in FURNITURE
        assert "156543.03392804097" in FURNITURE

    def test_the_reference_value_is_the_web_mercator_constant(self):
        # 2 pi R / 256 for R = 6378137 m, the tile-pyramid resolution at zoom 0.
        expected = 2 * math.pi * 6378137 / 256
        assert expected == pytest.approx(156543.03392804097, rel=1e-9)

    def test_the_length_is_a_number_a_reader_can_divide(self):
        assert "const STEPS = [1, 2, 5]" in FURNITURE
        assert "function niceBelow(" in FURNITURE

    def test_it_rounds_down_so_the_bar_never_overstates_the_distance(self):
        assert "if (step * decade <= value) best = step * decade" in FURNITURE

    def test_it_switches_to_kilometres(self):
        assert "function formatDistance(" in FURNITURE
        assert "km" in FURNITURE and "' m'" not in FURNITURE


class TestTheGraticule:
    def test_labels_carry_a_hemisphere(self):
        assert "lat < 0 ? 'S' : 'N'" in FURNITURE
        assert "lon < 0 ? 'W' : 'E'" in FURNITURE

    def test_the_interval_adapts_to_the_extent(self):
        assert "function graticuleStep(" in FURNITURE
        assert "function decimalsFor(" in FURNITURE

    def test_lines_are_drawn_before_labels(self):
        """A grid line printed over a label makes the label unreadable."""
        lines = FURNITURE.index("labels.forEach(")
        assert lines > FURNITURE.index("context.setLineDash([]);")


@pytest.mark.parametrize("name", ["flir.js", "size_map.js"])
class TestTheMapsThatHadNoneNowDrawIt:
    def test_the_export_calls_the_shared_furniture(self, name):
        source = read(name)
        assert "CCFLUXMapFurniture.draw(" in source
        assert "drawMapFurniture(context" in source

    def test_it_supplies_a_projection_and_bounds(self, name):
        source = read(name)
        assert "project:" in source
        assert "north:" in source and "south:" in source
        assert "east:" in source and "west:" in source

    def test_it_supplies_the_ground_scale(self, name):
        assert "CCFLUXMapFurniture.metresPerPixel(" in read(name)

    def test_a_missing_helper_does_not_break_the_export(self, name):
        assert "typeof CCFLUXMapFurniture" in read(name)


class TestTheMiroRackMapKeepsItsOwn:
    source = read("miro_rack_map.js")

    def test_it_already_drew_a_north_arrow_and_scale_bar(self):
        assert "drawNorthArrow(context" in self.source
        assert "drawScaleBar(context" in self.source

    def test_it_labels_its_corners(self):
        assert "SW ${southWest.lat.toFixed(5)}" in self.source
        assert "NE ${northEast.lat.toFixed(5)}" in self.source

    def test_it_gains_only_the_graticule(self):
        assert "CCFLUXMapFurniture.graticule(context" in self.source
        assert "CCFLUXMapFurniture.draw(" not in self.source

    def test_the_grid_is_clipped_to_the_map_area(self):
        """It has a header and a footer the shared helper cannot see."""
        assert "context.translate(0,headerHeight)" in self.source


class TestTheMicaSenseTrack:
    """A Plotly map subplot draws no furniture, so it is stated in the layout."""

    source = read("micasense.js")

    def test_north_and_the_coordinate_range_are_annotated(self):
        assert "mapFurnitureAnnotations(" in self.source
        assert "'▲<br><b>N</b>'" in self.source
        assert "WGS 84 · north up · OpenStreetMap basemap" in self.source

    def test_the_annotations_are_paper_anchored(self):
        """Data-anchored furniture would drift when the export is resized."""
        assert "xref: 'paper', yref: 'paper'" in self.source

    def test_the_hemispheres_are_named(self):
        assert "range(lats, 'N', 'S')" in self.source
        assert "range(lons, 'E', 'W')" in self.source


def test_every_map_page_is_served_the_helper():
    server = (Path(__file__).parents[1] / "app" / "server.py").read_text(encoding="utf-8")
    bundles = re.findall(
        r'elif path (?:==|in) ([^\n]+):\n\s+self\._send_javascript_bundle\((.*?)\n\s+\)',
        server, re.DOTALL,
    )
    needing = [
        (route, body) for route, body in bundles
        if "flir.js" in body or "size_map.js" in body or "miro_rack_map.js" in body
    ]
    assert needing, "no map bundle was found in the route table"
    for route, body in needing:
        assert "map_furniture.js" in body, f"{route.strip()} is not served the helper"
