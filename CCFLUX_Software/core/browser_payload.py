"""Bound what a workspace page is asked to draw, without hiding a spike.

A FLIR flight produces one record per frame, and there are far more frames than
a plot has pixels or a map has useful markers. Sending every one of them made
the browser parse tens of megabytes and then build every trace and every marker
on the main thread, which it cannot interrupt: the page sat behind its loading
overlay with nothing moving, because nothing could repaint until the work
finished.

Plain stride decimation would fix the cost and lose the science - the one frame
carrying a temperature spike is exactly the one an even stride is likely to
drop. So the series is bucketed, and each bucket contributes its first record
together with the records holding that bucket's extreme values. The envelope a
reader sees is therefore the real envelope, at a size a browser can draw.

The full-resolution record stays in temperature_frames.csv; this governs the
view only.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from typing import Any

# Comfortably beyond a screen's worth of detail, far below a flight's worth.
DEFAULT_VIEW_LIMIT = 6000


def _numeric(value: Any) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if number == number else None      # NaN is not a value


def decimate_for_view(
    records: Sequence[Mapping[str, Any]],
    *,
    limit: int = DEFAULT_VIEW_LIMIT,
    extreme_fields: Iterable[str] = (),
) -> tuple[list[Mapping[str, Any]], int]:
    """Return (records to draw, how many there were).

    Everything is returned unchanged when it already fits. Otherwise the series
    is divided into equal buckets and each contributes its first record plus the
    records holding the smallest and largest value of every named field, so no
    extreme is lost. Order is preserved and no record is duplicated.
    """
    total = len(records)
    if limit <= 0:
        raise ValueError("limit must be positive")
    if total <= limit:
        return list(records), total

    fields = tuple(extreme_fields)
    # Each bucket may contribute its first record and two extremes per field.
    # One place is reserved for the final record, which is forced in below and
    # is not the first of any bucket.
    per_bucket = 1 + 2 * len(fields)
    buckets = max(1, (limit - 1) // per_bucket)
    keep: set[int] = {0, total - 1}

    for bucket in range(buckets):
        start = (bucket * total) // buckets
        end = ((bucket + 1) * total) // buckets
        if start >= end:
            continue
        keep.add(start)
        for field in fields:
            lowest = highest = None
            low_index = high_index = start
            for index in range(start, end):
                value = _numeric(records[index].get(field))
                if value is None:
                    continue
                if lowest is None or value < lowest:
                    lowest, low_index = value, index
                if highest is None or value > highest:
                    highest, high_index = value, index
            if lowest is not None:
                keep.add(low_index)
                keep.add(high_index)

    return [records[index] for index in sorted(keep)], total
