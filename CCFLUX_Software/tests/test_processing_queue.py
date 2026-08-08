import threading
import time

import pytest

from core.enums import ProcessingStatus
from core.exceptions import ProcessingError
from core.priority_manager import (
    CAMPAIGN_PROCESSING_ORDER,
    create_default_priority_queue,
)
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
    """The campaign order: Noseboom first, then the cameras FLIR, GoPro, MicaSense."""
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
    # Camera products are one job each. FLIR's temperature conversion and
    # Noseboom match used to be a separate "detailed" job the operator had to
    # find and start; they now run as part of the FLIR job.
    assert [job.display_name for job in jobs[-3:]] == [
        "FLIR metadata quick check",
        "GoPro metadata quick check",
        "MicaSense metadata quick check",
    ]
    assert not any(job.detailed for job in jobs)


def test_the_queue_and_the_campaign_order_agree():
    """One order, declared once; the presentation lists read it from here."""
    jobs = create_default_priority_queue().ordered()
    assert [job.instrument_id for job in jobs] == list(CAMPAIGN_PROCESSING_ORDER)


def test_priority_never_contradicts_the_declared_order():
    """A job is dispatched on (priority, order); a stale priority outranks it.

    GoPro was priority 3 while MicaSense and FLIR were 2, so GoPro ran last
    whatever order the definition gave.
    """
    jobs = create_default_priority_queue().ordered()
    for group in WorkerGroup:
        in_group = [job for job in jobs if job.worker_group is group]
        priorities = [job.priority for job in in_group]
        assert priorities == sorted(priorities), (
            f"{group.value} priorities disagree with the declared order"
        )


def test_the_order_holds_for_whichever_instruments_are_selected():
    """Deselecting one must not reshuffle the rest."""
    queue = create_default_priority_queue()
    chosen = ("noseboom", "picarro", "sif", "gopro_quick")
    for job_id in chosen:
        queue.set_enabled(job_id, True)
        queue.get(job_id).task = lambda context: None

    dispatched = []
    for group in (WorkerGroup.FAST_SCIENCE, WorkerGroup.CAMERA_METADATA):
        while (job := queue.next_for_group(group)) is not None:
            dispatched.append(job.job_id)
            job.status = ProcessingStatus.COMPLETE

    assert dispatched == ["noseboom", "picarro", "sif", "gopro_quick"]


def test_the_cameras_run_in_the_campaign_sequence():
    """Their group has one worker, so this is the actual run order."""
    queue = create_default_priority_queue()
    for job_id in ("micasense_quick", "gopro_quick", "flir_quick"):
        queue.set_enabled(job_id, True)
        queue.get(job_id).task = lambda context: None

    dispatched = []
    while (job := queue.next_for_group(WorkerGroup.CAMERA_METADATA)) is not None:
        dispatched.append(job.instrument_id)
        job.status = ProcessingStatus.COMPLETE

    assert dispatched == ["flir", "gopro", "micasense"]


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


class TestTheCameraPoolGrowsWithTheAllocation:
    """CAMERA_METADATA held one worker whatever the machine had.

    Its three jobs - FLIR, GoPro, MicaSense - therefore ran strictly one after
    another, and MicaSense runs for hours over 9 999 captures, so the other two
    waited behind it on a 16-core machine exactly as on a 4-core one.
    """

    def test_a_small_allocation_is_unchanged(self):
        for workers, expected in ((1, 0), (2, 1), (3, 1), (4, 1), (5, 1)):
            assert (
                worker_group_capacities(workers)[WorkerGroup.CAMERA_METADATA]
                == expected
            ), workers

    def test_it_grows_once_there_is_room(self):
        assert worker_group_capacities(6)[WorkerGroup.CAMERA_METADATA] == 2
        assert worker_group_capacities(10)[WorkerGroup.CAMERA_METADATA] == 3

    def test_it_never_exceeds_the_jobs_that_exist(self):
        from core.priority_manager import DEFAULT_PROCESSING_JOBS
        from core.processing_manager import CAMERA_METADATA_JOB_COUNT

        declared = sum(
            1 for job in DEFAULT_PROCESSING_JOBS
            if job[3] is WorkerGroup.CAMERA_METADATA
        )
        assert CAMERA_METADATA_JOB_COUNT == declared
        for workers in (12, 16, 32, 64):
            assert (
                worker_group_capacities(workers)[WorkerGroup.CAMERA_METADATA]
                <= declared
            )

    def test_fast_science_always_keeps_a_worker(self):
        """A camera run must never be able to stall the flight instruments."""
        for workers in range(1, 65):
            assert worker_group_capacities(workers)[WorkerGroup.FAST_SCIENCE] >= 1

    def test_the_cameras_never_take_more_than_a_third(self):
        from core.processing_manager import CAMERA_WORKER_SHARE

        for workers in range(6, 65):
            capacities = worker_group_capacities(workers)
            camera = (
                capacities[WorkerGroup.CAMERA_METADATA]
                + capacities[WorkerGroup.CAMERA_DETAILED]
            )
            assert camera <= max(2, workers // CAMERA_WORKER_SHARE + 1), workers

    def test_every_worker_is_given_out(self):
        for workers in range(1, 65):
            assert sum(worker_group_capacities(workers).values()) == workers

    def test_the_detailed_pool_is_untouched(self):
        """It has no queued jobs, so scaling it would reserve workers for
        nothing; zero would leave a future job queued in silence."""
        for workers, expected in ((1, 0), (3, 0), (4, 1), (32, 1)):
            assert (
                worker_group_capacities(workers)[WorkerGroup.CAMERA_DETAILED]
                == expected
            ), workers
