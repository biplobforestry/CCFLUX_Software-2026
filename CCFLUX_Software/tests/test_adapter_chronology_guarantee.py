"""Every adapter must put a delivery in chronological order and say that it did.

Loggers restart, exports rotate, and segments arrive out of order. Each adapter
already repairs this at its own load or segmentation step; these cases lock that
behaviour in so a future refactor cannot quietly remove it and leave the
downstream science reading a shuffled series.
"""

from pathlib import Path

import pandas as pd
import pytest

from core.detector import InputCandidate
from instruments.opc_hbx4 import OpcHbx4Adapter

from .test_opc_adapters import _opc_csv


def _shuffle_block(path: Path, first: int, second: int) -> Path:
    """Move a later block of rows in front of an earlier one."""
    lines = path.read_text(encoding="utf-8").splitlines()
    header, rows = lines[0], lines[1:]
    reordered = rows[:first] + rows[second:] + rows[first:second]
    path.write_text("\n".join([header, *reordered]) + "\n", encoding="utf-8")
    return path


def test_opc_out_of_order_delivery_is_sorted_quarantined_and_reported(tmp_path: Path):
    source = _shuffle_block(_opc_csv(tmp_path / "OPC_HBX4.csv", "hbx4", rows=40), 10, 25)
    adapter = OpcHbx4Adapter(
        output_root=tmp_path / "output", flight_name="Flight_2707"
    )
    candidate = InputCandidate("opc_hbx4", (source,), 1.0, "out-of-order fixture")

    result = adapter.process_quicklook(adapter.load(candidate), {"bin_units": "number_cm3"})

    integrity = result.metadata["source_integrity"]
    assert integrity["out_of_order_transitions"] > 0
    assert integrity["raw_source_modified"] is False
    assert any("chronological sort" in warning for warning in result.warnings)

    evaluated = adapter._evaluated
    assert evaluated["recorded_time"].is_monotonic_increasing

    # The auditable copy is written beside the outputs; the delivery is untouched.
    chronological = Path(integrity["chronological_copy"])
    assert chronological.is_file()
    assert chronological != source
    assert pd.read_csv(source)["_time"].tolist() != sorted(
        pd.read_csv(source)["_time"].tolist()
    )


def test_opc_rows_without_a_timestamp_are_quarantined_not_dropped_silently(
    tmp_path: Path,
):
    source = _opc_csv(tmp_path / "OPC_HBX4.csv", "hbx4", rows=30)
    lines = source.read_text(encoding="utf-8").splitlines()
    broken = lines[5].split(",")
    broken[0] = "not-a-timestamp"
    lines[5] = ",".join(broken)
    source.write_text("\n".join(lines) + "\n", encoding="utf-8")

    adapter = OpcHbx4Adapter(
        output_root=tmp_path / "output", flight_name="Flight_2707"
    )
    result = adapter.process_quicklook(
        adapter.load(InputCandidate("opc_hbx4", (source,), 1.0, "broken row")),
        {"bin_units": "number_cm3"},
    )

    integrity = result.metadata["source_integrity"]
    assert integrity["invalid_timestamp_rows"] == 1
    assert any("quarantined" in warning for warning in result.warnings)
    quarantined = Path(integrity["quarantined_rows"])
    assert quarantined.is_file()
    # The rejected row is preserved for audit rather than discarded.
    assert len(pd.read_csv(quarantined)) == 1
