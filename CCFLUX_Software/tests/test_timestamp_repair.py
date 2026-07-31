from datetime import datetime, timedelta, timezone

import pandas as pd
import pytest

from core.timestamp_repair import repair_chronology, repair_interval

START = datetime(2026, 7, 27, 8, 0, tzinfo=timezone.utc)


def _frame(stamps):
    return pd.DataFrame({"_time": stamps, "value": range(len(stamps))})


def test_out_of_order_block_is_sorted_and_reported():
    # One backward step, at 7 -> 3, so exactly one transition is reported.
    offsets = (0, 1, 2, 6, 7, 3, 4, 5, 8)
    frame = _frame([(START + timedelta(seconds=s)).isoformat() for s in offsets])

    repaired, report = repair_chronology(frame, "_time")

    assert pd.to_datetime(repaired["_time"], utc=True).is_monotonic_increasing
    assert report.out_of_order_transitions == 1
    assert report.reordered and report.repaired
    assert report.valid_rows == len(offsets)
    assert any("chronological sort" in item for item in report.warnings)


def test_already_chronological_delivery_is_not_reported_as_repaired():
    frame = _frame([(START + timedelta(seconds=s)).isoformat() for s in range(5)])

    repaired, report = repair_chronology(frame, "_time")

    assert list(repaired["value"]) == [0, 1, 2, 3, 4]
    assert not report.repaired
    assert report.warnings == ()


def test_unparsable_rows_are_excluded_rather_than_guessed():
    frame = _frame([START.isoformat(), "not-a-time", "", (START + timedelta(seconds=1)).isoformat()])

    repaired, report = repair_chronology(frame, "_time")

    assert report.invalid_rows == 2
    assert report.valid_rows == 2
    assert len(repaired) == 2
    assert any("no parsable" in item for item in report.warnings)


def test_duplicates_are_only_removed_when_requested():
    stamps = [START.isoformat(), START.isoformat(), (START + timedelta(seconds=1)).isoformat()]

    kept, kept_report = repair_chronology(_frame(stamps), "_time")
    dropped, dropped_report = repair_chronology(
        _frame(stamps), "_time", drop_duplicates=True
    )

    assert kept_report.duplicate_rows == 1
    assert len(kept) == 3
    assert len(dropped) == 2
    assert dropped_report.duplicate_rows == 1


def test_sort_is_stable_for_identical_timestamps():
    stamps = [START.isoformat()] * 3
    repaired, _ = repair_chronology(_frame(stamps), "_time")

    assert list(repaired["value"]) == [0, 1, 2]


def test_missing_column_is_an_explicit_error():
    with pytest.raises(KeyError, match="Timestamp column is missing"):
        repair_chronology(_frame([START.isoformat()]), "absent")


def test_reversed_interval_is_swapped():
    repair = repair_interval(START + timedelta(hours=2), START)

    assert repair.start == START
    assert repair.end == START + timedelta(hours=2)
    assert repair.usable and repair.repaired
    assert any("swapped" in item for item in repair.warnings)


def test_missing_ends_are_filled_from_available_coverage():
    available_end = START + timedelta(hours=5)

    repair = repair_interval(
        None, None, available_start=START, available_end=available_end
    )

    assert (repair.start, repair.end) == (START, available_end)
    assert repair.usable
    assert len(repair.warnings) == 2


def test_interval_reaching_outside_the_data_is_clamped():
    available_end = START + timedelta(hours=5)

    repair = repair_interval(
        START - timedelta(days=7),
        available_end + timedelta(days=7),
        available_start=START,
        available_end=available_end,
    )

    assert (repair.start, repair.end) == (START, available_end)
    assert len(repair.warnings) == 2


def test_zero_length_interval_is_reported_unusable():
    repair = repair_interval(START, START)

    assert not repair.usable
    assert any("identical" in item for item in repair.warnings)


def test_naive_inputs_are_treated_as_utc():
    repair = repair_interval(
        datetime(2026, 7, 27, 8, 0), datetime(2026, 7, 27, 9, 0)
    )

    assert repair.start == START
    assert repair.start.tzinfo is timezone.utc
    assert not repair.repaired
