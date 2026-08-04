"""Straight-flight legs by the moving-window stability test.

The criteria and their arithmetic come from make_straight_leg_maps.py: ground
speed, circular heading standard deviation, heading rate, roll, the altitude
range inside the window, and vertical speed. Consecutive samples meeting all
of them are one leg, and a leg is kept when it lasts long enough.
"""
import re
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from app.scan_backend import STRAIGHT_LEG_SETTINGS
from instruments.noseboom.legacy_bridge import LegacyNoseboomBridge

ASSETS = Path(__file__).resolve().parents[1] / "app" / "assets"
MODULE = LegacyNoseboomBridge().module
RETIRED = (
    "min_speed_mps", "max_turn_rate_dps", "max_roll_deg",
    "max_heading_range_deg", "min_leg_seconds", "min_leg_distance_m",
    "target_leg_distance_m", "max_leg_heading_drift_deg",
    "max_cross_track_m", "max_altitude_deviation_m",
)


def _table(count=300, *, heading=90.0, speed=12.0, roll=0.0,
           altitude=300.0, vertical=0.0):
    index = pd.date_range("2026-08-03T12:00:00Z", periods=count, freq="1s")
    return pd.DataFrame(
        {
            "plot_lat": 51.4 + np.arange(count) * 1e-5,
            "plot_lon": 6.9 + np.arange(count) * 1e-5,
            "altitude_m": np.full(count, altitude, dtype=float),
            "ground_speed_mps": np.full(count, speed, dtype=float),
            "heading_deg": np.full(count, heading, dtype=float),
            "roll_deg": np.full(count, roll, dtype=float),
            "vertical_speed_mps": np.full(count, vertical, dtype=float),
            "wind_mps": np.full(count, 5.0),
        },
        index=index,
    )


class TestDefaults:
    def test_the_eight_criteria_are_the_configured_ones(self):
        assert set(MODULE.STRAIGHT_DEFAULTS) == set(STRAIGHT_LEG_SETTINGS)

    @pytest.mark.parametrize("key,value", [
        ("minimum_ground_speed_mps", 8.0),
        ("minimum_segment_duration_s", 60),
        ("heading_window_s", 30),
        ("maximum_heading_std_deg", 10.0),
        ("maximum_heading_rate_dps", 3.0),
        ("maximum_roll_angle_deg", 10.0),
        ("maximum_altitude_range_m", 100.0),
        ("maximum_vertical_speed_mps", 2.2),
    ])
    def test_each_default(self, key, value):
        assert MODULE.STRAIGHT_DEFAULTS[key] == value

    @pytest.mark.parametrize("key", RETIRED)
    def test_no_superseded_setting_survives(self, key):
        assert key not in MODULE.STRAIGHT_DEFAULTS
        assert key not in STRAIGHT_LEG_SETTINGS


class TestHeadingStandardDeviation:
    def test_a_constant_heading_is_perfectly_steady(self):
        value = MODULE.heading_std_deg(pd.Series(np.full(60, 90.0)), 30)
        assert float(np.nanmax(value)) == pytest.approx(0.0, abs=1e-6)

    def test_headings_either_side_of_north_do_not_read_as_a_half_turn(self):
        """359 and 1 degrees are two degrees apart, not 358."""
        series = pd.Series(np.tile([359.0, 1.0], 30))
        assert float(np.nanmax(MODULE.heading_std_deg(series, 30))) < 5.0

    def test_a_scattered_heading_is_not_steady(self):
        series = pd.Series(np.linspace(0.0, 180.0, 60))
        assert float(np.nanmax(MODULE.heading_std_deg(series, 30))) > 20.0


