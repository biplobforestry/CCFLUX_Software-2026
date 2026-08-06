from __future__ import annotations

import base64
from datetime import datetime, timedelta
import io
import json
from pathlib import Path
from types import SimpleNamespace
import threading

import pandas as pd
from PIL import Image

from app.miro_rack_bridge import (
    MIRO_MAP_GASES,
    PICARRO_MAP_GASES,
    MiroRackBridge,
    _absent_instrument_meta,
)


class _NullLogger:
    def log(self, *args, **kwargs):
        return None


class _MinimalBackend:
    """Enough dashboard for the bridge to build its page and bootstrap."""

    def __init__(self, snapshot=None):
        self.logger = _NullLogger()
        self._snapshot = snapshot or {}

    def snapshot(self):
        return self._snapshot

    def _persist_project_logs(self):
        return None


def test_miro_rack_page_namespaces_legacy_api_and_adds_required_controls(tmp_path):
    class Logger:
        def log(self, *args, **kwargs):
            return None

    class Backend:
        logger = Logger()

        def _persist_project_logs(self):
            return None

    bridge = MiroRackBridge(tmp_path, Backend())
    html = bridge.page_html().decode("utf-8")

    assert "/api/miro-rack/analyze" in html
    assert "/api/miro-rack/export" in html
    assert "/miro_rack/plotly.min.js" in html
    assert "Update MIRO" in html
    assert "Update Picarro" in html
    assert "DateTime Filter" in html
    assert "Investigation Time Filter" in html
    assert "ccfluxFilterInside" in html
    assert "max-width:none" in html
    assert "Math.min(19,innerWidth/96)" in html
    assert "Mapview" in html
    assert "state.restored_session && state.rack_results_available" in html
    assert "await afterProjectLoad()" in html
    assert "PDF" in html and "PNG" in html and "SVG" in html
    assert "&copy; 2026 Biplob Dey &middot; Forschungszentrum J&uuml;lich GmbH" in html


def test_georeferencing_uses_nearest_recorded_timestamp_and_keeps_gas_values():
    start = datetime(2026, 7, 20, 10, 0, 0)
    navigation = pd.DataFrame(
        {
            "timestamp": [start + timedelta(seconds=value) for value in range(4)],
            "latitude": [50.0, 50.1, 50.2, 50.3],
            "longitude": [6.0, 6.1, 6.2, 6.3],
        }
    )
    prepared = MiroRackBridge._prepare_navigation(navigation)
    instrument = pd.DataFrame(
        {
            "timestamp": [
                start + timedelta(seconds=0.2),
                start + timedelta(seconds=2.2),
            ],
            "CO2 wet": [410.5, 411.25],
        }
    )

    layers = MiroRackBridge._instrument_layers(
        instrument, "MIRO", {"CO2 wet": "CO2 wet"}, prepared
    )

    assert [point["value"] for point in layers["CO2 wet"]] == [410.5, 411.25]
    assert [point["lat"] for point in layers["CO2 wet"]] == [50.0, 50.2]
    assert [point["lon"] for point in layers["CO2 wet"]] == [6.0, 6.2]


def test_georeferencing_accepts_saved_one_hz_time_field():
    start = datetime(2026, 7, 20, 10, 0, 0)
    navigation = MiroRackBridge._prepare_navigation(pd.DataFrame({
        "timestamp": [start, start + timedelta(seconds=1)],
        "latitude": [50.0, 50.1],
        "longitude": [6.0, 6.1],
    }))
    cached = pd.DataFrame({
        "time": [start.isoformat(), (start + timedelta(seconds=1)).isoformat()],
        "value": [410.5, 411.25],
    })

    layers = MiroRackBridge._instrument_layers(
        cached, "MIRO", {"CO2 wet": "value"}, navigation
    )

    assert [point["value"] for point in layers["CO2 wet"]] == [410.5, 411.25]

