"""Priority-aware isolated worker scheduling without instrument algorithms."""

from __future__ import annotations

import threading
import time
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass, field, replace
from datetime import timedelta
from .compat import StrEnum
from typing import Callable

from .enums import ProcessingStatus
from .exceptions import ProcessingCancelledError, ProcessingError
from .logging_manager import LogLevel, ProcessingLogManager


class WorkerGroup(StrEnum):
    FAST_SCIENCE = "fast_science"
    CAMERA_METADATA = "camera_metadata"
    CAMERA_DETAILED = "camera_detailed"


@dataclass(frozen=True, slots=True)
class JobOutcome:
    warning: str | None = None


ProgressReporter = Callable[[float, str], None]
JobTask = Callable[["ProcessingContext"], JobOutcome | None]


@dataclass(slots=True)
class ProcessingJob:
    job_id: str
    instrument_id: str
    display_name: str
    worker_group: WorkerGroup
    priority: int
    enabled: bool = True
    status: ProcessingStatus = ProcessingStatus.QUEUED
    progress: float = 0.0
    current_step: str = "Waiting"
    elapsed_time: timedelta = timedelta(0)
    safely_cancellable: bool = True
    detailed: bool = False
    order: int = 0
    error: str | None = None
    task: JobTask | None = field(default=None, repr=False)
    _cancel_event: threading.Event = field(
        default_factory=threading.Event, repr=False
    )
    _started_monotonic: float | None = field(default=None, repr=False)

    def snapshot(self) -> "ProcessingJob":
        elapsed = self.elapsed_time
        if self.status is ProcessingStatus.PROCESSING and self._started_monotonic:
            elapsed = timedelta(seconds=time.monotonic() - self._started_monotonic)
        return replace(
            self, task=None, elapsed_time=elapsed,
            _cancel_event=threading.Event(), _started_monotonic=None,
        )


class ProcessingContext:
    def __init__(self, job: ProcessingJob, reporter: ProgressReporter) -> None:
        self.job = job
        self._reporter = reporter

    def report_progress(self, percentage: float, step: str) -> None:
        if not 0 <= percentage <= 100:
            raise ValueError("progress must be between 0 and 100")
        self.check_cancelled()
        self._reporter(percentage, step)

    def check_cancelled(self) -> None:
        if self.job._cancel_event.is_set():
            raise ProcessingCancelledError(
                f"Job {self.job.job_id} was cancelled"
            )


class ProcessingPriorityQueue:
    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._jobs: dict[str, ProcessingJob] = {}

    def add(self, job: ProcessingJob) -> None:
        with self._lock:
            if job.job_id in self._jobs:
                raise ValueError(f"Duplicate job ID: {job.job_id}")
            if job.priority not in {1, 2, 3}:
                raise ValueError("priority must be 1, 2, or 3")
            job.order = len(self._jobs)
            if not job.enabled:
                job.status = ProcessingStatus.PAUSED
                job.current_step = "Disabled"
            self._jobs[job.job_id] = job

    def ordered(self) -> tuple[ProcessingJob, ...]:
        with self._lock:
            return tuple(
                job.snapshot()
                for job in sorted(
                    self._jobs.values(), key=lambda item: (item.priority, item.order)
                )
            )

    def get(self, job_id: str) -> ProcessingJob:
        with self._lock:
            try:
                return self._jobs[job_id]
            except KeyError as exc:
                raise KeyError(f"Unknown job ID: {job_id}") from exc

    def reorder(self, job_ids: list[str]) -> None:
        with self._lock:
            if len(job_ids) != len(set(job_ids)):
                raise ValueError("Reorder list contains duplicate job IDs")
            if set(job_ids) != set(self._jobs):
                raise ValueError("Reorder list must contain every registered job ID")
            for order, job_id in enumerate(job_ids):
                self._jobs[job_id].order = order
                self._jobs[job_id].priority = _priority_for_order(
                    order, len(job_ids)
                )

    def set_enabled(self, job_id: str, enabled: bool) -> None:
        if not isinstance(enabled, bool):
            raise ValueError("enabled must be boolean")
        with self._lock:
            job = self.get(job_id)
            if job.status is ProcessingStatus.PROCESSING and not enabled:
                raise ProcessingError("A running job must be cancelled, not disabled")
            job.enabled = enabled
            if enabled and job.status is ProcessingStatus.PAUSED:
                job.status = ProcessingStatus.QUEUED
                job.current_step = "Waiting"
            elif not enabled and job.status is ProcessingStatus.QUEUED:
                job.status = ProcessingStatus.PAUSED
                job.current_step = "Disabled"

    def pause(self, job_id: str) -> None:
        with self._lock:
            job = self.get(job_id)
            if job.status is not ProcessingStatus.QUEUED:
                raise ProcessingError("Only queued jobs can be paused")
            job.status = ProcessingStatus.PAUSED
            job.current_step = "Paused"

    def resume(self, job_id: str) -> None:
        with self._lock:
            job = self.get(job_id)
            if job.status is not ProcessingStatus.PAUSED or not job.enabled:
                raise ProcessingError("Only enabled paused jobs can be resumed")
            job.status = ProcessingStatus.QUEUED
            job.current_step = "Waiting"

    def cancel(self, job_id: str) -> None:
        with self._lock:
            job = self.get(job_id)
            if job.status is ProcessingStatus.PROCESSING:
                if not job.safely_cancellable:
                    raise ProcessingError("Running job is not safely cancellable")
                job._cancel_event.set()
                job.current_step = "Cancelling"
            elif job.status in {ProcessingStatus.QUEUED, ProcessingStatus.PAUSED}:
                job.status = ProcessingStatus.CANCELLED
                job.current_step = "Cancelled"
            else:
                raise ProcessingError("Job cannot be cancelled in its current state")

    def retry(self, job_id: str) -> None:
        with self._lock:
            job = self.get(job_id)
            if job.status is not ProcessingStatus.FAILED:
                raise ProcessingError("Only failed jobs can be retried")
            job.status = ProcessingStatus.QUEUED
            job.progress = 0.0
            job.current_step = "Waiting to retry"
            job.elapsed_time = timedelta(0)
            job.error = None
            job._cancel_event.clear()

    def next_for_group(self, group: WorkerGroup) -> ProcessingJob | None:
        with self._lock:
            candidates = [
                job
                for job in self._jobs.values()
                if job.worker_group is group
                and job.enabled
                and job.status is ProcessingStatus.QUEUED
                and job.task is not None
            ]
            if not candidates:
                return None
            return min(candidates, key=lambda item: (item.priority, item.order))


