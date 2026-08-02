"""Read Noseboom CSVs whether or not their columns carry the logger prefix.

Depending on how the logger was configured, a Noseboom export names its columns
either ``NoseBoom_WIND_vWind_x_m/s`` or ``WIND_vWind_x_m/s``. Both describe the
same measurement, so the prefix is removed as the file is read and every column
name the rest of the software uses is the unprefixed form.

Only that one leading prefix is removed, and only where it is present: a name
that does not start with it is passed through untouched, and ``NoseBoom_`` never
gets stripped from the middle of a name.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping

NOSEBOOM_COLUMN_PREFIX = "NoseBoom_"


def normalize_column_name(column: str) -> str:
    """Return *column* without its leading ``NoseBoom_``, if it has one."""
    return str(column).removeprefix(NOSEBOOM_COLUMN_PREFIX)


def duplicate_normalized_columns(columns: Iterable[str]) -> dict[str, list[str]]:
    """Normalized names that more than one original column maps onto."""
    sources: dict[str, list[str]] = {}
    for column in columns:
        sources.setdefault(normalize_column_name(column), []).append(str(column))
    return {name: found for name, found in sources.items() if len(found) > 1}


def describe_duplicates(duplicates: Mapping[str, list[str]]) -> str:
    return "; ".join(
        f"{name} (from {' and '.join(found)})"
        for name, found in sorted(duplicates.items())
    )


def normalize_columns(columns: Iterable[str], *, source: str | None = None) -> list[str]:
    """Normalized column names, refusing a header that becomes ambiguous.

    A file carrying both ``NoseBoom_WIND_vWind_x_m/s`` and
    ``WIND_vWind_x_m/s`` collapses them onto one name, and nothing in the file
    says which of the two the science should use. Rather than silently keeping
    whichever pandas happens to place last, that is refused here -- against the
    header, before any row is read.
    """
    columns = list(columns)
    duplicates = duplicate_normalized_columns(columns)
    if duplicates:
        where = f" in {source}" if source else ""
        raise ValueError(
            f"Duplicate Noseboom column name(s){where} after removing the "
            f"{NOSEBOOM_COLUMN_PREFIX!r} prefix: {describe_duplicates(duplicates)}. "
            "Keep only the prefixed or only the unprefixed copy of each column."
        )
    return [normalize_column_name(column) for column in columns]
