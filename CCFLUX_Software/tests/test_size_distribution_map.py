"""The OPC size distribution placed on the Noseboom flight track.

The OPC records concentration against its own clock and carries no position,
so each sample takes the position of the nearest Noseboom fix in time. A
sample with no fix close enough must be reported, not placed by guesswork.
"""
import base64
import re
import io
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from PIL import Image

from core.map_pdf_export import decode_map_png, render_map_pdf, safe_file_stem
from core.size_distribution_map import (
    build_map_payload,
    georeference_sensor,
    navigation_index,
    nearest_fix,
    parse_utc,
)

START = datetime(2026, 8, 3, 12, 0, tzinfo=timezone.utc)


def _fix(offset_seconds, lat=51.4, lon=6.9, altitude=300.0):
    return {
        "time": (START + timedelta(seconds=offset_seconds)).isoformat().replace("+00:00", "Z"),
        "lat": lat, "lon": lon, "altitude_m": altitude,
    }


def _heatmap(offsets, bins):
    """bins is a list of 24 rows, each with one value per time offset."""
    return {
        "time": [(START + timedelta(seconds=o)).isoformat().replace("+00:00", "Z") for o in offsets],
        "bin_index": list(range(len(bins))),
        "z": bins,
    }


class TestNavigationIndex:
    def test_fixes_are_returned_in_time_order(self):
        times, points = navigation_index([_fix(9), _fix(1), _fix(5)])
        assert times == sorted(times)
        assert len(points) == 3

    def test_a_receiver_that_has_not_locked_is_not_a_position(self):
        """Latitude and longitude of exactly zero is a real place, and never
        where the airship was."""
        times, _ = navigation_index([_fix(1, lat=0.0, lon=0.0), _fix(2)])
        assert len(times) == 1

    def test_a_fix_without_a_position_is_dropped(self):
        times, _ = navigation_index([{"time": _fix(1)["time"]}, _fix(2)])
        assert len(times) == 1

    def test_an_impossible_coordinate_is_dropped(self):
        times, _ = navigation_index([_fix(1, lat=95.0), _fix(2, lon=200.0), _fix(3)])
        assert len(times) == 1


class TestNearestFix:
    times = [START + timedelta(seconds=value) for value in (0, 10, 20)]

    def test_the_closer_of_the_two_neighbours_wins(self):
        index, delta = nearest_fix(self.times, START + timedelta(seconds=11), 5)
        assert index == 1 and delta == 1

    def test_a_sample_beyond_the_tolerance_is_refused(self):
        assert nearest_fix(self.times, START + timedelta(seconds=15), 2) is None

    def test_a_sample_before_the_first_fix_still_matches_if_close(self):
        index, delta = nearest_fix(self.times, START - timedelta(seconds=1), 2)
        assert index == 0 and delta == 1

    def test_no_navigation_means_no_match(self):
        assert nearest_fix([], START, 10) is None


class TestGeoreferenceSensor:
    def test_every_sample_with_a_fix_is_placed(self):
        times, points = navigation_index([_fix(o) for o in range(0, 30, 2)])
        placed = georeference_sensor(
            _heatmap([0, 4, 8], [[1.0, 2.0, 3.0], [0.5, 0.0, 1.5]]), times, points
        )
        assert placed["matched_count"] == 3
        assert placed["unmatched_count"] == 0
        assert placed["points"][0]["lat"] == 51.4

    def test_the_total_is_the_sum_over_the_bins(self):
        times, points = navigation_index([_fix(0)])
        placed = georeference_sensor(_heatmap([0], [[1.5], [2.5], [4.0]]), times, points)
        assert placed["points"][0]["total"] == 8.0
        assert placed["points"][0]["values"] == [1.5, 2.5, 4.0]

    def test_a_sample_far_from_any_fix_is_counted_not_placed(self):
        times, points = navigation_index([_fix(0)])
        placed = georeference_sensor(
            _heatmap([0, 600], [[1.0, 2.0]]), times, points, maximum_delta_seconds=2.0
        )
        assert placed["matched_count"] == 1
        assert placed["unmatched_count"] == 1

    def test_a_sample_with_no_readable_time_is_reported(self):
        times, points = navigation_index([_fix(0)])
        heatmap = _heatmap([0], [[1.0]])
        heatmap["time"] = ["not-a-time"]
        placed = georeference_sensor(heatmap, times, points)
        assert placed["undated_count"] == 1
        assert placed["matched_count"] == 0

    def test_a_bin_with_no_reading_stays_none_and_does_not_sum(self):
        times, points = navigation_index([_fix(0)])
        placed = georeference_sensor(_heatmap([0], [[None], [3.0]]), times, points)
        assert placed["points"][0]["values"] == [None, 3.0]
        assert placed["points"][0]["total"] == 3.0

    def test_a_long_flight_is_thinned_but_keeps_both_ends(self):
        offsets = list(range(0, 2000))
        times, points = navigation_index([_fix(o) for o in offsets])
        placed = georeference_sensor(
            _heatmap(offsets, [[1.0] * len(offsets)]), times, points, point_limit=100
        )
        assert 100 <= placed["matched_count"] <= 102
        assert placed["points"][0]["time"].startswith("2026-08-03T12:00:00")
        assert placed["sampled_from"] == 2000


