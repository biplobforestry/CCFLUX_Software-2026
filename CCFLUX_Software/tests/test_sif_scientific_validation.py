"""The bundled SIF science must reproduce the validated campaign reference.

The bundled copy of airflox_sif_automation.py had diverged from the validated
original in its timestamp handling and zero-GPS filtering. Against the
Flight_2124 reference that cost one FULL measurement, shifted every acquisition
timestamp, and put the FLUO altitude 100% out. Nothing in the dashboard caught
it, because nothing compared the output to the reference.

These cases run the bundled pipeline over the Flight_2124 raw delivery and
compare every product with the reference output, so a future edit to the
protected science fails here instead of in a campaign result. They need the
campaign disks and skip cleanly without them.
"""

import importlib.util
import sys
from argparse import Namespace
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

RAW = Path("/Volumes/external_HD/Flight_2124")
REFERENCE = Path(
    "/Volumes/Biplob/Zeppelin_System/Integration_code/Hatchbox/SIF/Flight_2124"
)
BUNDLED = Path(__file__).resolve().parents[1] / "instruments" / "sif" / "legacy"
ESSENTIALS = Path(__file__).resolve().parents[1] / "instruments" / "sif" / "essentials"

# This reference was regenerated on 2026-08-01 from the pipeline corrected
# against AIRFLOX_GUI_30.9.R. The version it replaced had the solar zenith angle
# out by 0.22 degrees, retrieved SIF out by up to 0.70 mW m-2 nm-1 sr-1, and 126
# of 448 timestamps replaced by interpolated ones; it is kept beside this one as
# Flight_2124_superseded_20260801. These cases are a regression guard against
# the corrected output - the scientific authority is
# test_sif_r_reference_validation.py, which checks against R itself.

requires_campaign_data = pytest.mark.skipif(
    not (RAW.is_dir() and REFERENCE.is_dir()),
    reason=(
        f"Needs the Flight_2124 raw delivery at {RAW} and the validated "
        f"reference at {REFERENCE}."
    ),
)

# Every product the reference carries, and the tolerance each is held to.
# Radiance and reflectance are pure spectral arithmetic and must be exact;
# the index tables accumulate a few floating-point operations more.
SPECTRAL_PRODUCTS = (
    "FLOX/Incoming_radiance_FULL_Flight_2124_FULL.csv",
    "FLOX/Reflectance_FULL_Flight_2124_FULL.csv",
    "FLOX/Reflected_radiance_FULL_Flight_2124_FULL.csv",
    "FLUO/Incoming_radiance_FLUO_Flight_2124_FLUO.csv",
    "FLUO/Reflectance_FLUO_Flight_2124_FLUO.csv",
    "FLUO/Reflected_radiance_FLUO_Flight_2124_FLUO.csv",
)
INDEX_PRODUCTS = (
    "FLOX/ALL_INDEX_AIRFLOX_FULL_Flight_2124_FULL.csv",
    "FLUO/ALL_INDEX_AIRFLOX_FLUO_Flight_2124_FLUO.csv",
)
COMBINED_RAW = (
    "_combined/Flight_2124_FULL.CSV",
    "_combined/Flight_2124_FLUO.CSV",
)


@pytest.fixture(scope="module")
def produced(tmp_path_factory):
    """Run the bundled pipeline once over the reference flight.

    The skip marks are evaluated at collection time. A campaign disk unmounted
    while the suite is running would otherwise fail this fixture with an error
    instead of skipping, so the check is repeated here.
    """
    if not (RAW.is_dir() and REFERENCE.is_dir()):
        pytest.skip(f"The campaign disks holding {RAW} are no longer mounted.")
    sys.path.insert(0, str(BUNDLED))
    spec = importlib.util.spec_from_file_location(
        "ccflux_sif_validation", BUNDLED / "airflox_sif_automation.py"
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)

    output = tmp_path_factory.mktemp("sif_validation")
    module.run_flight(
        Namespace(
            directory=RAW,
            flight_name="Flight_2124",
            output=output,
            essentials=ESSENTIALS,
            log=None,
            altitude_filter="no",
            apply_nonlinearity_correction="no",
            spectral_shift_correction="no",
            raw_min_kb=100,
            time_filter="default",
            time_start_utc=None,
            time_end_utc=None,
            platform_mode="uav_airship",
            static_lat=None,
            static_lon=None,
            static_alt=None,
        )
    )
    return output / "Flight_2124"