def test_mapview_has_four_dependent_layer_selectors_and_width_control():
    root = Path(__file__).resolve().parents[1]
    html = (root / "app" / "assets" / "miro_rack_map.html").read_text(
        encoding="utf-8"
    )
    script = (root / "app" / "assets" / "miro_rack_map.js").read_text(
        encoding="utf-8"
    )

    assert html.count('class="selection"') == 4
    assert html.count('class="layer-width"') == 4
    assert html.count('class="layer-offset"') == 4
    assert html.count('class="palette"') == 4
    for palette in ("cool", "YlOrRd", "viridis", "plasma"):
        assert f'<option value="{palette}">{palette}</option>' in html
    assert "gasOptions(selection)" in script
    assert "canonicalGas" in script
    assert "offsetTrack" in script
    assert "item.width" in script
    assert "item.offsetMetres" in script
    assert "namedPalettes" in script
    assert "bindTooltip" in script
    assert "groupStyles" in script
    assert 'id="flightTrackToggle"' in html
    assert 'id="exportMap"' in html
    assert 'id="resetMapPosition"' in html
    assert 'aria-label="Reset map position"' in html
    assert "dashArray:'2 8'" in script
    assert "exportCurrentMapPdf" in script
    assert "/api/miro-rack/map/export" in script
    assert "drawNorthArrow" in script
    assert "drawScaleBar" in script
    assert "utcMilliseconds" in script
    assert "function resetMapPosition()" in script
    assert "mapHomeBounds = bounds.length ? L.latLngBounds(bounds)" in script
    assert (
        "byId('resetMapPosition').addEventListener('click',resetMapPosition)"
        in script
    )


def test_mapview_exposes_nine_miro_trace_gases_and_three_picarro_compounds():
    assert MIRO_MAP_GASES == (
        "CO", "N2O", "NO", "NO2", "CH4", "SO2", "NH3", "O3", "CO2"
    )
    assert PICARRO_MAP_GASES == ("CO2", "CH4", "H2O")
    assert "H2O" not in MIRO_MAP_GASES


def test_high_resolution_map_pdf_is_saved_with_the_flight_project(tmp_path):
    class Logger:
        def log(self, *args, **kwargs):
            return None

    class Backend:
        def __init__(self):
            self.logger = Logger()
            self._lock = threading.RLock()
            self._flight_project = SimpleNamespace(
                flight_output_root=tmp_path,
                output_locations={},
            )
            self.checkpoints = 0

        def snapshot(self):
            return {"flight_id": "Flight_2707"}

        def _checkpoint_project(self):
            self.checkpoints += 1

        def _persist_project_logs(self):
            return None

    source = io.BytesIO()
    Image.new("RGB", (1200, 700), "white").save(source, format="PNG")
    encoded = base64.b64encode(source.getvalue()).decode("ascii")
    backend = Backend()
    bridge = MiroRackBridge(tmp_path, backend)

    filename, pdf = bridge.export_map_pdf({
        "image": f"data:image/png;base64,{encoded}",
        "flight_name": "Flight_2707",
        "timeframe": "2026-07-27 06:22:00 UTC to 2026-07-27 09:19:00 UTC",
    })

    assert filename.startswith("Flight_2707_MIRO_Rack_Map_")
    assert pdf.startswith(b"%PDF")
    saved = tmp_path / "exports" / "miro_rack_map" / filename
    assert saved.read_bytes() == pdf
    assert backend._flight_project.output_locations["miro_rack_map_exports"] == saved.parent
    assert backend.checkpoints == 1


def test_main_dashboard_links_miro_and_picarro_to_new_tab():
    root = Path(__file__).resolve().parents[1]
    html = (root / "app" / "assets" / "dashboard.html").read_text(
        encoding="utf-8"
    )

    assert (
        'data-name="MIRO" href="/miro_rack" target="_blank"' in html
    )
    assert (
        'data-name="Picarro" href="/miro_rack" target="_blank"' in html
    )

