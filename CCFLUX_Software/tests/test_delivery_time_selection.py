"""Every instrument processes only what falls inside the selected interval."""

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from app.scan_backend import DashboardScanBackend

FLIGHT_START = datetime(2026, 7, 27, 5, 30, tzinfo=timezone.utc)
FLIGHT_END = datetime(2026, 7, 27, 10, 0, tzinfo=timezone.utc)


def _picarro(path: Path, start: datetime, rows: int = 40) -> Path:
    """A Picarro .dat delivery: whitespace columns with DATE and TIME."""
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = ["DATE TIME CO2_sync CH4_sync"]
    for index in range(rows):
        stamp = start + timedelta(seconds=index)
        lines.append(
            f"{stamp:%Y-%m-%d} {stamp:%H:%M:%S}.000 410.0 1.9"
        )
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


@pytest.fixture()
def backend(tmp_path):
    return DashboardScanBackend(tmp_path)


def test_files_outside_the_interval_are_excluded(backend, tmp_path):
    inside = _picarro(tmp_path / "in.dat", FLIGHT_START + timedelta(hours=1))
    earlier = _picarro(tmp_path / "early.dat", FLIGHT_START - timedelta(days=1))
    later = _picarro(tmp_path / "late.dat", FLIGHT_END + timedelta(days=1))

    kept = backend._deliveries_for_interval(
        "picarro", (earlier, inside, later), FLIGHT_START, FLIGHT_END
    )

    assert kept == (inside,)


def test_a_single_file_is_never_inspected(backend, tmp_path):
    only = _picarro(tmp_path / "only.dat", FLIGHT_START - timedelta(days=10))

    # One file is the delivery by definition; rejecting it would leave nothing
    # to process and cost a read to learn that.
    assert backend._deliveries_for_interval(
        "picarro", (only,), FLIGHT_START, FLIGHT_END
    ) == (only,)


def test_files_whose_coverage_cannot_be_read_are_kept(backend, tmp_path):
    inside = _picarro(tmp_path / "in.dat", FLIGHT_START + timedelta(hours=1))
    unreadable = tmp_path / "unreadable.dat"
    unreadable.write_text("no columns here\n", encoding="utf-8")

    kept = backend._deliveries_for_interval(
        "picarro", (inside, unreadable), FLIGHT_START, FLIGHT_END
    )

    # Being unable to judge a file is not evidence it belongs elsewhere.
    assert set(kept) == {inside, unreadable}


def test_no_overlapping_file_names_what_was_found(backend, tmp_path):
    earlier = _picarro(tmp_path / "early.dat", FLIGHT_START - timedelta(days=1))
    later = _picarro(tmp_path / "late.dat", FLIGHT_END + timedelta(days=1))

    with pytest.raises(RuntimeError, match="No picarro source file covers"):
        backend._deliveries_for_interval(
            "picarro", (earlier, later), FLIGHT_START, FLIGHT_END
        )


def test_selection_is_skipped_without_an_interval(backend, tmp_path):
    first = _picarro(tmp_path / "a.dat", FLIGHT_START)
    second = _picarro(tmp_path / "b.dat", FLIGHT_START - timedelta(days=5))

    assert backend._deliveries_for_interval(
        "picarro", (first, second), None, None
    ) == (first, second)


def test_large_deliveries_are_left_alone(backend, tmp_path, monkeypatch):
    first = _picarro(tmp_path / "a.dat", FLIGHT_START)
    second = _picarro(tmp_path / "b.dat", FLIGHT_START - timedelta(days=5))
    # Reading a very large set twice would cost more than it saves.
    monkeypatch.setattr(backend, "DELIVERY_SELECTION_MAX_BYTES", 1)

    assert backend._deliveries_for_interval(
        "picarro", (first, second), FLIGHT_START, FLIGHT_END
    ) == (first, second)


def test_micasense_unset_camera_clock_is_reported_not_treated_as_coverage():
    """A camera dated from the epoch must not look like a different flight."""
    from core.time_extraction import _is_unset_camera_clock

    assert _is_unset_camera_clock("1970:01:01 00:00:10")
    assert _is_unset_camera_clock("1970-01-01 00:00:10")
    assert not _is_unset_camera_clock("2026:07:27 05:19:52")
    assert not _is_unset_camera_clock("")
