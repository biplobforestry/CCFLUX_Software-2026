"""FLIR Level 2: official radiometry, correction inputs, and georeferencing."""

from pathlib import Path

import numpy as np
import pytest

from app.scan_backend import DEFAULT_FLIR_LEVEL2_OPTIONS, DashboardScanBackend
from instruments.flir.level2_bridge import (
    APPARENT,
    CORRECTED,
    PROVENANCE_ASSUMED,
    PROVENANCE_MEASURED,
    LegacyFlirLevel2Bridge,
)

# Calibration constants as they appear in the campaign export.
CALIBRATION = {
    "R": 23844.2734375, "B": 1537.800048828125, "F": 1.0499999523162842,
    "J0": 19528, "J1": 35.69045639038086, "X": 0.7319999933242798,
    "alpha1": 1.2390000136974777e-8, "alpha2": 1.1094999585736787e-8,
    "beta1": 0.0031799999997019768, "beta2": 0.0031802000012248755,
}
MEASURED = {
    "mode": CORRECTED,
    "environment_inputs_provenance": PROVENANCE_MEASURED,
    "emissivity": 0.95,
    "object_distance_m": 300.0,
    "atmospheric_temperature_c": 18.4,
    "reflected_apparent_temperature_c": 17.9,
    "relative_humidity_percent": 62.0,
}


@pytest.fixture(scope="module")
def bridge():
    return LegacyFlirLevel2Bridge()


def test_bundled_radiometry_matches_the_reference_source(bridge):
    """The radiometry must stay byte-identical to the validated original."""
    reference = Path("/Users/biplob/Documents/Zeppelin_integration/FLIR/flir_radiometry.py")
    if not reference.is_file():
        pytest.skip(f"Reference source is not available at {reference}")
    assert bridge.radiometry_path.read_bytes() == reference.read_bytes()


def test_apparent_mode_applies_no_environment_correction(bridge):
    counts = np.array([[24236.0, 24440.0], [25703.0, 24500.0]])

    temperature, diagnostics = bridge.radiometry.counts_to_temperature(
        counts, CALIBRATION, None
    )

    assert diagnostics["method"] == "apparent_blackbody_temperature"
    # Emissivity 1, transmission 1, no reflected or path term.
    assert diagnostics["atmospheric_transmission"] == 1.0
    assert diagnostics["reflected_radiance_term"] == 0.0
    assert diagnostics["atmospheric_radiance_term"] == 0.0
    assert np.all(np.isfinite(temperature))
    # Real July counts land in a plausible ambient band.
    assert 0 < float(np.nanmean(temperature)) < 80


def test_corrected_mode_uses_the_official_terms(bridge):
    counts = np.array([[24236.0, 25703.0]])
    inputs = bridge.correction_inputs(MEASURED)

    temperature, diagnostics = bridge.radiometry.counts_to_temperature(
        counts, CALIBRATION, inputs
    )

    assert diagnostics["method"] == "flir_full_environment_corrected_temperature"
    assert 0 < diagnostics["atmospheric_transmission"] < 1
    assert diagnostics["water_content_g_m3"] > 0
    assert diagnostics["reflected_radiance_term"] > 0
    assert diagnostics["atmospheric_radiance_term"] > 0
    assert "T=B/ln(R/" in diagnostics["equation"]
    # Correcting for emissivity and path raises the retrieved temperature.
    apparent, _ = bridge.radiometry.counts_to_temperature(counts, CALIBRATION, None)
    assert float(np.nanmean(temperature)) != float(np.nanmean(apparent))


def test_apparent_mode_needs_no_measurements(bridge):
    assert bridge.correction_inputs({"mode": APPARENT}) is None


def test_corrected_mode_refuses_missing_measurements(bridge):
    incomplete = {k: v for k, v in MEASURED.items() if k != "relative_humidity_percent"}

    with pytest.raises(ValueError, match="relative_humidity_percent"):
        bridge.correction_inputs(incomplete)


@pytest.fixture()
def backend(tmp_path):
    return DashboardScanBackend(tmp_path)


def test_default_options_are_apparent_and_not_quantitative(backend):
    options = backend.snapshot()["flir_level2_options"]
    assert options["mode"] == APPARENT
    assert options["environment_inputs_provenance"] == PROVENANCE_ASSUMED
    assert DEFAULT_FLIR_LEVEL2_OPTIONS["emissivity"] is None


