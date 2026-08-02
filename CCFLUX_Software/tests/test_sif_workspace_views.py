"""The SIF workspace shows what was asked for, and the frequency curve is true.

Three changes, each with a way to get quietly wrong:

The altitude-relationship plot was removed, and the distribution widened to
match the plot above it. A card left referring to a plot that no longer exists
would throw on every render.

The spectra panel was removed. A bookmark or history entry pointing at
/sif/spectra would then match no card and leave the page blank.

The map offers vegetation indices instead of every retrieved column - but a
custom index list would match none of the known names, and an empty map with no
explanation is worse than an unfiltered one.

The frequency curve is a frequency polygon: the same counts as the bars, joined
at the bin centres. It is checked by running it, because a curve that looks
plausible and counts wrongly is exactly the failure that matters.
"""

import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
ASSETS = ROOT / "app" / "assets"
HTML = (ASSETS / "sif.html").read_text(encoding="utf-8")
SCRIPT = (ASSETS / "sif.js").read_text(encoding="utf-8")


# ----------------------------------------------------------------- structure
def test_the_altitude_relationship_plot_is_gone():
    assert "altitudePlot" not in HTML
    assert "altitudePlot" not in SCRIPT
    assert "Altitude relationship" not in HTML


def test_the_spectra_panel_is_gone():
    assert 'data-section="spectra"' not in HTML
    assert 'data-view="spectra"' not in HTML
    assert "spectraPlot" not in HTML
    assert "spectraPlot" not in SCRIPT


def test_the_distribution_spans_the_full_width():
    """`wide` is what makes a card span both grid columns, as the plot above
    it does; without it the card keeps half the width it had when the altitude
    plot sat beside it."""
    card = next(
        line for line in HTML.splitlines() if "histogramPlot" in line and "<article" in line
    )
    assert 'class="chart-card wide"' in card

    overview = next(
        line for line in HTML.splitlines() if "overviewPlot" in line and "<article" in line
    )
    assert 'class="chart-card wide"' in overview, "the plot it must match"


def test_a_removed_view_falls_back_instead_of_blanking_the_page():
    assert "VIEWS.includes(requested) ? requested : 'overview'" in SCRIPT
    views = re.search(r"const VIEWS = \[([^\]]+)\]", SCRIPT).group(1)
    assert "spectra" not in views
    assert "overview" in views and "timeseries" in views and "map" in views


def test_every_remaining_tab_has_a_card():
    tabs = set(re.findall(r'data-view="([a-z]+)"', HTML))
    sections = set(re.findall(r'data-section="([a-z]+)"', HTML))

    assert tabs == {"overview", "timeseries", "map"}
    assert tabs <= sections, f"tab with no card: {tabs - sections}"


def test_the_map_has_its_own_index_selector():
    assert 'id="mapVariableSelect"' in HTML
    assert "mapVariableSelect" in SCRIPT


def test_no_element_the_script_needs_is_missing():
    referenced = set(re.findall(r"getElementById\('([A-Za-z0-9_]+)'\)", SCRIPT))
    # The map legend builds these itself, inside the Leaflet control.
    runtime_built = {"mapLegendName", "mapLegendMin", "mapLegendMax"}
    missing = {name for name in referenced - runtime_built if f'id="{name}"' not in HTML}

    assert not missing, f"sif.js reaches for ids the page does not have: {missing}"


# ------------------------------------------------------------------- runtime
HARNESS = """
const captured = {};
globalThis.Plotly = { react: (id, traces) => { captured[id] = traces; } };
globalThis.layout = (x, y) => ({ x, y });
globalThis.config = {};
let payload = null;

__BODY__

const out = {};
const values = [];
for (let i = 0; i < 5000; i++) values.push(i % 2 ? 0.2 + (i % 50) / 500 : 0.75 + (i % 30) / 600);
values.push(NaN, null, undefined);

renderDistribution(values, 'NDVI');
const traces = captured.histogramPlot;
out.traceCount = traces.length;
out.hasHistogram = traces[0].type === 'histogram';
out.hasCurve = traces[1].type === 'scatter' && traces[1].mode.indexOf('lines') >= 0;
const counts = traces[1].y, centres = traces[1].x;
out.countsAllValues = counts.reduce((a, b) => a + b, 0) === values.filter(Number.isFinite).length;
out.onePointPerBin = counts.length === centres.length;
out.centresIncrease = centres.every((v, i) => i === 0 || v > centres[i - 1]);
out.allFinite = counts.every(Number.isFinite) && centres.every(Number.isFinite);
const half = Math.floor(counts.length / 2);
out.bothModesVisible = Math.max.apply(null, counts.slice(0, half)) > 0
                    && Math.max.apply(null, counts.slice(half)) > 0;

renderDistribution([NaN, null], 'NDVI');
out.emptyDrawsNothing = captured.histogramPlot.length === 0;

const mode = { variables: { NDVI: [], PRI: [], EVI: [], MTCI: [],
  'SIF_A_ifld [mW m-2nm-1sr-1]': [], 'PAR inc [W m-2]': [], 'temp1 [C]': [] } };
const offered = indexVariables(mode);
out.onlyIndices = offered.length === 4 && offered.every(n => ['NDVI','PRI','EVI','MTCI'].indexOf(n) >= 0);
out.customFallsBack = indexVariables({ variables: { MY_INDEX: [], OTHER: [] } }).length === 2;

// A payload that declares its own indices wins over the built-in list, so an
// operator's own index definition file is honoured.
payload = { index_names: ['MY_INDEX'] };
const declared = indexVariables({ variables: { MY_INDEX: [], NDVI: [], OTHER: [] } });
out.declaredWins = declared.length === 1 && declared[0] === 'MY_INDEX';

// L800 is in the bundled index file, so it must be offered by the fallback too.
payload = null;
out.l800Offered = indexVariables({ variables: { L800: [], 'temp1 [C]': [] } }).indexOf('L800') >= 0;

JSON.stringify(out);
"""