def test_map_only_resampling_is_one_hz_and_does_not_modify_source():
    source = pd.DataFrame({
        "timestamp": pd.to_datetime([
            "2026-07-27T06:00:00.000",
            "2026-07-27T06:00:00.400",
            "2026-07-27T06:00:01.100",
        ]),
        "value": [1.0, 3.0, 5.0],
    })
    original = source.copy(deep=True)

    records = MiroRackBridge._one_hz_series(source["timestamp"], source["value"])

    assert [item["value"] for item in records] == [2.0, 5.0]
    assert pd.DataFrame.equals(source, original)


def test_mapview_identifies_one_hz_policy_and_professional_header():
    root = Path(__file__).resolve().parents[1]
    html = (root / "app" / "assets" / "miro_rack_map.html").read_text(encoding="utf-8")
    script = (root / "app" / "assets" / "miro_rack_map.js").read_text(encoding="utf-8")

    # The standalone subpages embed the shared airship mark directly; the
    # iframe treatment survives only in the injected MIRO Rack header.
    assert 'src="/campaign-main-airship.svg"' in html
    assert "1 Hz Mapview copy aligned with Noseboom navigation" not in html
    assert "saved during main processing" not in html
    assert 'id="mapBusyDialog"' in html
    assert "requestAnimationFrame" in script
    assert "zoomstart" in script and "zoomend" in script
    assert "addEventListener('change'" in script
    assert "font-size:13px" in script
    assert html.count(">Default</option>") == 4
    assert "Existing (default)" not in html

def test_main_processing_prepares_and_reuses_saved_map_payload(tmp_path):
    start = datetime(2026, 7, 20, 10, 0, 0)
    quicklook = tmp_path / "noseboom_browser.json"
    quicklook.write_text(json.dumps({"points": [
        {"time": start.isoformat(), "lat": 50.0, "lon": 6.0},
        {"time": (start + timedelta(seconds=1)).isoformat(), "lat": 50.1, "lon": 6.1},
    ]}), encoding="utf-8")

    class Logger:
        def log(self, *args, **kwargs):
            return None

    class Backend:
        def __init__(self):
            self.logger = Logger()
            self._lock = threading.RLock()
            self._flight_project = SimpleNamespace(
                flight_output_root=tmp_path,
                output_locations={"noseboom_quicklook": quicklook},
            )

        def _checkpoint_project(self):
            return None

        def _persist_project_logs(self):
            return None

    bridge = MiroRackBridge(tmp_path, Backend())
    bridge._bind_current_project()
    bridge._map_series["MIRO"] = {"CO2": [
        {"time": start.isoformat(), "value": 410.5},
        {"time": (start + timedelta(seconds=1)).isoformat(), "value": 411.0},
    ]}
    bridge._map_units["MIRO"] = {"CO2": "ppm"}

    result = bridge.prepare_map_during_main_processing()

    assert result["ready"] is True
    assert result["payload"]["prepared_during"] == "main processing"
    assert result["payload"]["source"] == str(quicklook)
    assert result["payload"]["layers"]["MIRO"]["CO2"][0]["lat"] == 50.0
    bridge._map_payload = None
    status = bridge.start_map_job()
    assert status["ready"] is True
    assert status["percent"] == 100.0


