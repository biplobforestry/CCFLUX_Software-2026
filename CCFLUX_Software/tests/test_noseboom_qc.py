"""Noseboom quality control, following the campaign evaluation script.

The arithmetic is the script's: the flow-uncertainty limit test, the circular
correlation of wind direction against heading and ground track, the vertical
wind statistics, and wind compared with the nearest airport's METAR reports
inside a +/-2.5 minute window.
"""
import math
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from core.noseboom_qc import (
    AIRPORTS,
    COLUMNS,
    build_qc_payload,
    circular_correlation,
    circular_difference_degrees,
    fetch_metar,
    flow_uncertainty,
    haversine_km,
    relative_humidity_from_dewpoint,
    select_nearest_airport,
    sensor_means_at_report_times,
    vertical_wind,
)

ASSETS = Path(__file__).resolve().parents[1] / "app" / "assets"
START = datetime(2026, 8, 3, 12, 0, tzinfo=timezone.utc)


def _frame(count=600, **overrides):
    times = pd.date_range(START, periods=count, freq="1s", tz="UTC")
    data = {
        "_time": times,
        COLUMNS["alpha"]: np.full(count, 0.2),
        COLUMNS["beta"]: np.full(count, 0.2),
        COLUMNS["latitude"]: np.full(count, 51.29),
        COLUMNS["longitude"]: np.full(count, 6.77),
        COLUMNS["wind_speed"]: np.full(count, 5.0),
        COLUMNS["wind_direction"]: np.full(count, 180.0),
        COLUMNS["vertical_wind"]: np.linspace(-1, 1, count),
        COLUMNS["ned_north"]: np.full(count, 10.0),
        COLUMNS["ned_east"]: np.full(count, 0.0),
        COLUMNS["ned_down"]: np.zeros(count),
    }
    data.update(overrides)
    return pd.DataFrame(data)


class TestAirportSelection:
    def test_the_nearest_configured_airport_is_chosen(self):
        """A flight over Duesseldorf must not be validated against Groningen."""
        chosen = select_nearest_airport(pd.Series([51.29]), pd.Series([6.77]))
        assert chosen["icao"] == "EDDL"
        assert chosen["distance_km"] < 5

    def test_a_flight_over_the_ruhr_picks_dortmund(self):
        chosen = select_nearest_airport(pd.Series([51.52]), pd.Series([7.61]))
        assert chosen["icao"] == "EDLW"

    def test_a_flight_over_the_netherlands_picks_a_dutch_airport(self):
        chosen = select_nearest_airport(pd.Series([52.31]), pd.Series([4.76]))
        assert chosen["icao"] == "EHAM"

    def test_the_airport_is_always_named(self):
        chosen = select_nearest_airport(pd.Series([51.29]), pd.Series([6.77]))
        assert chosen["name"] == "Duesseldorf Airport"

    def test_alternatives_are_reported_for_context(self):
        chosen = select_nearest_airport(pd.Series([51.29]), pd.Series([6.77]))
        assert len(chosen["alternatives"]) == 5
        assert chosen["alternatives"][0]["distance_km"] >= chosen["distance_km"]

    def test_a_flight_without_a_position_selects_nothing(self):
        assert select_nearest_airport(pd.Series([np.nan]), pd.Series([np.nan])) is None

    def test_every_airport_is_in_germany_or_the_netherlands(self):
        for icao in AIRPORTS:
            assert icao.startswith(("ED", "EH"))


class TestGeometry:
    def test_haversine_matches_a_known_separation(self):
        """Duesseldorf to Cologne Bonn is about 55 km."""
        distance = haversine_km(51.2895, 6.7668, 50.8659, 7.1427)
        assert 50 < distance < 60

    def test_a_point_is_no_distance_from_itself(self):
        assert haversine_km(51.0, 6.0, 51.0, 6.0) == pytest.approx(0.0)

    @pytest.mark.parametrize("first,second,expected", [
        (10.0, 350.0, 20.0), (350.0, 10.0, -20.0), (180.0, 180.0, 0.0),
    ])
    def test_direction_differences_wrap_through_north(self, first, second, expected):
        assert circular_difference_degrees(
            np.array([first]), np.array([second])
        )[0] == pytest.approx(expected)


