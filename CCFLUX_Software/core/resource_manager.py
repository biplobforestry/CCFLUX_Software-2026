"""Conservative CPU/RAM policy and bounded-processing utilities."""

from __future__ import annotations

import gc
import os
import subprocess
import sys
from collections import OrderedDict
from dataclasses import dataclass
from itertools import islice
from typing import Callable, Generic, Iterable, Iterator, TypeVar

from .exceptions import ResourceLimitError
from .logging_manager import ProcessingLogManager

GIB = 1024**3
DEFAULT_SAFE_RAM_FRACTION = 0.75
DEFAULT_RESERVED_RAM_BYTES = GIB

T = TypeVar("T")
K = TypeVar("K")
V = TypeVar("V")


@dataclass(frozen=True, slots=True)
class SystemResources:
    total_logical_cores: int
    reserved_gui_cores: int
    safely_available_workers: int
    total_ram_bytes: int
    safely_available_ram_bytes: int


@dataclass(frozen=True, slots=True)
class ResourceLimits:
    cpu_cores: int
    memory_bytes: int
    reserved_gui_cores: int = 1

    @property
    def worker_count(self) -> int:
        return self.cpu_cores

    def __post_init__(self) -> None:
        if isinstance(self.cpu_cores, bool) or self.cpu_cores < 0:
            raise ValueError("worker_count cannot be negative")
        if isinstance(self.memory_bytes, bool) or self.memory_bytes < 1:
            raise ValueError("memory_bytes must be positive")
        if isinstance(self.reserved_gui_cores, bool) or self.reserved_gui_cores < 1:
            raise ValueError("reserved_gui_cores must be at least one")


@dataclass(frozen=True, slots=True)
class CameraBatchPolicy:
    maximum_batch_files: int = 32
    maximum_thumbnail_count: int = 128
    maximum_thumbnail_bytes: int = 256 * 1024 * 1024

    def __post_init__(self) -> None:
        if self.maximum_batch_files < 1:
            raise ValueError("maximum_batch_files must be positive")
        if self.maximum_thumbnail_count < 1:
            raise ValueError("maximum_thumbnail_count must be positive")
        if self.maximum_thumbnail_bytes < 1:
            raise ValueError("maximum_thumbnail_bytes must be positive")


