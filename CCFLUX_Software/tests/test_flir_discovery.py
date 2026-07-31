import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

from core.flir_discovery import (
    describe_selection,
    discover_flir_exports,
    locate_time_window_bytes,
    read_export_coverage,
    select_exports_for_interval,
)

FLIGHT_START = datetime(2026, 7, 27, 5, 20, tzinfo=timezone.utc)


def _export(path: Path, start: datetime, frames: int, *, pad_bytes: int = 0) -> Path:
    """Write a FLIR-shaped JSON stream with one timestamp per frame."""
    path.parent.mkdir(parents=True, exist_ok=True)
    records = []
    for index in range(frames):
        stamp = (start + timedelta(seconds=index)).isoformat().replace("+00:00", "Z")
        records.append(
            {
                "timestamp": {"$date": stamp},
                "raw_stats": {"min": 0, "max": 1, "mean": 0.5},
                # Padding stands in for the pixel array that dominates a real export.
                "raw": [0] * pad_bytes,
            }
        )
    path.write_text(json.dumps(records), encoding="utf-8")
    return path


def test_coverage_is_read_from_the_first_and_last_frame(tmp_path: Path):
    path = _export(tmp_path / "camera.FLIR.json", FLIGHT_START, 20)

    export = read_export_coverage(path)

    assert export.has_coverage
    assert export.utc_start == FLIGHT_START
    assert export.utc_end == FLIGHT_START + timedelta(seconds=19)
    assert export.size_bytes > 0


def test_unreadable_and_empty_exports_report_a_reason(tmp_path: Path):
    empty = tmp_path / "empty.json"
    empty.write_text("", encoding="utf-8")
    nonsense = tmp_path / "nonsense.json"
    nonsense.write_text('{"no":"timestamps here"}', encoding="utf-8")

    assert not read_export_coverage(empty).has_coverage
    assert "empty" in read_export_coverage(empty).reason
    assert "no FLIR timestamp" in read_export_coverage(nonsense).reason
    assert not read_export_coverage(tmp_path / "absent.json").has_coverage


def test_export_covering_the_flight_is_selected_over_a_bench_recording(tmp_path: Path):
    bench = _export(tmp_path / "bench" / "camera.FLIR.json",
                    datetime(2026, 7, 15, 13, 21, tzinfo=timezone.utc), 30)
    flight = _export(tmp_path / "flight" / "camera.FLIR.json", FLIGHT_START, 30)

    exports = discover_flir_exports([tmp_path])
    accepted, rejected = select_exports_for_interval(
        exports, FLIGHT_START, FLIGHT_START + timedelta(seconds=25)
    )

    assert [item.path for item in accepted] == [flight]
    assert [item.path for item in rejected] == [bench]
    summary = describe_selection(accepted, rejected, FLIGHT_START, FLIGHT_START)
    assert "Ignored 1 export" in summary
    # Campaign exports share a filename, so the parent folder disambiguates them.
    assert "bench/camera.FLIR.json" in summary


def test_no_overlapping_export_is_reported_with_what_was_found(tmp_path: Path):
    _export(tmp_path / "camera.FLIR.json",
            datetime(2026, 7, 15, 13, 21, tzinfo=timezone.utc), 10)

    exports = discover_flir_exports([tmp_path])
    accepted, rejected = select_exports_for_interval(
        exports, FLIGHT_START, FLIGHT_START + timedelta(hours=1)
    )

    assert accepted == ()
    message = describe_selection(accepted, rejected, FLIGHT_START, FLIGHT_START)
    assert "No FLIR export covers" in message
    assert "2026-07-15" in message


def test_every_readable_export_is_accepted_when_no_interval_is_set(tmp_path: Path):
    _export(tmp_path / "a.json", FLIGHT_START, 5)
    _export(tmp_path / "b.json", datetime(2026, 1, 1, tzinfo=timezone.utc), 5)

    accepted, rejected = select_exports_for_interval(
        discover_flir_exports([tmp_path]), None, None
    )

    assert len(accepted) == 2
    assert rejected == ()


def test_byte_window_brackets_the_selection_and_never_drops_frames(tmp_path: Path):
    # Padding makes the file large enough for the bisect to narrow meaningfully.
    path = _export(tmp_path / "camera.FLIR.json", FLIGHT_START, 400, pad_bytes=400)
    size = path.stat().st_size
    selection_start = FLIGHT_START + timedelta(seconds=150)
    selection_end = FLIGHT_START + timedelta(seconds=200)

    low, high = locate_time_window_bytes(
        path, selection_start, selection_end, margin_bytes=4096
    )

    assert 0 <= low < high <= size
    window = path.read_bytes()[low:high]
    # Every frame inside the selection must survive inside the window.
    for offset in range(150, 201):
        stamp = (FLIGHT_START + timedelta(seconds=offset)).isoformat()
        assert stamp.replace("+00:00", "Z").encode() in window


def test_byte_window_is_the_whole_file_without_an_interval(tmp_path: Path):
    path = _export(tmp_path / "camera.FLIR.json", FLIGHT_START, 10)

    assert locate_time_window_bytes(path, None, None) == (0, path.stat().st_size)


def test_overlap_seconds_measures_the_shared_interval(tmp_path: Path):
    path = _export(tmp_path / "camera.FLIR.json", FLIGHT_START, 61)
    export = read_export_coverage(path)

    inside = export.overlap_seconds(
        FLIGHT_START + timedelta(seconds=10), FLIGHT_START + timedelta(seconds=40)
    )
    outside = export.overlap_seconds(
        FLIGHT_START + timedelta(days=1), FLIGHT_START + timedelta(days=2)
    )

    assert inside == 30.0
    assert outside == 0.0
