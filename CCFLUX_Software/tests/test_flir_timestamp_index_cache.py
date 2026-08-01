"""Level 2 must not re-read the whole FLIR export to find the same frames.

Indexing the Flight_2707 export is a full pass over 36 GB: measured at 95.8 s
and 388 MiB/s. The index was written to timestamp_index.csv after every run and
never read back, so each Level 2 run paid it again. Loading the cached index
instead takes 0.1 s and returns byte-identical entries.

The cache is only safe while it describes the export actually selected, so
these cases pin the three ways it must refuse: a source that changed on disk, a
different export, and a file that cannot be parsed.
"""

import csv
from pathlib import Path

import pytest

from app.scan_backend import DashboardScanBackend


class _Health:
    """Stand-in for flir_health_temperature's index reader."""

    def __init__(self, entries=None, error=None):
        self.entries = entries
        self.error = error
        self.calls = 0

    def load_timestamp_index(self, path):
        self.calls += 1
        if self.error is not None:
            raise self.error
        return self.entries


def _index_file(tmp_path, sources):
    path = tmp_path / "timestamp_index.csv"
    with path.open("w", encoding="utf-8", newline="") as stream:
        writer = csv.writer(stream)
        writer.writerow(["timestamp_utc", "source_file"])
        for source in sources:
            writer.writerow(["2026-07-27T05:20:00Z", str(source)])
    return path


def _entries(sources):
    return [
        (None, "", Path(source), 1024 * index)
        for index, source in enumerate(sources)
    ]


def test_a_matching_index_is_reused(tmp_path):
    backend = DashboardScanBackend(tmp_path)
    export = tmp_path / "camera.FLIR_Zeppelin.json"
    export.write_bytes(b"x")
    index = _index_file(tmp_path, [export])
    health = _Health(entries=_entries([export]))

    reused = backend._cached_flir_timestamp_index(index, [export], health)

    assert reused is not None and len(reused) == 1
    assert health.calls == 1


def test_a_missing_index_means_scan(tmp_path):
    backend = DashboardScanBackend(tmp_path)
    export = tmp_path / "camera.FLIR_Zeppelin.json"
    export.write_bytes(b"x")
    health = _Health(entries=_entries([export]))

    assert backend._cached_flir_timestamp_index(
        tmp_path / "absent.csv", [export], health
    ) is None
    assert health.calls == 0, "a missing file must not be opened"


def test_a_stale_index_means_scan(tmp_path):
    """load_timestamp_index raises when a source's size or mtime moved on."""
    backend = DashboardScanBackend(tmp_path)
    export = tmp_path / "camera.FLIR_Zeppelin.json"
    export.write_bytes(b"x")
    index = _index_file(tmp_path, [export])
    health = _Health(error=ValueError(f"source changed since index creation: {export}"))

    assert backend._cached_flir_timestamp_index(index, [export], health) is None


def test_an_index_for_another_export_means_scan(tmp_path):
    """Reusing another delivery's byte offsets would read the wrong frames."""
    backend = DashboardScanBackend(tmp_path)
    selected = tmp_path / "flight_b.json"
    other = tmp_path / "flight_a.json"
    for path in (selected, other):
        path.write_bytes(b"x")
    index = _index_file(tmp_path, [other])
    health = _Health(entries=_entries([other]))

    assert backend._cached_flir_timestamp_index(index, [selected], health) is None


def test_a_partial_index_means_scan(tmp_path):
    """Two exports selected, one indexed: the second would be invisible."""
    backend = DashboardScanBackend(tmp_path)
    first = tmp_path / "part_1.json"
    second = tmp_path / "part_2.json"
    for path in (first, second):
        path.write_bytes(b"x")
    index = _index_file(tmp_path, [first])
    health = _Health(entries=_entries([first]))

    assert backend._cached_flir_timestamp_index(
        index, [first, second], health
    ) is None


def test_an_empty_index_means_scan(tmp_path):
    backend = DashboardScanBackend(tmp_path)
    export = tmp_path / "camera.FLIR_Zeppelin.json"
    export.write_bytes(b"x")
    index = _index_file(tmp_path, [export])

    assert backend._cached_flir_timestamp_index(
        index, [export], _Health(entries=[])
    ) is None


def test_an_unreadable_index_never_raises(tmp_path):
    """A damaged cache must cost a rescan, not the run."""
    backend = DashboardScanBackend(tmp_path)
    export = tmp_path / "camera.FLIR_Zeppelin.json"
    export.write_bytes(b"x")
    index = _index_file(tmp_path, [export])

    assert backend._cached_flir_timestamp_index(
        index, [export], _Health(error=RuntimeError("truncated"))
    ) is None


def test_a_health_module_without_a_loader_means_scan(tmp_path):
    backend = DashboardScanBackend(tmp_path)
    export = tmp_path / "camera.FLIR_Zeppelin.json"
    export.write_bytes(b"x")
    index = _index_file(tmp_path, [export])

    class _NoLoader:
        pass

    assert backend._cached_flir_timestamp_index(
        index, [export], _NoLoader()
    ) is None


def test_level2_consults_the_cache_before_scanning():
    source = (Path(__file__).parents[1] / "app" / "scan_backend.py").read_text(
        encoding="utf-8"
    )
    cache = source.index("_cached_flir_timestamp_index(\n            index_path")
    scan = source.index("entries, _ = health_module.scan_timestamps")
    assert cache < scan, "the index must be consulted before the export is read"
    # And the scan must still happen when the cache says no.
    assert "if entries is None:" in source