def _lift(name):
    start = SCRIPT.index(f"function {name}(")
    end = SCRIPT.index("\n  }", start) + len("\n  }")
    return SCRIPT[start:end]


@pytest.fixture(scope="module")
def outcome(tmp_path_factory):
    if sys.platform != "darwin" or not shutil.which("osascript"):
        pytest.skip("JavaScriptCore is only reachable through osascript on macOS")

    veg = SCRIPT[SCRIPT.index("const vegetationIndices = ["):]
    veg = veg[: veg.index("];") + 2]
    names = SCRIPT[SCRIPT.index("const INDEX_NAMES ="):]
    names = names[: names.index(";", names.index("!/\\s/.test(name))")) + 1]

    body = "\n\n".join([veg, names, _lift("renderDistribution"), _lift("indexVariables")])
    built = tmp_path_factory.mktemp("sif") / "runtime.js"
    built.write_text(HARNESS.replace("__BODY__", body), encoding="utf-8")

    result = subprocess.run(
        ["osascript", "-l", "JavaScript", str(built)],
        capture_output=True, text=True, timeout=120, check=False,
    )
    if result.returncode != 0:
        pytest.fail(f"the SIF plotting code did not run: {result.stderr.strip()}")
    return json.loads(result.stdout.strip())


@pytest.mark.parametrize(
    "behaviour",
    [
        "hasHistogram", "hasCurve", "countsAllValues", "onePointPerBin",
        "centresIncrease", "allFinite", "bothModesVisible", "emptyDrawsNothing",
        "onlyIndices", "customFallsBack", "declaredWins", "l800Offered",
    ],
)
def test_distribution_and_index_behaviour(outcome, behaviour):
    assert outcome[behaviour] is True


def test_the_distribution_draws_bars_and_a_curve(outcome):
    assert outcome["traceCount"] == 2


# --------------------------------------------------- toolbar and the map's tab
def test_the_toolbar_holds_both_controls():
    """Index moved up into the toolbar, where Variable was."""
    assert 'data-toolbar-for="plots"' in HTML
    assert 'data-toolbar-for="map"' in HTML
    assert 'id="mapVariableSelect"' in HTML

    toolbar = HTML[HTML.index('id="modeSelect"'):]
    toolbar = toolbar[: toolbar.index("</div>")]
    assert "mapVariableSelect" in toolbar, "the Index control belongs in the toolbar"


def test_only_the_control_that_drives_the_view_is_shown():
    assert "node.dataset.toolbarFor !== (view === 'map' ? 'map' : 'plots')" in SCRIPT


def test_hiding_a_toolbar_label_actually_hides_it():
    """`.toolbar label` sets display:flex, which outranks the browser's
    [hidden]{display:none}; without an explicit rule the swap does nothing."""
    assert ".toolbar label[hidden]{display:none}" in HTML


def test_the_map_can_be_opened_on_its_own():
    assert 'id="mapNewTabBtn"' in HTML
    assert "window.open(`/sif/map?${parameters}`, '_blank', 'noopener')" in SCRIPT
    # Both, so the new tab arrives on the same product as well as the same index.
    opener = SCRIPT[SCRIPT.index("mapNewTabBtn"):]
    opener = opener[: opener.index("};")]
    assert "mode:" in opener and "index:" in opener


def test_the_new_tab_arrives_on_the_index_it_was_opened_for():
    assert "new URLSearchParams(location.search).get('index')" in SCRIPT
    assert "new URLSearchParams(location.search).get('mode')" in SCRIPT


def test_the_requested_index_does_not_override_a_later_choice():
    """It is read where the list is built, which happens once - not on every
    redraw, or picking another index in that tab would snap straight back."""
    body = SCRIPT[SCRIPT.index("function mapVariable("):]
    body = body[: body.index("\n  }")]
    guard = body.index("if (select.dataset.names !== names.join(' '))")

    assert body.index("get('index')") > guard


def test_the_map_has_a_full_screen_control():
    assert 'id="mapFullscreenBtn"' in HTML
    assert "requestFullscreen" in SCRIPT
    # Entering and leaving both change the container size under Leaflet.
    assert "fullscreenchange" in SCRIPT
    assert "invalidateSize" in SCRIPT
