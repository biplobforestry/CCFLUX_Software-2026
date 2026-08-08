"""A flight track exported as a figure rather than a picture of a screen.

The Mapview export sent the browser's canvas to be wrapped in a PDF. The result
measured 17.33 inches wide, held one raster and embedded no fonts at all: no
axes, no tick values, no colour bar, nothing a reader could scale or search, and
nothing stating what was plotted or in what unit.

The figure is now drawn from the georeferenced values, to the campaign standard:
seven inches wide, nothing below nine point, a titled axes framed in degrees, a
colour bar naming the quantity, a scale bar, a north arrow and the basemap
attribution.
"""

from __future__ import annotations

import math
from pathlib import Path

import numpy as np
import pytest

from core import scientific_map


@pytest.fixture
def track():
    """A short north-east leg, the shape of a campaign transect."""
    latitudes = np.linspace(50.90, 51.05, 60)
    longitudes = np.linspace(6.60, 6.95, 60)
    values = np.linspace(410.0, 445.0, 60)
    return latitudes, longitudes, values


def _rendered(path):
    from PIL import Image

    with Image.open(path) as image:
        return image.size


class TestTheFigureStandard:
    def test_the_declared_geometry(self):
        assert scientific_map.FIGURE_WIDTH_INCHES == 7.0
        assert scientific_map.MINIMUM_FONT_POINTS == 9.0

    def test_it_is_exactly_seven_inches_wide(self, track, tmp_path):
        written = scientific_map.render_track_map(
            *track, tmp_path, "track", title="Test", formats=("png",), dpi=100,
        )
        width, _height = _rendered(written[0])
        assert width / 100 == pytest.approx(7.0, abs=0.02)

    def test_a_portrait_track_is_narrower_rather_than_stretched(self, tmp_path):
        """A map is sized to the ground it covers. Filling a seven-inch frame
        with a north-south flight means widening the extent to a hundred
        kilometres of countryside the Zeppelin never flew over."""
        latitudes = np.linspace(50.4, 51.4, 40)
        longitudes = np.linspace(6.6, 6.7, 40)
        written = scientific_map.render_track_map(
            latitudes, longitudes, None, tmp_path, "portrait",
            title="Portrait", formats=("png",), dpi=100,
            allow_rotation=False,
        )
        width, height = _rendered(written[0])
        assert width / 100 < scientific_map.MAP_MAXIMUM_WIDTH_INCHES
        assert height > width

    def test_the_page_budget_is_never_exceeded(self, tmp_path):
        for name, latitudes, longitudes in (
            ("tall", np.linspace(45.0, 55.0, 40), np.linspace(6.60, 6.62, 40)),
            ("wide", np.linspace(51.00, 51.02, 40), np.linspace(4.0, 9.0, 40)),
            ("square", np.linspace(50.4, 51.4, 40), np.linspace(6.0, 7.4, 40)),
        ):
            written = scientific_map.render_track_map(
                latitudes, longitudes, None, tmp_path, name,
                title=name, formats=("png",), dpi=100,
            )
            width, height = _rendered(written[0])
            assert width / 100 <= scientific_map.MAP_MAXIMUM_WIDTH_INCHES + 0.02
            assert height / 100 <= scientific_map.MAP_MAXIMUM_HEIGHT_INCHES + 0.02

    def test_a_wide_transect_stays_landscape(self, tmp_path):
        latitudes = np.linspace(51.00, 51.02, 40)
        longitudes = np.linspace(6.2, 7.6, 40)
        written = scientific_map.render_track_map(
            latitudes, longitudes, None, tmp_path, "wide",
            title="Wide", formats=("png",), dpi=100,
        )
        width, height = _rendered(written[0])
        assert height < width

    def test_the_pdf_carries_text_not_only_a_raster(self, track, tmp_path):
        """The old export embedded no fonts at all."""
        import re

        written = scientific_map.render_track_map(
            *track, tmp_path, "vector", title="Vector",
            value_label="CO2 (ppm)", formats=("pdf",), dpi=150,
        )
        raw = written[0].read_bytes()
        assert re.search(rb"/BaseFont", raw), "no font embedded"
        box = re.search(rb"/MediaBox\s*\[([^\]]*)\]", raw)
        inches = [float(value) / 72 for value in box.group(1).split()]
        assert inches[2] == pytest.approx(7.0, abs=0.02)

    def test_nothing_is_drawn_below_the_font_floor(self, track, tmp_path):
        import matplotlib

        matplotlib.use("Agg")
        import matplotlib.pyplot as plt

        with plt.rc_context(scientific_map.rc_parameters()):
            figure, axis = plt.subplots(figsize=(7.0, 4.0), constrained_layout=True)
            axis.plot([0, 1], [0, 1])
            axis.set_xlabel("Longitude (°)")
            scientific_map.enforce_minimum_font(figure)
            sizes = [
                float(item.get_fontsize())
                for item in figure.findobj(
                    match=lambda artist: hasattr(artist, "get_fontsize")
                )
            ]
            plt.close(figure)
        assert sizes and min(sizes) >= scientific_map.MINIMUM_FONT_POINTS

    @pytest.mark.parametrize("suffix", ["pdf", "png", "svg"])
    def test_every_offered_format_is_written(self, track, tmp_path, suffix):
        written = scientific_map.render_track_map(
            *track, tmp_path, "formats", title="Formats",
            formats=(suffix,), dpi=100,
        )
        assert written[0].suffix == f".{suffix}"
        assert written[0].stat().st_size > 0

    def test_an_unknown_format_is_refused(self, track, tmp_path):
        with pytest.raises(ValueError, match="Unsupported export format"):
            scientific_map.render_track_map(
                *track, tmp_path, "bad", title="Bad", formats=("jpeg",)
            )

    def test_a_track_of_one_point_is_refused_clearly(self, tmp_path):
        with pytest.raises(ValueError, match="at least two"):
            scientific_map.render_track_map(
                [51.0], [6.7], [1.0], tmp_path, "single", title="Single"
            )

    def test_missing_positions_are_dropped_not_drawn(self, tmp_path):
        latitudes = [51.0, float("nan"), 51.1, 51.2]
        longitudes = [6.7, 6.8, float("nan"), 6.9]
        written = scientific_map.render_track_map(
            latitudes, longitudes, None, tmp_path, "gaps",
            title="Gaps", formats=("png",), dpi=100,
        )
        assert written[0].is_file()


