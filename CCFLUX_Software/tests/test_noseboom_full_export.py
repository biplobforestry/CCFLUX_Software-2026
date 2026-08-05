"""The Noseboom download can write the instrument's whole record, not fourteen columns.

The interactive download produced the columns the browser plots. A flight file
carries around 140, and the other 126 were reachable only by opening the raw
CSV. "Full" writes all of them, with EVENT and a Flight ID so a table separated
from its project still says which flight it belongs to.

Two things are easy to get wrong here and are what these tests are for. The
write must stream - a flight is on the order of a million rows across 140
columns, and assembling that in memory before writing is how an export becomes
an out-of-memory error. And a requested frequency must aggregate without
quietly changing what a column means: the median for numbers, matching the
fourteen-column export, and the first value for text, because an average of a
label is nothing.
"""

import csv

import pandas as pd
import pytest

from core.noseboom_full_export import (
    EVENT_COLUMN,
    FLIGHT_ID_COLUMN,
    TIME_COLUMN,
    export_full_table,
    full_column_names,
)

SECOND_NS = 1_000_000_000


def _source(tmp_path, *, rows=1000, hz=100, prefix="", start_ns=1_700_000_000 * SECOND_NS,
            name="NoseBoom.csv", event=True, encoding="utf-8"):
    """A file shaped like a Noseboom export: many columns, one time column."""
    step = SECOND_NS // hz
    columns = {
        f"{prefix}{TIME_COLUMN}": [start_ns + index * step for index in range(rows)],
        f"{prefix}INS_Filter_LLHPos_Latitude_deg": [47.6 + index * 1e-6 for index in range(rows)],
        f"{prefix}WIND_vWind_x_m/s": [float(index % 10) for index in range(rows)],
        f"{prefix}Airflow_Flow_OAT_degC": [20.0 + (index % 5) * 0.1 for index in range(rows)],
    }
    if event:
        columns[f"{prefix}{EVENT_COLUMN}"] = ["" if index % 100 else "MARK" for index in range(rows)]
    path = tmp_path / name
    pd.DataFrame(columns).to_csv(path, index=False, encoding=encoding)
    return path, start_ns, start_ns + (rows - 1) * step


def _read(path, sep=","):
    return pd.read_csv(path, encoding="utf-8-sig", sep=sep)


def test_every_column_is_written(tmp_path):
    source, low, high = _source(tmp_path)
    target = tmp_path / "full.csv"

    rows, columns = export_full_table(
        [source], target, start_ns=low, end_ns=high, flight_id="Flight_2707"
    )

    written = _read(target)
    assert rows == 1000
    assert set(written.columns) == set(columns)
    # Every source column survives, and nothing is silently dropped.
    for name in ("INS_Filter_LLHPos_Latitude_deg", "WIND_vWind_x_m/s", "Airflow_Flow_OAT_degC"):
        assert name in written.columns


def test_the_event_column_names_the_flight(tmp_path):
    """The logger writes EVENT and fills it with the literal string "EVENT" on
    every row, which identifies nothing. The flight id is what a row separated
    from its project needs, so it is written there."""
    source, low, high = _source(tmp_path, rows=50)
    target = tmp_path / "full.csv"

    export_full_table([source], target, start_ns=low, end_ns=high, flight_id="Flight_2707")

    written = _read(target)
    assert set(written[EVENT_COLUMN]) == {"Flight_2707"}


def test_no_duplicate_flight_column_is_written(tmp_path):
    """EVENT carries the flight, so a second column saying the same thing is
    noise in a table that already has 140 columns."""
    source, low, high = _source(tmp_path, rows=20)
    target = tmp_path / "full.csv"

    export_full_table([source], target, start_ns=low, end_ns=high, flight_id="F")

    assert FLIGHT_ID_COLUMN not in _read(target).columns


def test_the_event_column_is_written_for_every_row(tmp_path):
    """Whatever the logger put there is replaced: it wrote the literal "EVENT"."""
    source, low, high = _source(tmp_path, rows=300)
    target = tmp_path / "full.csv"

    export_full_table([source], target, start_ns=low, end_ns=high, flight_id="Flight_2707")

    written = _read(target)
    assert EVENT_COLUMN in written.columns
    assert (written[EVENT_COLUMN] == "Flight_2707").all()


