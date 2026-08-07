"""The trace-gas map offers a logarithmic colour scale per layer.

A gas whose values span orders of magnitude is unreadable on a linear ramp:
the whole track takes one colour and the structure disappears. Each layer can
be switched to a logarithmic scale, and back, without touching the data.
"""
import re
import shutil
import subprocess
from pathlib import Path

import pytest

ASSETS = Path(__file__).resolve().parents[1] / "app" / "assets"
MARKUP = (ASSETS / "miro_rack_map.html").read_text(encoding="utf-8")
SCRIPT = (ASSETS / "miro_rack_map.js").read_text(encoding="utf-8")


def test_every_layer_offers_the_log_option_unchecked():
    controls = re.findall(r'<input class="logscale" type="checkbox"([^>]*)>', MARKUP)
    assert len(controls) == len(re.findall(r'class="selection"', MARKUP)) == 4
    # Unchecked, so the default view stays linear until the operator asks.
    assert not any("checked" in control for control in controls)


def test_the_option_is_read_drawn_and_redrawn():
    assert "logScale:selection.querySelector('.logscale').checked" in SCRIPT
    assert "selection.querySelector('.logscale').addEventListener" in SCRIPT
    # Both the live map and the exported image colour through the same flag.
    assert SCRIPT.count("style.palette,style.logScale") == 2


def test_layers_scaled_differently_do_not_share_one_legend():
    key = re.search(r"const groupKey = item =>\s*(.+?);", SCRIPT, re.S).group(1)
    assert "logScale" in key


def _evaluate(expression):
    prelude = re.search(
        r"const logUsable = .+?\n  \}\n", SCRIPT, re.S
    ).group(0).replace("  ", "")
    # osascript prints the value of the last expression; console.log goes to stderr.
    source = f"{prelude}\nString({expression});"
    script = Path(__file__).with_name("_logscale_tmp.js")
    script.write_text(source, encoding="utf-8")
    try:
        out = subprocess.run(
            ["osascript", "-l", "JavaScript", str(script)],
            capture_output=True, text=True, timeout=60,
        )
    finally:
        script.unlink(missing_ok=True)
    assert out.returncode == 0, out.stderr
    return float(out.stdout.strip())


# shutil.which, not a "which" subprocess: that command does not exist on
# Windows, so the guard raised FileNotFoundError at import and one unavailable
# macOS tool stopped the whole suite from being collected on this platform.
@pytest.mark.skipif(
    shutil.which("osascript") is None,
    reason="JavaScriptCore is not available",
)
class TestScalePosition:
    def test_the_midpoint_of_a_log_ramp_is_the_geometric_mean(self):
        assert _evaluate("scalePosition(31.6227766, 1, 1000, true)") == pytest.approx(0.5, abs=1e-6)

    def test_a_linear_ramp_is_unchanged(self):
        assert _evaluate("scalePosition(500, 0, 1000, false)") == pytest.approx(0.5)

    def test_the_ends_map_to_the_ends(self):
        assert _evaluate("scalePosition(1, 1, 1000, true)") == 0.0
        assert _evaluate("scalePosition(1000, 1, 1000, true)") == 1.0

    def test_a_range_reaching_zero_falls_back_to_linear(self):
        """A logarithm of zero is undefined; the legend says the scale is linear."""
        assert _evaluate("scalePosition(500, 0, 1000, true)") == pytest.approx(0.5)

    def test_a_negative_range_falls_back_to_linear(self):
        assert _evaluate("scalePosition(0, -5, 5, true)") == pytest.approx(0.5)


def test_the_legend_names_the_mapping_and_the_fallback():
    assert "logarithmic needs a positive range" in SCRIPT
    assert "Math.exp((Math.log(low)+Math.log(high))/2)" in SCRIPT
