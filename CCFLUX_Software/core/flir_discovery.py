"""Locate FLIR JSON exports and read their UTC coverage without full reads.

A campaign disk routinely holds more than one ``camera.FLIR_*.json``: a bench
recording, an earlier flight, and the flight actually being reviewed. They share
a filename, so the one that happens to sit in the selected folder wins by
accident. Choosing by *time coverage* instead of by location removes that class
of mistake.

Coverage is read from the head and tail of each export only. FLIR writes frames
in acquisition order, so the first and last timestamps bound the file; a 39 GB
export is characterised from ~32 MiB.
"""

from __future__ import annotations

import re
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Sequence

EDGE_SCAN_BYTES = 16 * 1024 * 1024
TIMESTAMP_BYTES_RE = re.compile(
    rb'"timestamp"\s*:\s*(?:\{\s*"\$date"\s*:\s*)?"([^"\\]+)"'
)


@dataclass(frozen=True, slots=True)
class FlirExport:
    """One discovered FLIR JSON export and the UTC interval it covers."""

    path: Path
    size_bytes: int
    utc_start: datetime | None = None
    utc_end: datetime | None = None
    reason: str | None = None

    @property
    def has_coverage(self) -> bool:
        return self.utc_start is not None and self.utc_end is not None

    def overlap_seconds(
        self, start: datetime | None, end: datetime | None
    ) -> float:
        """Seconds this export shares with ``start``–``end`` (0.0 if disjoint)."""
        if not self.has_coverage or start is None or end is None:
            return 0.0
        first = max(self.utc_start, start)
        last = min(self.utc_end, end)
        return max(0.0, (last - first).total_seconds())

    @property
    def label(self) -> str:
        """Parent folder plus filename; campaign exports share a filename."""
        parent = self.path.parent.name
        return f"{parent}/{self.path.name}" if parent else self.path.name

    def describe(self) -> str:
        if not self.has_coverage:
            return f"{self.label}: {self.reason or 'no readable timestamps'}"
        return (
            f"{self.label}: {_iso(self.utc_start)} to {_iso(self.utc_end)} UTC "
            f"({self.size_bytes / 1e9:.1f} GB)"
        )


def iter_flir_json_files(roots: Iterable[Path]) -> tuple[Path, ...]:
    """Collect candidate FLIR JSON exports below the supplied roots."""
    found: list[Path] = []
    for value in roots:
        root = Path(value)
        try:
            if root.is_dir():
                found.extend(
                    path for path in root.rglob("*.json") if path.is_file()
                )
            elif root.is_file() and root.suffix.casefold() == ".json":
                found.append(root)
        except OSError:
            continue
    return tuple(sorted(dict.fromkeys(found), key=lambda path: str(path).casefold()))


def read_export_coverage(path: Path) -> FlirExport:
    """Bound one export's UTC interval from its first and last timestamps."""
    path = Path(path)
    try:
        size = path.stat().st_size
    except OSError as exc:
        return FlirExport(path, 0, reason=f"cannot be read: {exc}")
    if size <= 0:
        return FlirExport(path, 0, reason="file is empty")

    offsets = [0] if size <= EDGE_SCAN_BYTES else [0, size - EDGE_SCAN_BYTES]
    matches: list[tuple[int, str]] = []
    try:
        with path.open("rb") as stream:
            for offset in offsets:
                stream.seek(offset)
                data = stream.read(min(EDGE_SCAN_BYTES, size - offset))
                for match in TIMESTAMP_BYTES_RE.finditer(data):
                    try:
                        matches.append(
                            (offset + match.start(), match.group(1).decode("utf-8"))
                        )
                    except UnicodeDecodeError:
                        continue
    except OSError as exc:
        return FlirExport(path, size, reason=f"cannot be read: {exc}")

    if not matches:
        return FlirExport(path, size, reason="no FLIR timestamp field was found")
    matches.sort(key=lambda item: item[0])
    first = _parse_utc(matches[0][1])
    last = _parse_utc(matches[-1][1])
    if first is None or last is None:
        return FlirExport(path, size, reason="timestamps could not be parsed")
    if last < first:
        first, last = last, first
    return FlirExport(path, size, first, last)


