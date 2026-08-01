"""The GUI log views are bounded; the log on disk is not.

Both in-memory lists grew for the lifetime of the process. A campaign day with
camera processing produces a great many entries, and the oldest are the least
useful on screen — but they still have to be recoverable, which is what the
persistent JSONL is for. Investigating a crash reads the file, not the GUI.
"""

from pathlib import Path

import pytest

from core.logging_manager import LogLevel, ProcessingLogManager


def _manager(tmp_path, limit=None):
    return ProcessingLogManager(tmp_path / "diagnostics.jsonl", memory_limit=limit)


def test_the_in_memory_views_stop_growing(tmp_path):
    manager = _manager(tmp_path, limit=50)

    for index in range(500):
        manager.log(LogLevel.INFO, "test", f"entry {index}")

    assert len(manager.records(visible_only=False)) == 50
    assert len(manager.records(visible_only=True)) == 50


def test_the_newest_entries_are_the_ones_kept(tmp_path):
    """An operator looking at the log wants what just happened."""
    manager = _manager(tmp_path, limit=10)

    for index in range(100):
        manager.log(LogLevel.INFO, "test", f"entry {index}")

    messages = [record.message for record in manager.records(visible_only=False)]
    assert messages[-1] == "entry 99"
    assert messages[0] == "entry 90"


def test_nothing_is_lost_from_the_file(tmp_path):
    """The bound is a display limit, not a retention policy."""
    manager = _manager(tmp_path, limit=10)

    for index in range(200):
        manager.log(LogLevel.INFO, "test", f"entry {index}")

    written = (tmp_path / "diagnostics.jsonl").read_text(encoding="utf-8")
    lines = [line for line in written.splitlines() if line.strip()]
    assert len(lines) == 200, "the persistent log must keep every entry"
    assert '"entry 0"' in written, "the oldest entry must still be recoverable"


def test_the_shipped_limit_is_generous_enough_to_be_useful(tmp_path):
    assert ProcessingLogManager.IN_MEMORY_RECORD_LIMIT >= 10_000
    manager = _manager(tmp_path)
    assert manager.memory_limit == ProcessingLogManager.IN_MEMORY_RECORD_LIMIT


def test_filtering_still_works_after_trimming(tmp_path):
    manager = _manager(tmp_path, limit=20)

    for index in range(100):
        severity = LogLevel.WARNING if index % 2 else LogLevel.INFO
        manager.log(severity, "test", f"entry {index}", instrument="sif")

    warnings = manager.records(severities=(LogLevel.WARNING,), visible_only=False)
    assert warnings, "severity filtering must survive the bound"
    assert all(record.severity is LogLevel.WARNING for record in warnings)
    assert all(record.instrument == "sif" for record in warnings)


def test_clearing_the_view_leaves_the_session_records(tmp_path):
    manager = _manager(tmp_path, limit=100)
    for index in range(10):
        manager.log(LogLevel.INFO, "test", f"entry {index}")

    manager.clear_visible()

    assert not manager.records(visible_only=True)
    assert len(manager.records(visible_only=False)) == 10