JobCallback = Callable[[ProcessingJob], None]


class ProcessingScheduler:
    """Dispatch jobs independently and publish each result immediately."""

    def __init__(
        self,
        queue: ProcessingPriorityQueue,
        *,
        total_workers: int,
        logger: ProcessingLogManager | None = None,
        result_callback: JobCallback | None = None,
    ) -> None:
        if total_workers < 0:
            raise ValueError("total_workers cannot be negative")
        self.queue = queue
        self.logger = logger
        self.result_callback = result_callback
        capacities = worker_group_capacities(total_workers)
        self._capacities = capacities
        self._executors = {
            group: ThreadPoolExecutor(
                max_workers=count, thread_name_prefix=f"ccflux-{group.value}"
            )
            for group, count in capacities.items()
            if count > 0
        }
        self._running: dict[WorkerGroup, set[Future[None]]] = {
            group: set() for group in WorkerGroup
        }
        self._lock = threading.RLock()

    def dispatch(self) -> None:
        with self._lock:
            for group, capacity in self._capacities.items():
                while len(self._running[group]) < capacity:
                    job = self.queue.next_for_group(group)
                    if job is None:
                        break
                    job.status = ProcessingStatus.PROCESSING
                    job.current_step = "Starting"
                    job._started_monotonic = time.monotonic()
                    if self.result_callback:
                        self.result_callback(job.snapshot())
                    future = self._executors[group].submit(self._run, job)
                    self._running[group].add(future)
                    future.add_done_callback(
                        lambda completed, active_group=group: self._finished(
                            active_group, completed
                        )
                    )

    def shutdown(
        self, wait: bool = True, *, cancel_pending: bool = False
    ) -> None:
        if cancel_pending:
            for snapshot in self.queue.ordered():
                if snapshot.status is ProcessingStatus.PROCESSING:
                    if snapshot.safely_cancellable:
                        self.queue.cancel(snapshot.job_id)
                elif snapshot.status in {
                    ProcessingStatus.QUEUED,
                    ProcessingStatus.PAUSED,
                }:
                    self.queue.cancel(snapshot.job_id)
        for executor in self._executors.values():
            executor.shutdown(wait=wait, cancel_futures=False)

    def _run(self, job: ProcessingJob) -> None:
        started = job._started_monotonic or time.monotonic()
        try:
            outcome = job.task(
                ProcessingContext(
                    job,
                    lambda progress, step: self._progress(job, progress, step),
                )
            )
            job.progress = 100.0
            if outcome and outcome.warning:
                job.status = ProcessingStatus.WARNING
                job.current_step = outcome.warning
            else:
                job.status = ProcessingStatus.COMPLETE
                job.current_step = "Complete"
        except ProcessingCancelledError:
            job.status = ProcessingStatus.CANCELLED
            job.current_step = "Cancelled"
        except Exception as exc:
            failed_step = job.current_step or "processing"
            job.status = ProcessingStatus.FAILED
            job.current_step = f"Failed during {failed_step}"
            job.error = str(exc)
            if self.logger:
                self.logger.capture_exception(
                    "processing-worker",
                    f"Job failed: {job.display_name}",
                    exc,
                    instrument=job.instrument_id,
                    job_id=job.job_id,
                    processing_step=job.current_step,
                )
        finally:
            job.elapsed_time = timedelta(seconds=time.monotonic() - started)
            job._started_monotonic = None
            if self.result_callback:
                self.result_callback(job.snapshot())

    def _progress(self, job: ProcessingJob, progress: float, step: str) -> None:
        job.progress = progress
        job.current_step = step
        if self.result_callback:
            self.result_callback(job.snapshot())

    def _finished(self, group: WorkerGroup, future: Future[None]) -> None:
        with self._lock:
            self._running[group].discard(future)
        self.dispatch()


def worker_group_capacities(total_workers: int) -> dict[WorkerGroup, int]:
    if total_workers <= 0:
        return {group: 0 for group in WorkerGroup}
    metadata = 1 if total_workers >= 2 else 0
    detailed = 1 if total_workers >= 4 else 0
    fast = total_workers - metadata - detailed
    return {
        WorkerGroup.FAST_SCIENCE: fast,
        WorkerGroup.CAMERA_METADATA: metadata,
        WorkerGroup.CAMERA_DETAILED: detailed,
    }


def _priority_for_order(order: int, total: int) -> int:
    if total < 3:
        return order + 1
    first_boundary = max(1, round(total * 0.55))
    second_boundary = max(first_boundary + 1, round(total * 0.8))
    return 1 if order < first_boundary else 2 if order < second_boundary else 3
