"""The frame index is built over the bytes needed, and survives the run.

Two costs were being paid on every FLIR run over Flight_CC0806's 32 GB export:

  * the whole file was indexed however narrow the Time Filter was, because
    ``locate_time_window_bytes`` was imported and never called; and
  * the index was written under ``_run_output_root``, which stamps a new folder
    per attempt, so the next run looked elsewhere and the cache could never hit.

Windowing an index makes it partial, so reuse now has to prove the saved index
covers the bytes the new request needs.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

pytest.importorskip("numpy")

from app.scan_backend import DashboardScanBackend
from core.flir_discovery import locate_time_window_bytes
from instruments.flir.level2_bridge import LegacyFlirLevel2Bridge

health = LegacyFlirLevel2Bridge().health

# These cases exercise indexing and document boundaries only - no temperature
# conversion - so the raw array is kept small deliberately.
ROWS, COLUMNS = 8, 16


def _document(index: int) -> str:
    stamp = f"2026-08-06T05:{30 + index:02d}:00.000Z"
    grid = [[24200 + index] * COLUMNS for _ in range(ROWS)]
    return json.dumps(
        {
            "_id": {"$oid": f"6a741c587e847e8a2a6290{index:02x}"},
            "timestamp": {"$date": stamp},
            "calibration": {"R": 23844.27, "B": 1537.8},
            "raw_stats": {"min": 0, "max": 1, "mean": 0.5},
            "raw": grid,
        },
        separators=(",", ":"),
    )


def _export(path: Path, count: int) -> Path:
    path.write_text(
        "\n".join(_document(index) for index in range(count)) + "\n",
        encoding="utf-8",
    )
    return path


def _all_entries(path: Path):
    _, found = health.scan_one_range((path, 0, path.stat().st_size, 4096))
    return found


class TestOnlyTheNeededBytesAreRead:
    def test_a_window_indexes_only_the_frames_inside_it(self, tmp_path):
        path = _export(tmp_path / "export.json", 6)
        every = _all_entries(path)
        assert len(every) == 6
        window = (every[2][3] - 10, every[4][3] + 10)

        entries, _ = health.scan_timestamps([path], 2, 1, {path: window})

        assert [entry[0] for entry in entries] == [
            every[2][0], every[3][0], every[4][0]
        ]

    def test_no_window_still_reads_the_whole_file(self, tmp_path):
        path = _export(tmp_path / "export.json", 5)

        entries, _ = health.scan_timestamps([path], 2, 1)

        assert len(entries) == 5

    def test_a_window_is_clamped_to_the_file(self, tmp_path):
        path = _export(tmp_path / "export.json", 3)
        size = path.stat().st_size

        entries, _ = health.scan_timestamps([path], 2, 1, {path: (-500, size * 4)})

        assert len(entries) == 3

    def test_the_located_window_is_a_superset_of_the_interval(self, tmp_path):
        """The bisect must never exclude a frame the filter asks for."""
        path = _export(tmp_path / "export.json", 8)
        every = _all_entries(path)
        start, end = every[3][0], every[5][0]

        low, high = locate_time_window_bytes(path, start, end)
        entries, _ = health.scan_timestamps([path], 2, 1, {path: (low, high)})

        stamps = {entry[0] for entry in entries}
        assert {every[3][0], every[4][0], every[5][0]} <= stamps


class TestBoundariesStillResolveInsideAWindow:
    def test_a_windowed_index_finds_the_same_document_starts(self, tmp_path):
        """Starting mid-file must not change where a document begins."""
        path = _export(tmp_path / "export.json", 6)
        every = _all_entries(path)
        full = health.object_spans(every)

        windowed = health.object_spans(every[3:])

        assert [start for start, _ in windowed] == [start for start, _ in full[3:]]

    def test_the_first_frame_search_is_bounded(self, tmp_path, monkeypatch):
        """Without a bound this reads back to byte zero of a 32 GB export."""
        path = _export(tmp_path / "export.json", 6)
        every = _all_entries(path)
        # Far smaller than the gap between frames, so the floor is reached
        # before any separator is seen.
        monkeypatch.setattr(health, "MAX_DOCUMENT_SEARCH_BYTES", 8)

        with pytest.raises(ValueError, match="no document separator was found"):
            health.object_spans(every[3:])

    def test_the_bound_is_generous_enough_for_a_real_frame(self):
        # A 640x480 array of decimal counts is under 2 MB.
        assert health.MAX_DOCUMENT_SEARCH_BYTES >= 32 * 1024 * 1024


class _Health:
    """Stand-in for the index reader, as in test_flir_timestamp_index_cache."""

    def __init__(self, entries):
        self.entries = entries
        self.calls = 0

    def load_timestamp_index(self, path):
        self.calls += 1
        return self.entries


def _index_and_coverage(backend, tmp_path, export, window):
    index = tmp_path / backend.FLIR_INDEX_NAME
    index.write_text("timestamp_utc,source_file\n", encoding="utf-8")
    backend._write_flir_index_coverage(
        tmp_path / backend.FLIR_INDEX_COVERAGE_NAME, {export: window}
    )
    return index


class TestTheIndexIsOnlyReusedWhenItCovers:
    def test_it_lives_outside_the_per_attempt_run_folder(self, tmp_path):
        from core.flight_project import FlightProject

        project = FlightProject(
            flight_id="Flight_CC0806",
            flight_folder_path=tmp_path / "raw",
            output_folder_path=tmp_path / "out",
        )
        root = DashboardScanBackend._flir_index_root(project)

        assert root == project.flight_output_root / "processed" / "flir"
        # The run folder is stamped per attempt; the index root is not, which is
        # the whole difference between a cache that can hit and one that cannot.
        run = DashboardScanBackend._run_output_root(project, "flir")
        assert "runs" in run.parts
        assert run.name.endswith("Z") and run.parent.name == "runs"
        assert "runs" not in root.parts
        assert root in run.parents

    def test_a_narrower_request_reuses_a_wider_index(self, tmp_path):
        backend = DashboardScanBackend(tmp_path)
        export = tmp_path / "camera.FLIR.json"
        export.write_bytes(b"x" * 5000)
        index = _index_and_coverage(backend, tmp_path, export, (0, 5000))
        stub = _Health([(None, "", export, 10)])

        reused = backend._cached_flir_timestamp_index(
            index, [export], stub, {export: (1000, 4000)}
        )

        assert reused is not None
        assert stub.calls == 1

    def test_a_wider_request_refuses_a_narrow_index(self, tmp_path):
        """Otherwise the run silently processes a fraction of the interval."""
        backend = DashboardScanBackend(tmp_path)
        export = tmp_path / "camera.FLIR.json"
        export.write_bytes(b"x" * 5000)
        index = _index_and_coverage(backend, tmp_path, export, (1000, 4000))
        stub = _Health([(None, "", export, 10)])

        assert backend._cached_flir_timestamp_index(
            index, [export], stub, {export: (0, 5000)}
        ) is None

    def test_an_index_without_a_coverage_record_is_refused(self, tmp_path):
        """It cannot prove what it holds, so it cannot be trusted."""
        backend = DashboardScanBackend(tmp_path)
        export = tmp_path / "camera.FLIR.json"
        export.write_bytes(b"x" * 5000)
        index = tmp_path / backend.FLIR_INDEX_NAME
        index.write_text("timestamp_utc,source_file\n", encoding="utf-8")

        assert backend._cached_flir_timestamp_index(
            index, [export], _Health([(None, "", export, 10)]), {export: (0, 5000)}
        ) is None

    def test_a_changed_export_is_refused(self, tmp_path):
        backend = DashboardScanBackend(tmp_path)
        export = tmp_path / "camera.FLIR.json"
        export.write_bytes(b"x" * 5000)
        index = _index_and_coverage(backend, tmp_path, export, (0, 5000))
        export.write_bytes(b"y" * 9000)

        assert backend._cached_flir_timestamp_index(
            index, [export], _Health([(None, "", export, 10)]), {export: (0, 5000)}
        ) is None

    def test_omitting_the_window_keeps_the_previous_behaviour(self, tmp_path):
        """The existing callers and tests pass no window and must be unaffected."""
        backend = DashboardScanBackend(tmp_path)
        export = tmp_path / "camera.FLIR.json"
        export.write_bytes(b"x" * 5000)
        index = tmp_path / backend.FLIR_INDEX_NAME
        index.write_text("timestamp_utc,source_file\n", encoding="utf-8")

        reused = backend._cached_flir_timestamp_index(
            index, [export], _Health([(None, "", export, 10)])
        )

        assert reused is not None


def test_the_task_windows_the_scan_and_shares_the_index():
    source = (Path(__file__).parents[1] / "app" / "scan_backend.py").read_text(
        encoding="utf-8"
    )
    body = source[source.index("def _flir_detailed_task("):]
    body = body[: body.find("\n    def ", 1)]

    assert "locate_time_window_bytes(path, selected_start, selected_end)" in body
    assert "_flir_index_root(project)" in body
    assert "windows," in body, "the window must reach scan_timestamps"
    # Written only after the index it vouches for.
    assert body.index("write_timestamp_index") < body.index(
        "_write_flir_index_coverage"
    )