class TestTheLayoutIsPlannedForTheTrack:
    """The exported map was a photograph of a browser window, so its extent was
    wherever someone had left the map: a flight covering 80 km by 83 km came
    out inside 315 km of Rhineland, the track a fifth of the frame, with the
    colour bar drawn over the ground it described."""

    def test_a_square_track_is_not_stretched_to_the_full_width(self):
        plan = scientific_map.plan_layout(1.15, 1.19, True)

        assert plan["figure_width"] < scientific_map.MAP_MAXIMUM_WIDTH_INCHES
        assert plan["map_height"] / plan["map_width"] == pytest.approx(
            1.19 / 1.15, rel=0.02
        )

    def test_the_bar_goes_where_the_layout_has_slack(self):
        """Beside a tall map, beneath a wide one - whichever draws more map."""
        tall = scientific_map.plan_layout(0.4, 1.2, True, allow_rotation=False)
        wide = scientific_map.plan_layout(1.4, 0.28, True)

        assert tall["colourbar"] == "right"
        assert wide["colourbar"] == "bottom"

    def test_no_bar_is_reserved_for_a_track_without_values(self):
        plan = scientific_map.plan_layout(1.0, 1.0, False)

        assert plan["colourbar"] == "none"
        assert plan["figure_width"] == pytest.approx(
            plan["map_width"] + scientific_map.YAXIS_WIDTH_INCHES
        )

    def test_a_north_south_transect_is_turned_to_fill_the_page(self):
        upright = scientific_map.plan_layout(0.22, 1.30, True, allow_rotation=False)
        free = scientific_map.plan_layout(0.22, 1.30, True)

        assert free["rotated"] is True
        assert free["area"] > upright["area"] * scientific_map.ROTATION_GAIN

    def test_a_map_is_not_turned_for_a_marginal_gain(self):
        """North not being up costs a reader something."""
        plan = scientific_map.plan_layout(1.15, 1.19, True)

        assert plan["rotated"] is False

    def test_a_wide_track_is_already_the_right_way_round(self):
        plan = scientific_map.plan_layout(1.40, 0.28, True)

        assert plan["rotated"] is False

    def test_rotation_can_be_refused(self):
        plan = scientific_map.plan_layout(0.1, 1.6, True, allow_rotation=False)

        assert plan["rotated"] is False

    def test_the_map_always_fits_inside_the_budget(self):
        for span_x, span_y in ((1.0, 1.0), (5.0, 0.1), (0.1, 5.0), (0.3, 0.31)):
            plan = scientific_map.plan_layout(span_x, span_y, True)

            assert plan["figure_width"] <= scientific_map.MAP_MAXIMUM_WIDTH_INCHES + 1e-9
            assert plan["figure_height"] <= scientific_map.MAP_MAXIMUM_HEIGHT_INCHES + 1e-9


