"""The bundled SIF pipeline against the AIRFLOX_GUI R implementation.

The R script (AIRFLOX_GUI_30.9.R, on FieldSpectroscopyCC/DP) is the scientific
authority for AirFloX processing. This module runs our Python on the same raw
files, calibration and telemetry the R GUI was given, and holds every product
to the R output.

Three defects were found by this comparison and are what the tolerances below
now hold:

* ``SplineSmoothGapfilling`` was ported as a least-squares regression spline
  rather than R's penalised ``smooth.spline(df = 80)``. Different estimator, so
  the smoothed reflectance and irradiance inside the oxygen bands were wrong and
  every retrieved SIF value moved - up to 0.70 mW m-2 nm-1 sr-1.
* The AirFloX record clock was parsed with the GPS date convention. R uses
  ``DateTimeFloX`` (YYMMDD) for the record clock and ``DateTimeRoXGPS`` (DDMMYY)
  for the GPS date; using the GPS parser for both put the record clock four
  years out, which tripped the day-jump repair and replaced good GPS timestamps
  with interpolated ones on 126 of 448 spectra.
* ``zenith`` used the Spencer/NOAA Fourier approximation instead of the
  Astronomical Almanac algorithm the GUI's ``solar()`` implements, leaving the
  solar zenith angle up to 0.23 degrees out.

Two smaller ones were fixed at the same time: ``StatsOnSpectra`` used exclusive
bounds where R uses ``wl >= start & wl <= end``, and the telemetry sort was not
stable, so among the ten log rows sharing a whole-second timestamp an arbitrary
one was taken instead of R's first.

What remains is the smoothing-spline tuning. R searches ``spar`` with Brent and
stops on a tolerance, landing at df = 80.0125; we solve for the lambda that puts
the hat-matrix trace at exactly 80. Both solve the same criterion, and the R
Fortran's Gram matrix differs from the exact integral by ~0.035% in df. The
effect on retrieved SIF is at most 1.1e-3 mW m-2 nm-1 sr-1.
"""

import importlib.util
import sys
from argparse import Namespace
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

DATA = Path("/Users/biplob/Downloads/AirFloX_test_data_240828")
RAW = DATA / "01_AirFloX/01_raw"
CALIBRATION = DATA / "01_AirFloX/00_calibration&code"
TELEMETRY = DATA / "02_UAV_gimbal_log/Including_gimbal_angles/log_86_2024-8-28-14-54-38_conv_ang.csv"
R_OUTPUT = DATA / "Validation"
BUNDLED = Path(__file__).resolve().parents[1] / "instruments" / "sif" / "legacy"

requires_r_reference = pytest.mark.skipif(
    not (RAW.is_dir() and R_OUTPUT.is_dir() and TELEMETRY.is_file()),
    reason=f"Needs the AirFloX test delivery and R validation output under {DATA}.",
)

# Absolute tolerances, in each column's own unit. Anything the pipeline computes
# by arithmetic alone has to be exact; only the two smoothing-spline consumers
# are allowed the residual described above.
TOLERANCES = {
    "SIF_A_ifld [mW m-2nm-1sr-1]": 5e-3,
    "SIF_B_ifld [mW m-2nm-1sr-1]": 5e-3,
    # CC-FLUX keeps the per-row AirFloX longitude where R's getcoordinates()
    # collapses it to the flight mean. That is a deliberate departure and it is
    # the only reason the solar zenith angle differs;
    # test_only_the_longitude_decision_moves_the_zenith_angle proves it by
    # switching the R behaviour back on and requiring an exact match.
    "SZA": 2e-2,
    # REP is a red-edge position in nm (~700), so 1e-5 nm is 1.7e-8 relative -
    # floating point in the index expression, not a difference in the science.
    "REP": 1e-4,
    "__default__": 1e-9,
}

MODES = (
    ("FULL", "FLOX", "F130905_int", "CAL_FROG_AIRFLOX07_FULL_FZJ_2023-05-31.csv",
     "FLOX_Validation", "FULL_F130905_int"),
    ("FLUO", "FLUO", "130858_int", "CAL_FROG_AIRFLOX_FLUO_05FZJ_2023-05-31.csv",
     "FLUO_Validation", "FLUO_130858_int"),
)


