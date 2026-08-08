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

import os
import time
from pathlib import Path
from uuid import uuid4

# Windows refuses to rename over, or away from, a file any process holds open,
# and answers WinError 32. It is transient and not the writer's doing: Defender
# opens a freshly written file to scan it, Explorer and the search indexer read
# what appears in a watched folder, and a workspace page may be fetching the
# payload being replaced. On Flight_CC0807 that surfaced as OPC HBX-4 failing
# with "cannot access the file because it is being used by another process"
# while HBX-5, written a moment apart, completed - which is why it looked
# random.
REPLACE_ATTEMPTS = 8
REPLACE_BACKOFF_SECONDS = 0.05


def write_text_atomic(path: Path, text: str, *, encoding: str = "utf-8") -> Path:
    """Write a file so a reader sees the old copy or the new one, never a part.

    The temporary carries this process and a fresh token, because two writers
    sharing one ".tmp" name will overwrite each other's half-written file and
    then race to rename it. The rename is retried, because on Windows it fails
    for reasons that have nothing to do with this program and pass in
    milliseconds.
    """
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(
        f".{target.name}.{os.getpid()}.{uuid4().hex}.writing"
    )
    temporary.write_text(text, encoding=encoding)
    try:
        for attempt in range(REPLACE_ATTEMPTS):
            try:
                os.replace(temporary, target)
                return target
            except PermissionError:
                if attempt == REPLACE_ATTEMPTS - 1:
                    raise
                time.sleep(REPLACE_BACKOFF_SECONDS * (attempt + 1))
    except BaseException:
        # A temporary left behind would be published by the project bundler as
        # if it were a product.
        temporary.unlink(missing_ok=True)
        raise
    return target

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
