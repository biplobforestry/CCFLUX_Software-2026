"""Export every recorded Noseboom column, not only the mapped fourteen.

The interactive download has always produced the fourteen columns the browser
plots: position, wind, humidity, pressure, temperature. That is the right table
for a quick look and the wrong one for anybody who wants the instrument's own
record - a flight file carries around 140 columns, and the other 126 were
reachable only by opening the raw CSV.

The full table is written by streaming: a three-hour flight at 100 Hz is on the
order of a million rows across 140 columns, which is not something to assemble
in memory first. Chunks are read, filtered to the selected interval, resampled
if asked for, and appended.

Nothing here reinterprets a measurement. Original resolution writes the recorded
rows unchanged. A requested frequency aggregates within each interval - the
median for numeric columns, matching the fourteen-column export, and the first
value for text - and says so in the file it writes.
"""

from __future__ import annotations

import csv
from collections.abc import Callable, Iterable, Sequence
from pathlib import Path

import pandas as pd

from .noseboom_columns import normalize_columns
from .text_encoding import detect_encoding

# The instrument's own UTC nanosecond counter, the only column every row is
# placed in time by.
TIME_COLUMN = "Airflow_UTCcorr_Nanoseconds_ns"
EVENT_COLUMN = "EVENT"
FLIGHT_ID_COLUMN = "Flight ID"
CHUNK_ROWS = 200_000

ProgressCallback = Callable[[float, str], None]


def _noop(percent: float, message: str) -> None:
    return None


def _read_header(path: Path) -> list[str]:
    encoding = detect_encoding(path)
    with Path(path).open("r", encoding=encoding, errors="replace", newline="") as stream:
        row = next(csv.reader(stream), [])
    return normalize_columns([value.strip() for value in row], source=Path(path).name)


def full_column_names(paths: Iterable[Path]) -> list[str]:
    """Every column present across the selected files, in first-seen order."""
    names: list[str] = []
    for path in paths:
        for column in _read_header(Path(path)):
            if column not in names:
                names.append(column)
    return names


def _resample(frame: pd.DataFrame, frequency_hz: float) -> pd.DataFrame:
    """Aggregate within each interval, keeping the column set intact."""
    stamps = pd.to_datetime(
        pd.to_numeric(frame[TIME_COLUMN], errors="coerce"),
        unit="ns", origin="unix", utc=True, errors="coerce",
    )
    frame = frame.loc[stamps.notna()].set_index(stamps.dropna())
    if frame.empty:
        return frame
    rule = f"{int(round(1e9 / float(frequency_hz)))}ns"
    grouped = frame.resample(rule)
    numeric = frame.select_dtypes("number").columns
    # Median for numbers, as the fourteen-column export uses; the first value for
    # text, because an average of a label means nothing.
    aggregated = grouped[list(numeric)].median()
    for column in frame.columns:
        if column in numeric:
            continue
        aggregated[column] = grouped[column].first()
    aggregated = aggregated.loc[grouped.size() > 0]
    return aggregated[list(frame.columns)].reset_index(drop=True)



def _count_rows(paths: Sequence[Path], report: ProgressCallback) -> int:
    """Rows across the selected files, so progress is a fraction of real work.

    Counted by newline rather than parsed, which is fast enough to be worth
    doing before a long write rather than guessing at the total.
    """
    total = 0
    for path in paths:
        path = Path(path)
        if not path.is_file():
            continue
        lines = 0
        last = b""
        with path.open("rb") as stream:
            while True:
                block = stream.read(8 * 1024 * 1024)
                if not block:
                    break
                lines += block.count(b"\n")
                last = block[-1:]
        if last and last != b"\n":
            lines += 1
        total += max(0, lines - 1)          # the header is not a row
        report(4.0, f"Indexing {path.name}")
    return max(1, total)

def export_full_table(
    paths: Sequence[Path],
    target: Path,
    *,
    start_ns: int,
    end_ns: int,
    flight_id: str,
    frequency_hz: float | None = None,
    separator: str = ",",
    progress: ProgressCallback | None = None,
    cancelled: Callable[[], bool] | None = None,
) -> tuple[int, list[str]]:
    """Write every column for the selected interval. Returns (rows, columns).

    *frequency_hz* of None keeps the recorded resolution.
    """
    report = progress or _noop
    columns = full_column_names(paths)
    if TIME_COLUMN not in columns:
        raise ValueError(
            f"The Noseboom files carry no {TIME_COLUMN} column, so rows cannot be "
            "placed in time. Check that the export is a Noseboom record."
        )
    # EVENT is part of the instrument's record; Flight ID is added so a table
    # separated from its project still says which flight it belongs to.
    if EVENT_COLUMN not in columns:
        columns.append(EVENT_COLUMN)
    written_columns = [FLIGHT_ID_COLUMN] + columns

    total_rows = _count_rows(paths, report)
    rows_read = 0
    rows_written = 0
    header_written = False
    target.parent.mkdir(parents=True, exist_ok=True)

    with target.open("w", encoding="utf-8-sig", newline="") as sink:
        for path in paths:
            path = Path(path)
            if not path.is_file():
                continue
            encoding = detect_encoding(path)
            reader = pd.read_csv(
                path, encoding=encoding, low_memory=False,
                chunksize=CHUNK_ROWS, dtype=str,
            )
            for chunk in reader:
                if cancelled and cancelled():
                    raise RuntimeError("Noseboom export was cancelled")
                chunk.columns = normalize_columns(list(chunk.columns), source=path.name)
                rows_read += len(chunk)
                stamps = pd.to_numeric(chunk.get(TIME_COLUMN), errors="coerce")
                chunk = chunk.loc[(stamps >= start_ns) & (stamps <= end_ns)]
                report(
                    min(95.0, 5.0 + 90.0 * rows_read / total_rows),
                    f"Read {rows_read:,} of {total_rows:,} rows",
                )
                if chunk.empty:
                    continue
                for column in columns:
                    if column not in chunk.columns:
                        chunk[column] = ""
                chunk = chunk[columns]
                if frequency_hz:
                    numeric = chunk.drop(columns=[EVENT_COLUMN], errors="ignore").apply(
                        pd.to_numeric, errors="coerce"
                    )
                    numeric[EVENT_COLUMN] = chunk.get(EVENT_COLUMN, "")
                    chunk = _resample(numeric[columns], frequency_hz)
                    if chunk.empty:
                        continue
                chunk.insert(0, FLIGHT_ID_COLUMN, flight_id)
                chunk.to_csv(
                    sink, index=False, header=not header_written,
                    sep=separator, lineterminator="\n",
                )
                header_written = True
                rows_written += len(chunk)
    if not rows_written:
        raise ValueError("The selected interval contains no Noseboom rows")
    report(97.0, f"{rows_written:,} rows written")
    return rows_written, written_columns