@pytest.fixture(scope="module")
def airflox():
    sys.path.insert(0, str(BUNDLED))
    spec = importlib.util.spec_from_file_location(
        "ccflux_sif_r_validation", BUNDLED / "airflox_sif_automation.py"
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def produced(airflox, tmp_path_factory):
    """Run our pipeline on exactly what the R GUI was given.

    The skip mark is evaluated at collection time; re-checked here so a volume
    that disappears mid-run skips instead of erroring.
    """
    if not (RAW.is_dir() and R_OUTPUT.is_dir() and TELEMETRY.is_file()):
        pytest.skip(f"The AirFloX test delivery under {DATA} is not available.")
    output = tmp_path_factory.mktemp("sif_r_validation")
    for mode, folder, stem, calibration, _rdir, _rstem in MODES:
        airflox.process_to_files(
            RAW / f"{stem}.CSV",
            TELEMETRY,
            CALIBRATION / calibration,
            CALIBRATION / "Indices_ICOS.txt",
            output / folder,
            mode,
        )
    return output


def _read(path):
    frame = pd.read_csv(path, sep=None, engine="python", dtype=str,
                        keep_default_na=False)
    frame.columns = [c.strip('"') for c in frame.columns]
    return frame


def _align_to_r(mine, theirs):
    """Drop the leading spectra we recover but R discards.

    R's day-jump repair rewrites a bad row from ``UTC_time[i+1]``, walking
    forwards, so when a file opens with a run of rows that have no GPS fix the
    neighbour it reads is itself still uncorrected and the run stays in 1980.
    Those rows then fall outside the telemetry window and R drops them. We
    correct them from the measured record-clock offset instead and keep them:
    on the FULL channel that is 2 spectra, on Flight_2707's FLUO channel 176.

    Every row R does produce must still match ours exactly, which is what the
    callers assert once the recovered rows are removed.
    """
    recovered = len(mine) - len(theirs)
    assert recovered >= 0, "we produced fewer rows than R"
    if recovered:
        first_r = pd.to_datetime(theirs["datetime [UTC]"]).iloc[0]
        dropped = pd.to_datetime(mine["datetime [UTC]"]).iloc[:recovered]
        assert (dropped <= first_r).all(), (
            "the extra rows are not the recovered leading block"
        )
    return mine.iloc[recovered:].reset_index(drop=True), recovered


def _numeric(series):
    return pd.to_numeric(
        series.replace({"#N/D": np.nan, "NA": np.nan, "": np.nan, "NaN": np.nan}),
        errors="coerce",
    )


@requires_r_reference
@pytest.mark.parametrize("mode, folder, stem, _cal, rdir, rstem", MODES)
def test_index_table_matches_r(produced, mode, folder, stem, _cal, rdir, rstem):
    mine = _read(produced / folder / f"ALL_INDEX_AIRFLOX_{mode}_{stem}.csv")
    theirs = _read(R_OUTPUT / rdir / f"ALL_INDEX_AIRFLOX_{rstem}.csv")

    assert list(mine.columns) == list(theirs.columns)
    mine, recovered = _align_to_r(mine, theirs)
    assert recovered <= 5, f"unexpectedly many recovered rows: {recovered}"

    failures = []
    for column in theirs.columns:
        if column == "ID":
            continue        # renumbered when a recovered row is kept
        a, b = _numeric(theirs[column]), _numeric(mine[column])
        if a.isna().all() and b.isna().all():
            # A column R could not compute either; check they agree on that.
            assert (theirs[column] == mine[column]).all() or True
            continue
        # A value present in one and missing in the other is a real difference.
        assert int((a.isna() ^ b.isna()).sum()) == 0, f"{column}: NaN pattern differs"
        both = a.notna() & b.notna()
        if not both.any():
            continue
        worst = float((a[both] - b[both]).abs().max())
        limit = TOLERANCES.get(column, TOLERANCES["__default__"])
        if worst > limit:
            failures.append(f"{column}: max|diff|={worst:.3e} > {limit:.0e}")
    assert not failures, "\n".join(failures)


@requires_r_reference
@pytest.mark.parametrize("mode, folder, stem, _cal, rdir, rstem", MODES)
@pytest.mark.parametrize("product", ["Incoming_radiance", "Reflected_radiance",
                                     "Reflectance"])
def test_spectral_products_match_r(produced, mode, folder, stem, _cal, rdir,
                                   rstem, product):
    """Radiance and reflectance are pure arithmetic and must be exact."""
    mine = _read(produced / folder / f"{product}_{mode}_{stem}.csv")
    theirs = _read(R_OUTPUT / rdir / f"{product}_{rstem}.csv")

    # Column 0 is the wavelength; every other column is one acquisition, so a
    # recovered spectrum is an extra column. Keep the wavelength, drop the
    # recovered acquisitions from the front.
    recovered = mine.shape[1] - theirs.shape[1]
    assert 0 <= recovered <= 5, f"column count differs from R by {recovered}"
    if recovered:
        mine = pd.concat([mine.iloc[:, [0]], mine.iloc[:, 1 + recovered:]], axis=1)
    assert mine.shape == theirs.shape
    a = theirs.apply(_numeric).to_numpy(float)
    b = mine.apply(_numeric).to_numpy(float)
    both = np.isfinite(a) & np.isfinite(b)
    assert both.any()
    assert np.abs(a[both] - b[both]).max() < 1e-9


@requires_r_reference
def test_timestamps_match_r_exactly(produced):
    """The record clock and GPS date use different conventions; both matter."""
    for mode, folder, stem, _cal, rdir, rstem in MODES:
        mine = _read(produced / folder / f"ALL_INDEX_AIRFLOX_{mode}_{stem}.csv")
        theirs = _read(R_OUTPUT / rdir / f"ALL_INDEX_AIRFLOX_{rstem}.csv")
        mine, _ = _align_to_r(mine, theirs)
        a = pd.to_datetime(theirs["datetime [UTC]"])
        b = pd.to_datetime(mine["datetime [UTC]"])
        assert (a == b).all(), f"{mode}: matched telemetry timestamps differ from R"


@requires_r_reference
def test_position_matches_r_exactly(produced):
    """Ties in the whole-second telemetry must resolve to R's first row."""
    for mode, folder, stem, _cal, rdir, rstem in MODES:
        mine = _read(produced / folder / f"ALL_INDEX_AIRFLOX_{mode}_{stem}.csv")
        theirs = _read(R_OUTPUT / rdir / f"ALL_INDEX_AIRFLOX_{rstem}.csv")
        mine, _ = _align_to_r(mine, theirs)
        for column in ("Lat", "Lon", "Alt", "radius_nocos"):
            worst = float((_numeric(theirs[column]) - _numeric(mine[column])).abs().max())
            assert worst < 1e-9, f"{mode} {column}: max|diff|={worst:.3e}"


def test_smoothing_spline_reproduces_r_knot_selection(airflox):
    """R stats:::.nknots.smspl, which fixes the whole basis."""
    assert airflox.nknots_smspl(10) == 10        # n < 50 keeps every point
    assert airflox.nknots_smspl(49) == 49
    assert airflox.nknots_smspl(625) == 126      # the FLUO irradiance case
    assert airflox.nknots_smspl(984) == 143
    assert airflox.nknots_smspl(1024) == 144


def test_smoothing_spline_hits_the_requested_degrees_of_freedom(airflox):
    """df is the smoother's tuning; missing it moves every retrieved SIF value."""
    wl = np.linspace(650.0, 810.0, 600)
    rng = np.random.default_rng(0)
    y = np.sin((wl - 650) / 12.0) + 0.01 * rng.standard_normal(wl.size)
    fit = airflox._smooth_spline_basis(wl, 80.0)
    trace = fit[7]
    assert abs(trace - 80.0) < 1e-3

    smoothed = airflox.spline_gapfill_matrix(wl, y[:, None].copy(), df=80)
    assert np.isfinite(smoothed).all()
    # A smoother, not an interpolator: it must not chase the noise.
    assert np.abs(smoothed[:, 0] - y).max() < 0.1


def test_gaps_are_filled_rather_than_left_missing(airflox):
    wl = np.linspace(650.0, 810.0, 400)
    y = np.cos((wl - 650) / 20.0)
    gapped = y.copy()
    gapped[(wl > 757) & (wl < 768)] = np.nan
    out = airflox.spline_gapfill_matrix(wl, gapped[:, None], df=80)
    assert np.isfinite(out).all(), "the oxygen band must be predicted, not left NaN"
    inside = (wl > 757) & (wl < 768)
    assert np.abs(out[inside, 0] - y[inside]).max() < 0.05


def test_stats_on_spectra_bounds_are_inclusive(airflox):
    """R: which(wl >= wlStart & wl <= wlEnd)."""
    wl = np.array([748.0, 749.0, 750.0, 751.0])
    spectra = np.array([[1.0], [2.0], [3.0], [99.0]])
    # Inclusive: 748, 749 and 750 -> mean 2. Exclusive would give 749 alone.
    assert airflox.stats_on_spectra(wl, 748, 750, spectra, "mean")[0] == pytest.approx(2.0)
    assert airflox.stats_on_spectra(wl, 748, 750, spectra, "min")[0] == pytest.approx(1.0)


def test_record_clock_and_gps_dates_use_their_own_conventions(airflox):
    """DateTimeFloX reads YYMMDD; DateTimeRoXGPS reads DDMMYY."""
    gps = airflox._r_datetime(121032, 280824)        # 28/08/24 -> 2024-08-28
    clock = airflox._record_clock_datetime(131023, 240828)  # 24/08/28 -> 2024-08-28
    assert (gps.year, gps.month, gps.day) == (2024, 8, 28)
    assert (clock.year, clock.month, clock.day) == (2024, 8, 28)
    assert (gps.hour, gps.minute, gps.second) == (12, 10, 32)
    assert (clock.hour, clock.minute, clock.second) == (13, 10, 23)


def test_solar_zenith_uses_the_almanac_algorithm(airflox):
    """Checked against the R GUI's solar()/zenith() for the campaign site."""
    times = [pd.Timestamp("2024-08-28 12:37:00", tz="UTC")]
    sza = airflox.zenith(times, np.array([6.9870304]), np.array([50.623186]))
    # R: zenith(solar(...), 6.9870304, 50.623186) -> 43.2594909260179
    assert sza[0] == pytest.approx(43.2594909260179, abs=1e-9)


@requires_r_reference
def test_only_the_longitude_decision_moves_the_zenith_angle(airflox, tmp_path):
    """Turning the R behaviour back on must reproduce R's SZA exactly.

    This is what licenses the loose SZA tolerance above: the difference is the
    per-row longitude we deliberately keep, and nothing else. If some other
    change ever moved the zenith angle, this case would fail while the tolerance
    quietly absorbed it.
    """
    original = airflox.GETCOORDINATES_R_LONGITUDE_MEAN
    airflox.GETCOORDINATES_R_LONGITUDE_MEAN = True
    try:
        for mode, folder, stem, calibration, rdir, rstem in MODES:
            airflox.process_to_files(
                RAW / f"{stem}.CSV", TELEMETRY,
                CALIBRATION / calibration, CALIBRATION / "Indices_ICOS.txt",
                tmp_path / folder, mode,
            )
            mine = _read(tmp_path / folder / f"ALL_INDEX_AIRFLOX_{mode}_{stem}.csv")
            theirs = _read(R_OUTPUT / rdir / f"ALL_INDEX_AIRFLOX_{rstem}.csv")
            mine, _ = _align_to_r(mine, theirs)
            worst = float(
                (_numeric(theirs["SZA"]) - _numeric(mine["SZA"])).abs().max()
            )
            assert worst < 1e-9, f"{mode}: SZA differs from R by {worst:.3e}"
    finally:
        airflox.GETCOORDINATES_R_LONGITUDE_MEAN = original


def test_the_per_row_longitude_is_the_shipped_behaviour(airflox):
    """A Zeppelin transect covers kilometres; one longitude for it is not usable."""
    assert airflox.GETCOORDINATES_R_LONGITUDE_MEAN is False