def _worst_relative_difference(left: pd.DataFrame, right: pd.DataFrame) -> float:
    worst = 0.0
    for column in left.select_dtypes(include=[np.number]).columns:
        a = left[column].to_numpy(float)
        b = right[column].to_numpy(float)
        usable = np.isfinite(a) & np.isfinite(b)
        if not usable.any():
            continue
        difference = np.abs(a[usable] - b[usable]) / np.maximum(
            1e-12, np.abs(b[usable])
        )
        worst = max(worst, float(difference.max()))
    return worst


@requires_campaign_data
@pytest.mark.parametrize("relative_path", SPECTRAL_PRODUCTS)
def test_spectral_products_match_the_reference(produced, relative_path):
    mine = pd.read_csv(produced / relative_path, sep=None, engine="python")
    reference = pd.read_csv(REFERENCE / relative_path, sep=None, engine="python")

    # Column labels are acquisition times: a timestamp regression shows up here
    # before it shows up in any value.
    assert list(mine.columns) == list(reference.columns)
    assert len(mine) == len(reference)
    assert _worst_relative_difference(mine, reference) == 0.0


@requires_campaign_data
@pytest.mark.parametrize("relative_path", INDEX_PRODUCTS)
def test_index_products_match_the_reference(produced, relative_path):
    mine = pd.read_csv(produced / relative_path, sep=None, engine="python")
    reference = pd.read_csv(REFERENCE / relative_path, sep=None, engine="python")

    assert list(mine.columns) == list(reference.columns)
    # One dropped measurement was the first symptom of the divergence.
    assert len(mine) == len(reference)
    assert _worst_relative_difference(mine, reference) < 1e-9


@requires_campaign_data
@pytest.mark.parametrize("relative_path", COMBINED_RAW)
def test_combined_raw_is_byte_identical(produced, relative_path):
    assert (produced / relative_path).read_bytes() == (
        REFERENCE / relative_path
    ).read_bytes()


@requires_campaign_data
def test_gis_exports_are_written_for_both_modes(produced):
    for mode, stem in (("FLOX", "FULL"), ("FLUO", "FLUO")):
        for suffix in (".shp", ".shx", ".dbf", ".prj", ".cpg"):
            asset = produced / mode / "GIS" / f"AIRFLOX_Flight_2124_{stem}{suffix}"
            assert asset.is_file(), f"missing {asset}"
            assert asset.stat().st_size > 0


