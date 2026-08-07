"""The Trace Gas Investigation page.

Built after Flight_CC0806, where MIRO CO2 sat at R2 = 0.05 against the Picarro
while H2O, through the identical readers and clock, reached 0.996. The
disagreement was a cell-temperature drift of 6.2 ppm per degree over a 7.7
degree warm-up - roughly 48 ppm laid over an atmospheric signal whose standard
deviation was 3.45 ppm. These cases hold the arithmetic that established that,
and the two promises the export makes about the figures it writes.
"""

from __future__ import annotations

import importlib.util
import math
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from app import trace_gas_export
from app.trace_gas_investigation import (
    COLLINEARITY_LIMIT,
    DEFAULT_RESOLUTION,
    RESOLUTIONS,
    Fit,
    agreement,
    build_frame,
    investigate,
    joint_fit,
    parse_filters,
    straight_line,
)

ROOT = Path(__file__).resolve().parents[1]
LEGACY = ROOT / "legacy_integration" / "MIRO_Rack"


def _module(name: str):
    spec = importlib.util.spec_from_file_location(
        f"ccflux_trace_gas_{name}", LEGACY / f"{name}.py"
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def miro_module():
    return _module("miro")


@pytest.fixture(scope="module")
def picarro_module():
    return _module("picarro")


def _synthetic(minutes: int = 240, *, drift_per_degree: float = 6.0):
    """A flight whose answer is known: CO2 with a known thermal drift on it.

    The cell warms while the aircraft climbs, exactly as on the campaign, so a
    simple regression against either driver alone is confounded and only the
    joint fit can recover the coefficient that was put in.
    """
    stamps = pd.date_range("2026-08-06 06:00:00", periods=minutes * 60, freq="1s")
    step = np.arange(len(stamps), dtype=float) / len(stamps)
    rng = np.random.default_rng(20260806)
    # The cell warms through the day but not monotonically - it responds to the
    # cabin as well as to the clock. Without that wobble it is a linear function
    # of time, no fit can tell the two apart, and the partial coefficients are
    # split arbitrarily between them. Real records have it; a fixture that omits
    # it is testing a degenerate case.
    altitude = 50.0 + 600.0 * np.sin(np.pi * step)  # climbs and descends
    cell = 25.0 + 8.0 * step + 1.2 * np.sin(9.0 * np.pi * step)
    truth = 412.0 + 3.0 * np.sin(6.0 * np.pi * step) + rng.normal(0, 0.2, len(stamps))
    measured = truth + drift_per_degree * (cell - cell[0])
    miro = pd.DataFrame({
        "timestamp": stamps,
        "CO2 wet": measured / 1e6,
        "CH4 wet": (2.0 + 0.01 * np.sin(4 * np.pi * step)) / 1e6,
        "T Cell C": cell,
        # Outside air follows the altitude through the lapse rate, not the cell,
        # plus weather of its own - without that last term it is an exact linear
        # combination of altitude and the clock and no fit can be asked to
        # apportion the three.
        "Outside T": (24.0 - 0.0065 * altitude + 2.0 * step
                      + rng.normal(0, 0.25, len(stamps))),
        "Laser housing T": 28.0 + 0.2 * step + 0.1 * np.cos(13.0 * np.pi * step),
        "p Cell": np.full(len(stamps), 85.0),
        "VValve 0": np.zeros(len(stamps)),
    })
    picarro = pd.DataFrame({
        "timestamp": stamps,
        "CO2_sync": truth,
        "CH4_sync": 2.0 + 0.01 * np.sin(4 * np.pi * step),
    })
    navigation = pd.DataFrame({"timestamp": stamps, "altitude": altitude})
    return miro, picarro, navigation


@pytest.fixture(scope="module")
def result(miro_module, picarro_module):
    miro, picarro, navigation = _synthetic()
    return investigate(
        miro, picarro, navigation, parse_filters({"resolution_seconds": 60}),
        miro_module=miro_module, picarro_module=picarro_module,
    )


class TestTheArithmetic:
    def test_a_straight_line_recovers_what_was_put_in(self):
        x = np.arange(200, dtype=float)
        y = 3.5 * x - 12.0
        fit = straight_line(x, y)
        assert fit.slope == pytest.approx(3.5)
        assert fit.intercept == pytest.approx(-12.0)
        assert fit.r_squared == pytest.approx(1.0)

    def test_too_few_points_is_reported_not_fitted(self):
        fit = straight_line(np.array([1.0, 2.0]), np.array([1.0, 2.0]))
        assert not math.isfinite(fit.slope)
        assert fit.samples == 2

    def test_a_driver_that_never_moves_yields_no_slope(self):
        """A constant cell pressure must not produce an infinite sensitivity."""
        x = np.full(100, 85.0)
        y = np.linspace(400.0, 420.0, 100)
        assert not math.isfinite(straight_line(x, y).slope)

    def test_missing_samples_are_dropped_pairwise(self):
        x = np.array([1.0, 2.0, np.nan, 4.0, 5.0])
        y = np.array([2.0, 4.0, 8.0, 8.0, np.nan])
        fit = straight_line(x, y)
        assert fit.samples == 3
        assert fit.slope == pytest.approx(2.0)

    def test_the_joint_fit_separates_two_correlated_drivers(self):
        """The reason the page reports a partial coefficient at all."""
        rng = np.random.default_rng(7)
        step = np.linspace(0, 1, 800)
        cell = 25.0 + 8.0 * step
        altitude = 50.0 + 600.0 * np.sin(np.pi * step)
        y = 400.0 + 6.0 * cell - 0.01 * altitude + rng.normal(0, 0.05, 800)

        partial, r_squared, condition = joint_fit(
            {"cell": cell, "altitude": altitude}, y
        )

        assert partial["cell"].slope == pytest.approx(6.0, abs=0.05)
        assert partial["altitude"].slope == pytest.approx(-0.01, abs=0.001)
        assert r_squared == pytest.approx(1.0, abs=1e-3)
        assert condition < COLLINEARITY_LIMIT

    def test_two_drivers_on_the_same_line_are_detected_not_split(self):
        """A cell that warms steadily is very nearly a clock.

        Least squares will happily hand back a coefficient for each, split
        arbitrarily between them. Reporting that as physics is the failure this
        guards: the condition number says the split is not identifiable.
        """
        step = np.linspace(0, 1, 500)
        clock = step
        cell = 25.0 + 8.0 * step          # an exact linear function of the clock
        y = 400.0 + 6.0 * cell

        partial, _r_squared, condition = joint_fit({"cell": cell, "clock": clock}, y)

        assert condition > COLLINEARITY_LIMIT
        # The fit still describes the data; only the apportioning is unusable.
        assert partial["cell"].slope != pytest.approx(6.0, abs=0.3)

    def test_agreement_reports_bias_separately_from_scatter(self):
        reference = np.linspace(400.0, 420.0, 300)
        measured = reference + 12.0
        stats = agreement(reference, measured)
        assert stats["r_squared"] == pytest.approx(1.0)
        assert stats["bias"] == pytest.approx(12.0)
        assert stats["rmse"] == pytest.approx(12.0)
        assert stats["slope"] == pytest.approx(1.0)


class TestTheKnownDrift:
    """A synthetic flight carrying a drift of exactly 6 ppm per degree."""

    def test_the_reference_comparison_is_wrecked_by_the_drift(self, result):
        entry = next(e for e in result["species"] if e["name"] == "CO2 wet")
        assert entry["reference"]["r_squared"] < 0.2

    def test_the_drift_is_found_on_the_difference_and_measured(self, result):
        entry = next(e for e in result["species"] if e["name"] == "CO2 wet")
        detrended = entry["reference_detrended"]
        assert detrended["driver"] == "T Cell C"
        assert detrended["slope_per_unit"] == pytest.approx(6.0, abs=0.05)

    def test_removing_it_restores_the_comparison(self, result):
        entry = next(e for e in result["species"] if e["name"] == "CO2 wet")
        before = entry["reference"]["r_squared"]
        after = entry["reference_detrended"]["r_squared"]
        assert after > 0.95
        assert after > before
        assert entry["reference_detrended"]["slope"] == pytest.approx(1.0, abs=0.05)

    def test_the_fit_is_on_the_difference_not_the_species(self, result):
        """Fitting the species itself deletes real signal.

        On the campaign that turned an honest H2O agreement of 0.996 into 0.597,
        because the cell warms as the air does and the regression removed the
        atmosphere along with the drift.
        """
        entry = next(e for e in result["species"] if e["name"] == "CH4 wet")
        # CH4 here carries no drift, so its comparison must survive untouched.
        assert entry["reference"]["r_squared"] > 0.95
        assert entry["reference_detrended"]["r_squared"] > 0.95

    def test_the_intercept_travels_so_a_plot_can_reproduce_the_score(self, result):
        entry = next(e for e in result["species"] if e["name"] == "CO2 wet")
        assert "intercept" in entry["reference_detrended"]
        assert entry["reference_detrended"]["intercept"] is not None

    def test_a_confounded_simple_slope_is_flagged(self, result):
        """Altitude carries the thermal drift here; the page must say so."""
        entry = next(e for e in result["species"] if e["name"] == "CO2 wet")
        altitude = entry["drivers"]["altitude"]
        assert altitude["confounded"] is True

    def test_the_partial_coefficient_recovers_the_real_sensitivity(self, result):
        entry = next(e for e in result["species"] if e["name"] == "CO2 wet")
        assert entry["partial_reliable"] is True
        assert entry["drivers"]["T Cell C"]["partial_slope"] == pytest.approx(6.0, abs=0.3)

    def test_an_unidentifiable_split_is_marked_rather_than_believed(
        self, miro_module, picarro_module
    ):
        """A cell that warms perfectly linearly cannot be told from the clock."""
        miro, picarro, navigation = _synthetic()
        stamps = miro["timestamp"]
        step = np.arange(len(stamps), dtype=float) / len(stamps)
        miro["T Cell C"] = 25.0 + 8.0 * step        # no wobble: exactly the clock
        miro["Outside T"] = miro["T Cell C"] - 1.5
        miro["Laser housing T"] = 28.0 + 0.2 * step

        result = investigate(
            miro, picarro, navigation, parse_filters({}),
            miro_module=miro_module, picarro_module=picarro_module,
        )

        entry = next(e for e in result["species"] if e["name"] == "CO2 wet")
        assert entry["partial_reliable"] is False
        assert entry["drivers"]["T Cell C"]["confounded"] is False
        assert any("nearly the same line" in note for note in result["notes"])

    def test_the_simple_slope_is_reported_whatever_the_collinearity(
        self, miro_module, picarro_module
    ):
        """It is a measurement, not an inference, so it always stands."""
        miro, picarro, navigation = _synthetic()
        stamps = miro["timestamp"]
        step = np.arange(len(stamps), dtype=float) / len(stamps)
        miro["T Cell C"] = 25.0 + 8.0 * step
        result = investigate(
            miro, picarro, navigation, parse_filters({}),
            miro_module=miro_module, picarro_module=picarro_module,
        )
        entry = next(e for e in result["species"] if e["name"] == "CO2 wet")
        assert entry["drivers"]["T Cell C"]["slope"] == pytest.approx(6.0, abs=0.3)

    def test_percent_per_unit_is_relative_to_the_species_own_mean(self, result):
        entry = next(e for e in result["species"] if e["name"] == "CO2 wet")
        cell = entry["drivers"]["T Cell C"]
        assert cell["percent_per_unit"] == pytest.approx(
            cell["slope"] / entry["mean"] * 100.0, rel=1e-9
        )


class TestSpeciesWithoutAReference:
    def test_they_are_still_evaluated(self, miro_module, picarro_module):
        """CO, N2O, NO, NO2, SO2, NH3 and O3 have no Picarro, and a species that
        follows the cell temperature is suspect whether or not one exists."""
        miro, _picarro, navigation = _synthetic()
        miro["O3 wet"] = (30.0 + 2.0 * miro["T Cell C"]) / 1e9
        result = investigate(
            miro, None, navigation, parse_filters({}),
            miro_module=miro_module, picarro_module=picarro_module,
        )
        entry = next(e for e in result["species"] if e["name"] == "O3 wet")
        assert "reference" not in entry
        assert entry["drivers"]["T Cell C"]["r_squared"] > 0.99
        assert entry["unit"] == "ppb"

    def test_the_absence_of_a_reference_is_stated(self, miro_module, picarro_module):
        miro, _picarro, navigation = _synthetic()
        result = investigate(
            miro, None, navigation, parse_filters({}),
            miro_module=miro_module, picarro_module=picarro_module,
        )
        assert any("no species has a reference" in note for note in result["notes"])


class TestUnits:
    def test_each_species_is_reported_in_the_workspace_unit(self, result):
        units = {entry["name"]: entry["unit"] for entry in result["species"]}
        assert units["CO2 wet"] == "ppm"
        assert units["CH4 wet"] == "ppm"

    def test_the_values_are_scaled_out_of_mole_fraction(self, result):
        """MIRO writes 4.46756E-4 for 446.8 ppm; a table of 0.000446 is unreadable."""
        entry = next(e for e in result["species"] if e["name"] == "CO2 wet")
        assert 380.0 < entry["mean"] < 500.0


class TestFilters:
    def test_an_unknown_averaging_window_is_refused(self):
        with pytest.raises(ValueError, match="Averaging window"):
            parse_filters({"resolution_seconds": 37})

    def test_every_offered_window_is_accepted(self):
        for seconds in RESOLUTIONS:
            assert parse_filters({"resolution_seconds": seconds}).resolution_seconds == seconds

    def test_the_default_is_a_minute(self):
        assert parse_filters({}).resolution_seconds == DEFAULT_RESOLUTION

    def test_a_backwards_window_is_refused(self):
        with pytest.raises(ValueError, match="after its end"):
            parse_filters({"start": "2026-08-06 12:00", "end": "2026-08-06 09:00"})

    def test_an_inverted_altitude_band_is_refused(self):
        with pytest.raises(ValueError, match="above the highest"):
            parse_filters({"altitude_min": 500, "altitude_max": 100})

    def test_an_unreadable_time_is_refused_rather_than_ignored(self):
        with pytest.raises(ValueError, match="readable date"):
            parse_filters({"start": "yesterday afternoon"})

    def test_the_time_window_narrows_the_samples(self, miro_module, picarro_module):
        miro, picarro, navigation = _synthetic()
        whole = investigate(
            miro, picarro, navigation, parse_filters({}),
            miro_module=miro_module, picarro_module=picarro_module,
        )
        part = investigate(
            miro, picarro, navigation,
            parse_filters({"start": "2026-08-06 07:00:00", "end": "2026-08-06 08:00:00"}),
            miro_module=miro_module, picarro_module=picarro_module,
        )
        assert part["window"]["samples"] < whole["window"]["samples"]
        assert part["window"]["start"].startswith("2026-08-06T07")

    def test_the_altitude_band_narrows_the_samples(self, miro_module, picarro_module):
        miro, picarro, navigation = _synthetic()
        banded = investigate(
            miro, picarro, navigation,
            parse_filters({"altitude_min": 400, "altitude_max": 700}),
            miro_module=miro_module, picarro_module=picarro_module,
        )
        assert 400 <= banded["window"]["altitude_min"]
        assert banded["window"]["altitude_max"] <= 700

    def test_a_window_that_empties_the_record_says_so(self, miro_module, picarro_module):
        miro, picarro, navigation = _synthetic()
        with pytest.raises(ValueError, match="No samples remain"):
            investigate(
                miro, picarro, navigation,
                parse_filters({"start": "2027-01-01 00:00", "end": "2027-01-02 00:00"}),
                miro_module=miro_module, picarro_module=picarro_module,
            )

    def test_zeroing_cycles_are_excluded_by_default(self, miro_module, picarro_module):
        """A window that caught only a zero cycle is not a sample.

        Counting the resulting all-NaN rows made the filter look as though it
        had changed nothing.
        """
        miro, picarro, navigation = _synthetic()
        # Whole averaging windows, so the exclusion removes rows rather than
        # merely thinning them.
        miro.loc[3600:7199, "VValve 0"] = 1.0
        kept = investigate(
            miro, picarro, navigation, parse_filters({"stable_ambient_only": True}),
            miro_module=miro_module, picarro_module=picarro_module,
        )
        everything = investigate(
            miro, picarro, navigation, parse_filters({"stable_ambient_only": False}),
            miro_module=miro_module, picarro_module=picarro_module,
        )
        assert kept["window"]["samples"] < everything["window"]["samples"]
        assert any("Zeroing cycles" in note for note in kept["notes"])


class TestWhatTheBrowserReceives:
    def test_nothing_that_json_cannot_carry(self, result):
        """NaN is not JSON; a missing statistic must be null, never a zero."""
        import json

        text = json.dumps(result, allow_nan=False)
        assert "NaN" not in text and "Infinity" not in text

    def test_the_series_carry_every_species_and_driver(self, result):
        series = result["series"]
        for entry in result["species"]:
            assert entry["name"] in series
        for driver in result["drivers"]:
            assert driver["name"] in series

    def test_long_records_are_decimated_not_truncated(self, miro_module, picarro_module):
        miro, picarro, navigation = _synthetic(minutes=600)
        result = investigate(
            miro, picarro, navigation, parse_filters({"resolution_seconds": 1}),
            miro_module=miro_module, picarro_module=picarro_module,
        )
        series = result["series"]
        assert result["window"]["samples"] > len(series["time"])
        assert series["decimation"] > 1
        # The last sample still stands for the end of the window, so the plot
        # does not silently stop early.
        assert series["time"][-1] <= result["window"]["end"]


class TestMissingNavigation:
    def test_altitude_is_optional_and_its_absence_is_stated(
        self, miro_module, picarro_module
    ):
        miro, picarro, _navigation = _synthetic()
        result = investigate(
            miro, picarro, None, parse_filters({}),
            miro_module=miro_module, picarro_module=picarro_module,
        )
        assert all(driver["name"] != "altitude" for driver in result["drivers"])
        assert any("No Noseboom altitude" in note for note in result["notes"])


class TestTheMiroLoaderCarriesInstrumentState:
    def test_the_housekeeping_columns_are_declared(self, miro_module):
        assert miro_module.HOUSEKEEPING_COLUMNS == (
            "T Cell C", "Outside T", "Laser housing T", "p Cell"
        )

    def test_they_survive_the_load(self, miro_module, tmp_path):
        """Without them there is nothing to attribute a drift to."""
        path = tmp_path / "MGA.txt"
        header = ";".join([
            "t-stamp", *miro_module.GAS_COLUMNS,
            *miro_module.HOUSEKEEPING_COLUMNS, "VValve 0",
        ])
        rows = [header]
        for second in range(5):
            values = ["4,00000E-4"] * len(miro_module.GAS_COLUMNS)
            state = ["3,20000E+1", "3,10000E+1", "2,80000E+1", "8,50000E+1"]
            rows.append(
                f"06.08.2026 06:00:0{second},00;" + ";".join([*values, *state, "0,00000E+0"])
            )
        path.write_text("\n".join(rows) + "\n", encoding="utf-8")

        data, _meta = miro_module.load_folder(tmp_path)

        for column in miro_module.HOUSEKEEPING_COLUMNS:
            assert column in data.columns, column
            assert data[column].notna().all()
        assert data["T Cell C"].iloc[0] == pytest.approx(32.0)


class TestTheFigureExport:
    """Seven inches wide, nothing below nine point - checked on the result."""

    def test_the_promised_geometry_is_the_declared_one(self):
        assert trace_gas_export.FIGURE_WIDTH_INCHES == 7.0
        assert trace_gas_export.MINIMUM_FONT_POINTS == 9.0

    def test_every_offered_format_is_written(self, result, tmp_path):
        written = trace_gas_export.render(
            result, tmp_path, "TraceGas_test", view="scatter",
            species="CO2 wet", driver="T Cell C",
            formats=("pdf", "png", "svg"), dpi=100,
        )
        assert {path.suffix for path in written} == {".pdf", ".png", ".svg"}
        assert all(path.is_file() and path.stat().st_size > 0 for path in written)

    @pytest.mark.parametrize("view", ["overview", "series", "scatter", "matrix"])
    def test_each_figure_honours_the_seven_inch_width(self, result, tmp_path, view):
        from PIL import Image

        dpi = 100
        written = trace_gas_export.render(
            result, tmp_path, f"TraceGas_{view}", view=view,
            species="CO2 wet", driver="T Cell C", formats=("png",), dpi=dpi,
        )
        with Image.open(written[0]) as image:
            width, _height = image.size
        assert width / dpi <= trace_gas_export.FIGURE_WIDTH_INCHES + 0.02

    def test_nothing_is_drawn_below_the_font_floor(self, result, tmp_path):
        """constrained_layout shrinks tick labels to make room, so this is
        measured on the laid-out figure rather than trusted from the settings."""
        import matplotlib

        matplotlib.use("Agg")
        import matplotlib.pyplot as plt

        trace_gas_export.render(
            result, tmp_path, "TraceGas_font", view="overview",
            species="CO2 wet", driver="T Cell C", formats=("png",), dpi=100,
        )
        # render() closes its figure; rebuild one the same way and inspect it.
        with plt.rc_context(trace_gas_export._rc()):
            figure, axis = plt.subplots(figsize=(7.0, 3.0), constrained_layout=True)
            trace_gas_export._draw_scatter(
                axis, result,
                next(e for e in result["species"] if e["name"] == "CO2 wet"),
                {"name": "T Cell C", "label": "Cell temperature", "unit": "degC"},
            )
            trace_gas_export._enforce_minimum_font(figure)
            sizes = [
                float(text.get_fontsize())
                for text in figure.findobj(
                    match=lambda artist: hasattr(artist, "get_fontsize")
                )
            ]
            plt.close(figure)
        assert sizes
        assert min(sizes) >= trace_gas_export.MINIMUM_FONT_POINTS

    def test_an_unknown_format_is_refused(self, result, tmp_path):
        with pytest.raises(ValueError, match="Unsupported export format"):
            trace_gas_export.render(
                result, tmp_path, "TraceGas_bad", formats=("jpeg",)
            )

    def test_an_unknown_figure_is_refused(self, result, tmp_path):
        with pytest.raises(ValueError, match="Unknown figure"):
            trace_gas_export.render(result, tmp_path, "x", view="everything")

    def test_the_resolution_is_clamped_to_something_printable(self):
        assert trace_gas_export.resolve_dpi(5) == trace_gas_export.MINIMUM_DPI
        assert trace_gas_export.resolve_dpi(99999) == trace_gas_export.MAXIMUM_DPI
        assert trace_gas_export.resolve_dpi("not a number") == 600

    def test_the_subtitle_keeps_the_word_utc(self, result, tmp_path):
        """A global T-for-space replace turned "UTC" into "U C"."""
        source = (ROOT / "app" / "trace_gas_export.py").read_text(encoding="utf-8")
        assert '.replace("T", " ", 1)' in source
        assert '.replace("T", " ")' not in source


class TestThePageIsReachable:
    def test_the_button_is_on_the_miro_rack_page(self):
        bridge = (ROOT / "app" / "miro_rack_bridge.py").read_text(encoding="utf-8")
        assert 'id="ccfluxTraceGas"' in bridge
        assert "Trace gas investigation" in bridge
        assert "/miro_rack/trace_gas" in bridge

    def test_the_routes_are_served(self):
        server = (ROOT / "app" / "server.py").read_text(encoding="utf-8")
        for route in (
            '"/miro_rack/trace_gas"',
            '"/miro_rack/trace_gas.js"',
            '"/api/miro-rack/trace-gas/data"',
            '"/api/miro-rack/trace-gas/export"',
        ):
            assert route in server, route

    def test_the_page_and_its_script_exist(self):
        assert (ROOT / "app" / "assets" / "trace_gas.html").is_file()
        assert (ROOT / "app" / "assets" / "trace_gas.js").is_file()

    def test_the_page_loads_its_script_and_plotly(self):
        html = (ROOT / "app" / "assets" / "trace_gas.html").read_text(encoding="utf-8")
        assert "/miro_rack/trace_gas.js" in html
        assert "/vendor/plotly.min.js" in html

    def test_the_script_is_served_through_the_javascript_helper(self):
        """_send_file takes its type from mimetypes and download= is keyword-only.

        Passing a content type positionally raised TypeError, the handler
        answered 400, and the page came up with every control inert: no error,
        no plots, just the static HTML. The bundle helper sets
        text/javascript itself, which a Windows registry mapping .js to
        text/plain would otherwise break even had the call been legal.
        """
        server = (ROOT / "app" / "server.py").read_text(encoding="utf-8")
        route = server[server.index('elif path == "/miro_rack/trace_gas.js":'):]
        route = route[:route.index("\n        elif ")]
        assert "self._send_javascript_bundle(" in route
        assert "self._send_file(" not in route

    def test_no_route_passes_send_file_a_positional_content_type(self):
        """The signature is (path, *, download): a second positional is a 400."""
        import ast
        import inspect

        from app.server import DashboardRequestHandler

        signature = inspect.signature(DashboardRequestHandler._send_file)
        positional = [
            name for name, parameter in signature.parameters.items()
            if parameter.kind is parameter.POSITIONAL_OR_KEYWORD and name != "self"
        ]
        assert positional == ["path"]

        tree = ast.parse((ROOT / "app" / "server.py").read_text(encoding="utf-8"))
        offenders = [
            node.lineno
            for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "_send_file"
            and len(node.args) > 1
        ]
        assert not offenders, (
            f"_send_file given a second positional argument at line(s) {offenders}"
        )

    def test_the_filters_the_engine_accepts_are_the_ones_the_page_sends(self):
        script = (ROOT / "app" / "assets" / "trace_gas.js").read_text(encoding="utf-8")
        for key in ("resolution_seconds", "start", "end", "altitude_min",
                    "altitude_max", "stable_ambient_only"):
            assert key in script, key

    def test_processing_is_required_before_the_page_can_run(self):
        bridge = (ROOT / "app" / "miro_rack_bridge.py").read_text(encoding="utf-8")
        assert "before opening the" in bridge
        assert "_loaded_gas_frames" in bridge
