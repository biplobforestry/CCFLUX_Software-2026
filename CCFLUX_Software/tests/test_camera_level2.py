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


def test_legacy_queue_action_cannot_bypass_confirmation(tmp_path: Path):
    backend = DashboardScanBackend(tmp_path)
    with pytest.raises(ValueError, match="explicit confirmation"):
        backend.update_queue(
            {"action": "start_detailed", "job_id": "flir_detailed"}
        )
    assert backend.processing_queue.get("flir_detailed").enabled is False


def test_detailed_endpoint_requires_explicit_confirmation(tmp_path: Path):
    backend = DashboardScanBackend(tmp_path)
    with pytest.raises(ValueError, match="Explicit"):
        backend.start_detailed_processing(
            {
                "job_id": "flir_detailed",
                "selected_routines": ["frame_temperature_statistics"],
                "confirmed": False,
            }
        )
    assert backend.processing_queue.get("flir_detailed").enabled is False
