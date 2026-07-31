"""Repair broken and out-of-order time information without discarding evidence.

Campaign deliveries are not always clean: a logger restarts and writes a block
out of order, an export rotates around midnight, a row arrives with no parsable
timestamp, or an operator types an interval backwards. None of that should stop
a review, but none of it may be corrected silently either — every repair here
returns a report the caller is expected to log and surface.

Raw files are never modified. Repairs apply to in-memory frames and to the
interval the operator selected.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any


@dataclass(frozen=True, slots=True)
class ChronologyRepair:
    """What had to be done to put a delivery in chronological order."""

    total_rows: int = 0
    valid_rows: int = 0
    invalid_rows: int = 0
    duplicate_rows: int = 0
    out_of_order_transitions: int = 0
    reordered: bool = False
    warnings: tuple[str, ...] = ()

    @property
    def repaired(self) -> bool:
        return bool(
            self.reordered or self.invalid_rows or self.duplicate_rows
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "total_rows": self.total_rows,
            "valid_rows": self.valid_rows,
            "invalid_rows": self.invalid_rows,
            "duplicate_rows": self.duplicate_rows,
            "out_of_order_transitions": self.out_of_order_transitions,
            "reordered": self.reordered,
            "repaired": self.repaired,
            "warnings": list(self.warnings),
        }


def repair_chronology(
    frame: Any,
    column: str,
    *,
    drop_invalid: bool = True,
    drop_duplicates: bool = False,
) -> tuple[Any, ChronologyRepair]:
    """Return ``frame`` in chronological order plus a report of the repairs.

    Rows whose timestamp cannot be parsed are dropped by default rather than
    being silently reinterpreted, because a guessed instant is worse than a
    declared gap. The sort is stable, so rows sharing a timestamp keep their
    delivered order.
    """
    import pandas as pd

    if column not in getattr(frame, "columns", ()):
        raise KeyError(f"Timestamp column is missing: {column}")

    total = int(len(frame))
    parsed = pd.to_datetime(frame[column], errors="coerce", utc=True)
    invalid_mask = parsed.isna()
    invalid = int(invalid_mask.sum())

    working = frame.loc[~invalid_mask].copy() if drop_invalid else frame.copy()
    valid_times = parsed.loc[~invalid_mask] if drop_invalid else parsed
    transitions = int((valid_times.diff().dt.total_seconds() < 0).sum())
    duplicates = int(valid_times.duplicated().sum())

    warnings: list[str] = []
    if invalid:
        warnings.append(
            f"{invalid:,} of {total:,} row(s) had no parsable {column} value and "
            + ("were excluded from the chronological series."
               if drop_invalid else "were retained unsorted.")
        )
    if transitions:
        warnings.append(
            f"{transitions:,} out-of-order {column} transition(s) were corrected "
            "with a stable chronological sort. The delivered file is unchanged."
        )
    if duplicates:
        warnings.append(
            f"{duplicates:,} duplicated {column} value(s) were detected and "
            + ("removed, keeping the first occurrence."
               if drop_duplicates else "retained.")
        )

    order_column = "__ccflux_chronology"
    working[order_column] = valid_times.to_numpy()
    working = working.sort_values(order_column, kind="stable")
    if drop_duplicates:
        working = working.drop_duplicates(order_column, keep="first")
    working = working.drop(columns=order_column)

    return working, ChronologyRepair(
        total_rows=total,
        valid_rows=int(len(working)),
        invalid_rows=invalid,
        duplicate_rows=duplicates,
        out_of_order_transitions=transitions,
        reordered=transitions > 0,
        warnings=tuple(warnings),
    )


@dataclass(frozen=True, slots=True)
class IntervalRepair:
    """A usable analysis interval, and what had to be changed to get one."""

    start: datetime | None
    end: datetime | None
    warnings: tuple[str, ...] = field(default=())

    @property
    def repaired(self) -> bool:
        return bool(self.warnings)

    @property
    def usable(self) -> bool:
        return (
            self.start is not None
            and self.end is not None
            and self.start < self.end
        )


def repair_interval(
    start: datetime | None,
    end: datetime | None,
    *,
    available_start: datetime | None = None,
    available_end: datetime | None = None,
) -> IntervalRepair:
    """Coerce a possibly broken interval into a usable one, reporting each fix.

    Handles the cases seen in practice: the two ends supplied the wrong way
    round, one end missing, and an interval that reaches outside the data. A
    request that cannot be repaired at all is returned unusable so the caller
    can refuse it with a specific message.
    """
    warnings: list[str] = []
    start = _as_utc(start)
    end = _as_utc(end)
    available_start = _as_utc(available_start)
    available_end = _as_utc(available_end)

    if start is not None and end is not None and start > end:
        start, end = end, start
        warnings.append(
            "The analysis start was later than the end; the two were swapped."
        )
    if start is None and available_start is not None:
        start = available_start
        warnings.append(
            "No analysis start was set; the earliest available time was used."
        )
    if end is None and available_end is not None:
        end = available_end
        warnings.append(
            "No analysis end was set; the latest available time was used."
        )
    if start is not None and available_start is not None and start < available_start:
        start = available_start
        warnings.append(
            "The analysis start was before the available data and was moved to "
            "the earliest available time."
        )
    if end is not None and available_end is not None and end > available_end:
        end = available_end
        warnings.append(
            "The analysis end was after the available data and was moved to the "
            "latest available time."
        )
    if start is not None and end is not None and start == end:
        warnings.append(
            "The analysis start and end are identical, so no data can be selected."
        )
    return IntervalRepair(start, end, tuple(warnings))


def _as_utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)
