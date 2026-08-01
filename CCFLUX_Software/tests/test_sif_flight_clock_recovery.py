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
