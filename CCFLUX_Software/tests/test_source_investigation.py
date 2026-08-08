"""Going deeper than "is this signal real": what was under it, and what blew.

The Trace Gas Investigation says whether an enhancement is the air or the
instrument. This is the page for the next question, and the traps it has to
avoid are all about not quietly changing the number an operator reads off it:

* the MIRO stores mole fractions, so a page labelling them ppb without scaling
  is wrong by a factor of a billion;
* a bearing cannot be averaged, interpolated or smoothed as a plain number,
  because 359 and 1 average to 180 - a wind from the north read as from the
  south;
* a filter applied to the drawn curve must never reach the numbers, and a
  decimated record must not hide the spike it was opened to find.
"""

from __future__ import annotations

import importlib.util
import sys
from datetime import datetime, timedelta
from pathlib import Path

import pytest

pytest.importorskip("pandas")
pytest.importorskip("scipy")

import numpy as np
import pandas as pd

from app import source_investigation as engine
from core.legacy_paths import legacy_integration_path

ROOT = Path(__file__).resolve().parents[1]


def _legacy(package: str, name: str):
    folder = legacy_integration_path(package)
    if str(folder) not in sys.path:
        sys.path.insert(0, str(folder))
    label = f"ccflux_test_{package}_{name}"
    spec = importlib.util.spec_from_file_location(label, folder / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    # Registered before it is executed: a dataclass declared inside resolves its
    # own annotations through sys.modules[cls.__module__], which is None until
    # the module is there, and the import fails on an attribute of None.
    sys.modules[label] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture()
def miro_data():
    """A 1 Hz MIRO hour with a one-second NO2 spike in the middle of it."""
    began = datetime(2026, 8, 6, 12, 0)
    count = 3600
    stamps = [began + timedelta(seconds=index) for index in range(count)]
    baseline = 1.0 + 0.15 * np.sin(np.arange(count) / 240.0)
    noise = np.resize([0.04, -0.05, 0.03, -0.02], count)
    no2 = baseline + noise
    no2[1800] = 75.6
    frame = pd.DataFrame({"timestamp": stamps})
    # Stored as mole fractions, the way the instrument writes them.
    frame["NO2 wet"] = no2 / 1e9
    frame["CO2 wet"] = (430.0 + np.linspace(0, 6, count)) / 1e6
    frame["H2O wet"] = (1.2 + np.zeros(count)) / 100.0
    frame["T Cell C"] = 25.0 + np.linspace(0.0, 4.0, count)
    return frame


@pytest.fixture()
def navigation():
    began = datetime(2026, 8, 6, 12, 0)
    count = 3600
    index = np.arange(count)
    return pd.DataFrame({
        "timestamp": [began + timedelta(seconds=int(value)) for value in index],
        "lat": 50.90 + 0.02 * np.sin(index / 600.0),
        "lon": 6.60 + 0.03 * np.cos(index / 700.0),
        "altitude": 300.0 + 80.0 * np.sin(index / 900.0),
        "wind_mps": 5.0 + 2.0 * np.sin(index / 800.0),
        "wind_dir_deg": np.full(count, 225.0),
        "heading_deg": (index * 0.5) % 360.0,
        "ground_speed_mps": 12.0 + np.zeros(count),
    })


class TestUnits:
    """The MIRO writes mole fractions and every label on the page says ppb."""

    def test_the_gases_are_scaled_to_the_units_they_are_labelled_with(
        self, miro_data
    ):
        frame = engine.combined_frame(miro_data, None)

        assert frame["NO2 wet"].max() == pytest.approx(75.6, rel=1e-9)
        assert frame["CO2 wet"].iloc[0] == pytest.approx(430.0, rel=1e-9)
        assert frame["H2O wet"].iloc[0] == pytest.approx(1.2, rel=1e-9)

    def test_housekeeping_is_left_alone(self, miro_data):
        """It is already in degrees and millibar; scaling it would be a bug."""
        frame = engine.combined_frame(miro_data, None)

        assert frame["T Cell C"].iloc[0] == pytest.approx(25.0)

    def test_the_fallback_scale_matches_the_instrument_module(self):
        """The table here exists so this module can be tested without the
        legacy package. It must not drift from the one that ships."""
        miro = _legacy("MIRO_Rack", "miro")

        for column in engine.GAS_CHANNELS:
            assert engine.gas_unit_scale(column) == miro.gas_unit_scale(column)

    def test_the_module_is_preferred_over_the_fallback(self, miro_data):
        class Stub:
            @staticmethod
            def gas_unit_scale(column):
                return "ppt", 1e12

        frame = engine.combined_frame(miro_data, None, miro_module=Stub())

        assert frame["NO2 wet"].max() == pytest.approx(75.6 * 1e3, rel=1e-9)


class TestSmoothingNeverReachesTheNumbers:
    def test_the_raw_record_is_returned_beside_the_curve(self, miro_data):
        frame = engine.combined_frame(miro_data, None)
        filters = engine.parse_filters({"smoothing": "savgol"})

        payload = engine.build_rows(frame, filters)

        assert max(v for v in payload["raw"]["NO2 wet"] if v is not None) > 70.0

    def test_a_region_is_measured_on_the_raw_record(self, miro_data):
        """Read off the smoothed curve the spike is under a tenth of itself."""
        frame = engine.combined_frame(miro_data, None)
        region = engine.parse_region({
            "region_start": "2026-08-06T12:25:00",
            "region_end": "2026-08-06T12:35:00",
        })

        result = engine.investigate_region(frame, region)

        assert result["enhancements"]["NO2 wet"]["maximum"] == pytest.approx(75.6)

    def test_the_page_is_told_that_smoothing_is_display_only(self, miro_data):
        frame = engine.combined_frame(miro_data, None)
        payload = engine.build_rows(frame, engine.parse_filters({}))

        assert "drawn curve only" in payload["smoothing"]["note"]

    def test_a_gap_is_not_bridged_by_the_filter(self, miro_data):
        """A stretch the instrument did not report must stay empty rather than
        having a smooth line drawn across it."""
        miro_data.loc[1000:1200, "NO2 wet"] = np.nan
        frame = engine.combined_frame(miro_data, None)

        payload = engine.build_rows(frame, engine.parse_filters({}))

        drawn = payload["series"]["NO2 wet"]
        assert any(value is None for value in drawn)


class TestTheSpikeSurvivesDecimation:
    """43 431 samples drawn as 6 000 points means seven in eight are not drawn,
    and a one-second plume is one sample."""

    def test_the_envelope_carries_the_true_excursion(self):
        """A flight-length record, where seven samples in eight are not drawn
        and the spike is one of them."""
        count = engine.MAXIMUM_ROW_POINTS * 8
        began = datetime(2026, 8, 6, 6, 0)
        values = np.full(count, 1.0)
        # Deliberately on an index the decimation does not land on.
        spike = (count // 2) + 3
        values[spike] = 75.6
        frame = engine.combined_frame(
            pd.DataFrame({
                "timestamp": [began + timedelta(seconds=i) for i in range(count)],
                "NO2 wet": values / 1e9,
            }),
            None,
        )

        payload = engine.build_rows(
            frame, engine.parse_filters({"smoothing": "none"})
        )

        assert payload["decimation"] > 1
        drawn = [v for v in payload["raw"]["NO2 wet"] if v is not None]
        assert max(drawn) < 2.0, "the spike was drawn; pick an index that is not"
        high = [v for v in payload["envelope"]["NO2 wet"]["high"] if v is not None]
        assert max(high) == pytest.approx(75.6)

    def test_the_envelope_brackets_the_drawn_line(self, miro_data):
        frame = engine.combined_frame(miro_data, None)
        payload = engine.build_rows(
            frame, engine.parse_filters({"smoothing": "none"})
        )

        band = payload["envelope"]["NO2 wet"]
        for drawn, low, high in zip(payload["raw"]["NO2 wet"],
                                    band["low"], band["high"]):
            if drawn is None:
                continue
            assert low <= drawn <= high

    def test_a_bearing_gets_no_envelope(self, miro_data, navigation):
        """A band between two bearings is meaningless across north."""
        frame = engine.combined_frame(miro_data, navigation)
        payload = engine.build_rows(frame, engine.parse_filters({}))

        assert "wind_dir_deg" not in payload["envelope"]


class TestBearingsAreNotOrdinaryNumbers:
    def test_smoothing_holds_a_wind_that_sits_on_north(self):
        """Filtered as plain degrees, a direction wandering either side of 360
        swings to south and back on every crossing."""
        times = pd.Series(pd.date_range("2026-08-06 12:00", periods=600, freq="1s"))
        values = np.resize([358.0, 359.0, 0.0, 1.0, 2.0, 1.0], 600).astype(float)

        smoothed = engine.smooth_values(times, values, "savgol", 15, 2, circular=True)

        offset = (smoothed - 0.0 + 180.0) % 360.0 - 180.0
        assert np.nanmax(np.abs(offset)) < 5.0

    def test_the_mean_of_359_and_1_is_north(self):
        assert engine.circular_mean(np.array([359.0, 1.0])) == pytest.approx(
            0.0, abs=1e-9
        )

    def test_the_legacy_navigation_carries_wind_direction(self):
        """resample_navigation dropped WIND_dir_deg, so the adapter's straight-leg
        summary asked for a bearing that was never there."""
        noseboom = _legacy("Noseboom", "noseboom_browser_GUI")

        assert "wind_dir_deg" in noseboom.CIRCULAR_NAVIGATION_COLUMNS
        assert "heading_deg" in noseboom.CIRCULAR_NAVIGATION_COLUMNS
        # Not in the plain list, which is passed straight to .median().
        assert "wind_dir_deg" not in noseboom.NAVIGATION_COLUMNS

    def test_a_short_gap_in_a_bearing_is_filled_the_short_way(self):
        noseboom = _legacy("Noseboom", "noseboom_browser_GUI")
        values = pd.Series([359.0, np.nan, 1.0])

        filled = noseboom.interpolate_circular_deg(values)

        offset = (filled.iloc[1] - 0.0 + 180.0) % 360.0 - 180.0
        assert abs(offset) < 1.0, "the fill went the long way, through south"


class TestTheWindRose:
    def test_it_reports_the_direction_the_wind_came_from(self, miro_data, navigation):
        frame = engine.combined_frame(miro_data, navigation)
        region = engine.parse_region({
            "region_start": "2026-08-06T12:10:00",
            "region_end": "2026-08-06T12:20:00",
        })

        result = engine.investigate_region(frame, region)

        assert result["windrose"]["convention"].startswith("Direction the wind")
        assert result["statistics"]["wind_direction"]["label"] == "SW"

    def test_every_sample_lands_in_exactly_one_sector(self, miro_data, navigation):
        frame = engine.combined_frame(miro_data, navigation)
        region = engine.parse_region({
            "region_start": "2026-08-06T12:00:00",
            "region_end": "2026-08-06T12:59:00",
        })

        rose = engine.investigate_region(frame, region)["windrose"]

        assert sum(petal["count"] for petal in rose["petals"]) == rose["samples"]

    def test_a_sector_is_centred_on_its_compass_point(self):
        """North spans 348.75 to 11.25, not 0 to 22.5, or a northerly wind is
        split between two petals."""
        directions = np.array([355.0, 5.0, 359.0])
        speeds = np.array([4.0, 4.0, 4.0])

        rose = engine.windrose(directions, speeds)

        north = next(p for p in rose["petals"] if p["label"] == "N")
        assert north["count"] == 3

    def test_a_gust_above_the_top_edge_is_still_counted(self):
        rose = engine.windrose(np.array([180.0]), np.array([40.0]))

        assert rose["samples"] == 1
        south = next(p for p in rose["petals"] if p["label"] == "S")
        assert south["count"] == 1
        assert south["bands"][-1]["to"] is None

    def test_the_derived_direction_matches_the_convention(self):
        """Verified against Flight_CC0806: 37 171 paired samples, and the
        largest disagreement with the instrument's own bearing was 0.0000 deg."""
        speed, bearing = 6.0, 225.0
        frame = pd.DataFrame({
            "wind_u_mps": [-speed * np.sin(np.deg2rad(bearing))],
            "wind_v_mps": [-speed * np.cos(np.deg2rad(bearing))],
        })

        derived = engine.wind_direction_from_components(frame)

        assert derived.iloc[0] == pytest.approx(bearing)

    def test_it_is_derived_only_when_the_record_has_no_bearing(
        self, miro_data, navigation
    ):
        frame = engine.combined_frame(miro_data, navigation)
        region = engine.parse_region({
            "region_start": "2026-08-06T12:10:00",
            "region_end": "2026-08-06T12:20:00",
        })

        assert engine.investigate_region(frame, region)["windrose"][
            "derived_direction"
        ] is False


class TestTheRegion:
    def test_a_backwards_drag_selects_the_same_interval(self):
        forward = engine.parse_region({
            "region_start": "2026-08-06T12:10:00",
            "region_end": "2026-08-06T12:20:00",
        })
        backward = engine.parse_region({
            "region_start": "2026-08-06T12:20:00",
            "region_end": "2026-08-06T12:10:00",
        })

        assert forward == backward

    def test_a_click_without_a_drag_is_refused_clearly(self):
        with pytest.raises(engine.SourceInvestigationError, match="no duration"):
            engine.parse_region({
                "region_start": "2026-08-06T12:10:00",
                "region_end": "2026-08-06T12:10:00",
            })

    def test_the_whole_track_is_returned_with_the_region_marked(
        self, miro_data, navigation
    ):
        """Context is most of what places a source: which leg it was, and
        whether the same ground was passed earlier without seeing it."""
        region = engine.parse_region({
            "region_start": "2026-08-06T12:10:00",
            "region_end": "2026-08-06T12:20:00",
        })

        track = engine.region_track(navigation, region)

        assert track["available"] is True
        assert len(track["track"]) > len(track["region"])
        assert track["bounds"]["north"] >= track["bounds"]["south"]

    def test_a_region_outside_the_navigation_says_so(self, navigation):
        region = engine.parse_region({
            "region_start": "2026-08-07T12:10:00",
            "region_end": "2026-08-07T12:20:00",
        })

        track = engine.region_track(navigation, region)

        assert track["available"] is False
        assert "not known" in track["reason"]

    def test_a_flight_without_navigation_still_says_why(self):
        region = engine.parse_region({
            "region_start": "2026-08-06T12:10:00",
            "region_end": "2026-08-06T12:20:00",
        })

        track = engine.region_track(None, region)

        assert track["available"] is False
        assert "Noseboom" in track["reason"]


class TestTheCatalogue:
    def test_it_offers_only_what_the_flight_actually_wrote(self, miro_data):
        catalogue = engine.channel_catalogue(miro_data, None)

        keys = {item["key"] for item in catalogue}
        assert "NO2 wet" in keys
        assert "SO2 wet" not in keys, "offered a channel this flight has no data for"

    def test_the_ten_species_and_the_housekeeping_are_grouped_apart(self):
        """A rise that follows the cell temperature is the instrument, and the
        two have to be selectable onto one panel to see that."""
        assert len(engine.GAS_CHANNELS) == 10
        assert set(engine.HOUSEKEEPING_CHANNELS) == {
            "T Cell C", "Outside T", "Laser housing T", "p Cell"
        }

    def test_altitude_and_navigation_are_offered_beside_the_gases(
        self, miro_data, navigation
    ):
        catalogue = engine.channel_catalogue(miro_data, navigation)

        groups = {item["key"]: item["group"] for item in catalogue}
        assert groups["altitude"] == "Navigation"
        assert groups["NO2 wet"] == "Trace gas"

    def test_a_bearing_is_flagged_as_one(self, miro_data, navigation):
        catalogue = engine.channel_catalogue(miro_data, navigation)

        entry = next(item for item in catalogue if item["key"] == "wind_dir_deg")
        assert entry["circular"] is True


class TestTheFilters:
    def test_the_row_count_is_bounded(self):
        with pytest.raises(engine.SourceInvestigationError, match="1 and"):
            engine.parse_filters({"rows": 99})

    def test_an_unknown_smoothing_is_refused(self):
        with pytest.raises(engine.SourceInvestigationError, match="Smoothing"):
            engine.parse_filters({"smoothing": "wavelet"})

    def test_the_default_window_leaves_a_short_plume_intact(self):
        """Measured: a one-second spike came through a 15 s window at under 9%
        of itself, so the default is not 15 s."""
        assert engine.DEFAULT_SMOOTHING_SECONDS <= 5
        assert engine.DEFAULT_SMOOTHING == "savgol"

    def test_a_time_filter_that_selects_nothing_says_so(self, miro_data):
        frame = engine.combined_frame(miro_data, None)
        filters = engine.parse_filters({
            "start": "2027-01-01T00:00:00", "end": "2027-01-02T00:00:00",
        })

        with pytest.raises(engine.SourceInvestigationError, match="No MIRO samples"):
            engine.build_rows(frame, filters)

    def test_an_aware_bound_is_accepted_beside_a_naive_record(self, miro_data):
        """Comparing a tz-aware bound against a naive index raises."""
        frame = engine.combined_frame(miro_data, None)
        filters = engine.parse_filters({
            "start": "2026-08-06T12:10:00+00:00",
            "end": "2026-08-06T12:20:00+00:00",
        })

        payload = engine.build_rows(frame, filters)

        assert payload["samples"] > 0

    def test_a_flight_with_no_miro_is_refused_with_the_reason(self):
        with pytest.raises(engine.SourceInvestigationError, match="Process MIRO"):
            engine.combined_frame(None, None)