def test_bundled_science_differs_from_the_original_only_where_recorded():
    """The radiometry must be untouched; only the recorded repairs may differ.

    Two CCFLUX changes are permitted in ``airflox_sif_automation.py``, both
    driven by Flight_2707 and both proven above to leave Flight_2124 identical:

    * ``retain_zero_gps`` — the AirFloX GPS row filter is skipped when position
      comes from the Noseboom/gimbal log. Flight_2707 has no fix at all, so the
      filter discarded all 380 measurements.
    * ``_record_clock_datetime`` / ``_gps_is_unusable`` — fall back to the
      instrument record clock when the GPS clock never left its 1980 power-on
      default, which otherwise placed every Flight_2707 measurement in 2080.

    Anything else diverging means the radiometry itself has been edited.
    """
    from core.legacy_paths import legacy_integration_path

    original = legacy_integration_path(
        "Hatchbox", "SIF", "scripts", "airflox_sif_automation.py"
    )
    if not original.is_file():
        pytest.skip(f"Protected original is not available at {original}")

    # Only these top-level definitions may differ. Everything else in the file
    # is radiometry and must be byte-identical to the validated original.
    MAY_DIFFER = {
        "read_drox_full",        # gained drop_zero_gps
        "process_common",        # threads retain_zero_gps
        "process_full",          # threads retain_zero_gps
        "process_fluo",          # threads retain_zero_gps
        "process_to_files",      # decides retain_zero_gps from the position source
        "get_gps_utc",           # record-clock fallback, and DateTimeFloX parsing
        "_record_clock_datetime",  # new helper
        "_gps_is_unusable",        # new helper
        # A receiver that never locks still emits GPS_TIME_UTC. On
        # Flight_CCT0803 both AirFloX channels read GPS_lat=0.00000 while
        # a few rows carried a time two hours from the record clock, and
        # judging a fix by the timestamp alone shifted the flight by
        # 7219 s. A time is now trusted only where a position was
        # reported; with none, the record clock is UTC.
        "gps_position_mask",       # new helper
        "real_gps_fix_mask",       # a fix now needs a position, not only a plausible date
        "measure_record_clock_offset",  # measured only from rows with a position
        # Corrections made after comparing against AIRFLOX_GUI_30.9.R, each one
        # proven against the R output in test_sif_r_reference_validation.py.
        "spline_gapfill_matrix",   # R smooth.spline(df=80), not a regression spline
        "nknots_smspl",            # new: R stats:::.nknots.smspl
        "_bspline_design",         # new: the smoothing-spline basis
        "_bspline_penalty",        # new: the integrated second-derivative penalty
        "_smooth_spline_basis",    # new: knot placement and the lambda search
        "_smooth_spline_predict",  # new: evaluation with R's linear extrapolation
        "stats_on_spectra",        # inclusive bounds, as in R's StatsOnSpectra
        "zenith",                  # Astronomical Almanac, not the Spencer series
        "solar",                   # new: the GUI's solar() ported exactly
        "fill_bad_gps",            # R getcoordinates() rounding and longitude mean
        "r_round_half_even",       # new helper
        "match_data",              # stable tie-break; no flooring of the AirFloX time
        # Its own body is unchanged. The splitter below attributes trailing
        # module-level lines to the preceding def, and the new
        # GETCOORDINATES_R_LONGITUDE_MEAN constant sits after it.
        "parse_gps_coord",
        # Flight_2707: the AirFloX record clock is set to campaign local time and
        # its GPS gets a fix on only part of the FLUO channel and none of FULL.
        # The offset is measured from the spectra that do have a fix and applied
        # to the rest, instead of discarding them.
        "real_gps_fix_mask",            # new helper
        "measure_record_clock_offset",  # new helper
        "_apply_record_clock_offset",   # new helper
        "probe_record_clock_offset",    # new helper
        "run_flight",                   # probes the channels before processing
        "_gps_is_unusable",             # trailing lines attributed by the splitter
    }

    mine = _top_level_definitions(BUNDLED / "airflox_sif_automation.py")
    theirs = _top_level_definitions(original)

    differing = {
        name
        for name in set(mine) | set(theirs)
        if mine.get(name) != theirs.get(name)
    }
    unexplained = sorted(differing - MAY_DIFFER)
    assert not unexplained, (
        "These definitions diverged from the validated original without being "
        f"recorded as a repair: {unexplained}"
    )


def _top_level_definitions(path: Path) -> dict[str, str]:
    """Split a module into its top-level ``def`` blocks, keyed by name."""
    definitions: dict[str, str] = {}
    name, body = "__module__", []
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.startswith("def "):
            definitions[name] = "\n".join(body)
            name = line[4:].split("(", 1)[0].strip()
            body = [line]
        else:
            body.append(line)
    definitions[name] = "\n".join(body)
    return definitions
