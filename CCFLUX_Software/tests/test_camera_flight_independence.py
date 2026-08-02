"""Camera work and flight science must not block one another.

The scheduler has always run them in separate pools with reserved capacity, so
neither can starve the other. One check did not know that: it asked whether any
job anywhere was dispatched, and refused everything if so. Starting the remote
sensing products therefore locked every flight instrument out of selection and
out of Start Processing -- "Please wait! System is busy now!" -- for as long as
the camera run took, which is the longest run in the software.

Genuinely global changes, the Time Filter and the worker allocation, still
require the whole workflow to be idle: they apply to both halves at once.
"""

import pytest

from app.scan_backend import DashboardScanBackend
from core.enums import DetectionStatus, ProcessingStatus
from core.processing_manager import WorkerGroup

CAMERA_JOB = "flir_quick"
FLIGHT_JOB = "noseboom"


def _dispatch(backend, job_id):
    """Put one job into the state a dispatched job holds."""
    job = backend.processing_queue.get(job_id)
    job.enabled = True
    job.task = lambda context: None
    job.status = ProcessingStatus.PROCESSING
    return job


def _make_ready(backend, job_id):
    """A scanned, healthy instrument, so selection reaches the busy check."""
    state = backend._instruments[backend.processing_queue.get(job_id).instrument_id]
    state.detection_status = DetectionStatus.READY
    state.ambiguous = False
    state.errors = []
    return state


@pytest.fixture
def backend(tmp_path):
    return DashboardScanBackend(tmp_path)


def test_the_two_halves_are_told_apart(backend):
    camera = backend.processing_queue.get(CAMERA_JOB)
    flight = backend.processing_queue.get(FLIGHT_JOB)

    assert camera.worker_group in DashboardScanBackend.CAMERA_WORKER_GROUPS
    assert flight.worker_group is WorkerGroup.FAST_SCIENCE
    assert DashboardScanBackend._job_domain(camera) == "camera"
    assert DashboardScanBackend._job_domain(flight) == "flight"


def test_a_camera_run_leaves_the_flight_half_idle(backend):
    _dispatch(backend, CAMERA_JOB)

    assert backend._busy_domains() == {"camera"}
    assert backend._processing_configuration_is_busy("camera") is True
    assert backend._processing_configuration_is_busy("flight") is False
    # Something is running, so the whole-workflow question is still yes.
    assert backend._processing_configuration_is_busy() is True


def test_a_flight_instrument_can_be_selected_while_cameras_run(backend):
    """This is the reported failure: it raised "System is busy now!"."""
    _dispatch(backend, CAMERA_JOB)
    _make_ready(backend, FLIGHT_JOB)

    backend.update_queue({"action": "enable", "job_id": FLIGHT_JOB})

    assert backend.processing_queue.get(FLIGHT_JOB).enabled is True


def test_a_camera_job_is_still_protected_while_its_own_half_runs(backend):
    _dispatch(backend, CAMERA_JOB)

    with pytest.raises(ValueError, match="Camera processing is still running"):
        backend.update_queue({"action": "disable", "job_id": CAMERA_JOB})


def test_the_refusal_says_the_other_half_is_free(backend):
    _dispatch(backend, CAMERA_JOB)

    with pytest.raises(ValueError, match="other half of the workflow can be used"):
        backend.update_queue({"action": "disable", "job_id": CAMERA_JOB})


def test_reordering_needs_both_halves_idle(backend):
    """The queue is one ordered list, so this is not a per-half change."""
    _dispatch(backend, CAMERA_JOB)

    with pytest.raises(ValueError, match="System is busy"):
        backend.update_queue({"action": "reorder", "job_ids": [FLIGHT_JOB]})


def test_the_time_filter_still_needs_everything_idle(backend):
    """One global interval applies to both halves at once."""
    _dispatch(backend, CAMERA_JOB)

    with pytest.raises(ValueError, match="System is busy"):
        backend.update_time_filter({"action": "full"})


def test_the_worker_allocation_still_needs_everything_idle(backend):
    _dispatch(backend, CAMERA_JOB)

    with pytest.raises(ValueError, match="System is busy"):
        backend.update_resources(worker_count=2, memory_bytes=None)


def test_the_snapshot_reports_each_half(backend):
    _dispatch(backend, CAMERA_JOB)

    snapshot = backend._queue_snapshot()

    assert snapshot["camera_busy"] is True
    assert snapshot["flight_busy"] is False
    assert snapshot["busy_domains"] == ["camera"]
    assert snapshot["busy"] is True


def test_starting_is_blocked_only_by_what_is_selected(backend):
    """Start must stay available for the half that is not running."""
    _dispatch(backend, CAMERA_JOB)
    _make_ready(backend, CAMERA_JOB)
    _make_ready(backend, FLIGHT_JOB)
    backend.processing_queue.get(FLIGHT_JOB).enabled = True

    snapshot = backend._queue_snapshot()

    # Both halves are selected and the camera half is running, so Start would
    # touch something already in flight.
    assert snapshot["start_blocked"] is True

    # Deselect the running camera job: only the idle flight half remains, and
    # Start is no longer held back by the camera run.
    backend.processing_queue.get(CAMERA_JOB).enabled = False
    assert backend._queue_snapshot()["start_blocked"] is False


def test_both_halves_can_be_busy_at_once(backend):
    _dispatch(backend, CAMERA_JOB)
    _dispatch(backend, FLIGHT_JOB)

    assert backend._busy_domains() == {"camera", "flight"}
    snapshot = backend._queue_snapshot()
    assert snapshot["camera_busy"] is True and snapshot["flight_busy"] is True
    assert snapshot["busy_domains"] == ["camera", "flight"]
