"""Noseboom CSVs are read whether or not their columns carry the logger prefix.

Depending on how the logger was configured, the same measurement is exported as
``NoseBoom_WIND_vWind_x_m/s`` or as ``WIND_vWind_x_m/s``. Both must load, and a
file carrying both spellings of one column is ambiguous rather than merely
redundant -- nothing in it says which copy the science should use -- so that is
refused with the offending names rather than resolved by guessing.
"""

import csv
import importlib.util
from pathlib import Path

import pytest

from core.noseboom_columns import (
    NOSEBOOM_COLUMN_PREFIX,
    duplicate_normalized_columns,
    normalize_column_name,
    normalize_columns,
)
from core.time_extraction import TimestampExtractor
from instruments.noseboom.adapter import TIME_COLUMN, _columns

ROOT = Path(__file__).resolve().parents[1]

COLUMNS = [
    "Airflow_UTCcorr_Nanoseconds_ns",
    "TIMESTAMP",
    "INS_Filter_LLHPos_Latitude_deg",
    "INS_Filter_LLHPos_Longitude_deg",
    "INS_Filter_LLHPos_ElipsoidHeight_m",
    "GNSSRecv1_LLHPos_Latitude_deg",
    "GNSSRecv1_LLHPos_Longitude_deg",
    "WIND_vWind_x_m/s",
    "WIND_vWind_y_m/s",
    "WIND_vWind_z_m/s",
]

FIRST_NS = 1_785_000_000_000_000_000


def write_csv(path: Path, columns, rows=3) -> Path:
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(columns)
        for index in range(rows):
            values = []
            for column in columns:
                bare = normalize_column_name(column)
                if bare == "Airflow_UTCcorr_Nanoseconds_ns":
                    values.append(str(FIRST_NS + index * 1_000_000_000))
                elif bare == "TIMESTAMP":
                    values.append(f"2026-07-27 07:19:{28 + index:02d}")
                elif "Latitude" in bare:
                    values.append(f"{47.65 + index * 1e-5:.6f}")
                elif "Longitude" in bare:
                    values.append(f"{9.37 + index * 1e-5:.6f}")
                else:
                    values.append("1.5")
            writer.writerow(values)
    return path


@pytest.mark.parametrize(
    ("column", "expected"),
    [
        ("NoseBoom_Airflow_UTCcorr_Nanoseconds_ns", "Airflow_UTCcorr_Nanoseconds_ns"),
        ("Airflow_UTCcorr_Nanoseconds_ns", "Airflow_UTCcorr_Nanoseconds_ns"),
        ("NoseBoom_WIND_vWind_x_m/s", "WIND_vWind_x_m/s"),
        ("WIND_vWind_x_m/s", "WIND_vWind_x_m/s"),
    ],
)
def test_the_documented_examples(column, expected):
    assert normalize_column_name(column) == expected


def test_only_the_leading_prefix_goes():
    """Stripping it anywhere else would rename a genuinely different column."""
    assert normalize_column_name("WIND_NoseBoom_x") == "WIND_NoseBoom_x"
    assert normalize_column_name("NoseBoom_NoseBoom_x") == "NoseBoom_x"
    assert normalize_column_name("noseboom_WIND") == "noseboom_WIND"


@pytest.mark.parametrize("prefixed", [False, True])
def test_both_spellings_are_detected_and_validated(tmp_path, prefixed):
    columns = [
        (NOSEBOOM_COLUMN_PREFIX + column) if prefixed else column
        for column in COLUMNS
    ]
    path = write_csv(tmp_path / "NoseBoom.csv", columns)

    found = _columns(path)

    assert TIME_COLUMN in found
    assert "WIND_vWind_x_m/s" in found
    assert not any(name.startswith(NOSEBOOM_COLUMN_PREFIX) for name in found)


@pytest.mark.parametrize("prefixed", [False, True])
def test_the_time_range_is_found_either_way(tmp_path, prefixed):
    """Scanning reads the range before anything else runs; a prefixed file used
    to arrive here with no recognised timestamp column at all."""
    columns = [
        (NOSEBOOM_COLUMN_PREFIX + column) if prefixed else column
        for column in COLUMNS
    ]
    path = write_csv(tmp_path / "NoseBoom.csv", columns)

    result = TimestampExtractor().extract_instrument("noseboom", [path])

    assert result.utc_start_time is not None
    assert result.utc_end_time is not None
    assert result.utc_start_time < result.utc_end_time


