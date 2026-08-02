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