def discover_flir_exports(
    roots: Iterable[Path], *, max_workers: int = 4
) -> tuple[FlirExport, ...]:
    """Read coverage for every candidate export, several files at a time."""
    candidates = iter_flir_json_files(roots)
    if not candidates:
        return ()
    workers = max(1, min(max_workers, len(candidates)))
    with ThreadPoolExecutor(
        max_workers=workers, thread_name_prefix="ccflux-flir-discovery"
    ) as executor:
        results = list(executor.map(read_export_coverage, candidates))
    return tuple(
        sorted(
            results,
            key=lambda item: (
                item.utc_start is None,
                item.utc_start or datetime.max.replace(tzinfo=timezone.utc),
            ),
        )
    )


def select_exports_for_interval(
    exports: Sequence[FlirExport],
    start: datetime | None,
    end: datetime | None,
) -> tuple[tuple[FlirExport, ...], tuple[FlirExport, ...]]:
    """Split exports into those overlapping ``start``–``end`` and those not.

    With no interval every readable export is accepted, because there is no
    basis on which to reject one.
    """
    if start is None or end is None:
        accepted = tuple(item for item in exports if item.has_coverage)
        return accepted, tuple(item for item in exports if not item.has_coverage)
    accepted = tuple(
        item for item in exports if item.overlap_seconds(start, end) > 0
    )
    rejected = tuple(item for item in exports if item not in accepted)
    return (
        tuple(
            sorted(
                accepted,
                key=lambda item: item.overlap_seconds(start, end),
                reverse=True,
            )
        ),
        rejected,
    )


def describe_selection(
    accepted: Sequence[FlirExport],
    rejected: Sequence[FlirExport],
    start: datetime | None,
    end: datetime | None,
) -> str:
    """One operator-facing line explaining which exports were used and why."""
    if accepted:
        chosen = "; ".join(item.describe() for item in accepted)
        message = f"Selected {len(accepted)} FLIR export(s) covering the analysis interval: {chosen}"
        if rejected:
            message += (
                f". Ignored {len(rejected)} export(s) outside it: "
                + "; ".join(item.describe() for item in rejected)
            )
        return message
    if rejected:
        return (
            f"No FLIR export covers {_iso(start)} to {_iso(end)} UTC. "
            "Found: " + "; ".join(item.describe() for item in rejected)
            + ". Select the Camera Folder holding the export recorded during "
            "this flight."
        )
    return "No FLIR JSON export was found below the selected folders."


PROBE_BYTES = 4 * 1024 * 1024


def probe_timestamp_at(path: Path, offset: int, size: int) -> datetime | None:
    """First frame timestamp at or after ``offset``, or None near the end."""
    if offset >= size:
        return None
    with path.open("rb") as stream:
        stream.seek(offset)
        data = stream.read(min(PROBE_BYTES, size - offset))
    match = TIMESTAMP_BYTES_RE.search(data)
    return _parse_utc(match.group(1).decode("utf-8", "replace")) if match else None


def locate_time_window_bytes(
    path: Path,
    start: datetime | None,
    end: datetime | None,
    *,
    margin_bytes: int = 8 * 1024 * 1024,
) -> tuple[int, int]:
    """Byte range of ``path`` that can contain frames within ``start``–``end``.

    FLIR writes frames in acquisition order, so timestamps increase with byte
    offset and the boundaries can be bisected. Indexing a narrow selection out
    of a large export then reads only the relevant span instead of the whole
    file. The result is padded by ``margin_bytes`` at both ends and is always a
    superset of the true range, so the per-frame time filter still decides what
    is actually processed — this only avoids reading bytes that cannot match.
    """
    size = path.stat().st_size
    if start is None or end is None or size <= 2 * margin_bytes:
        return 0, size

    def first_at_or_after(target: datetime) -> int:
        low, high = 0, size
        while high - low > PROBE_BYTES:
            middle = (low + high) // 2
            stamp = probe_timestamp_at(path, middle, size)
            if stamp is None or stamp < target:
                low = middle
            else:
                high = middle
        return low

    lower = first_at_or_after(start)
    # Search for the end boundary only in the remaining tail.
    upper_low, upper_high = lower, size
    while upper_high - upper_low > PROBE_BYTES:
        middle = (upper_low + upper_high) // 2
        stamp = probe_timestamp_at(path, middle, size)
        if stamp is None or stamp <= end:
            upper_low = middle
        else:
            upper_high = middle
    return (
        max(0, lower - margin_bytes),
        min(size, upper_high + margin_bytes),
    )


def _parse_utc(value: str) -> datetime | None:
    text = value.strip()
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value else None
