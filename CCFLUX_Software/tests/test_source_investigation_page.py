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
pytest.importorskip("scipy")

import numpy as np
import pandas as pd

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

    def test_each_series_gets_its_own_scale_and_ticks(self):
        """Sharing one axis between CO at 20 000 ppb and N2O at 300 flattens
        the second onto the axis line, and it reads as nothing happening."""
        assert "for (const side of ['left', 'right'])" in SCRIPT
        assert "function axisFor(" in SCRIPT
        block = SCRIPT[SCRIPT.index("function axisFor("):]
        block = block[: block.index("function rangeFor(")]
        assert "overlaying = 'y'" in block
        assert "anchor: 'free'" in block
        assert "position:" in block
        # The ticks and the title carry the series' own colour, or there is no
        # telling which of four scales belongs to which trace.
        assert "tickfont: {color: entry.colour" in block

    def test_the_axes_stack_outwards_from_the_plot(self):
        """Adding a series must not move the ones already being read."""
        block = SCRIPT[SCRIPT.index("function axisFor("):]
        assert "counts.left : counts.right) - 1 - order" in block[:900]

    def test_the_plot_makes_room_for_the_stacked_axes(self):
        assert "domain: [plan.left.length * AXIS_WIDTH" in SCRIPT

    def test_a_row_grows_taller_as_axes_are_added(self):
        """Otherwise a row with six series is a squeezed strip."""
        assert "function rowHeight(" in SCRIPT
        assert "ROW_HEIGHT_PER_AXIS" in SCRIPT
        block = SCRIPT[SCRIPT.index("Plotly.react(target"):]
        assert "element.style.height" in SCRIPT[:SCRIPT.index("Plotly.react(target")]

    def test_a_pair_of_spikes_does_not_set_the_scale(self):
        """Measured on Flight_CC0806: CO sits near 120 ppb with two spikes at
        7 000, and a full-range axis drew it as a flat line along the bottom.
        On the body of the trace it reads 78-392 ppb with its plumes visible."""
        assert "function rangeFor(" in SCRIPT
        block = SCRIPT[SCRIPT.index("function rangeFor("):]
        block = block[: block.index("function axisTitle(")]
        assert "at(0.005)" in block and "at(0.995)" in block
        assert "axis.range = range" in SCRIPT

    def test_a_concentration_axis_does_not_start_below_zero(self):
        """Padding the range pushed CO's axis to -457, which says a negative
        concentration is possible."""
        block = SCRIPT[SCRIPT.index("function rangeFor("):]
        assert "sorted[0] >= 0 ? Math.max(0," in block

    def test_altitude_is_drawn_at_a_weight_that_can_be_seen(self):
        """A one-pixel light grey line is easy to take for grid work."""
        block = SCRIPT[SCRIPT.index("function defaultRows("):]
        block = block[: block.index("function renderRows(")]
        assert "key: 'altitude'" in block
        assert "width: 1.4" in block

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

    def test_clicking_a_curve_reads_the_moment_clicked(self):
        """The faster gesture when a feature is narrow: a drag across a plume a
        few pixels wide is awkward, a click on it is not."""
        assert "plotly_click" in SCRIPT
        assert "function setRegionAround(" in SCRIPT
        block = SCRIPT[SCRIPT.index("element.on('plotly_click'"):]
        assert "loadRegion()" in block[:400]

    def test_a_click_inside_an_existing_selection_keeps_it(self):
        """Otherwise a click to inspect a chosen region would silently shrink
        it to two minutes."""
        assert "withinRegion(point.x)" in SCRIPT

    def test_the_click_window_does_not_pick_up_a_timezone(self):
        """The record is naive UTC; a Date built without one shifts every bound
        by an hour in summer."""
        block = SCRIPT[SCRIPT.index("function setRegionAround("):]
        block = block[: block.index("function setRegion(")]
        assert "}Z`" in block
        assert "toISOString()" in block

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

    def test_zero_air_is_not_drawn_as_atmosphere(self):
        """The MIRO switches to zero air on a solenoid, and what it reports
        while that valve is over is the calibration."""
        from app import source_investigation as engine

        assert engine.VALVE_COLUMN == "VValve 0"
        assert engine.AMBIENT_VALVE_STATE == 0
        assert engine.SETTLE_SECONDS > 0

    def test_the_page_reports_how_much_was_removed(self):
        """A page that silently drops a fifth of a flight is worse than one
        that never dropped anything."""
        assert "ambient.note" in SCRIPT

    def test_the_map_library_is_loaded_from_the_path_that_serves_it(self):
        """'/vendor/leaflet.js' is not served; the page died on 'L is not
        defined' the moment a region was clicked."""
        assert "/vendor/leaflet/leaflet.js" in PAGE
        assert "/vendor/leaflet/leaflet.css" in PAGE
        assert '"/vendor/leaflet/leaflet.js"' in SERVER


class TestTheWindActuallyReachesThePage:
    """The page placed a region on the map and then said it carried no wind
    record, while the Noseboom product it was reading held every column. The
    bridge's navigation was built for the Mapview, which needs a position and
    nothing else, so the wind was dropped on the way through."""

    def _navigation(self):
        import numpy as np
        import pandas as pd

        from app.miro_rack_bridge import MiroRackBridge

        points = pd.DataFrame({
            "time": pd.date_range("2026-08-07 06:00", periods=120, freq="1s"),
            "lat": 50.9 + np.linspace(0, 0.02, 120),
            "lon": 6.6 + np.linspace(0, 0.02, 120),
            "altitude_m": np.full(120, 400.0),
            "wind_mps": np.full(120, 5.0),
            "wind_dir_deg": np.full(120, 225.0),
            "heading_deg": np.full(120, 90.0),
            "ground_speed_mps": np.full(120, 12.0),
        })
        return MiroRackBridge._prepare_navigation(points)

    @pytest.mark.parametrize("column", (
        "wind_mps", "wind_dir_deg", "heading_deg", "ground_speed_mps",
    ))
    def test_the_column_survives_preparation(self, column):
        assert column in self._navigation().columns

    def test_a_rose_can_be_drawn_from_what_survives(self):
        from app import source_investigation as engine

        navigation = self._navigation()
        miro = pd.DataFrame({
            "timestamp": pd.date_range("2026-08-07 06:00", periods=120, freq="1s"),
            "NO2 wet": np.linspace(1.0, 2.0, 120) / 1e9,
        })
        frame = engine.combined_frame(miro, navigation)
        region = engine.parse_region({
            "region_start": "2026-08-07T06:00:10",
            "region_end": "2026-08-07T06:01:50",
        })

        rose = engine.investigate_region(frame, region)["windrose"]

        assert rose is not None, "the region still reports no wind record"
        assert rose["samples"] > 0
        strongest = max(rose["petals"], key=lambda petal: petal["count"])
        assert strongest["label"] == "SW"

    def test_the_position_columns_are_still_required(self):
        """The Mapview depends on this function; carrying more must not make
        it accept a frame with no fix in it."""
        import pandas as pd

        from app.miro_rack_bridge import MiroRackBridge

        with pytest.raises(ValueError, match="latitude"):
            MiroRackBridge._prepare_navigation(
                pd.DataFrame({"time": pd.date_range("2026-08-07", periods=3)})
            )


class TestItSaysWhatItIsDoing:
    def test_the_panel_says_it_is_computing(self):
        """The bar at the top of the page is off screen once the rows are
        scrolled past, which is exactly when a region is chosen."""
        block = SCRIPT[SCRIPT.index("async function loadRegion("):]
        assert "Computing the wind rose" in block[:600]


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
