"""Recovering AirFloX spectra whose GPS never got a fix.

On Flight_2707 the AirFloX record clock is set to campaign local time and runs
2 h 0 m 40 s ahead of UTC, its FULL channel never acquires a GPS fix, and its
FLUO channel only acquires one after 177 spectra. Taking the record clock as
UTC put every FULL spectrum two hours past the end of the Noseboom telemetry,
so 153 of 378 FULL and 181 of 376 FLUO spectra were discarded during matching.

The instrument itself supplies the correction: on the spectra where the GPS did
get a fix, the difference between the record clock and GPS UTC is the offset.
It is measured rather than assumed, because it is not a whole timezone step -
the clock is also 40 s fast - and because FULL and FLUO are two channels of one
box that share the record clock, so the channel with a fix calibrates the one
without.

SIF, gimbal and Noseboom then sit on one UTC timeline, which is what the
campaign owner states is true of the flight data.
"""

import importlib.util
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

BUNDLED = Path(__file__).resolve().parents[1] / "instruments" / "sif" / "legacy"


@pytest.fixture(scope="module")
def airflox():
    sys.path.insert(0, str(BUNDLED))
    spec = importlib.util.spec_from_file_location(
        "ccflux_sif_flight_clock", BUNDLED / "airflox_sif_automation.py"
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _utc(text):
    return datetime.fromisoformat(text).replace(tzinfo=timezone.utc)


def test_the_1980_power_on_default_is_not_a_fix(airflox):
    record = [_utc("2026-07-27T09:43:40")]
    gps_default = [_utc("1980-01-05T00:00:41")]
    gps_real = [_utc("2026-07-27T07:43:00")]

    assert not airflox.real_gps_fix_mask(gps_default, record)[0]
    assert airflox.real_gps_fix_mask(gps_real, record)[0]


def test_a_flight_crossing_midnight_is_still_a_fix(airflox):
    """The rule is a two-day window, not a same-date check."""
    record = [_utc("2026-07-28T00:10:00")]
    gps = [_utc("2026-07-27T22:10:00")]

    assert airflox.real_gps_fix_mask(gps, record)[0]


def test_the_offset_is_measured_not_assumed(airflox):
    """Flight_2707's clock is 2 h 0 m 40 s ahead - not a whole timezone step."""
    gps = [_utc("2026-07-27T07:43:00") + timedelta(seconds=50 * i) for i in range(20)]
    record = [t + timedelta(seconds=7240) for t in gps]

    offset, count, spread = airflox.measure_record_clock_offset(gps, record)

    assert offset == pytest.approx(7240.0)
    assert count == 20
    assert spread == pytest.approx(0.0)
    assert offset != 2 * 3600, "a timezone assumption would have given 7200"


def test_rows_without_a_fix_are_ignored_when_measuring(airflox):
    gps = [_utc("1980-01-05T00:00:41"), _utc("1980-01-05T00:01:31"),
           _utc("2026-07-27T07:43:00"), _utc("2026-07-27T07:43:50")]
    record = [_utc("2026-07-27T09:43:40"), _utc("2026-07-27T09:44:30"),
              _utc("2026-07-27T09:43:40"), _utc("2026-07-27T09:44:30")]

    offset, count, _spread = airflox.measure_record_clock_offset(gps, record)

    assert count == 2, "only the rows with a genuine fix may be used"
    assert offset == pytest.approx(7240.0)


def test_no_fix_anywhere_reports_none(airflox):
    gps = [_utc("1980-01-05T00:00:41")]
    record = [_utc("2026-07-27T09:43:40")]

    offset, count, _spread = airflox.measure_record_clock_offset(gps, record)

    assert offset is None and count == 0


def test_the_offset_is_subtracted_to_reach_utc(airflox):
    """The record clock runs ahead, so UTC is behind it."""
    record = [_utc("2026-07-27T09:43:40"), pd.NaT]

    corrected = airflox._apply_record_clock_offset(record, 7240.0)

    assert corrected[0] == _utc("2026-07-27T07:43:00")
    assert pd.isna(corrected[1]), "a missing timestamp must stay missing"


def test_the_channels_share_one_record_clock(airflox):
    """FULL has no fix at all; FLUO's offset is what makes it recoverable."""
    assert isinstance(airflox.RECORD_CLOCK_OFFSET_HINT, dict)
    source = (BUNDLED / "airflox_sif_automation.py").read_text(encoding="utf-8")
    # run_flight must establish the hint before the first channel is processed,
    # because FULL is written first but FLUO is the one holding the fix.
    probe = source.index("RECORD_CLOCK_OFFSET_HINT.clear()")
    full_processing = source.index("step('full_process','running'")
    assert probe < full_processing, (
        "the record clock must be probed before FULL is processed"
    )


def test_solar_zenith_is_recomputed_from_the_noseboom_position(airflox):
    """The AirFloX reports 0.00000 with no fix, which is the Gulf of Guinea."""
    source = (BUNDLED / "airflox_sif_automation.py").read_text(encoding="utf-8")
    assert "position_usable" in source
    assert "solar zenith angle recomputed from the Noseboom position" in source

    times = [_utc("2026-07-27T05:20:12")]
    # R, at the Flight_2707 mean position and at (0, 0).
    over_the_lake = airflox.zenith(times, np.array([9.3718]), np.array([47.6499]))
    at_null_island = airflox.zenith(times, np.array([0.0]), np.array([0.0]))

    assert over_the_lake[0] == pytest.approx(77.38, abs=0.02)
    assert at_null_island[0] == pytest.approx(100.94, abs=0.02)
    assert over_the_lake[0] < 90, "the sun was up; 0/0 put it below the horizon"


def test_a_row_r_can_repair_is_left_to_r(airflox):
    """R rewrites an isolated bad row from its neighbour; that must not change.

    The correction only touches rows still implausible after R's own repair has
    run, which is what keeps the 2024-08-28 reference reproducing exactly.
    """
    source = (BUNDLED / "airflox_sif_automation.py").read_text(encoding="utf-8")
    repair = source.index("utc[i]=utc[i+1]-timedelta(seconds=diffs[i])")
    recovery = source.index("still_wrong=np.flatnonzero")
    assert repair < recovery, "R's repair must run before ours"


class TestOneTruncatedBlockDoesNotEndTheFlight:
    """R adds (CPU2-CPU1)/1000 to each acquisition time; Python's timedelta
    refuses a NaN and answers "cannot convert float NaN to integer".

    Flight_CC0807's FULL channel holds 1 310 blocks and exactly one of them has
    blank metadata fields - no CPU pair, no GPS date, no coordinates. That one
    row raised three minutes into the run and took both channels with it. The
    offset is sub-second and refines a time already known from the block, so a
    block without it keeps its own time.
    """

    class _Raw:
        def __init__(self, cpu1, cpu2):
            self.cpu1 = np.asarray(cpu1, dtype=float)
            self.cpu2 = np.asarray(cpu2, dtype=float)

    def test_a_missing_pair_becomes_no_correction(self, airflox):
        offsets = airflox.cpu_time_offsets(
            self._Raw([1000.0, np.nan, 3000.0], [1500.0, np.nan, 4000.0])
        )
        assert list(offsets) == [0.5, 0.0, 1.0]

    def test_half_a_pair_is_still_unusable(self, airflox):
        offsets = airflox.cpu_time_offsets(
            self._Raw([1000.0, np.nan], [1500.0, 2000.0])
        )
        assert list(offsets) == [0.5, 0.0]

    def test_every_offset_survives_timedelta(self, airflox):
        """The exact call that raised."""
        offsets = airflox.cpu_time_offsets(
            self._Raw([np.nan, 0.0], [np.nan, 250.0])
        )
        base = datetime(2026, 8, 7, 8, 44, 7, tzinfo=timezone.utc)
        moved = [base + timedelta(seconds=float(value)) for value in offsets]
        assert moved[0] == base
        assert moved[1] == base + timedelta(seconds=0.25)

    def test_a_good_file_is_left_alone(self, airflox, capsys):
        offsets = airflox.cpu_time_offsets(self._Raw([0.0, 1000.0], [100.0, 1100.0]))
        assert list(offsets) == [0.1, 0.1]
        assert "CPU timestamp pair" not in capsys.readouterr().out

    def test_the_operator_is_told_how_many(self, airflox, capsys):
        airflox.cpu_time_offsets(self._Raw([np.nan, 0.0], [np.nan, 100.0]))
        spoken = capsys.readouterr().out
        assert "missing or unreadable on 1 of 2 spectra" in spoken
        assert "keep their recorded time" in spoken

    def test_the_conversion_is_milliseconds(self, airflox):
        """R: CPU1sec <- (CPU2 - CPU1)/1000."""
        offsets = airflox.cpu_time_offsets(self._Raw([0.0], [7200.0]))
        assert offsets[0] == pytest.approx(7.2)

    def test_process_common_uses_the_guarded_helper(self):
        source = (BUNDLED / "airflox_sif_automation.py").read_text(encoding="utf-8")
        body = source[source.index("def process_common("):]
        body = body[: body.index("\ndef ")]
        assert "cpu_time_offsets(raw)" in body
        assert "(raw.cpu2-raw.cpu1)/1000" not in body


class TestACoordinateOffTheGlobeIsNotAPosition:
    """Flight_CC0807's FULL channel: 1 307 rows of 0.00000 and two truncated
    blocks that parse as latitude 2495 and 2601.

    Those two were the only rows that looked like a fix, so fill_bad_gps copied
    2495 into every zero row, the file reported that it had a position, the
    Noseboom recomputation never ran, and the exported solar zenith angle
    reached 156 degrees at midday. gps_position_mask already rejected them; the
    same judgement now applies where the value is used.
    """

    def test_a_real_position_is_untouched(self, airflox):
        """The R reference flight sits at 50.62 N, 6.99 E - nothing may change."""
        lat = np.array([50.6232, 50.6231, 50.6230])
        lon = np.array([6.9870, 6.9871, 6.9872])
        filled_lat, filled_lon = airflox.fill_bad_gps(lat.copy(), lon.copy())
        assert np.allclose(filled_lat, np.round(lat, 4))
        assert np.allclose(filled_lon, np.round(lon, 4))

    def test_a_good_row_still_fills_the_bad_ones(self, airflox):
        lat, lon = np.array([0.0, 50.6232]), np.array([0.0, 6.9870])
        filled_lat, _ = airflox.fill_bad_gps(lat, lon)
        assert filled_lat[0] == pytest.approx(50.6232)

    def test_an_impossible_latitude_is_cleared(self, airflox):
        lat, lon = np.array([0.0, 2495.0, 0.0]), np.array([0.0, 2522.0, 0.0])
        filled_lat, filled_lon = airflox.fill_bad_gps(lat, lon)
        assert np.isnan(filled_lat[1]) and np.isnan(filled_lon[1])
        # And it must not have been copied over the zeroes.
        assert filled_lat[0] == 0.0 and filled_lat[2] == 0.0

    def test_it_is_cleared_rather_than_only_flagged(self, airflox):
        """Flagging alone left the value in place when no good row existed."""
        filled_lat, _ = airflox.fill_bad_gps(
            np.array([0.0, 2495.0]), np.array([0.0, 2522.0])
        )
        assert 2495.0 not in list(filled_lat)

    def test_a_longitude_past_180_is_rejected_too(self, airflox):
        """Rejected, then repaired from the good row - which is the point of
        this routine. What must not survive is the 2522 itself."""
        filled_lat, filled_lon = airflox.fill_bad_gps(
            np.array([50.0, 50.0]), np.array([6.9, 2522.0])
        )
        assert 2522.0 not in list(filled_lon)
        assert filled_lon[0] == pytest.approx(6.9)
        assert filled_lon[1] == pytest.approx(6.9)

    def test_with_no_good_row_it_stays_cleared(self, airflox):
        _lat, filled_lon = airflox.fill_bad_gps(
            np.array([0.0, 50.0]), np.array([0.0, 2522.0])
        )
        assert np.isnan(filled_lon[1])

    def test_the_operator_is_told(self, airflox, capsys):
        airflox.fill_bad_gps(np.array([0.0, 2495.0]), np.array([0.0, 2522.0]))
        assert "outside the globe" in capsys.readouterr().out

    def test_a_clean_file_says_nothing(self, airflox, capsys):
        airflox.fill_bad_gps(np.array([50.6232]), np.array([6.9870]))
        assert "outside the globe" not in capsys.readouterr().out


class TestWhetherTheFileCarriesAPositionAtAll:
    """The judgement that decides if the solar zenith angle is recomputed."""

    def _usable(self, airflox, lat, lon):
        filled_lat, filled_lon = airflox.fill_bad_gps(
            np.asarray(lat, float), np.asarray(lon, float)
        )
        return bool(np.any(np.isfinite(filled_lat) & np.isfinite(filled_lon)
                           & ((filled_lat != 0) | (filled_lon != 0))))

    def test_all_zeroes_is_no_position(self, airflox):
        assert self._usable(airflox, [0.0, 0.0, 0.0], [0.0, 0.0, 0.0]) is False

    def test_a_single_nan_does_not_invent_one(self, airflox):
        """NaN is not equal to zero, so testing the two apart accepted it."""
        assert self._usable(airflox, [0.0, np.nan], [0.0, np.nan]) is False

    def test_garbage_does_not_count_as_a_position(self, airflox):
        assert self._usable(airflox, [0.0, 2495.0], [0.0, 2522.0]) is False

    def test_a_genuine_fix_does(self, airflox):
        assert self._usable(airflox, [0.0, 50.6232], [0.0, 6.9870]) is True

    def test_the_check_requires_both_on_the_same_row(self):
        source = (BUNDLED / "airflox_sif_automation.py").read_text(encoding="utf-8")
        assert "np.any(np.isfinite(lat)&np.isfinite(lon)" in source
        assert "np.isfinite(lat).any() and np.isfinite(lon).any()" not in source