def test_a_file_without_event_still_gets_the_column(tmp_path):
    """The column is part of the agreed output, so its absence upstream must not
    change the shape of the table."""
    source, low, high = _source(tmp_path, rows=20, event=False)
    target = tmp_path / "full.csv"

    export_full_table([source], target, start_ns=low, end_ns=high, flight_id="F")

    assert EVENT_COLUMN in _read(target).columns


def test_original_resolution_keeps_every_recorded_row(tmp_path):
    source, low, high = _source(tmp_path, rows=500, hz=100)
    target = tmp_path / "full.csv"

    rows, _ = export_full_table(
        [source], target, start_ns=low, end_ns=high, flight_id="F", frequency_hz=None
    )

    assert rows == 500


@pytest.mark.parametrize("hz,expected", [(50, 250), (10, 50)])
def test_a_requested_frequency_reduces_the_rows(tmp_path, hz, expected):
    """500 rows at 100 Hz is five seconds; 10 Hz of five seconds is fifty rows."""
    source, low, high = _source(tmp_path, rows=500, hz=100)
    target = tmp_path / "full.csv"

    rows, _ = export_full_table(
        [source], target, start_ns=low, end_ns=high, flight_id="F", frequency_hz=hz
    )

    assert abs(rows - expected) <= 1, f"{rows} rows for {hz} Hz"


def test_resampling_keeps_the_whole_column_set(tmp_path):
    source, low, high = _source(tmp_path, rows=400)
    original = tmp_path / "a.csv"
    reduced = tmp_path / "b.csv"

    _, wide = export_full_table([source], original, start_ns=low, end_ns=high, flight_id="F")
    _, narrow = export_full_table(
        [source], reduced, start_ns=low, end_ns=high, flight_id="F", frequency_hz=10
    )

    assert wide == narrow


def test_only_the_selected_interval_is_written(tmp_path):
    source, low, high = _source(tmp_path, rows=1000, hz=100)
    target = tmp_path / "full.csv"
    middle_start = low + 2 * SECOND_NS
    middle_end = low + 4 * SECOND_NS

    rows, _ = export_full_table(
        [source], target, start_ns=middle_start, end_ns=middle_end, flight_id="F"
    )

    written = _read(target)
    stamps = pd.to_numeric(written[TIME_COLUMN])
    assert rows == 201
    assert stamps.min() >= middle_start and stamps.max() <= middle_end


def test_an_interval_with_no_rows_is_refused(tmp_path):
    source, low, high = _source(tmp_path, rows=100)
    target = tmp_path / "full.csv"

    with pytest.raises(ValueError, match="no Noseboom rows"):
        export_full_table(
            [source], target, start_ns=high + SECOND_NS, end_ns=high + 2 * SECOND_NS,
            flight_id="F",
        )


def test_a_file_without_the_time_column_is_refused(tmp_path):
    path = tmp_path / "not_noseboom.csv"
    pd.DataFrame({"a": [1], "b": [2]}).to_csv(path, index=False)

    with pytest.raises(ValueError, match=TIME_COLUMN):
        export_full_table([path], tmp_path / "x.csv", start_ns=0, end_ns=1, flight_id="F")


def test_the_logger_prefix_is_removed_from_the_written_header(tmp_path):
    """A prefixed export must produce the same column names as an unprefixed one."""
    source, low, high = _source(tmp_path, rows=40, prefix="NoseBoom_")
    target = tmp_path / "full.csv"

    export_full_table([source], target, start_ns=low, end_ns=high, flight_id="F")

    written = _read(target)
    assert TIME_COLUMN in written.columns
    assert not any(name.startswith("NoseBoom_") for name in written.columns)