class TestPicarroOnlyFlightPopulatesTheOverview:
    """Flight_CCT0803 carries Picarro and no MIRO.

    The rack overview stayed empty on it - no time series, no distribution and
    an empty Trace gas list - while /api/miro-rack/results already held a
    complete Picarro analysis. Every remaining both-or-nothing gate between the
    payload and the page is covered here.
    """

    def _page(self, tmp_path):
        return MiroRackBridge(tmp_path, _MinimalBackend()).page_html().decode("utf-8")

    def test_the_bootstrap_block_runs_with_only_one_source_path(self, tmp_path):
        """This was the gate that stopped everything downstream."""
        html = self._page(tmp_path)
        assert "(state.miro_path || state.picarro_path)" in html
        assert "state.miro_path && state.picarro_path" not in html

    def test_the_session_restore_needs_only_one_instrument(self, tmp_path):
        html = self._page(tmp_path)
        assert "meta?.miro?.rows||meta?.picarro?.rows" in html
        assert "meta?.miro?.rows&&meta?.picarro?.rows" not in html

    def test_a_restored_session_renders_whichever_results_exist(self, tmp_path):
        html = self._page(tmp_path)
        assert "restoreLoadedResults" in html
        assert "if(hasPicarro)await renderPicarro(result.picarro)" in html

    def test_either_folder_is_enough_to_load(self, tmp_path):
        html = self._page(tmp_path)
        assert "miroPath.value.trim()||picarroPath.value.trim()" in html
        assert "Select a MIRO folder, a Picarro folder, or both." in html

    def test_the_gas_selectors_tolerate_an_absent_instrument(self, tmp_path):
        html = self._page(tmp_path)
        assert "app.meta.miro?.gases" in html
        assert "app.meta.picarro?.gases" in html
        assert "Array.isArray(values)?values:[]" in html

    def test_the_summary_names_the_instrument_that_was_not_recorded(self, tmp_path):
        html = self._page(tmp_path)
        assert "Not recorded on this flight" in html

    def test_a_gas_selector_with_no_options_stays_disabled(self, tmp_path):
        html = self._page(tmp_path)
        assert "!picarroGas.options.length" in html
        assert "!miroGas.options.length" in html

    def test_the_absent_instrument_block_has_the_loader_keys(self):
        """The page reads rows and gases off it, so the shape has to match."""
        absent = _absent_instrument_meta()
        assert absent["rows"] == 0
        assert absent["gases"] == []
        assert absent["start"] is None and absent["end"] is None
        assert absent["files_used"] == 0

    def test_bootstrap_reports_only_the_detected_source(self, tmp_path):
        """Picarro is detected inside a folder named MIRO on this flight."""
        backend = _MinimalBackend({
            "flight_id": "Flight_CCT0803",
            "instruments": {
                "picarro": {"candidate_paths": [str(tmp_path / "MIRO" / "03")]},
                "miro": {"candidate_paths": []},
            },
        })
        (tmp_path / "MIRO" / "03").mkdir(parents=True)
        bridge = MiroRackBridge(tmp_path, backend)
        state = bridge.bootstrap()
        assert state["picarro_path"].endswith("03")
        assert state["miro_path"] == ""


class TestPicarroOnlyLoadWorker:
    """The legacy loader raised out of the job when a folder was absent."""

    def _module(self, tmp_path):
        bridge = MiroRackBridge(tmp_path, _MinimalBackend())
        return bridge.module

    def test_an_absent_miro_folder_does_not_fail_the_load(self, tmp_path):
        source = (
            Path(__file__).resolve().parents[1]
            / "legacy_integration" / "MIRO_Rack" / "MIRO_Rack_GUI.py"
        ).read_text(encoding="utf-8")
        worker = source[source.index("def load_worker("):source.index("def comparison_payload(")]
        assert "if miro_path:" in worker
        assert "if picarro_path:" in worker
        assert "loading Picarro alone" in worker

    def test_neither_folder_is_still_refused(self, tmp_path):
        source = (
            Path(__file__).resolve().parents[1]
            / "legacy_integration" / "MIRO_Rack" / "MIRO_Rack_GUI.py"
        ).read_text(encoding="utf-8")
        assert "Select a MIRO folder, a Picarro folder, or both." in source

    def test_a_comparison_still_needs_both(self, tmp_path):
        """Only the overview is single-instrument; a correlation is not."""
        source = (
            Path(__file__).resolve().parents[1]
            / "legacy_integration" / "MIRO_Rack" / "MIRO_Rack_GUI.py"
        ).read_text(encoding="utf-8")
        worker = source[source.index("def comparison_worker("):]
        assert "if mdata is None or pdata is None" in worker[:600]