class TestBuildMapPayload:
    def _payload(self, **kwargs):
        opc = {"sensors": {
            "hbx4": {"label": "HBX-4", "heatmap": _heatmap([0, 2], [[1.0, 2.0]])},
            "hbx5": {"label": "HBX-5", "heatmap": _heatmap([0, 2], [[0.1, 0.2]])},
        }}
        return build_map_payload(opc, [_fix(0), _fix(2)], **kwargs)

    def test_both_sensors_are_placed_independently(self):
        payload = self._payload(flight_id="Flight_CCT0803")
        assert payload["available"] is True
        assert payload["sensors"]["hbx4"]["matched_count"] == 2
        assert payload["sensors"]["hbx5"]["label"] == "HBX-5"
        assert payload["flight_id"] == "Flight_CCT0803"

    def test_the_flight_track_comes_from_the_navigation(self):
        assert len(self._payload()["flight_track"]) == 2

    def test_no_navigation_says_so_rather_than_showing_an_empty_map(self):
        payload = build_map_payload(
            {"sensors": {"hbx4": {"heatmap": _heatmap([0], [[1.0]])}}}, []
        )
        assert payload["available"] is False
        assert "no usable position fix" in payload["message"]

    def test_navigation_that_never_overlaps_says_so(self):
        payload = build_map_payload(
            {"sensors": {"hbx4": {"heatmap": _heatmap([0], [[1.0]])}}}, [_fix(9999)]
        )
        assert payload["available"] is False
        assert "within" in payload["message"]


def _png_data_url(width=1200, height=700, mode="RGBA"):
    image = Image.new(mode, (width, height), (255, 0, 0, 255) if mode == "RGBA" else (255, 0, 0))
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return "data:image/png;base64," + base64.b64encode(buffer.getvalue()).decode()


class TestMapPdfExport:
    def test_a_transparent_map_prints_on_white_paper(self):
        image = decode_map_png(_png_data_url())
        assert image.mode == "RGB"

    def test_a_pdf_is_produced_with_a_timestamped_name(self):
        filename, pdf = render_map_pdf(
            _png_data_url(), flight_name="Flight_CCT0803",
            map_name="OPC map", subject="test", filename_tag="OPC_Map",
        )
        assert filename.startswith("Flight_CCT0803_OPC_Map_")
        assert filename.endswith(".pdf")
        assert pdf[:5] == b"%PDF-"

    @pytest.mark.parametrize("bad", [
        "", "not-a-data-url", "data:image/jpeg;base64,AAAA",
        "data:image/png;base64,####",
    ])
    def test_anything_that_is_not_a_png_is_refused(self, bad):
        with pytest.raises(ValueError):
            decode_map_png(bad)

    def test_an_image_too_small_to_read_is_refused(self):
        with pytest.raises(ValueError, match="dimensions"):
            decode_map_png(_png_data_url(100, 80))

    def test_a_flight_name_cannot_escape_the_export_folder(self):
        stem = safe_file_stem("../../etc/passwd")
        assert stem == "etc_passwd"
        assert safe_file_stem("Flight/2026") == "Flight_2026"
        assert safe_file_stem("") == "Flight"


