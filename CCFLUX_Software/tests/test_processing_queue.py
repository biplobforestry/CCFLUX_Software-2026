import threading
import time

import pytest

from core.enums import ProcessingStatus
from core.exceptions import ProcessingError
from core.priority_manager import create_default_priority_queue
from core.processing_manager import (
    ProcessingJob,
    ProcessingPriorityQueue,
    ProcessingScheduler,
    WorkerGroup,
    worker_group_capacities,
)


def _job(
    job_id: str,
    *,
    priority: int = 1,
    group: WorkerGroup = WorkerGroup.FAST_SCIENCE,
    task=None,
) -> ProcessingJob:
    return ProcessingJob(
        job_id=job_id,
        instrument_id=job_id,
        display_name=job_id,
        worker_group=group,
        priority=priority,
        task=task,
    )


def _wait_for(predicate, timeout: float = 2.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.005)
    raise AssertionError("condition was not reached")


def test_default_ordering_matches_priority_groups():
    jobs = create_default_priority_queue().ordered()
    assert [job.display_name for job in jobs[:6]] == [
        "Noseboom",
        "MIRO",
        "Picarro",
        "OPC HBX-4",
        "OPC HBX-5",
        "Partector Pro",
    ]
    assert jobs[6].display_name == "INS Gimbal"
    assert jobs[7].display_name == "SIF"
    assert jobs[-2].display_name == "Detailed MicaSense processing"
    assert jobs[-1].display_name == "Detailed FLIR processing"


def test_running_job_snapshot_reports_live_elapsed_time():
    job = _job("elapsed")
    job.status = ProcessingStatus.PROCESSING
    job._started_monotonic = time.monotonic() - 2
    assert job.snapshot().elapsed_time.total_seconds() >= 1.9


def test_drag_and_drop_reorder_state_affects_queue_order():
    queue = ProcessingPriorityQueue()
    for name in ("a", "b", "c"):
        queue.add(_job(name))

    queue.reorder(["c", "a", "b"])

    assert [job.job_id for job in queue.ordered()] == ["c", "a", "b"]


def test_enable_disable_pause_and_resume():
    queue = ProcessingPriorityQueue()
    queue.add(_job("miro"))
    queue.set_enabled("miro", False)
    assert not queue.get("miro").enabled
    assert queue.get("miro").status is ProcessingStatus.PAUSED
    queue.set_enabled("miro", True)
    queue.pause("miro")
    assert queue.get("miro").status is ProcessingStatus.PAUSED
    queue.resume("miro")
    assert queue.get("miro").status is ProcessingStatus.QUEUED


def test_running_job_can_be_cancelled_cooperatively():
    queue = ProcessingPriorityQueue()

    def task(context):
        while True:
            context.check_cancelled()
            time.sleep(0.002)

    queue.add(_job("miro", task=task))
    scheduler = ProcessingScheduler(queue, total_workers=1)
    scheduler.dispatch()
    _wait_for(lambda: queue.get("miro").status is ProcessingStatus.PROCESSING)
    queue.cancel("miro")
    _wait_for(lambda: queue.get("miro").status is ProcessingStatus.CANCELLED)
    scheduler.shutdown()


def test_safe_shutdown_cancels_running_and_queued_jobs():
    queue = ProcessingPriorityQueue()
    started = threading.Event()

    def cancellable(context):
        started.set()
        while True:
            context.check_cancelled()
            time.sleep(0.005)

    running = _job("running", task=cancellable)
    queued = _job("queued", task=lambda context: None)
    queue.add(running)
    queue.add(queued)
    scheduler = ProcessingScheduler(queue, total_workers=1)
    scheduler.dispatch()
    assert started.wait(1)

    scheduler.shutdown(wait=True, cancel_pending=True)

    assert queue.get("running").status is ProcessingStatus.CANCELLED
    assert queue.get("queued").status is ProcessingStatus.CANCELLED


def test_non_cancellable_running_job_is_protected():
    queue = ProcessingPriorityQueue()
    job = _job("miro")
    job.status = ProcessingStatus.PROCESSING
    job.safely_cancellable = False
    queue.add(job)
    queue.get("miro").status = ProcessingStatus.PROCESSING
    with pytest.raises(ProcessingError, match="not safely"):
        queue.cancel("miro")


def test_failed_job_can_be_retried():
    queue = ProcessingPriorityQueue()
    queue.add(_job("miro", task=lambda context: (_ for _ in ()).throw(RuntimeError("bad"))))
    scheduler = ProcessingScheduler(queue, total_workers=1)
    scheduler.dispatch()
    _wait_for(lambda: queue.get("miro").status is ProcessingStatus.FAILED)
    queue.get("miro").task = lambda context: None
    queue.retry("miro")
    scheduler.dispatch()
    _wait_for(lambda: queue.get("miro").status is ProcessingStatus.COMPLETE)
    scheduler.shutdown()


def test_job_failure_is_isolated_from_other_job():
    queue = ProcessingPriorityQueue()
    queue.add(_job("bad", task=lambda context: (_ for _ in ()).throw(ValueError("bad"))))
    queue.add(_job("good", task=lambda context: None))
    scheduler = ProcessingScheduler(queue, total_workers=2)
    scheduler.dispatch()
    _wait_for(
        lambda: all(
            queue.get(name).status
            in {ProcessingStatus.COMPLETE, ProcessingStatus.FAILED}
            for name in ("bad", "good")
        )
    )
    assert queue.get("bad").status is ProcessingStatus.FAILED
    assert queue.get("good").status is ProcessingStatus.COMPLETE
    scheduler.shutdown()


def test_priority_affects_actual_scheduling():
    execution = []
    queue = ProcessingPriorityQueue()
    queue.add(_job("low", priority=3, task=lambda context: execution.append("low")))
    queue.add(_job("high", priority=1, task=lambda context: execution.append("high")))
    scheduler = ProcessingScheduler(queue, total_workers=1)
    scheduler.dispatch()
    _wait_for(lambda: len(execution) == 2)
    assert execution == ["high", "low"]
    scheduler.shutdown()


def test_completed_result_is_published_without_waiting_for_all_jobs():
    release_second = threading.Event()
    callbacks = []
    queue = ProcessingPriorityQueue()
    queue.add(_job("first", task=lambda context: None))
    queue.add(_job("second", task=lambda context: release_second.wait(1)))
    scheduler = ProcessingScheduler(
        queue, total_workers=2, result_callback=callbacks.append
    )
    scheduler.dispatch()
    _wait_for(
        lambda: any(
            job.job_id == "first" and job.status is ProcessingStatus.COMPLETE
            for job in callbacks
        )
    )
    assert queue.get("second").status is ProcessingStatus.PROCESSING
    release_second.set()
    scheduler.shutdown()


def test_camera_workers_never_consume_all_workers():
    for workers in range(1, 9):
        capacities = worker_group_capacities(workers)
        camera = (
            capacities[WorkerGroup.CAMERA_METADATA]
            + capacities[WorkerGroup.CAMERA_DETAILED]
        )
        assert camera < workers
