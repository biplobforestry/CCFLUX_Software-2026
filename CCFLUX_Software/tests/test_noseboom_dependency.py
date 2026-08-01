"""Skipping the Noseboom must not be silent.

Every job starts unselected, which is deliberate — the operator chooses what to
run. But the Noseboom is the campaign's UTC and navigation reference, and both
GoPro and the detailed FLIR conversion read its *processed* 1 Hz points. Leaving
it unticked used to finish the run without comment and hand back camera products
with no positions at all.

It is still the operator's call. It just has to be a deliberate one.
"""

from datetime import datetime, timezone
from pathlib import Path

import pytest

from app.scan_backend import DashboardScanBackend
from core.enums import DetectionStatus, ProcessingStatus
from core.processing_manager import ProcessingPriorityQueue
from core.priority_manager import create_default_priority_queue


def _backend(tmp_path):
    backend = DashboardScanBackend(tmp_path)
    for instrument_id in ("noseboom", "gopro", "flir"):
        state = backend._instruments[instrument_id]
        state.detection_status = DetectionStatus.READY
        state.file_count = 5
        state.utc_start_time = datetime(2026, 7, 27, 6, tzinfo=timezone.utc)
        state.utc_end_time = datetime(2026, 7, 27, 9, tzinfo=timezone.utc)
    return backend


TASKS = {"noseboom": None, "gopro_quick": None, "flir_quick": None}


def test_every_job_starts_unselected():
    """The operator picks what runs; nothing is chosen for them."""
    queue = create_default_priority_queue()

    for job in queue.ordered():
        assert job.enabled is False
        assert job.status is ProcessingStatus.PAUSED


def test_an_unselected_job_says_why_it_is_paused():
    """'Disabled' reads like a fault. Nobody ticked it."""
    queue = create_default_priority_queue()

    assert queue.get("noseboom").current_step == "Not selected"


def test_selecting_a_job_queues_it():
    queue = create_default_priority_queue()

    queue.set_enabled("noseboom", True)

    assert queue.get("noseboom").status is ProcessingStatus.QUEUED
    assert queue.get("noseboom").current_step == "Waiting"


def test_gopro_without_noseboom_is_reported(tmp_path):
    backend = _backend(tmp_path)
    backend.processing_queue.set_enabled("gopro_quick", True)

    messages = backend._noseboom_dependency_messages(TASKS)

    assert messages and "capture positions" in messages[0]


def test_flir_without_noseboom_is_reported(tmp_path):
    backend = _backend(tmp_path)
    backend.processing_queue.set_enabled("flir_quick", True)

    messages = backend._noseboom_dependency_messages(TASKS)

    assert messages and "georeferencing" in messages[0]


def test_selecting_noseboom_clears_the_warning(tmp_path):
    backend = _backend(tmp_path)
    backend.processing_queue.set_enabled("gopro_quick", True)
    backend.processing_queue.set_enabled("noseboom", True)

    assert backend._noseboom_dependency_messages(TASKS) == []


def test_a_noseboom_processed_earlier_also_clears_it(tmp_path):
    """A second run in the same session must not nag about work already done."""
    backend = _backend(tmp_path)
    backend.processing_queue.set_enabled("gopro_quick", True)
    backend._instruments["noseboom"].quicklook = {
        "points": [{"latitude": 47.6, "longitude": 9.3}]
    }

    assert backend._noseboom_dependency_messages(TASKS) == []


def test_instruments_that_do_not_need_it_are_not_reported(tmp_path):
    """SIF builds its own position log from the raw folder."""
    backend = _backend(tmp_path)
    state = backend._instruments["sif"]
    state.detection_status = DetectionStatus.READY
    backend.processing_queue.set_enabled("sif", True)

    assert backend._noseboom_dependency_messages({"sif": None, "noseboom": None}) == []


def test_the_dependency_set_is_the_one_that_reads_processed_points():
    source = (Path(__file__).parents[1] / "app" / "scan_backend.py").read_text(
        encoding="utf-8"
    )

    assert set(DashboardScanBackend.NOSEBOOM_DEPENDENTS) == {"gopro", "flir"}
    # The claim is that these two tasks read the processed points. Assert it of
    # the methods themselves rather than counting occurrences in the file.
    for method in ("_gopro_quick_task", "_flir_detailed_task"):
        start = source.index(f"def {method}(")
        body = source[start : source.index("\n    def ", start + 1)]
        assert '_instruments["noseboom"].quicklook' in body, (
            f"{method} no longer reads the processed Noseboom points"
        )
    # And one that does not, so the set is not simply everything.
    start = source.index("def _sif_task(")
    sif_body = source[start : source.index("\n    def ", start + 1)]
    assert '_instruments["noseboom"].quicklook' not in sif_body


def test_processing_refuses_until_it_is_confirmed(tmp_path):
    backend = _backend(tmp_path)
    backend.processing_queue.set_enabled("gopro_quick", True)

    messages = backend._noseboom_dependency_messages(TASKS)

    assert messages, "the operator must be told before the run, not after"
    # And the refusal names the reference and the consequence.
    text = "; ".join(messages)
    assert "no capture positions" in text