class TestCircularCorrelation:
    def test_a_series_correlates_with_itself(self):
        values = np.array([10.0, 80.0, 190.0, 300.0, 45.0])
        assert circular_correlation(values, values) == pytest.approx(1.0, abs=1e-9)

    def test_a_constant_series_has_no_correlation(self):
        assert math.isnan(circular_correlation(np.full(5, 90.0), np.arange(5.0)))

    def test_too_few_points_is_not_a_number(self):
        assert math.isnan(circular_correlation(np.array([10.0]), np.array([20.0])))


class TestFlowUncertainty:
    def test_alpha_and_beta_at_ninety_are_outside_the_limits(self):
        frame = _frame(100)
        frame.loc[:24, COLUMNS["alpha"]] = 90.0
        frame.loc[:24, COLUMNS["beta"]] = 90.0
        section = flow_uncertainty(frame, frame["_time"])
        assert section["samples_at_limit"] == 25
        assert section["percentage_at_limit"] == pytest.approx(25.0)

    def test_one_angle_at_ninety_is_not_outside_the_limits(self):
        """The check is both angles together, as the script writes it."""
        frame = _frame(100)
        frame.loc[:49, COLUMNS["alpha"]] = 90.0
        assert flow_uncertainty(frame, frame["_time"])["samples_at_limit"] == 0

    def test_the_series_is_bounded_for_a_browser(self):
        frame = _frame(20_000)
        section = flow_uncertainty(frame, frame["_time"])
        assert len(section["time"]) <= 4002


class TestVerticalWind:
    def test_the_statistics_describe_the_recorded_series(self):
        frame = _frame(101, **{COLUMNS["vertical_wind"]: np.linspace(-1, 1, 101)})
        section = vertical_wind(frame, frame["_time"])
        assert section["mean"] == pytest.approx(0.0, abs=1e-9)
        assert section["minimum"] == pytest.approx(-1.0)
        assert section["maximum"] == pytest.approx(1.0)

    def test_a_missing_series_is_reported_not_invented(self):
        frame = _frame(50, **{COLUMNS["vertical_wind"]: np.full(50, np.nan)})
        assert vertical_wind(frame, frame["_time"])["available"] is False


class TestReportMatching:
    def test_only_samples_inside_the_window_are_averaged(self):
        times = pd.Series(pd.date_range(START, periods=600, freq="1s", tz="UTC"))
        values = np.arange(600.0)
        means, counts = sensor_means_at_report_times(
            times, values, [START + timedelta(seconds=300)]
        )
        # +/-150 s around second 300 covers seconds 150..450 inclusive.
        assert counts[0] == 301
        assert means[0] == pytest.approx(300.0)

    def test_a_report_with_no_samples_nearby_stays_unmatched(self):
        times = pd.Series(pd.date_range(START, periods=10, freq="1s", tz="UTC"))
        means, counts = sensor_means_at_report_times(
            times, np.arange(10.0), [START + timedelta(hours=5)]
        )
        assert counts[0] == 0 and math.isnan(means[0])

    def test_directions_are_averaged_through_north(self):
        """350 and 10 degrees average to due north, not to 180."""
        times = pd.Series(pd.date_range(START, periods=2, freq="1s", tz="UTC"))
        means, _ = sensor_means_at_report_times(
            times, np.array([350.0, 10.0]), [START], circular=True
        )
        # The script leaves the result on [0, 360], where 360 and 0 are north.
        assert min(means[0] % 360.0, 360.0 - (means[0] % 360.0)) == pytest.approx(0.0, abs=1e-6)


class TestHumidity:
    def test_saturated_air_reads_one_hundred_percent(self):
        value = relative_humidity_from_dewpoint(pd.Series([15.0]), pd.Series([15.0]))
        assert value.iloc[0] == pytest.approx(100.0)

    def test_a_drier_dewpoint_lowers_the_humidity(self):
        value = relative_humidity_from_dewpoint(pd.Series([20.0]), pd.Series([10.0]))
        assert 50 < value.iloc[0] < 60