def test_a_cp1252_file_is_read(tmp_path):
    """The degree sign in a unit is one byte in cp1252, which UTF-8 rejects."""
    source, low, high = _source(tmp_path, rows=30)
    text = source.read_text(encoding="utf-8").replace(
        "Airflow_Flow_OAT_degC", "Airflow_Flow_OAT_[°C]"
    )
    source.write_bytes(text.encode("cp1252"))
    target = tmp_path / "full.csv"

    rows, _ = export_full_table([source], target, start_ns=low, end_ns=high, flight_id="F")

    assert rows == 30
    assert "Airflow_Flow_OAT_[°C]" in _read(target).columns


def test_progress_is_reported_and_reaches_the_end(tmp_path):
    source, low, high = _source(tmp_path, rows=300)
    seen = []

    export_full_table(
        [source], tmp_path / "full.csv", start_ns=low, end_ns=high, flight_id="F",
        progress=lambda percent, step: seen.append((percent, step)),
    )

    assert seen, "an export long enough to watch must report progress"
    assert max(percent for percent, _ in seen) >= 95
    assert all(0 <= percent <= 100 for percent, _ in seen)


def test_a_cancelled_export_stops(tmp_path):
    source, low, high = _source(tmp_path, rows=100)

    with pytest.raises(RuntimeError, match="cancelled"):
        export_full_table(
            [source], tmp_path / "full.csv", start_ns=low, end_ns=high,
            flight_id="F", cancelled=lambda: True,
        )


def test_tab_separated_output(tmp_path):
    source, low, high = _source(tmp_path, rows=25)
    target = tmp_path / "full.txt"

    export_full_table(
        [source], target, start_ns=low, end_ns=high, flight_id="F", separator="\t"
    )

    written = _read(target, sep="\t")
    assert EVENT_COLUMN in written.columns
    assert len(written) == 25


def test_columns_are_gathered_across_several_files(tmp_path):
    first, low, high = _source(tmp_path, rows=10, name="a.csv")
    second = tmp_path / "b.csv"
    frame = pd.read_csv(first)
    frame["Extra_Sensor_value"] = 1.0
    frame.to_csv(second, index=False)

    names = full_column_names([first, second])

    assert "Extra_Sensor_value" in names
    assert names.index(TIME_COLUMN) < names.index("Extra_Sensor_value")


def test_the_written_file_is_valid_csv(tmp_path):
    """Streamed appends are where a header lands in the middle of a file."""
    source, low, high = _source(tmp_path, rows=250)
    target = tmp_path / "full.csv"

    export_full_table([source], target, start_ns=low, end_ns=high, flight_id="F")

    with target.open(encoding="utf-8-sig", newline="") as stream:
        rows = list(csv.reader(stream))
    header = rows[0]
    assert all(len(row) == len(header) for row in rows[1:])
    assert EVENT_COLUMN not in [cell for row in rows[1:] for cell in row], (
        "a header must not reappear part-way through a streamed file"
    )


# ------------------------------------------------------- the dialog and its API
from pathlib import Path

ASSETS = Path(__file__).resolve().parents[1] / "app" / "assets"
PAGE = (ASSETS / "noseboom.html").read_text(encoding="utf-8")
SCRIPT = (ASSETS / "noseboom.js").read_text(encoding="utf-8")


def test_the_dialog_offers_both_variable_sets():
    assert 'id="downloadVariables"' in PAGE
    assert 'value="limited"' in PAGE and 'value="full"' in PAGE


def test_original_resolution_is_offered_first():
    frequencies = PAGE[PAGE.index('id="downloadFrequency"'):]
    frequencies = frequencies[: frequencies.index("</select>")]
    values = [v for v in ("original", "50", "10") if f'value="{v}"' in frequencies]

    assert values[0] == "original"
    assert {"original", "50", "10"} <= set(
        __import__("re").findall(r'value="([a-z0-9]+)"', frequencies)
    )


def test_original_is_disabled_for_the_limited_set():
    """The limited table is resampled by definition, so it has no original.

    Original resolution is also unavailable from a project, which carries a
    10 Hz table rather than the raw CSV.
    """
    assert "option.disabled=!full||fromProject" in SCRIPT
    assert "if(!chosen||chosen.disabled)frequency.value='1'" in SCRIPT


