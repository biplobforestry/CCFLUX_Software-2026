"""Plot layout defects reported from Flight_CCT0803.

Three separate faults, all visible on screen:
  * the OPC bin-resolved heatmap coloured linearly from the lowest to the
    highest value, so the smallest bin took the whole ramp and every coarse
    bin read as empty;
  * the INS Welch spectra labelled every minor tick of a decade axis in
    exponent notation, nine crowded labels per decade down each side;
  * the centred plot title and the horizontal legend were drawn at the same
    height in the top margin and printed over each other.
"""
import re
from pathlib import Path

import pytest

ASSETS = Path(__file__).resolve().parents[1] / "app" / "assets"
SHARED_LAYOUT = ("ins_gimbal.js", "partector.js", "opc.js")


def read(name):
    return (ASSETS / name).read_text(encoding="utf-8")


@pytest.mark.parametrize("name", SHARED_LAYOUT)
class TestTitleAndLegendDoNotCollide:
    def test_the_title_is_anchored_to_the_top_of_the_figure(self, name):
        assert "y:1,yanchor:'top'" in read(name)

    def test_the_legend_sits_above_the_plot_not_in_the_title(self, name):
        # The placement is what this checks. A legend may also declare its
        # colours, so the whole literal is not the assertion.
        assert "legend:{orientation:'h',y:1.02,yanchor:'bottom',x:0" in read(name)
        assert "y:1.13" not in read(name)

    def test_the_top_margin_holds_both(self, name):
        tops = [int(value) for value in re.findall(r"margin:\{[^}]*t:(\d+)", read(name))]
        assert tops, f"{name} declares no top margin"
        assert min(tops) >= 90, f"{name} has a top margin of {min(tops)}px"


class TestWelchSpectraAxes:
    source = read("ins_gimbal.js")

    def test_decade_axes_carry_one_label_per_decade(self):
        assert "dtick:1,exponentformat:'power'" in self.source

    def test_the_crowded_exponent_labels_are_gone(self):
        assert "tickformat:'.3e'" not in self.source

    def test_each_axis_is_coloured_like_its_trace(self):
        assert "decadeAxis(colours.x)" in self.source
        assert "decadeAxis(colours.y)" in self.source


class TestHeatmapColourRange:
    source = read("opc.js")
    markup = read("opc.html")

    def test_the_range_comes_from_the_data(self):
        assert "zauto:false" in self.source
        assert "heatBounds(h.z)" in self.source

    def test_the_operator_can_switch_the_scale(self):
        assert 'id="heatLog"' in self.markup
        assert "$('heatLog').onchange=renderHeat" in self.source

    def test_a_size_distribution_opens_logarithmic(self):
        """Bin 0 outweighs the coarse bins by two decades on a real flight."""
        assert re.search(r'id="heatLog"[^>]*\bchecked\b', self.markup)

    def test_a_bin_that_counted_nothing_is_not_coloured_as_low(self):
        assert "number>0 ? Math.log10" in self.source
        assert ": null;" in self.source

    def test_the_title_states_the_scale_and_the_true_peak(self):
        assert "logarithmic colour scale" in self.source
        assert "peak ${format(bounds.max)}" in self.source


def _evaluate(expression):
    """Run heatBounds against JavaScriptCore, on the real function source."""
    import subprocess

    source = read("opc.js")
    body = re.search(r"  function heatBounds\(z\)\{.*?\n  \}\n", source, re.S).group(0)
    script = Path(__file__).with_name("_heatbounds_tmp.js")
    script.write_text(f"{body}\nJSON.stringify({expression});", encoding="utf-8")
    try:
        out = subprocess.run(["osascript", "-l", "JavaScript", str(script)],
                             capture_output=True, text=True, timeout=60)
    finally:
        script.unlink(missing_ok=True)
    assert out.returncode == 0, out.stderr
    import json

    return json.loads(out.stdout.strip())


class TestHeatBounds:
    def test_spikes_do_not_flatten_the_linear_range(self):
        """One cell 100x the rest must not push everything else to the floor."""
        rows = "[[" + ",".join(["1"] * 199) + ",100]]"
        bounds = _evaluate(f"heatBounds({rows})")
        assert bounds["max"] == 100
        assert bounds["high"] == 1

    def test_the_logarithmic_floor_ignores_the_lowest_hundredth(self):
        rows = "[[0.001," + ",".join(["1"] * 199) + "]]"
        bounds = _evaluate(f"heatBounds({rows})")
        assert bounds["positiveLow"] == 1

    def test_an_empty_grid_does_not_divide_by_zero(self):
        assert _evaluate("heatBounds([])")["high"] == 1
        assert _evaluate("heatBounds([[0,0,0]])")["high"] == 1

    def test_zeros_do_not_become_the_logarithmic_floor(self):
        bounds = _evaluate("heatBounds([[0,0,0,2,4,8]])")
        assert bounds["low"] == 0
        assert bounds["positiveLow"] > 0
