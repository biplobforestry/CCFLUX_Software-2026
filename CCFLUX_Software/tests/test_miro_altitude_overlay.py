"""Altitude on the right-hand scale of the MIRO and Picarro time series.

A gas record read against time says what changed. Read against altitude it says
where, and that is the reading a Zeppelin flight is for: a plume met at 300 m
and one met on the ground are different measurements even where the trace looks
the same. The profile was in the project - the Noseboom carries it and the
Trace Gas Investigation already regresses against it - but a reader of the gas
figures had to hold it in their head from another figure entirely.

It is context, not the measurement, so it is drawn in grey behind the trace and
named in its own colour rather than given a legend that would cost panel width.
"""

from __future__ import annotations

import importlib.util
import sys
from datetime import datetime, timedelta
from pathlib import Path

import pytest

pytest.importorskip("pandas")
pytest.importorskip("scipy")

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd

from core.legacy_paths import legacy_integration_path

ROOT = Path(__file__).resolve().parents[1]


def _legacy(name: str):
    """The rack modules import each other by bare name, so the folder goes on
    the path before any of them is executed."""
    folder = legacy_integration_path("MIRO_Rack")
    if str(folder) not in sys.path:
        sys.path.insert(0, str(folder))
    path = folder / f"{name}.py"
    spec = importlib.util.spec_from_file_location(f"ccflux_test_rack_{name}", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


rack_export = _legacy("export")


@pytest.fixture()
def navigation():
    """Eleven hours of one-second altitude, the shape of a campaign flight."""
    began = datetime(2026, 8, 6, 5, 0)
    seconds = np.arange(0, 41_340, 60, dtype=float)
    # Ground, climb, a working altitude, then descent.
    metres = np.where(
        seconds < 12_000, 127.0,
        np.where(seconds < 38_000, 350.0 + 60.0 * np.sin(seconds / 3_000.0), 127.0),
    )
    return pd.DataFrame({
        "timestamp": [began + timedelta(seconds=float(value)) for value in seconds],
        "altitude": metres,
    })


@pytest.fixture()
def panel():
    figure, axis = plt.subplots(figsize=(7.0, 3.0))
    axis.plot(
        pd.date_range("2026-08-06 06:44", "2026-08-06 18:17", freq="1min"),
        np.linspace(410.0, 445.0, 694),
    )
    yield axis
    plt.close(figure)


def _altitude_axes(figure):
    return [axis for axis in figure.axes if axis.get_ylabel() == "Altitude (m)"]


class TestTheOverlay:
    def test_it_draws_the_profile_on_a_second_scale(self, panel, navigation):
        twin = rack_export.altitude_overlay(panel, navigation)

        assert twin is not None and twin is not panel
        assert twin.get_ylabel() == "Altitude (m)"
        low, high = twin.get_ylim()
        assert low < 130.0 and high > 400.0

    def test_the_altitude_record_does_not_widen_the_gas_window(
        self, panel, navigation
    ):
        """The twin shares the host's x-axis, and navigation covers taxi and
        shutdown that the analyzer's selected timeframe deliberately excludes.
        Left alone it dragged the gas trace out to the navigation's own span."""
        before = panel.get_xlim()

        rack_export.altitude_overlay(panel, navigation)

        assert panel.get_xlim() == pytest.approx(before)

    def test_the_gas_trace_stays_in_front_of_its_context(self, panel, navigation):
        twin = rack_export.altitude_overlay(panel, navigation)

        assert panel.get_zorder() > twin.get_zorder()
        # Raising the host would otherwise hide the twin behind the host's
        # own opaque background.
        assert not panel.patch.get_visible()

    def test_the_scale_is_named_in_the_colour_of_its_trace(self, panel, navigation):
        twin = rack_export.altitude_overlay(panel, navigation)

        assert twin.yaxis.label.get_color() == rack_export.ALTITUDE_COLOUR
        assert twin.get_lines()[0].get_color() == rack_export.ALTITUDE_COLOUR

    def test_the_context_carries_no_second_grid(self, panel, navigation):
        twin = rack_export.altitude_overlay(panel, navigation)

        assert not any(line.get_visible() for line in twin.get_ygridlines())

    def test_the_profile_is_rasterised_like_every_other_dense_trace(
        self, panel, navigation
    ):
        twin = rack_export.altitude_overlay(panel, navigation)

        assert twin.get_lines()[0].get_rasterized()


class TestAltitudeIsOptional:
    """A MIRO folder processed on its own is a complete record. The figure is
    drawn without the profile rather than refused."""

    def test_no_navigation_at_all(self, panel):
        assert rack_export.altitude_overlay(panel, None) is None

    def test_an_empty_navigation_frame(self, panel):
        empty = pd.DataFrame({"timestamp": [], "altitude": []})
        assert rack_export.altitude_overlay(panel, empty) is None

    def test_navigation_that_carries_no_altitude_column(self, panel):
        """The Mapview never needed altitude, so a flight can have positions
        and no heights."""
        positions = pd.DataFrame({
            "timestamp": pd.date_range("2026-08-06 06:00", periods=5, freq="1min"),
            "lat": [51.0] * 5,
            "lon": [6.7] * 5,
        })
        assert rack_export.altitude_overlay(panel, positions) is None

    def test_every_altitude_missing(self, panel):
        blank = pd.DataFrame({
            "timestamp": pd.date_range("2026-08-06 06:00", periods=5, freq="1min"),
            "altitude": [float("nan")] * 5,
        })
        assert rack_export.altitude_overlay(panel, blank) is None

    def test_no_second_scale_is_added_when_it_is_absent(self, panel):
        figure = panel.get_figure()
        rack_export.altitude_overlay(panel, None)
        assert len(figure.axes) == 1


class TestTheFiguresCarryIt:
    """The two figures that have a time axis; the comparison is one analyzer
    against the other, so there is no time axis for a profile to share."""

    def test_the_picarro_series_is_offered_navigation(self):
        import inspect

        signature = inspect.signature(rack_export.picarro_figure)
        assert signature.parameters["navigation"].default is None

    def test_the_miro_gas_page_is_offered_navigation(self):
        import inspect

        signature = inspect.signature(rack_export.miro_figure)
        assert signature.parameters["navigation"].default is None

    def test_the_comparison_is_not(self):
        import inspect

        signature = inspect.signature(rack_export.comparison_figure)
        assert "navigation" not in signature.parameters

    def test_the_export_offers_it_and_passes_it_down(self):
        import inspect

        source = inspect.getsource(rack_export.export_figures)
        assert (
            inspect.signature(rack_export.export_figures)
            .parameters["navigation"].default is None
        )
        assert "picarro_figure(pdata, params, progress, navigation=navigation)" in source
        assert "navigation=navigation," in source

    def test_the_margin_is_reserved_for_the_second_scale(self):
        """Left at the full width, the altitude ticks and name were drawn off
        the edge of the page."""
        import inspect

        for function in (rack_export.picarro_figure, rack_export.miro_figure):
            source = inspect.getsource(function)
            assert "if drawn else" in source, function.__name__


class TestItReachesTheWorkspace:
    """The export runs inside the legacy workspace, which knows nothing of the
    other instruments, so the dashboard has to hand it the profile."""

    WORKSPACE = (
        ROOT / "legacy_integration" / "MIRO_Rack" / "MIRO_Rack_GUI.py"
    ).read_text(encoding="utf-8")
    BRIDGE = (ROOT / "app" / "miro_rack_bridge.py").read_text(encoding="utf-8")

    def test_the_worker_reads_it_from_the_store(self):
        block = self.WORKSPACE[self.WORKSPACE.index("def export_worker("):]
        block = block[: block.index("\ndef ")]
        assert 'STORE.get("navigation")' in block
        assert "navigation=navigation," in block

    def test_the_bridge_publishes_it_before_forwarding_an_export(self):
        block = self.BRIDGE[self.BRIDGE.index("    def forward_post("):]
        block = block[: block.index("\n    def _remember_request_state")]
        assert '"/api/export"' in block
        assert "self._publish_navigation()" in block

    def test_it_is_resolved_at_export_rather_than_snapshotted(self):
        """The Noseboom can be processed after the gases, and the figure should
        still show the profile."""
        block = self.BRIDGE[self.BRIDGE.index("    def _publish_navigation("):]
        block = block[: block.index("\n    def forward_post")]
        assert "self._investigation_navigation()" in block

    def test_the_workspace_still_runs_without_the_dashboard(self):
        """STORE has no navigation key when this GUI is started on its own."""
        block = self.WORKSPACE[self.WORKSPACE.index("def export_worker("):]
        assert 'STORE["navigation"]' not in block[: block.index("\ndef ")]