class ResourceManager:
    """Detect resources and validate allocations before worker admission."""

    def __init__(
        self,
        *,
        reserved_gui_cores: int = 1,
        safe_ram_fraction: float = DEFAULT_SAFE_RAM_FRACTION,
        reserved_ram_bytes: int = DEFAULT_RESERVED_RAM_BYTES,
        cpu_detector: Callable[[], int | None] = os.cpu_count,
        ram_detector: Callable[[], int] | None = None,
        logger: ProcessingLogManager | None = None,
    ) -> None:
        if isinstance(reserved_gui_cores, bool) or reserved_gui_cores < 1:
            raise ValueError("reserved_gui_cores must be at least one")
        if not 0 < safe_ram_fraction < 1:
            raise ValueError("safe_ram_fraction must be between zero and one")
        if reserved_ram_bytes < 0:
            raise ValueError("reserved_ram_bytes cannot be negative")
        self._cpu_detector = cpu_detector
        self._ram_detector = ram_detector or detect_total_ram_bytes
        self.reserved_gui_cores = reserved_gui_cores
        self.safe_ram_fraction = safe_ram_fraction
        self.reserved_ram_bytes = reserved_ram_bytes
        self.logger = logger
        self.system = self.detect()

    def detect(self) -> SystemResources:
        cores = self._cpu_detector()
        if isinstance(cores, bool) or not isinstance(cores, int) or cores < 1:
            raise ResourceLimitError("Could not detect a valid logical CPU count")
        total_ram = self._ram_detector()
        if isinstance(total_ram, bool) or not isinstance(total_ram, int) or total_ram < 1:
            raise ResourceLimitError("Could not detect a valid total RAM value")
        safe_workers = max(0, cores - self.reserved_gui_cores)
        safe_ram = min(
            int(total_ram * self.safe_ram_fraction),
            max(0, total_ram - self.reserved_ram_bytes),
        )
        if safe_ram < 1:
            raise ResourceLimitError("No safe RAM budget is available")
        return SystemResources(
            total_logical_cores=cores,
            reserved_gui_cores=self.reserved_gui_cores,
            safely_available_workers=safe_workers,
            total_ram_bytes=total_ram,
            safely_available_ram_bytes=safe_ram,
        )

    def validate_worker_count(self, value: object) -> int:
        if isinstance(value, bool) or not isinstance(value, int):
            raise ValueError("Worker count must be an integer")
        if value < 0:
            raise ValueError("Worker count cannot be negative")
        if value > self.system.safely_available_workers:
            raise ResourceLimitError(
                f"Worker count {value} exceeds the safe limit of "
                f"{self.system.safely_available_workers}; "
                f"{self.reserved_gui_cores} core(s) are reserved"
            )
        return value

    def validate_memory_budget(self, value: object) -> int:
        if isinstance(value, bool) or not isinstance(value, int):
            raise ValueError("RAM budget must be an integer number of bytes")
        if value < 1:
            raise ValueError("RAM budget must be positive")
        if value > self.system.safely_available_ram_bytes:
            self._warn(
                f"Requested RAM budget {value} exceeds safe limit "
                f"{self.system.safely_available_ram_bytes}"
            )
            raise ResourceLimitError(
                f"RAM budget exceeds safe limit of "
                f"{self.system.safely_available_ram_bytes} bytes"
            )
        return value

    def create_limits(self, worker_count: object, memory_bytes: object) -> ResourceLimits:
        return ResourceLimits(
            cpu_cores=self.validate_worker_count(worker_count),
            memory_bytes=self.validate_memory_budget(memory_bytes),
            reserved_gui_cores=self.reserved_gui_cores,
        )

    def memory_budget_per_worker(self, limits: ResourceLimits) -> int:
        if limits.worker_count == 0:
            return limits.memory_bytes
        return limits.memory_bytes // limits.worker_count

    def worker_environment(self, limits: ResourceLimits) -> dict[str, str]:
        """Serializable limits for future worker-process initialization."""
        return {
            "CCFLUX_WORKER_COUNT": str(limits.worker_count),
            "CCFLUX_MAX_MEMORY_BYTES": str(limits.memory_bytes),
            "CCFLUX_MEMORY_PER_WORKER_BYTES": str(
                self.memory_budget_per_worker(limits)
            ),
        }

    def admit_task(
        self,
        estimated_peak_bytes: int,
        limits: ResourceLimits,
        *,
        task_name: str,
    ) -> None:
        if isinstance(estimated_peak_bytes, bool) or estimated_peak_bytes < 0:
            raise ValueError("estimated_peak_bytes must be non-negative")
        if estimated_peak_bytes > limits.memory_bytes:
            message = (
                f"Task {task_name!r} postponed: estimated peak memory "
                f"{estimated_peak_bytes} exceeds budget {limits.memory_bytes}"
            )
            self._warn(message)
            raise ResourceLimitError(message)

    def _warn(self, message: str) -> None:
        if self.logger is not None:
            self.logger.resource_warning(message)


def detect_total_ram_bytes() -> int:
    """Detect physical RAM without requiring psutil."""
    if sys.platform == "win32":
        import ctypes

        class _MemoryStatus(ctypes.Structure):
            _fields_ = [
                ("length", ctypes.c_ulong),
                ("memory_load", ctypes.c_ulong),
                ("total_physical", ctypes.c_ulonglong),
                ("available_physical", ctypes.c_ulonglong),
                ("total_page_file", ctypes.c_ulonglong),
                ("available_page_file", ctypes.c_ulonglong),
                ("total_virtual", ctypes.c_ulonglong),
                ("available_virtual", ctypes.c_ulonglong),
                ("available_extended_virtual", ctypes.c_ulonglong),
            ]

        status = _MemoryStatus()
        status.length = ctypes.sizeof(status)
        if not ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status)):
            raise ResourceLimitError("Windows RAM detection failed")
        return int(status.total_physical)

    try:
        page_size = os.sysconf("SC_PAGE_SIZE")
        pages = os.sysconf("SC_PHYS_PAGES")
        total = int(page_size) * int(pages)
        if total > 0:
            return total
    except (AttributeError, OSError, ValueError):
        pass

    if sys.platform == "darwin":
        completed = subprocess.run(
            ["sysctl", "-n", "hw.memsize"],
            capture_output=True,
            text=True,
            check=False,
        )
        if completed.returncode == 0:
            try:
                total = int(completed.stdout.strip())
                if total > 0:
                    return total
            except ValueError:
                pass
    raise ResourceLimitError("Could not detect total system RAM")