class TestParseUtc:
    def test_a_naive_timestamp_is_read_as_utc(self):
        assert parse_utc("2026-08-03T12:00:00").tzinfo is timezone.utc

    def test_a_zulu_timestamp_is_understood(self):
        assert parse_utc("2026-08-03T12:00:00Z") == START

    def test_nonsense_is_none_rather_than_an_exception(self):
        assert parse_utc("later") is None
        assert parse_utc(None) is None


class TestMapWorkspaceControls:
    """Everything asked for is present on the page and wired to a redraw."""

    from pathlib import Path as _Path

    ASSETS = _Path(__file__).resolve().parents[1] / "app" / "assets"
    markup = (ASSETS / "opc.html").read_text(encoding="utf-8")
    script = (ASSETS / "opc.js").read_text(encoding="utf-8")
    shared = (ASSETS / "size_map.js").read_text(encoding="utf-8")

    def test_the_workspace_offers_a_map_tab(self):
        assert 'data-view="map" href="/opc/map"' in self.markup
        assert 'data-section="map"' in self.markup

    @pytest.mark.parametrize("control", [
        "mapSensor", "mapChannel", "mapPalette", "mapLog",
        "mapNewTabBtn", "mapFullscreenBtn", "mapPdfBtn", "mapResetBtn",
    ])
    def test_each_control_exists(self, control):
        assert f'id="{control}"' in self.markup

    def test_the_colour_bar_is_vertical(self):
        """The ramp runs upwards and the labels are stacked down its side."""
        assert "linear-gradient(to top," in self.shared
        assert "tick.style.top = " in self.shared

    def test_changing_a_control_redraws(self):
        assert "['mapChannel','mapPalette','mapLog']" in self.script

    def test_both_sensors_can_be_shown(self):
        assert '<option value="hbx4">' in self.markup
        assert '<option value="hbx5">' in self.markup

    def test_the_map_is_laid_out_only_once_its_card_is_visible(self):
        """Leaflet measures its container; a map built while hidden is grey."""
        assert "if(view==='map')setTimeout(()=>sizeMap.show(),60)" in self.script
        assert "invalidateSize()" in self.shared

    def test_a_new_tab_carries_the_current_selection(self):
        assert "function permalink()" in self.shared
        assert "applyPermalink()" in self.shared

    def test_the_export_posts_the_visible_map(self):
        assert "'/api/opc/map/export'" in self.script
        assert "composeImage()" in self.shared


class TestPartectorWorkspace:
    """The Partector carries the same map and the same colour-scale fix."""

    from pathlib import Path as _Path

    ASSETS = _Path(__file__).resolve().parents[1] / "app" / "assets"
    markup = (ASSETS / "partector.html").read_text(encoding="utf-8")
    script = (ASSETS / "partector.js").read_text(encoding="utf-8")

    def test_the_workspace_offers_a_map_tab(self):
        assert 'data-view="map" href="/partector/map"' in self.markup
        assert 'id="partectorMap"' in self.markup

    def test_the_heatmap_range_comes_from_the_data(self):
        assert "zauto:false" in self.script
        assert "heatBounds(heat.z)" in self.script

    def test_the_heatmap_opens_by_decade(self):
        assert re.search(r'id="heatLog"[^>]*\bchecked\b', self.markup)
        assert "$('heatLog').onchange=renderHeat" in self.script

    def test_a_channel_that_counted_nothing_is_not_coloured_as_low(self):
        assert "number>0?Math.log10" in self.script

    def test_the_title_states_the_scale_and_the_true_peak(self):
        assert "logarithmic colour scale" in self.script
        assert "peak ${format(bounds.max)}" in self.script

    def test_the_map_uses_the_shared_module(self):
        assert "window.CCFLUX.createSizeMap" in self.script
        assert "'/api/partector/map'" in self.script


