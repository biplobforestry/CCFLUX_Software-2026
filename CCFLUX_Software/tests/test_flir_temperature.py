"""FLIR temperature: official radiometry, and one unconfigured way to run it.

Selecting FLIR converts temperature. There is no Level 2 stage to configure and
start: the conversion always ran regardless of what was configured, the only
reachable mode was apparent, and the environment-corrected path needed five
measured values the campaign does not record. What is left is the conversion
itself, always in apparent mode, labelled as such in every output.
"""

from pathlib import Path

import numpy as np
import pytest

from app.scan_backend import FLIR_TEMPERATURE_MODE, DashboardScanBackend
from instruments.flir.level2_bridge import APPARENT, LegacyFlirLevel2Bridge

# Calibration constants as they appear in the campaign export.
CALIBRATION = {
    "R": 23844.2734375, "B": 1537.800048828125, "F": 1.0499999523162842,
    "J0": 19528, "J1": 35.69045639038086, "X": 0.7319999933242798,
    "alpha1": 1.2390000136974777e-8, "alpha2": 1.1094999585736787e-8,
    "beta1": 0.0031799999997019768, "beta2": 0.0031802000012248755,
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


def test_the_preserved_corrected_science_is_still_intact(bridge):
    """Nothing drives it, but it must not have been damaged on the way out.

    The environment-corrected calculation stays in legacy_integration/FLIR
    unchanged. Only the application's path into it was removed, so a future
    campaign that does measure the five environment values can reach it again.
    """
    counts = np.array([[24236.0, 25703.0]])
    inputs = bridge.radiometry.CorrectionInputs(
        emissivity=0.95,
        object_distance_m=300.0,
        atmospheric_temperature_c=18.4,
        reflected_apparent_temperature_c=17.9,
        relative_humidity_percent=62.0,
        external_optics_transmission=1.0,
        external_optics_temperature_c=None,
    )
    inputs.validate()

    temperature, diagnostics = bridge.radiometry.counts_to_temperature(
        counts, CALIBRATION, inputs
    )

    assert diagnostics["method"] == "flir_full_environment_corrected_temperature"
    assert 0 < diagnostics["atmospheric_transmission"] < 1
    apparent, _ = bridge.radiometry.counts_to_temperature(counts, CALIBRATION, None)
    assert float(np.nanmean(temperature)) != float(np.nanmean(apparent))


class TestThereIsNoConfiguredStage:
    backend_source = (
        Path(__file__).parents[1] / "app" / "scan_backend.py"
    ).read_text(encoding="utf-8")
    server_source = (
        Path(__file__).parents[1] / "app" / "server.py"
    ).read_text(encoding="utf-8")

    def _quick_task_body(self) -> str:
        start = self.backend_source.index("def _flir_quick_task(")
        end = self.backend_source.find("\n    def ", start)
        return self.backend_source[start:end]

    def test_the_capability_declaration_layer_is_gone(self):
        assert not (Path(__file__).parents[1] / "core" / "camera_level2.py").exists()
        assert "camera_level2" not in self.backend_source

    def test_there_are_no_level2_options_to_configure(self):
        for name in (
            "DEFAULT_FLIR_LEVEL2_OPTIONS",
            "_flir_level2_options",
            "update_flir_level2_options",
            "level2_capabilities",
        ):
            assert name not in self.backend_source, name

    def test_the_options_endpoint_is_gone(self):
        assert "/api/flir/level2-options" not in self.server_source

    def test_no_routine_selection_remains(self):
        assert "_flir_level2_routines" not in self.backend_source
        assert "selected_routines" not in self.backend_source

    def test_the_state_payload_no_longer_carries_them(self, tmp_path):
        snapshot = DashboardScanBackend(tmp_path).snapshot()
        assert "flir_level2_options" not in snapshot
        assert "level2_capabilities" not in snapshot

    def test_the_conversion_still_runs_inside_the_flir_job(self):
        body = self._quick_task_body()
        assert "self._flir_detailed_task(context)" in body
        assert "Converting temperature and matching Noseboom navigation" in body

    def test_a_failed_conversion_still_keeps_the_metadata(self):
        body = self._quick_task_body()
        assert "except ProcessingCancelledError:" in body
        assert "the acquisition metadata was still written" in body
        assert "return JobOutcome(warning=warning)" in body

    def test_there_is_still_no_separate_detailed_start_path(self, tmp_path):
        backend = DashboardScanBackend(tmp_path)
        assert not hasattr(backend, "start_detailed_processing")
        with pytest.raises(ValueError, match="Unknown queue action"):
            backend.update_queue({"action": "start_detailed", "job_id": "flir_quick"})
        assert "/api/processing/detailed/start" not in self.server_source


class TestEveryOutputSaysItIsApparent:
    """Dropping the mode must not make the product silent about what it is."""

    source = __import__("inspect").getsource(
        DashboardScanBackend._flir_detailed_task
    )

    def test_the_mode_is_apparent(self):
        assert FLIR_TEMPERATURE_MODE == APPARENT == "apparent"

    def test_no_correction_inputs_are_built(self):
        assert "correction = None" in self.source

    def test_each_row_carries_navigation_and_the_mode(self):
        for column in (
            "noseboom_time_utc", "noseboom_time_delta_s", "latitude_deg",
            "longitude_deg", "altitude_m", "georeference_status",
            "temperature_mode", "quantitative",
        ):
            assert f'"{column}"' in self.source, f"{column} is not written to the row"

    def test_rows_are_never_marked_quantitative(self):
        assert 'row["quantitative"] = False' in self.source

    def test_the_reference_science_is_driven_not_reimplemented(self):
        assert "process_one_temperature" in self.source
        assert "scan_timestamps" in self.source
        assert "inspect_all_headers" in self.source

    def test_the_workspace_explains_what_apparent_temperature_is(self):
        assert "not a surface temperature" in self.source
        script = (
            Path(__file__).parents[1] / "app" / "assets" / "flir.js"
        ).read_text(encoding="utf-8")
        assert "temperature_interpretation" in script
        assert "Level 2 required" not in script


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


def test_a_small_allocation_does_not_block_the_conversion():
    """It owned a dedicated pool and refused to start without four workers,
    which stopped a 2-core laptop processing camera products at all."""
    import inspect

    source = inspect.getsource(DashboardScanBackend)
    assert "requires at least 4 workers" not in source
    assert not hasattr(DashboardScanBackend, "start_detailed_processing")


class TestFramesConvertInParallel:
    """Flight_CC0807 converted 11 561 frames one at a time, 06:20 to 08:21.

    Each frame is independent: process_one_temperature opens its own handle,
    reads its own byte span and shares nothing. The work inside is a read, a
    bytes.translate and numpy over 640x480, all of which release the interpreter
    lock, so the reads and the arithmetic overlap instead of queueing.
    """

    BACKEND = (
        Path(__file__).resolve().parents[1] / "app" / "scan_backend.py"
    ).read_text(encoding="utf-8")

    def _task(self) -> str:
        start = self.BACKEND.index("def _flir_detailed_task(")
        end = self.BACKEND.find("\n    def ", start + 1)
        return self.BACKEND[start:end if end != -1 else None]

    def test_the_conversion_uses_a_pool(self):
        body = self._task()
        assert "ThreadPoolExecutor(" in body
        assert "ccflux-flir-temperature" in body

    def test_the_reader_count_is_declared_and_bounded(self):
        from app.scan_backend import DashboardScanBackend

        assert 1 <= DashboardScanBackend.FLIR_TEMPERATURE_READERS <= 32

    def test_rows_keep_acquisition_order(self):
        """as_completed returns frames in whatever order they finish, so each
        result is placed by its index rather than appended."""
        body = self._task()
        assert "rows[pending[future]] = future.result()" in body
        assert "rows.append(" not in body

    def test_cancellation_is_still_checked_per_frame(self):
        body = self._task()
        block = body[body.index("as_completed(pending)"):]
        assert "context.check_cancelled()" in block[:400]

    def test_a_failure_does_not_leave_the_pool_working(self):
        body = self._task()
        assert "future.cancel()" in body

    def test_progress_is_still_reported(self):
        body = self._task()
        assert "Radiometric temperature" in body

    def test_the_frame_conversion_opens_its_own_handle(self):
        """What makes the frames independent in the first place."""
        source = (
            Path(__file__).resolve().parents[1] / "legacy_integration" / "FLIR"
            / "flir_health_temperature.py"
        ).read_text(encoding="utf-8")
        block = source[source.index("def raw_array_from_object("):]
        block = block[: block.index("\ndef ")]
        assert 'with path.open("rb") as stream:' in block

    def test_placeholder_rows_are_all_replaced(self):
        """A frame whose future never lands would leave None in the CSV."""
        body = self._task()
        assert "rows: list[dict[str, object]] = [None] * len(indices)" in body
        # Every index is submitted, so every slot is written.
        assert "for position, index in enumerate(indices)" in body