def iter_batches(items: Iterable[T], batch_size: int) -> Iterator[tuple[T, ...]]:
    """Stream fixed-size camera/file batches without materializing the dataset."""
    if isinstance(batch_size, bool) or batch_size < 1:
        raise ValueError("batch_size must be positive")
    iterator = iter(items)
    while batch := tuple(islice(iterator, batch_size)):
        yield batch


def iter_memory_bounded_batches(
    items: Iterable[T],
    *,
    size_of: Callable[[T], int],
    memory_budget_bytes: int,
    maximum_items: int,
) -> Iterator[tuple[T, ...]]:
    """Yield batches constrained by both estimated bytes and item count."""
    if memory_budget_bytes < 1 or maximum_items < 1:
        raise ValueError("memory budget and maximum_items must be positive")
    batch: list[T] = []
    batch_bytes = 0
    for item in items:
        item_bytes = size_of(item)
        if isinstance(item_bytes, bool) or item_bytes < 0:
            raise ValueError("size_of must return a non-negative integer")
        if item_bytes > memory_budget_bytes:
            raise ResourceLimitError(
                f"Single item estimate {item_bytes} exceeds batch memory budget "
                f"{memory_budget_bytes}"
            )
        if batch and (
            len(batch) >= maximum_items
            or batch_bytes + item_bytes > memory_budget_bytes
        ):
            yield tuple(batch)
            batch.clear()
            batch_bytes = 0
        batch.append(item)
        batch_bytes += item_bytes
    if batch:
        yield tuple(batch)


class BoundedThumbnailCache(Generic[K, V]):
    """LRU thumbnail cache bounded by count and estimated memory."""

    def __init__(
        self,
        *,
        maximum_count: int,
        maximum_bytes: int,
        size_of: Callable[[V], int],
    ) -> None:
        if maximum_count < 1 or maximum_bytes < 1:
            raise ValueError("thumbnail cache limits must be positive")
        self.maximum_count = maximum_count
        self.maximum_bytes = maximum_bytes
        self.size_of = size_of
        self._values: OrderedDict[K, tuple[V, int]] = OrderedDict()
        self.current_bytes = 0

    def put(self, key: K, value: V) -> None:
        size = self.size_of(value)
        if size < 0:
            raise ValueError("thumbnail size cannot be negative")
        if size > self.maximum_bytes:
            return
        previous = self._values.pop(key, None)
        if previous:
            self.current_bytes -= previous[1]
        self._values[key] = (value, size)
        self.current_bytes += size
        while (
            len(self._values) > self.maximum_count
            or self.current_bytes > self.maximum_bytes
        ):
            _, (_, removed_size) = self._values.popitem(last=False)
            self.current_bytes -= removed_size

    def get(self, key: K) -> V | None:
        entry = self._values.pop(key, None)
        if entry is None:
            return None
        self._values[key] = entry
        return entry[0]

    def clear(self) -> None:
        self._values.clear()
        self.current_bytes = 0

    def __len__(self) -> int:
        return len(self._values)


def release_large_temporary(value: object) -> None:
    """Best-effort cleanup; callers must also drop their own reference."""
    close = getattr(value, "close", None)
    if callable(close):
        close()
    clear = getattr(value, "clear", None)
    if callable(clear):
        clear()
    gc.collect()