def test_the_download_reports_progress_while_it_writes():
    assert "/api/noseboom/data-export/progress" in SCRIPT
    assert 'id="downloadProgressModal"' in PAGE
    assert "setDownloadProgress" in SCRIPT


def test_completion_is_stated_plainly():
    assert "'Download complete'" in SCRIPT
    assert 'id="downloadProgressClose"' in PAGE


def test_a_missed_poll_does_not_stop_the_download():
    poller = SCRIPT[SCRIPT.index("async function pollDownloadProgress"):]
    poller = poller[: poller.index("\n  }")]

    assert "catch(_)" in poller


def test_the_page_provides_every_element_the_download_needs():
    import re

    used = set(re.findall(r"byId\('(download[A-Za-z0-9_]*)'\)", SCRIPT))
    missing = {name for name in used if f'id="{name}"' not in PAGE}

    assert not missing, f"the download flow reaches for ids the page lacks: {missing}"


# ----------------------------------------------- event marks and the timestamp
def _marked(tmp_path, marks, rows=300, hz=100):
    start = 1_785_736_739_000_000_000
    step = 1_000_000_000 // hz
    path = tmp_path / "NoseBoom.csv"
    pd.DataFrame({
        TIME_COLUMN: [start + i * step for i in range(rows)],
        "lat": [47.65] * rows,
        EVENT_COLUMN: [marks.get(i, "") for i in range(rows)],
    }).to_csv(path, index=False)
    return path, start


def test_the_event_column_carries_the_flight_at_every_resolution(tmp_path):
    """Set before any resampling, so an interval cannot dilute or drop it."""
    source, _ = _marked(tmp_path, {10: "MARK_A", 60: "MARK_B", 250: "MARK_C"})

    for hz in (None, 1.0, 10.0):
        target = tmp_path / f"out_{hz}.csv"
        export_full_table([source], target, start_ns=0, end_ns=2**63 - 1,
                          flight_id="Flight_2707", frequency_hz=hz)

        assert set(_read(target)[EVENT_COLUMN]) == {"Flight_2707"}, f"at {hz}"


def test_what_the_logger_put_in_event_is_replaced(tmp_path):
    """Deliberate, and worth stating: the column is an identifier here, not a
    channel. A source that carried real annotations would lose them - the
    operator's own export carries the literal string "EVENT" on every row, which
    identifies nothing.
    """
    source, _ = _marked(tmp_path, {10: "TAKEOFF", 60: "LANDING"})
    target = tmp_path / "out.csv"

    export_full_table([source], target, start_ns=0, end_ns=2**63 - 1,
                      flight_id="Flight_2707")

    written = _read(target)[EVENT_COLUMN]
    assert set(written) == {"Flight_2707"}
    assert "TAKEOFF" not in set(written)


@pytest.mark.parametrize("hz", [None, 1.0, 10.0])
def test_the_timestamp_is_written_exactly(tmp_path, hz):
    """A nanosecond epoch needs 19 digits and float64 holds about 16. Taking the
    median wrote 1.785736739495e+18 - the interval midpoint, rounded, 64 ns from
    any row that ever existed."""
    source, start = _marked(tmp_path, {})
    target = tmp_path / "out.csv"

    export_full_table([source], target, start_ns=0, end_ns=2**63 - 1,
                      flight_id="F", frequency_hz=hz)

    raw = pd.read_csv(source, dtype=str)[TIME_COLUMN]
    written = pd.read_csv(target, encoding="utf-8-sig", dtype=str)[TIME_COLUMN]

    assert all(value.isdigit() for value in written), "no exponent, no decimal point"
    assert set(written) <= set(raw), "every timestamp is one that was recorded"


def test_the_resampled_timestamp_is_the_first_of_its_interval(tmp_path):
    source, start = _marked(tmp_path, {}, rows=300, hz=100)
    target = tmp_path / "out.csv"

    export_full_table([source], target, start_ns=0, end_ns=2**63 - 1,
                      flight_id="F", frequency_hz=1.0)

    written = pd.read_csv(target, encoding="utf-8-sig", dtype=str)[TIME_COLUMN]
    assert int(written.iloc[0]) == start
