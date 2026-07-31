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
    """Run the bundled pipeline once over the reference flight."""
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


def test_bundled_science_matches_the_protected_original():
    """The bundled copy must stay identical to the validated source.

    Only ``noseboom_gimbal_for_sif.py`` carries CCFLUX changes, and those are
    confined to file discovery and timestamp parsing — each is marked in place.
    ``airflox_sif_automation.py`` holds the radiometry and must never diverge.
    """
    from core.legacy_paths import legacy_integration_path

    original = legacy_integration_path(
        "Hatchbox", "SIF", "scripts", "airflox_sif_automation.py"
    )
    if not original.is_file():
        pytest.skip(f"Protected original is not available at {original}")
    assert (
        BUNDLED / "airflox_sif_automation.py"
    ).read_bytes() == original.read_bytes()