def test_corrected_mode_requires_every_environment_value(backend):
    with pytest.raises(ValueError, match="needs measured values for"):
        backend.update_flir_level2_options({"mode": CORRECTED, "emissivity": 0.95})


def test_measured_inputs_are_validated_and_stored(backend):
    options = backend.update_flir_level2_options(MEASURED)

    assert options["mode"] == CORRECTED
    assert options["environment_inputs_provenance"] == PROVENANCE_MEASURED
    assert options["emissivity"] == 0.95
    assert backend.snapshot()["flir_level2_options"]["object_distance_m"] == 300.0


@pytest.mark.parametrize(
    "payload, expected",
    [
        ({"mode": "guess"}, "apparent or corrected"),
        ({"mode": APPARENT, "environment_inputs_provenance": "hoped"}, "provenance"),
        ({**MEASURED, "emissivity": 1.4}, "emissivity must be between"),
        ({**MEASURED, "relative_humidity_percent": 140}, "relative_humidity"),
        ({**MEASURED, "valid_temperature_min_c": -20}, "both valid temperature limits"),
    ],
)
def test_invalid_options_are_rejected(backend, payload, expected):
    with pytest.raises(ValueError, match=expected):
        backend.update_flir_level2_options(payload)


def test_level2_still_requires_four_workers(backend):
    """Level 2 owns a dedicated pool; without it the job could never run."""
    import inspect

    source = inspect.getsource(DashboardScanBackend.start_detailed_processing)
    assert "worker_count < 4" in source


def test_task_georeferences_and_records_provenance():
    """Every temperature row must carry navigation and its quantitative status."""
    import inspect

    source = inspect.getsource(DashboardScanBackend._flir_detailed_task)
    for column in (
        "noseboom_time_utc", "noseboom_time_delta_s", "latitude_deg",
        "longitude_deg", "altitude_m", "georeference_status",
        "temperature_mode", "environment_inputs_provenance", "quantitative",
    ):
        assert f'"{column}"' in source, f"{column} is not written to the row"
    # The reference science must be driven, not reimplemented.
    assert "process_one_temperature" in source
    assert "scan_timestamps" in source
    assert "inspect_all_headers" in source


def test_export_listing_describes_the_post_processing_table(tmp_path):
    """The Export button must offer the file downstream correction needs."""
    backend = DashboardScanBackend(tmp_path)
    run = tmp_path / "Flight_2707" / "processed" / "flir" / "runs" / "x" / "level2"
    run.mkdir(parents=True)
    for name in ("temperature_frames.csv", "frame_health.csv", "summary.json"):
        (run / name).write_text("a,b\n1,2\n", encoding="utf-8")
    (run / "thumbnail.png").write_bytes(b"\x89PNG")
    backend._instruments["flir"].output_files = [
        str(run / n) for n in
        ("temperature_frames.csv", "frame_health.csv", "summary.json", "thumbnail.png")
    ]

    exports = backend.flir_exports()
    names = {item["name"] for item in exports}

    assert "temperature_frames.csv" in names
    assert "frame_health.csv" in names
    # Images are not a data export.
    assert "thumbnail.png" not in names
    table = next(i for i in exports if i["name"] == "temperature_frames.csv")
    assert "calibration constants" in table["description"]
    assert "Noseboom" in table["description"]
    assert table["url"].endswith("download=1")
    assert table["size_bytes"] > 0


def test_flir_page_offers_export_and_an_interactive_temperature_map():
    assets = Path(__file__).parents[1] / "app" / "assets"
    html = (assets / "flir.html").read_text(encoding="utf-8")
    script = (assets / "flir.js").read_text(encoding="utf-8")

    assert 'id="exportBtn"' in html and 'id="exportModal"' in html
    assert "/api/flir/exports" in script
    # The map is a Leaflet canvas coloured by a chosen temperature statistic,
    # the same treatment as the Noseboom map.
    assert 'id="thermalMap"' in html and 'id="mapMetric"' in html
    assert "L.circleMarker" in script and "fitBounds" in script
    assert "temperature_interpretation" in script