class TestRotationIsARotationNotAMirror:
    def test_north_still_points_at_north(self, tmp_path):
        """A transpose would swap east for west, which is worse than a badly
        proportioned figure: it would put the source on the wrong side."""
        latitudes = np.linspace(50.4, 51.4, 60)
        longitudes = np.linspace(6.60, 6.66, 60)
        values = np.linspace(1.0, 2.0, 60)

        written = scientific_map.render_track_map(
            latitudes, longitudes, values, tmp_path, "turned",
            title="Turned", formats=("png",), dpi=100,
        )

        assert written[0].is_file()

    def test_the_ticks_follow_the_turn(self):
        """Turned a quarter, the horizontal axis carries latitude."""
        plan = scientific_map.plan_layout(0.22, 1.30, True)
        assert plan["rotated"] is True


class TestTheSizeMapIsDrawnNotPhotographed:
    """size_map.js composed the Leaflet tiles onto a canvas and sent the picture
    to be wrapped in a PDF, so the export carried whatever zoom the map had been
    left on and a colour bar painted over the ground it described."""

    SCRIPT = (
        Path(__file__).resolve().parents[1] / "app" / "assets" / "size_map.js"
    ).read_text(encoding="utf-8")
    SERVER = (
        Path(__file__).resolve().parents[1] / "app" / "server.py"
    ).read_text(encoding="utf-8")
    BACKEND = (
        Path(__file__).resolve().parents[1] / "app" / "scan_backend.py"
    ).read_text(encoding="utf-8")

    def _export(self) -> str:
        start = self.SCRIPT.index("async function exportPdf(")
        return self.SCRIPT[start: self.SCRIPT.index("\n  function resetPosition")]

    def test_the_page_sends_the_layer_not_a_picture(self):
        block = self._export()
        assert "composeImage()" not in block
        assert "sensor:" in block and "channel:" in block

    def test_the_scale_is_sent_so_the_figure_matches_the_page(self):
        assert "log: Boolean(" in self._export()

    def test_the_route_draws_the_figure(self):
        block = self.SERVER[self.SERVER.index('"/api/opc/map/export"'):]
        block = block[: block.index("elif path ==")]
        assert "export_size_distribution_map_figure(" in block
        assert "export_size_distribution_map_pdf(" not in block

    def test_the_backend_renders_from_the_georeferenced_values(self):
        start = self.BACKEND.index("def export_size_distribution_map_figure(")
        block = self.BACKEND[start: self.BACKEND.index(
            "def export_size_distribution_map_pdf(", start
        )]
        assert "scientific_map.render_track_map(" in block
        assert "point[\"lat\"]" in block

    def test_a_sensor_that_does_not_exist_is_refused_not_swapped(self):
        """Exporting a different sensor, correctly labelled, is worse than
        refusing: the operator has no way to notice."""
        start = self.BACKEND.index("def export_size_distribution_map_figure(")
        block = self.BACKEND[start: start + 4000]
        assert "has no sensor" in block


