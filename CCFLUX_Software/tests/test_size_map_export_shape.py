"""The export must read the payload the view actually returns.

The drawn size-distribution export refused every request with "No georeferenced
Partector Pro samples to export" while the page beside it was drawing thousands
of them. size_distribution_map_view returns
{ready, flight_id, message, data: {available, sensors, ...}} and the export read
`available` and `sensors` off the outer dictionary, where neither exists.

It was checked before shipping, against a stub that returned the shape the
export assumed. The stub encoded the assumption instead of testing it, so the
check could only ever pass. These tests take the shape from the method that
produces it.
"""

from __future__ import annotations

import ast
import inspect
from pathlib import Path

import pytest

pytest.importorskip("pandas")

from app.scan_backend import DashboardScanBackend

ROOT = Path(__file__).resolve().parents[1]


def _returned_keys(function) -> set[str]:
    """The keys of the dictionary a function returns, read from its source."""
    tree = ast.parse(inspect.getsource(function).lstrip())
    keys: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Return) and isinstance(node.value, ast.Dict):
            keys.update(
                item.value for item in node.value.keys
                if isinstance(item, ast.Constant) and isinstance(item.value, str)
            )
    return keys


class TestTheViewsShape:
    def test_the_payload_is_nested_under_data(self):
        keys = _returned_keys(DashboardScanBackend.size_distribution_map_view)

        assert "data" in keys
        # If these ever move to the top level the export should follow, and this
        # test is what will say so.
        assert "available" not in keys
        assert "sensors" not in keys

    def test_the_export_reaches_through_data(self):
        source = inspect.getsource(
            DashboardScanBackend.export_size_distribution_map_figure
        )

        assert 'view.get("data")' in source
        assert 'view.get("sensors")' not in source
        assert 'view.get("available")' not in source

    def test_the_two_agree_on_every_key_the_export_reads(self):
        """Whatever the export pulls out of the payload must be something
        build_map_payload puts in."""
        from core.size_distribution_map import build_map_payload

        produced = _returned_keys(build_map_payload)
        for key in ("available", "sensors", "message"):
            assert key in produced, key


class TestItExportsWhatThePageDraws:
    """Driven through the real view shape, with the sensors a flight carries."""

    @pytest.fixture()
    def backend(self, tmp_path):
        import numpy as np

        count = 400
        index = np.arange(count)
        points = [
            {
                "lat": float(50.6 + 0.004 * value),
                "lon": float(6.3 + 0.005 * value),
                "values": [float(3000 + 40 * value), float(1500 + 20 * value)],
                "total": float(4500 + 60 * value),
            }
            for value in index
        ]
        view = {
            "ready": True,
            "flight_id": "Flight_CC0807",
            "message": "",
            "data": {
                "available": True,
                "sensors": {
                    "partector": {
                        "label": "Partector Pro",
                        "points": points,
                        "channels": ["10-30 nm", "30-50 nm"],
                    },
                },
            },
        }

        class Stub(DashboardScanBackend):
            SIZE_MAP_PAGES = DashboardScanBackend.SIZE_MAP_PAGES

            def __init__(self):
                import threading

                self._lock = threading.RLock()
                self._flight_project = None

            def size_distribution_map_view(self, page):
                return view

            class logger:
                @staticmethod
                def log(*args, **kwargs):
                    pass

            def _checkpoint_project(self):
                pass

        return Stub()

    def test_the_partector_map_exports(self, backend):
        filename, pdf = backend.export_size_distribution_map_figure(
            "partector", {"sensor": "partector", "channel": None, "log": True,
                          "flight_name": "Flight_CC0807"}
        )

        assert filename.endswith(".pdf")
        assert pdf.startswith(b"%PDF")
        assert len(pdf) > 5000

    def test_one_size_class_exports(self, backend):
        _filename, pdf = backend.export_size_distribution_map_figure(
            "partector", {"sensor": "partector", "channel": 1}
        )

        assert pdf.startswith(b"%PDF")

    def test_a_flight_with_nothing_placed_still_says_so(self, backend):
        backend.size_distribution_map_view = lambda page: {
            "ready": False, "message": "Process Noseboom first.", "data": {
                "available": False, "sensors": {}, "message": "",
            },
        }

        with pytest.raises(ValueError, match="Process Noseboom first"):
            backend.export_size_distribution_map_figure("partector", {})
