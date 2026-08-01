from pathlib import Path

import pytest

from app.scan_backend import DashboardScanBackend
from core.camera_level2 import (
    level2_capability_snapshot,
    validate_level2_selection,
)


def test_only_validated_executable_flir_routines_are_enabled():
    capabilities = level2_capability_snapshot()
    assert not any(item["available"] for item in capabilities["micasense"])
    assert {
        item["routine_id"]
        for item in capabilities["flir"]
        if item["available"]
    } == {
        "radiometric_temperature_conversion",
        "frame_temperature_statistics",
        "noseboom_georeferencing",
    }


def test_unavailable_and_unknown_routines_are_rejected():
    with pytest.raises(ValueError, match="Unavailable"):
        validate_level2_selection("flir", ["temperature_imagery"])
    with pytest.raises(ValueError, match="Unknown"):
        validate_level2_selection("flir", ["invented_algorithm"])


def test_level2_requires_nonempty_unique_selection():
    with pytest.raises(ValueError, match="Select at least"):
        validate_level2_selection("flir", [])
    with pytest.raises(ValueError, match="duplicates"):
        validate_level2_selection(
            "flir",
            ["frame_temperature_statistics", "frame_temperature_statistics"],
        )


def test_there_is_no_separate_detailed_start_path(tmp_path: Path):
    """The conversion runs inside the FLIR job. A second way in would be a way
    to start it without the operator asking."""
    backend = DashboardScanBackend(tmp_path)

    assert not hasattr(backend, "start_detailed_processing")
    with pytest.raises(ValueError, match="Unknown queue action"):
        backend.update_queue({"action": "start_detailed", "job_id": "flir_quick"})
    server = (Path(__file__).parents[1] / "app" / "server.py").read_text(
        encoding="utf-8"
    )
    assert "/api/processing/detailed/start" not in server


def test_the_flir_job_runs_the_conversion_and_the_georeferencing():
    """Metadata alone left the map view waiting on work nobody had started."""
    source = (Path(__file__).parents[1] / "app" / "scan_backend.py").read_text(
        encoding="utf-8"
    )
    start = source.index("def _flir_quick_task(")
    body = source[start : source.index("def _flir_level2_routines", start)]

    assert "_flir_detailed_task(context, self._flir_level2_routines())" in body
    assert "Converting temperature and matching Noseboom navigation" in body


def test_a_failed_conversion_keeps_the_metadata():
    """One run: a late failure must not throw away what already succeeded."""
    source = (Path(__file__).parents[1] / "app" / "scan_backend.py").read_text(
        encoding="utf-8"
    )
    start = source.index("def _flir_quick_task(")
    body = source[start : source.index("def _flir_level2_routines", start)]

    assert "except ProcessingCancelledError:" in body, "cancellation must propagate"
    assert "the acquisition metadata was still written" in body
    assert "return JobOutcome(warning=warning)" in body


def test_a_small_allocation_warns_rather_than_refusing():
    """A 2-core laptop could not process camera products at all before."""
    source = (Path(__file__).parents[1] / "app" / "scan_backend.py").read_text(
        encoding="utf-8"
    )

    assert "requires at least 4 workers" not in source
    # No capacity at all is still refused: the jobs would queue for ever in
    # silence. A small allocation is only slower, and is warned about instead.
    assert "have no worker capacity with" in source
    assert "will take longer" in source
    script = (Path(__file__).parents[1] / "app" / "assets" / "dashboard.js").read_text(
        encoding="utf-8"
    )
    assert "worker${workerCount === 1 ? '' : 's'} allocated" in script
    assert "healthApplyWorkers" in script, "the operator needs a way to change it"