class TestChannelLabels:
    def test_the_partector_names_its_channels_by_diameter(self):
        from core.size_distribution_map import channel_labels

        labels = channel_labels({"diameter_nm": [10.0, 16.26, 300.0]}, 3)
        assert [item["label"] for item in labels] == ["10 nm", "16.26 nm", "300 nm"]

    def test_the_opc_names_its_channels_by_bin(self):
        from core.size_distribution_map import channel_labels

        labels = channel_labels({"bin_index": [0, 1, 2]}, 3)
        assert [item["label"] for item in labels] == ["Bin 0", "Bin 1", "Bin 2"]

    def test_a_heatmap_naming_neither_still_gets_one_label_per_row(self):
        from core.size_distribution_map import channel_labels

        assert len(channel_labels({}, 4)) == 4


class TestInstrumentSensors:
    def test_a_two_sensor_payload_keeps_both(self):
        from core.size_distribution_map import instrument_sensors

        assert set(instrument_sensors({"sensors": {"hbx4": {}, "hbx5": {}}})) == {"hbx4", "hbx5"}

    def test_a_single_instrument_payload_becomes_one_sensor(self):
        from core.size_distribution_map import instrument_sensors

        found = instrument_sensors({"instrument_id": "partector", "heatmap": {"z": []}})
        assert set(found) == {"partector"}

    def test_a_payload_with_no_heatmap_has_no_sensor(self):
        from core.size_distribution_map import instrument_sensors

        assert instrument_sensors({}) == {}


class TestMapLayout:
    """Full screen must mean the whole display, and the legend must not
    print its title over its own topmost tick."""

    from pathlib import Path as _Path

    ASSETS = _Path(__file__).resolve().parents[1] / "app" / "assets"
    PAGES = ("opc.html", "partector.html")

    @pytest.mark.parametrize("page", PAGES)
    def test_the_card_becomes_the_viewport(self, page):
        markup = (self.ASSETS / page).read_text(encoding="utf-8")
        assert ".chart-card:fullscreen{height:100vh" in markup

    @pytest.mark.parametrize("page", PAGES)
    def test_the_map_takes_the_rows_the_toolbar_leaves(self, page):
        """The map was clamped to 860px, leaving the rest of the screen black."""
        markup = (self.ASSETS / page).read_text(encoding="utf-8")
        assert ".chart-card:fullscreen .map-shell{flex:1 1 auto;min-height:0}" in markup
        assert ".chart-card:fullscreen .size-map{height:100%" in markup

    @pytest.mark.parametrize("page", PAGES)
    def test_the_legend_cannot_outgrow_a_short_window(self, page):
        markup = (self.ASSETS / page).read_text(encoding="utf-8")
        assert "max-height:calc(100% - 48px);overflow:auto" in markup

    def test_ticks_are_positioned_from_the_top_like_every_other_legend(self):
        """`bottom:100%` put the whole label above the bar and over the title;
        `top` with translateY(-50%) centres it on its value."""
        shared = (self.ASSETS / "size_map.js").read_text(encoding="utf-8")
        assert "tick.style.top = `${100 - fraction * 100}%`" in shared
        assert "tick.style.bottom" not in shared
        for page in (*self.PAGES, "flir.html", "sif.html"):
            markup = (self.ASSETS / page).read_text(encoding="utf-8")
            assert "transform:translateY(-50%)" in markup

    @pytest.mark.parametrize("page", PAGES)
    def test_the_toolbar_carries_update_and_reset(self, page):
        markup = (self.ASSETS / page).read_text(encoding="utf-8")
        assert 'id="mapUpdateBtn"' in markup
        assert 'id="mapResetTopBtn"' in markup
        assert 'id="mapResetBtn"' in markup

    @pytest.mark.parametrize("script", ("opc.js", "partector.js"))
    def test_both_reset_buttons_do_the_same_thing(self, script):
        source = (self.ASSETS / script).read_text(encoding="utf-8")
        assert "['mapResetBtn','mapResetTopBtn']" in source
        assert "$('mapUpdateBtn').onclick=()=>sizeMap.show()" in source


def test_map_tiles_are_requested_with_cross_origin():
    """Without it the tiles taint the canvas and toDataURL refuses to read the
    map back: "Tainted canvases may not be exported." Every other map in the
    software already sets it."""
    assets = Path(__file__).resolve().parents[1] / "app" / "assets"
    for name in ("size_map.js", "flir.js", "miro_rack_map.js"):
        source = (assets / name).read_text(encoding="utf-8")
        assert "crossOrigin" in source, name
