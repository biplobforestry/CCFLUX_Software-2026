"""Picarro must load both DataLog_User flavours.

A CRDS analyzer writes either the synchronized log, whose species are suffixed
with _sync, or the plain log, whose species are not. Which one arrives is an
instrument setting. Flight_CC0806 delivered the plain flavour and every one of
its 24 files was skipped as "missing gas columns", so a complete 1.5-million-row
Picarro record loaded as nothing and the job failed.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

pytest.importorskip("pandas")
import pandas as pd

from core.legacy_paths import legacy_integration_path


def _picarro_module():
    path = legacy_integration_path("MIRO_Rack") / "picarro.py"
    spec = importlib.util.spec_from_file_location("ccflux_test_picarro", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


picarro = _picarro_module()

SYNCHRONIZED_HEADER = "DATE TIME species CO2_sync CO2_dry_sync CH4_sync CH4_dry_sync H2O_sync"
PLAIN_HEADER = "DATE TIME species CO2 CO2_dry CH4 CH4_dry H2O"


def _write(folder: Path, name: str, header: str, rows: list[str]) -> Path:
    path = folder / name
    path.write_text("\n".join([header, *rows]) + "\n", encoding="utf-8")
    return path


def _rows(prefix_zero: bool = False) -> list[str]:
    first_co2 = "0.0000000000E+00" if prefix_zero else "4.1500000000E+02"
    first_co2_dry = "0.0000000000E+00" if prefix_zero else "4.2100000000E+02"
    return [
        f"2026-08-06 06:44:15.943 25 {first_co2} {first_co2_dry} 1.9873222347E+00 1.9873222347E+00 1.0047985995E+00",
        "2026-08-06 06:44:15.964 28 4.1505531502E+02 4.2174920923E+02 1.9884988229E+00 2.0224321926E+00 1.0052612297E+00",
        "2026-08-06 06:44:15.985 10 4.1605531502E+02 4.2274920923E+02 1.9984988229E+00 2.0324321926E+00 1.0152612297E+00",
    ]


def test_synchronized_flavour_still_loads(tmp_path: Path) -> None:
    _write(tmp_path, "sync.dat", SYNCHRONIZED_HEADER, _rows())
    data, meta = picarro.load_folder(tmp_path)
    assert meta["rows"] == 3
    assert meta["column_variant"] == "synchronized"
    assert meta["skipped_files"] == []
    assert "CO2 raw" in meta["gases"]
    assert data["CO2_sync"].tolist() == [415.0, 415.05531502, 416.05531502]


def test_plain_flavour_loads_and_is_renamed(tmp_path: Path) -> None:
    """The failure this fixes: the plain log used to be skipped entirely."""
    _write(tmp_path, "plain.dat", PLAIN_HEADER, _rows())
    data, meta = picarro.load_folder(tmp_path)
    assert meta["rows"] == 3, "the plain DataLog_User must not be skipped"
    assert meta["files_used"] == 1
    assert meta["skipped_files"] == []
    assert meta["column_variant"] == "carried-forward"
    # Renamed at the boundary, so analyze(), comparison_series(), the browser
    # page and saved projects keep addressing the canonical names.
    assert "CO2_sync" in data.columns and "CO2" not in data.columns
    assert set(meta["gases"]) == {"CO2 raw", "CO2 dry", "CH4 raw", "CH4 dry", "H2O"}
    assert any("unsynchronized" in text for text in meta["warnings"])


def test_plain_flavour_placeholder_zero_is_not_a_measurement(tmp_path: Path) -> None:
    """Ambient CO2 is never 0 ppm; the leading zero is 'not yet measured'."""
    _write(tmp_path, "plain.dat", PLAIN_HEADER, _rows(prefix_zero=True))
    data, meta = picarro.load_folder(tmp_path)
    assert meta["placeholder_zeros_removed"] == 2
    assert data["CO2_sync"].isna().sum() == 1
    # The whole point: the reported minimum is a real concentration.
    assert data["CO2_sync"].min() == pytest.approx(415.05531502)
    assert any("placeholder zero" in text for text in meta["warnings"])


def test_synchronized_column_wins_when_a_file_carries_both(tmp_path: Path) -> None:
    _write(
        tmp_path,
        "both.dat",
        "DATE TIME CO2_sync CO2",
        ["2026-08-06 06:44:15.943 4.1500000000E+02 9.9900000000E+02"],
    )
    data, meta = picarro.load_folder(tmp_path)
    assert meta["column_variant"] == "synchronized"
    assert data["CO2_sync"].tolist() == [415.0]


def test_both_flavours_concatenate(tmp_path: Path) -> None:
    """A folder holding one of each must merge, not fail on mismatched names."""
    _write(tmp_path, "a_sync.dat", SYNCHRONIZED_HEADER, _rows())
    _write(
        tmp_path,
        "b_plain.dat",
        PLAIN_HEADER,
        [
            "2026-08-06 07:44:15.943 25 4.2500000000E+02 4.3100000000E+02 2.0000000000E+00 2.0300000000E+00 1.1000000000E+00"
        ],
    )
    data, meta = picarro.load_folder(tmp_path)
    assert meta["rows"] == 4
    assert meta["column_variant"] == "mixed"
    assert data["CO2_sync"].notna().all()


def test_unreadable_folder_says_why(tmp_path: Path) -> None:
    """The old message named no reason, which left the operator with a dead end."""
    _write(
        tmp_path,
        "wrong.dat",
        "DATE TIME CavityPressure",
        ["2026-08-06 06:44:15.943 1.5156469727E+02"],
    )
    with pytest.raises(RuntimeError) as error:
        picarro.load_folder(tmp_path)
    message = str(error.value)
    assert "1 of 1 file(s) were skipped" in message
    assert "wrong.dat" in message
    assert "No recognised gas column" in message


def test_missing_timestamp_column_is_named(tmp_path: Path) -> None:
    _write(tmp_path, "notime.dat", "DATE CO2", ["2026-08-06 4.1500000000E+02"])
    with pytest.raises(RuntimeError) as error:
        picarro.load_folder(tmp_path)
    assert "TIME" in str(error.value)


def test_detection_config_accepts_both_spellings() -> None:
    """Discovery must score the plain flavour on its gas columns too."""
    from core.configuration import load_detection_configuration

    root = Path(__file__).resolve().parents[1]
    configuration = load_detection_configuration(
        root / "configs" / "instrument_detection.yaml",
        root / "configs" / "file_patterns.yaml",
    )
    optional = set(configuration.pattern_sets["picarro"].optional_csv_columns)
    assert {"CO2_sync", "CH4_sync"} <= optional
    assert {"CO2", "CH4", "H2O", "CO2_dry", "CH4_dry"} <= optional