class TestPayload:
    def _payload(self, frame=None, metar=None):
        def fetch(icao, start, end):
            return (metar if metar is not None else pd.DataFrame()), {"error": None}

        return build_qc_payload(frame if frame is not None else _frame(), fetch=fetch)

    def test_every_section_the_workspace_shows_is_present(self):
        payload = self._payload()
        for key in ("flow_uncertainty", "direction_heading_track", "vertical_wind",
                    "wind_speed_validation", "wind_direction_validation", "metar"):
            assert key in payload

    def test_a_north_bound_track_is_zero_degrees(self):
        section = self._payload()["direction_heading_track"]
        assert section["track"][0] == pytest.approx(0.0)
        assert section["mean_ground_speed_mps"] == pytest.approx(10.0)

    def test_the_comparison_reports_bias_and_error(self):
        metar = pd.DataFrame({
            "time": [START + timedelta(seconds=100)],
            "wind_speed_mps": [4.0],
            "wind_direction_deg": [170.0],
        })
        payload = self._payload(metar=metar)
        speed = payload["wind_speed_validation"]
        assert speed["matched_reports"] == 1
        assert speed["bias"] == pytest.approx(1.0)
        direction = payload["wind_direction_validation"]
        assert direction["bias"] == pytest.approx(10.0)

    def test_no_report_leaves_the_comparison_empty_rather_than_guessed(self):
        payload = self._payload()
        assert payload["wind_speed_validation"]["matched_reports"] == 0
        assert payload["wind_speed_validation"]["bias"] is None

    def test_an_undated_window_is_refused(self):
        frame = _frame(5)
        frame["_time"] = pd.NaT
        assert build_qc_payload(frame, fetch=lambda *a: (pd.DataFrame(), {}))["available"] is False


class TestNetworkFailureIsSurvivable:
    def test_a_download_that_fails_reports_why(self):
        def broken(request, timeout=None):
            raise TimeoutError("The read operation timed out")

        metar, metadata = fetch_metar("EDDL", START, START + timedelta(hours=1), opener=broken)
        assert metar.empty
        assert "TimeoutError" in metadata["error"]


class TestWorkspaceSection:
    markup = (ASSETS / "noseboom.html").read_text(encoding="utf-8")
    script = (ASSETS / "noseboom.js").read_text(encoding="utf-8")

    def test_the_tab_sits_after_frequency(self):
        frequency = self.markup.index('data-stat="freq"')
        qc = self.markup.index('data-stat="qc"')
        altitude = self.markup.index('data-stat="alt"')
        assert frequency < qc < altitude

    def test_the_section_has_three_rows(self):
        rows = re.findall(r'<div class="qc-row (one|two)">', self.markup)
        assert rows == ["one", "two", "two"]

    @pytest.mark.parametrize("plot", [
        "qcFlow", "qcDirection", "qcVertical", "qcWindSpeed", "qcWindDirection",
    ])
    def test_each_plot_has_a_home(self, plot):
        assert f'id="{plot}"' in self.markup

    def test_the_rows_collapse_on_a_narrow_screen(self):
        assert "@media(max-width:1000px){.qc-row.two{grid-template-columns:1fr}}" in self.markup

    def test_the_plots_are_responsive(self):
        assert "responsive:true" in self.script
        assert "#qcView .js-plotly-plot" in self.script

    def test_the_airport_is_named_on_the_validation_plots(self):
        assert "airport.icao" in self.script and "airport.name" in self.script


class TestExportFigure:
    source = (Path(__file__).resolve().parents[1] / "app" / "noseboom_statistics_export.py").read_text(encoding="utf-8")

    def test_the_figure_is_seven_inches_wide(self):
        assert "figsize=(7, 8.4)" in self.source

    def test_the_smallest_type_is_eight_point(self):
        """Eight point is the floor for a figure that goes into a manuscript."""
        sizes = [int(value) for value in re.findall(r"fontsize=(\d+)", self.source)]
        assert sizes and min(sizes) >= 8

    def test_every_requested_format_is_written(self):
        assert "for index, output_format in enumerate(formats)" in self.source
        assert "figure.savefig(path, format=output_format, dpi=dpi" in self.source

    def test_the_reference_airport_is_named_on_the_figure(self):
        assert "reference {airport.get('icao')}" in self.source
