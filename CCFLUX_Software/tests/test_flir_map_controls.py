"""The FLIR map gets the treatment the SIF map has: a readable bar and controls.

The legend was a 12 px horizontal strip with the range printed as two unspaced
numbers, and the colour scale was fixed. The bar is now vertical with labelled
ticks, the scale is chosen, and the map can be opened on its own or filled to
the screen.
"""

import re
from pathlib import Path

import pytest

ASSETS = Path(__file__).resolve().parents[1] / "app" / "assets"
HTML = (ASSETS / "flir.html").read_text(encoding="utf-8")
SCRIPT = (ASSETS / "flir.js").read_text(encoding="utf-8")

REQUESTED = ("YlOrRd", "OrRd", "PuRd", "bwr")


@pytest.mark.parametrize("name", REQUESTED)
def test_the_requested_scale_is_offered(name):
    block = SCRIPT[SCRIPT.index("const PALETTES={"):]
    block = block[: block.index("\n  }")]

    assert f"{name}:" in block


def test_thermal_stays_the_default():
    """Perceptually ordered, which is what a quantitative temperature map needs;
    the sequential ramps are for matching an existing figure."""
    assert "const DEFAULT_PALETTE='Thermal'" in SCRIPT
    assert SCRIPT.index("Thermal:") < SCRIPT.index("YlOrRd:")


def test_the_colour_bar_is_vertical_and_labelled():
    assert 'id="legendRamp"' in HTML
    assert 'id="legendTicks"' in HTML
    assert "LEGEND_TICKS=6" in SCRIPT
    assert "to top" in SCRIPT, "the largest value belongs at the top"
    assert ".map-legend .colorbar{display:flex" in HTML


def test_the_old_horizontal_strip_is_gone():
    assert ".map-gradient{height:12px" not in HTML
    assert 'id="legendLow"' not in HTML and 'id="legendHigh"' not in HTML
    assert "legendLow" not in SCRIPT and "legendHigh" not in SCRIPT


def test_markers_and_legend_use_the_same_scale():
    """A map coloured by one scale and read against another misleads."""
    assert "color(Number(point[metric]),low,high,palette)" in SCRIPT
    assert "renderLegend(metricLabels[metric],low,high,palette" in SCRIPT


def test_tick_precision_comes_from_the_range():
    body = SCRIPT[SCRIPT.index("function tickFormatter("):]
    body = body[: body.index("\n  }")]

    assert "Math.abs(high-low)" in body
    assert "toFixed(decimals)" in body


def test_the_map_can_be_opened_on_its_own():
    assert 'id="mapNewTabBtn"' in HTML
    opener = SCRIPT[SCRIPT.index("mapNewTabBtn"):]
    opener = opener[: opener.index("};")]

    assert "metric:" in opener and "palette:" in opener
    assert "'_blank'" in opener


def test_the_new_tab_arrives_on_what_it_was_opened_for():
    assert "new URLSearchParams(location.search).get('metric')" in SCRIPT
    assert "new URLSearchParams(location.search).get('palette')" in SCRIPT


def test_full_screen_fills_the_display():
    assert 'id="mapFullscreenBtn"' in HTML
    assert ":fullscreen .thermal-map{flex:1;min-height:0;height:auto!important}" in HTML
    assert "fullscreenchange" in SCRIPT
    assert "invalidateSize" in SCRIPT


def test_every_control_exists_before_the_script_runs():
    """The defect that hung this page: an element declared below its script."""
    script = HTML.index('<script src="/flir.js">')
    for name in ("mapPalette", "mapNewTabBtn", "mapFullscreenBtn",
                 "legendRamp", "legendTicks", "legendNote"):
        at = HTML.find(f'id="{name}"')
        assert at != -1, f"{name} is not on the page"
        assert at < script, f"{name} is declared after the script"