class TestDetection:
    def _legs(self, frame, params=None):
        return MODULE.detect_straight(frame, params).attrs["straight_metrics"]

    def test_steady_flight_is_one_leg(self):
        legs = self._legs(_table(300))
        assert len(legs) == 1
        # The first sample has no predecessor, so no heading rate, and cannot
        # be judged; the leg runs from the second sample to the last.
        assert legs[0]["duration_s"] == pytest.approx(298.0)

    def test_flight_too_slow_is_no_leg(self):
        assert self._legs(_table(300, speed=5.0)) == []

    def test_flight_too_short_is_no_leg(self):
        assert self._legs(_table(45)) == []

    def test_banking_is_no_leg(self):
        assert self._legs(_table(300, roll=15.0)) == []

    def test_climbing_faster_than_the_limit_is_no_leg(self):
        assert self._legs(_table(300, vertical=3.0)) == []

    def test_a_turn_breaks_the_leg(self):
        frame = _table(400)
        # A 90 degree turn taken over ten seconds, well above 3 deg/s.
        frame.iloc[200:210, frame.columns.get_loc("heading_deg")] = np.linspace(90, 180, 10)
        frame.iloc[210:, frame.columns.get_loc("heading_deg")] = 180.0
        legs = self._legs(frame)
        assert len(legs) == 2

    def test_an_altitude_change_beyond_the_window_range_is_rejected(self):
        """The criterion is the spread inside the window, not across the leg:
        a slow drift over ten minutes is level flight, a fast climb is not."""
        gentle = _table(300)
        gentle["altitude_m"] = np.linspace(300, 800, 300)  # 50 m per window
        assert len(self._legs(gentle)) == 1

        steep = _table(300)
        steep["altitude_m"] = np.linspace(300, 3300, 300)  # 300 m per window
        assert self._legs(steep) == []

    def test_a_gap_in_the_record_ends_a_leg(self):
        """Samples either side of a gap were not one continuous transect."""
        first, second = _table(120), _table(120)
        second.index = second.index + pd.Timedelta(seconds=600)
        legs = self._legs(pd.concat([first, second]))
        assert len(legs) == 2

    def test_the_operator_can_raise_a_threshold(self):
        assert self._legs(_table(300), {"minimum_ground_speed_mps": 20.0}) == []

    def test_the_operator_can_lower_a_threshold(self):
        assert len(self._legs(_table(300, speed=5.0), {"minimum_ground_speed_mps": 4.0})) == 1

    def test_a_leg_reports_the_criteria_it_was_judged_by(self):
        leg = self._legs(_table(300))[0]
        for key in ("median_heading_std_deg", "max_heading_rate_dps",
                    "max_abs_roll_deg", "altitude_range_m",
                    "max_abs_vertical_speed_mps", "distance_km",
                    "duration_s", "mean_heading_deg", "mean_speed_mps"):
            assert key in leg

    def test_distance_comes_from_consecutive_positions(self):
        leg = self._legs(_table(300))[0]
        assert 0.3 < leg["distance_km"] < 0.6


class TestMethodsStatement:
    script = (ASSETS / "noseboom.js").read_text(encoding="utf-8")
    markup = (ASSETS / "noseboom.html").read_text(encoding="utf-8")

    def test_the_button_is_on_the_page(self):
        assert 'id="methodsBtn"' in self.markup
        assert "byId('methodsBtn').onclick=showMethods" in self.script

    def test_the_statement_reads_the_settings_in_force(self):
        """Copying it must quote the thresholds that produced these legs."""
        assert "payload?.data?.straight_settings" in self.script
        for key in STRAIGHT_LEG_SETTINGS:
            assert f"value('{key}')" in self.script

    def test_the_wording_is_the_agreed_statement(self):
        for phrase in (
            "moving-window stability test",
            "1 Hz resampled noseboom/INS data",
            "circular heading standard deviation",
            "merged into one straight-flight",
            "Airflow_UTCcorr_Nanoseconds_ns",
        ):
            assert phrase in self.script

    def test_the_leg_popup_reports_the_new_criteria(self):
        assert "leg.heading_std_deg" in self.script
        assert "leg.max_roll_deg" in self.script
        assert "heading_drift_deg" not in self.script
        assert "max_cross_track_m" not in self.script

    def test_the_wind_rose_still_opens_from_a_leg(self):
        assert "windRoseSvg(leg.windSamples)" in self.script
