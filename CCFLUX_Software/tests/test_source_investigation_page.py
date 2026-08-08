"""The page, its routes and its export - the parts the engine tests do not see.

The engine was landed and checked first because that is where a wrong number
would come from. This covers the wiring: that the button exists and opens the
page, that every call the script makes has a route behind it, and that the
exported figures are the three things a reader needs together rather than a
picture of the browser.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

pytest.importorskip("pandas")

ROOT = Path(__file__).resolve().parents[1]
ASSETS = ROOT / "app" / "assets"
SCRIPT = (ASSETS / "source_investigation.js").read_text(encoding="utf-8")
PAGE = (ASSETS / "source_investigation.html").read_text(encoding="utf-8")
SERVER = (ROOT / "app" / "server.py").read_text(encoding="utf-8")
BRIDGE = (ROOT / "app" / "miro_rack_bridge.py").read_text(encoding="utf-8")
EXPORT = (ROOT / "app" / "source_investigation_export.py").read_text(encoding="utf-8")


class TestItCanBeReached:
    def test_the_miro_rack_page_carries_the_button(self):
        assert 'id="ccfluxSourceInvestigation"' in BRIDGE

    def test_the_button_opens_the_page(self):
        assert "'/miro_rack/source_investigation'" in BRIDGE

    def test_it_opens_in_its_own_tab(self):
        """An investigation is read alongside the workspace it questions."""
        block = BRIDGE[BRIDGE.index("ccfluxSourceInvestigation').onclick"):]
        assert "_blank" in block[:200]

    def test_the_page_and_its_script_are_served(self):
        assert '"/miro_rack/source_investigation"' in SERVER
        assert '"/miro_rack/source_investigation.js"' in SERVER

    def test_the_script_goes_through_the_bundle_helper(self):
        """_send_file takes its type from mimetypes, and a Windows registry
        that maps .js to text/plain makes the browser refuse to run it."""
        block = SERVER[SERVER.index('"/miro_rack/source_investigation.js"'):]
        block = block[: block.index("elif ")]
        assert "_send_javascript_bundle(" in block


class TestEveryCallHasARouteAndAMethod:
    CALLS = (
        "/api/miro-rack/source/channels",
        "/api/miro-rack/source/rows",
        "/api/miro-rack/source/region",
        "/api/miro-rack/source/export",
    )

    @pytest.mark.parametrize("path", CALLS)
    def test_the_script_asks_for_it(self, path):
        assert f"'{path}'" in SCRIPT

    @pytest.mark.parametrize("path", CALLS)
    def test_the_server_answers_it(self, path):
        assert f'"{path}"' in SERVER

    @pytest.mark.parametrize("name", (
        "source_investigation_channels",
        "source_investigation_rows",
        "source_investigation_region",
        "export_source_investigation",
    ))
    def test_the_bridge_implements_it(self, name):
        assert f"def {name}(" in BRIDGE

    def test_the_bridge_methods_are_importable_and_bound(self):
        from app.miro_rack_bridge import MiroRackBridge

        for name in ("source_investigation_channels", "source_investigation_rows",
                     "source_investigation_region", "export_source_investigation"):
            assert callable(getattr(MiroRackBridge, name))


class TestWhatTheOperatorAskedFor:
    def test_the_row_count_and_time_filter_drive_an_update(self):
        assert 'id="rowCount"' in PAGE and 'id="update"' in PAGE
        assert 'id="startTime"' in PAGE and 'id="endTime"' in PAGE

    def test_smoothing_is_controllable_including_its_method(self):
        assert 'id="smoothing"' in PAGE
        for method in ("savgol", "moving", "spline", "none"):
            assert f'value="{method}"' in PAGE
        assert 'id="smoothSeconds"' in PAGE

    def test_each_row_has_a_left_and_a_right_axis(self):
        assert "for (const side of ['left', 'right'])" in SCRIPT
        assert "yaxis2" in SCRIPT

    def test_the_axis_menu_opens_on_right_click(self):
        assert "chip.oncontextmenu" in SCRIPT
        assert "function openMenu(" in SCRIPT

    def test_the_menu_offers_colour_and_line_width(self):
        block = SCRIPT[SCRIPT.index("function openMenu("):]
        block = block[: block.index("function toggle(")]
        assert "type = 'color'" in block
        assert "type = 'range'" in block

    def test_a_region_is_selected_by_dragging(self):
        assert "plotly_selected" in SCRIPT
        assert "dragmode: 'select'" in SCRIPT

    def test_right_clicking_the_selection_asks_where_it_was(self):
        block = SCRIPT[SCRIPT.index("element.oncontextmenu"):]
        assert "loadRegion()" in block[:300]

    def test_the_map_shows_the_whole_flight_with_the_region_marked(self):
        block = SCRIPT[SCRIPT.index("function drawMap("):]
        block = block[: block.index("function drawRose(")]
        assert "track.track.map" in block and "track.region.map" in block

    def test_the_rose_reads_the_way_a_rose_is_read(self):
        """North at the top, clockwise - the opposite of the plotting default."""
        block = SCRIPT[SCRIPT.index("function drawRose("):]
        assert "direction: 'clockwise'" in block[:2400]
        assert "rotation: 90" in block[:2400]


class TestTheHonestyOfTheDisplay:
    def test_the_raw_excursion_is_drawn_behind_every_line(self):
        """A one-second plume is one sample in eight; drawn only as a line it
        would be invisible on a decimated record."""
        assert "function bandFor(" in SCRIPT
        assert "envelope" in SCRIPT

    def test_the_page_says_smoothing_is_display_only(self):
        assert "computed from the raw record" in PAGE

    def test_the_export_says_it_on_the_figure_too(self):
        assert "Computed from the raw record inside the region." in EXPORT

    def test_the_background_is_shown_beside_the_enhancement(self):
        """An instrument whose noise sits below zero gives an enhancement
        larger than the peak, which reads as an error until the arithmetic is
        visible."""
        assert "entry['background']" in EXPORT
        assert "entry.background" in SCRIPT

    def test_a_flight_without_navigation_is_told_so_rather_than_left_blank(self):
        assert "No processed Noseboom navigation" in SCRIPT


class TestTheExport:
    def test_it_writes_the_rows_the_wind_and_the_map(self):
        for name in ("_rows_figure", "_wind_figure", "_region_map"):
            assert f"def {name}(" in EXPORT

    def test_the_rows_obey_the_campaign_standard(self):
        assert "figure_standard.PAGE_WIDTH_INCHES" in EXPORT
        assert "figure_standard.finalise(" in EXPORT

    def test_the_map_obeys_the_map_standard_instead(self):
        """A map stretched to a column width stops being a map of the flight."""
        assert "scientific_map.render_track_map(" in EXPORT

    def test_no_figure_is_authored_wider_than_the_page(self):
        tree = ast.parse(EXPORT)
        widths = [
            node.value.elts[0]
            for node in ast.walk(tree)
            if isinstance(node, ast.keyword) and node.arg == "figsize"
            and isinstance(node.value, ast.Tuple) and node.value.elts
        ]
        for element in widths:
            if isinstance(element, ast.Constant):
                assert element.value <= 7.0

    def test_the_layout_the_page_shows_is_the_layout_exported(self):
        """Otherwise the figure is of a different arrangement than the one the
        feature was spotted on."""
        assert "layout: state.rows" in SCRIPT
        assert "layout" in EXPORT