def test_a_mixed_header_is_refused_with_the_offending_names():
    mixed = ["WIND_vWind_x_m/s", "NoseBoom_WIND_vWind_x_m/s", "TIMESTAMP"]

    duplicates = duplicate_normalized_columns(mixed)
    assert set(duplicates) == {"WIND_vWind_x_m/s"}

    with pytest.raises(ValueError) as raised:
        normalize_columns(mixed, source="NoseBoom.csv")

    message = str(raised.value)
    assert "WIND_vWind_x_m/s" in message
    assert "NoseBoom_WIND_vWind_x_m/s" in message
    assert "NoseBoom.csv" in message


def test_the_adapter_refuses_a_mixed_file_rather_than_guessing(tmp_path):
    path = write_csv(
        tmp_path / "NoseBoom.csv",
        COLUMNS + ["NoseBoom_WIND_vWind_x_m/s"],
    )

    with pytest.raises(ValueError, match="WIND_vWind_x_m/s"):
        _columns(path)


def test_a_clean_header_is_returned_unchanged():
    assert normalize_columns(COLUMNS) == COLUMNS


def _load_sif_script():
    path = ROOT / "instruments" / "sif" / "legacy" / "noseboom_gimbal_for_sif.py"
    spec = importlib.util.spec_from_file_location("noseboom_gimbal_for_sif", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize("prefixed", [False, True])
def test_the_sif_preparation_reads_both_spellings(prefixed):
    """SIF matches gimbal attitude against Noseboom position, so a prefixed file
    has to be readable there too or SIF fails on a flight that scanned fine."""
    module = _load_sif_script()
    names = ["TIMESTAMP", "INS_Filter_LLHPos_Latitude_deg"]
    if prefixed:
        names = [NOSEBOOM_COLUMN_PREFIX + name for name in names]

    assert module.normalize_noseboom_fieldnames(names, "NoseBoom.csv") == [
        "TIMESTAMP",
        "INS_Filter_LLHPos_Latitude_deg",
    ]


def test_the_sif_preparation_refuses_a_mixed_header():
    module = _load_sif_script()

    with pytest.raises(SystemExit, match="WIND_vWind_x_m/s"):
        module.normalize_noseboom_fieldnames(
            ["WIND_vWind_x_m/s", "NoseBoom_WIND_vWind_x_m/s"], "NoseBoom.csv"
        )


# ------------------------------------------------------- detection sees it too
def test_a_prefixed_export_is_recognised_as_noseboom(tmp_path):
    """The prefix work reached the adapter, the loader and timestamp extraction,
    and not the one step that decides the file is Noseboom at all. The detection
    rule requires "Airflow_UTCcorr_Nanoseconds_ns"; a logger-prefixed export
    spells it "NoseBoom_Airflow_UTCcorr_Nanoseconds_ns", so an 810 MB flight
    export was reported as no Noseboom data found.
    """
    from core.scanner import _inspect_file

    path = tmp_path / "NoseBoom_20260803_055900_to_065900_UTC.csv"
    path.write_text(
        "NoseBoom_Airflow_UTCcorr_Nanoseconds_ns,NoseBoom_WIND_dir_deg\n"
        "1700000000000000000,182.4\n",
        encoding="utf-8",
    )

    inspection = _inspect_file(path, 64 * 1024, 2, inspect_text=True, inspect_exif=False)

    assert "Airflow_UTCcorr_Nanoseconds_ns" in inspection.columns, "the rule's name"
    assert "NoseBoom_Airflow_UTCcorr_Nanoseconds_ns" in inspection.columns, (
        "and the file's own, so anything expecting the raw name still matches"
    )


def test_an_unprefixed_export_is_unaffected(tmp_path):
    from core.scanner import _inspect_file

    path = tmp_path / "NoseBoom.csv"
    path.write_text(
        "Airflow_UTCcorr_Nanoseconds_ns,WIND_dir_deg\n1700000000000000000,182.4\n",
        encoding="utf-8",
    )

    inspection = _inspect_file(path, 64 * 1024, 2, inspect_text=True, inspect_exif=False)

    assert "Airflow_UTCcorr_Nanoseconds_ns" in inspection.columns


def test_other_instruments_keep_their_column_names(tmp_path):
    """Normalising every header is only safe because no other instrument uses
    that prefix; this pins that the names they are detected by are untouched."""
    from core.scanner import _inspect_file

    path = tmp_path / "picarro.dat"
    path.write_text("DATE,TIME,CO2_sync\n2026-07-27,05:19:39,412.5\n", encoding="utf-8")

    inspection = _inspect_file(path, 64 * 1024, 2, inspect_text=True, inspect_exif=False)

    assert {"DATE", "TIME", "CO2_sync"} <= inspection.columns


def test_the_loader_reads_a_prefixed_file(tmp_path):
    """The bridge chose its time column by testing FIELDS["time_ns"] against
    usecols - and usecols names columns as the file spells them. A prefixed
    export never matched, fell back to the literal "time_ns", and every read
    died with KeyError: 'time_ns'. Processing failed and the limited download
    failed; only the full export, which normalises for itself, worked.
    """
    from instruments.noseboom.legacy_bridge import LegacyNoseboomBridge

    start = 1_700_000_000_000_000_000
    rows = [
        "NoseBoom_Airflow_UTCcorr_Nanoseconds_ns,NoseBoom_INS_Filter_LLHPos_Latitude_deg,"
        "NoseBoom_INS_Filter_LLHPos_Longitude_deg,NoseBoom_INS_Filter_LLHPos_ElipsoidHeight_m,"
        "NoseBoom_WIND_vWind_x_m/s,NoseBoom_WIND_vWind_y_m/s,NoseBoom_WIND_vWind_z_m/s,"
        "NoseBoom_WIND_vWind_m/s,NoseBoom_WIND_dir_deg,NoseBoom_Airflow_Flow_OAT_degC,"
        "NoseBoom_Airflow_Flow_rel_humidity_,NoseBoom_Airflow_Sensor_pstat_hPa"
    ]
    for index in range(200):
        stamp = start + index * 10_000_000
        rows.append(
            f"{stamp},47.65,9.37,760.0,1.5,0.5,0.1,1.6,182.0,20.1,41.0,1001.2"
        )
    path = tmp_path / "NoseBoom_20260803_055900_to_065900_UTC.csv"
    path.write_text("\n".join(rows) + "\n", encoding="utf-8")

    data = LegacyNoseboomBridge().load_csv_window(
        [path], start, start + 200 * 10_000_000
    )

    assert len(data) == 200
    assert "time_ns" in data.columns, "the simplified name the science works on"
    assert data["lat"].notna().all()


def test_an_unprefixed_file_loads_exactly_as_before(tmp_path):
    from instruments.noseboom.legacy_bridge import LegacyNoseboomBridge

    start = 1_700_000_000_000_000_000
    rows = [
        "Airflow_UTCcorr_Nanoseconds_ns,INS_Filter_LLHPos_Latitude_deg,"
        "INS_Filter_LLHPos_Longitude_deg,INS_Filter_LLHPos_ElipsoidHeight_m,"
        "WIND_vWind_x_m/s,WIND_vWind_y_m/s,WIND_vWind_z_m/s,WIND_vWind_m/s,"
        "WIND_dir_deg,Airflow_Flow_OAT_degC,Airflow_Flow_rel_humidity_,"
        "Airflow_Sensor_pstat_hPa"
    ]
    for index in range(50):
        rows.append(
            f"{start + index * 10_000_000},47.65,9.37,760.0,1.5,0.5,0.1,1.6,182.0,20.1,41.0,1001.2"
        )
    path = tmp_path / "NoseBoom.csv"
    path.write_text("\n".join(rows) + "\n", encoding="utf-8")

    data = LegacyNoseboomBridge().load_csv_window(
        [path], start, start + 50 * 10_000_000
    )

    assert len(data) == 50
    assert data["lat"].notna().all()


def test_a_file_without_the_time_column_says_so(tmp_path):
    """Rather than KeyError on a name the operator never wrote."""
    from instruments.noseboom.legacy_bridge import LegacyNoseboomBridge

    path = tmp_path / "NoseBoom.csv"
    path.write_text("WIND_dir_deg,Airflow_Flow_OAT_degC\n182.0,20.1\n", encoding="utf-8")

    with pytest.raises(Exception) as failure:
        LegacyNoseboomBridge().load_csv_window([path], 0, 1)

    assert "time_ns" not in str(failure.value) or "Airflow_UTCcorr" in str(failure.value)
