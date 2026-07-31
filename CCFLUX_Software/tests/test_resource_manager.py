from pathlib import Path

import pytest

from core.exceptions import ResourceLimitError
from core.logging_manager import ProcessingLogManager
from core.resource_manager import (
    GIB,
    BoundedThumbnailCache,
    ResourceManager,
    iter_batches,
    iter_memory_bounded_batches,
)


def _manager(
    *,
    cores: int = 8,
    ram: int = 16 * GIB,
    logger: ProcessingLogManager | None = None,
) -> ResourceManager:
    return ResourceManager(
        cpu_detector=lambda: cores,
        ram_detector=lambda: ram,
        logger=logger,
    )


def test_cpu_detection_and_reserved_gui_core():
    manager = _manager(cores=8)

    assert manager.system.total_logical_cores == 8
    assert manager.system.reserved_gui_cores == 1
    assert manager.system.safely_available_workers == 7


def test_single_core_machine_reserves_core_and_allows_no_workers():
    manager = _manager(cores=1)

    assert manager.system.safely_available_workers == 0
    assert manager.validate_worker_count(0) == 0
    with pytest.raises(ResourceLimitError, match="safe limit"):
        manager.validate_worker_count(1)


def test_cpu_limit_and_invalid_input_validation():
    manager = _manager(cores=4)

    assert manager.validate_worker_count(3) == 3
    with pytest.raises(ResourceLimitError, match="reserved"):
        manager.validate_worker_count(4)
    for invalid in (-1, True, 1.5, "2"):
        with pytest.raises((ValueError, ResourceLimitError)):
            manager.validate_worker_count(invalid)


def test_ram_detection_and_safe_estimate():
    manager = _manager(ram=16 * GIB)

    assert manager.system.total_ram_bytes == 16 * GIB
    assert manager.system.safely_available_ram_bytes == 12 * GIB


def test_ram_validation_and_memory_warning_logging(tmp_path: Path):
    logger = ProcessingLogManager(tmp_path / "processing.jsonl")
    manager = _manager(ram=8 * GIB, logger=logger)

    assert manager.validate_memory_budget(4 * GIB) == 4 * GIB
    with pytest.raises(ResourceLimitError, match="safe limit"):
        manager.validate_memory_budget(7 * GIB)
    assert logger.records()[-1].component == "resource-manager"
    assert logger.records()[-1].severity.value == "WARNING"
    for invalid in (0, -1, True, 1.5, "4"):
        with pytest.raises((ValueError, ResourceLimitError)):
            manager.validate_memory_budget(invalid)


def test_worker_environment_exposes_memory_limits():
    manager = _manager()
    limits = manager.create_limits(4, 8 * GIB)

    environment = manager.worker_environment(limits)

    assert environment["CCFLUX_WORKER_COUNT"] == "4"
    assert environment["CCFLUX_MAX_MEMORY_BYTES"] == str(8 * GIB)
    assert environment["CCFLUX_MEMORY_PER_WORKER_BYTES"] == str(2 * GIB)


def test_task_is_postponed_before_estimate_exceeds_budget(tmp_path: Path):
    logger = ProcessingLogManager(tmp_path / "processing.jsonl")
    manager = _manager(logger=logger)
    limits = manager.create_limits(2, 2 * GIB)

    with pytest.raises(ResourceLimitError, match="postponed"):
        manager.admit_task(3 * GIB, limits, task_name="FLIR batch")

    assert "postponed" in logger.records()[-1].message


def test_camera_batches_stream_without_materializing_all_items():
    consumed = []

    def items():
        for value in range(7):
            consumed.append(value)
            yield value

    batches = iter_batches(items(), 3)
    assert next(batches) == (0, 1, 2)
    assert consumed == [0, 1, 2]
    assert list(batches) == [(3, 4, 5), (6,)]


def test_memory_bounded_batches_respect_bytes_and_count():
    batches = list(
        iter_memory_bounded_batches(
            [4, 4, 4, 2],
            size_of=lambda value: value,
            memory_budget_bytes=8,
            maximum_items=3,
        )
    )
    assert batches == [(4, 4), (4, 2)]

    with pytest.raises(ResourceLimitError, match="Single item"):
        list(
            iter_memory_bounded_batches(
                [9],
                size_of=lambda value: value,
                memory_budget_bytes=8,
                maximum_items=3,
            )
        )


def test_thumbnail_cache_evicts_by_memory_and_count():
    cache = BoundedThumbnailCache[str, bytes](
        maximum_count=2,
        maximum_bytes=6,
        size_of=len,
    )
    cache.put("a", b"aaa")
    cache.put("b", b"bb")
    cache.put("c", b"ccc")

    assert len(cache) == 2
    assert cache.get("a") is None
    assert cache.get("b") == b"bb"
    assert cache.current_bytes == 5