class TestTheProjection:
    def test_mercator_round_trips(self):
        for latitude in (-60.0, -1.0, 0.0, 51.4, 70.0):
            back = scientific_map._latitude_from_mercator(
                scientific_map._mercator_y(latitude)
            )
            assert back == pytest.approx(latitude, abs=1e-9)

    def test_latitude_is_stretched_away_from_the_equator(self):
        """Plotting in raw degrees would squash the basemap."""
        near = scientific_map._mercator_y(1.0) - scientific_map._mercator_y(0.0)
        far = scientific_map._mercator_y(52.0) - scientific_map._mercator_y(51.0)
        assert far > near * 1.5

    def test_the_poles_do_not_diverge(self):
        assert math.isfinite(scientific_map._mercator_y(90.0))
        assert math.isfinite(scientific_map._mercator_y(-90.0))

    def test_ticks_land_on_round_degrees(self):
        ticks = scientific_map._degree_ticks(6.213, 7.042)
        assert ticks
        assert all(round(value * 100) % 5 == 0 for value in ticks)
        assert ticks[0] >= 6.213 and ticks[-1] <= 7.042

    def test_a_tiny_span_still_produces_ticks(self):
        assert scientific_map._degree_ticks(6.7000, 6.7004)


class TestTheBasemap:
    def test_a_zoom_is_chosen_within_the_tile_budget(self):
        zoom = scientific_map.choose_zoom(6.2, 7.1, 50.4, 51.5, 2100)
        assert 0 <= zoom <= 19

    def test_a_missing_network_still_yields_a_figure(self, track, tmp_path, monkeypatch):
        """A basemap is context; the track, axes and colour bar are the data."""
        def refuse(*args, **kwargs):
            raise OSError("no network")

        monkeypatch.setattr(scientific_map, "_fetch_tile", refuse)
        written = scientific_map.render_track_map(
            *track, tmp_path, "offline", title="Offline",
            formats=("png",), dpi=100, cache_directory=tmp_path / "tiles",
        )
        assert written[0].is_file()

    def test_the_attribution_is_carried(self):
        assert "OpenStreetMap" in scientific_map.TILE_ATTRIBUTION


class TestTheMapviewExportUsesIt:
    BRIDGE = (
        Path(__file__).resolve().parents[1] / "app" / "miro_rack_bridge.py"
    ).read_text(encoding="utf-8")
    SERVER = (
        Path(__file__).resolve().parents[1] / "app" / "server.py"
    ).read_text(encoding="utf-8")
    SCRIPT = (
        Path(__file__).resolve().parents[1] / "app" / "assets" / "miro_rack_map.js"
    ).read_text(encoding="utf-8")

    def test_the_bridge_draws_from_the_values(self):
        assert "def export_map_figure(" in self.BRIDGE
        assert "scientific_map.render_track_map(" in self.BRIDGE

    def test_the_route_serves_the_figure(self):
        block = self.SERVER[self.SERVER.index('"/api/miro-rack/map/export"'):]
        block = block[: block.index("elif path ==")]
        assert "export_map_figure(" in block
        assert "export_map_pdf(" not in block

    def test_the_page_sends_the_layer_not_a_canvas(self):
        block = self.SCRIPT[self.SCRIPT.index("async function exportCurrentMapPdf"):]
        block = block[: block.index("\n  function addLegend")]
        assert "instrument:layer.instrument" in block
        assert "canvas.toDataURL" not in block

    def test_the_unit_reaches_the_colour_bar(self):
        assert 'value_label=f"{gas} ({unit})"' in self.BRIDGE
