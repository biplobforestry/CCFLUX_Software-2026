"""MIRO Rack browser integration backed by the preserved scientific modules."""

from __future__ import annotations

import base64
import binascii
import io
import importlib.util
import json
import math
import re
import sys
import tempfile
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from types import ModuleType
from typing import Any
from urllib.parse import urlencode

import numpy as np
import pandas as pd
from PIL import Image, UnidentifiedImageError

from core.legacy_paths import legacy_integration_path
from core.logging_manager import LogLevel
from core.map_pdf_export import safe_file_stem


LEGACY_DIRECTORY = legacy_integration_path("MIRO_Rack")
LEGACY_ENTRYPOINT = LEGACY_DIRECTORY / "MIRO_Rack_GUI.py"
MIRO_MAP_GASES = ("CO", "N2O", "NO", "NO2", "CH4", "SO2", "NH3", "O3", "CO2")
PICARRO_MAP_GASES = ("CO2", "CH4", "H2O")
MIRO_MAP_UNITS = {
    gas: ("ppm" if gas in {"CH4", "CO2"} else "ppb")
    for gas in MIRO_MAP_GASES
}
PICARRO_MAP_UNITS = {"CO2": "ppm", "CH4": "ppm", "H2O": "%"}


def _absent_instrument_meta() -> dict[str, Any]:
    """The metadata block for an instrument this flight does not carry.

    The same keys the preserved loaders return, so the page reads zero rows and
    no gases rather than tripping over a missing object. A flight with only one
    of the two rack instruments is normal, not an error.
    """
    return {
        "files_found": 0,
        "files_used": 0,
        "duplicate_files": [],
        "skipped_files": [],
        "duplicate_timestamps_removed": 0,
        "sorted": True,
        "rows": 0,
        "start": None,
        "end": None,
        "gases": [],
    }


class MiroRackBridge:
    """Namespace the legacy app and add non-blocking flight georeferencing."""

    def __init__(self, application_root: Path, dashboard_backend: Any) -> None:
        self.application_root = Path(application_root)
        self.dashboard_backend = dashboard_backend
        self.module = self._load_legacy_module()
        self._lock = threading.RLock()
        self._map_job: dict[str, Any] = {
            "running": False,
            "percent": 0.0,
            "message": "Mapview has not been synchronized.",
            "error": None,
            "ready": False,
        }
        self._map_payload: dict[str, Any] | None = None
        self._map_series: dict[str, dict[str, list[dict[str, Any]]]] = {
            "MIRO": {}, "Picarro": {}
        }
        self._map_units: dict[str, dict[str, str]] = {
            "MIRO": {}, "Picarro": {}
        }
        self._map_build_lock = threading.Lock()
        self._project_save_lock = threading.Lock()
        self._active_project_root: Path | None = None
        self._ui_state: dict[str, Any] = {}

    def _bind_current_project(self) -> Path | None:
        """Prevent cached map layers from leaking between loaded Flight Projects."""
        with self.dashboard_backend._lock:
            project = self.dashboard_backend._flight_project
            root = (
                Path(project.flight_output_root).resolve(strict=False)
                if project is not None
                else None
            )
        with self._lock:
            if root != self._active_project_root:
                self._active_project_root = root
                self._map_payload = None
                self._map_series = {"MIRO": {}, "Picarro": {}}
                self._map_units = {"MIRO": {}, "Picarro": {}}
                self._map_job = {
                    "running": False,
                    "percent": 0.0,
                    "message": "Mapview has not been prepared for this Flight Project.",
                    "error": None,
                    "ready": False,
                }
        return root
    @staticmethod
    def _load_legacy_module() -> ModuleType:
        if not LEGACY_ENTRYPOINT.is_file():
            raise FileNotFoundError(
                f"Preserved MIRO Rack GUI is unavailable: {LEGACY_ENTRYPOINT}"
            )
        directory_text = str(LEGACY_DIRECTORY)
        if directory_text not in sys.path:
            sys.path.insert(0, directory_text)
        spec = importlib.util.spec_from_file_location(
            "ccflux_preserved_miro_rack_gui", LEGACY_ENTRYPOINT
        )
        if spec is None or spec.loader is None:
            raise ImportError(f"Could not load {LEGACY_ENTRYPOINT}")
        module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = module
        spec.loader.exec_module(module)
        return module

    def page_html(self) -> bytes:
        html = str(self.module.HTML)
        html = html.replace(
            "size:Math.max(10,Math.min(14,innerWidth/115))",
            "size:Math.max(15,Math.min(19,innerWidth/96))",
        )
        html = html.replace(
            "function chartFontSize(){return Math.max(10,Math.min(14,innerWidth/115))}",
            "function chartFontSize(){return Math.max(15,Math.min(19,innerWidth/96))}",
        )
        html = html.replace("'/api/", "'/api/miro-rack/")
        html = html.replace('"/api/', '"/api/miro-rack/')
        html = html.replace(
            'src="/plotly.min.js"', 'src="/miro_rack/plotly.min.js"'
        )
        html = html.replace(
            '<footer class="app-footer"><span>&copy; Biplob Dey, 2026</span><span class="version">Version 1.0_26_07</span></footer>',
            '<footer class="app-footer"><span>&copy; 2026 Biplob Dey &middot; Forschungszentrum J&uuml;lich GmbH &middot; Version 1.0.2</span></footer>',
        )
        html = html.replace(
            "</head>",
            """
<style>
  .ccflux-program-bar{position:sticky;top:0;z-index:18;display:flex;align-items:center;
    justify-content:space-between;gap:20px;padding:12px 24px;background:#071827;
    border-bottom:1px solid #1e4255;color:#e9f7ff;box-shadow:0 4px 18px #00101855}
  .ccflux-program-bar strong{color:#35d5ff;font-family:"Segoe UI",Arial,sans-serif;
    font-size:clamp(25px,1.8vw,36px);font-weight:800;line-height:1.08;letter-spacing:.01em;white-space:nowrap}
  .ccflux-program-title>iframe{display:block;width:194px;height:98px;border:0;
    background:transparent;flex:0 0 194px;max-width:none}
  html{font-size:clamp(16px,calc(12px + .34vw),21px)}body{font-size:1rem}button,input,select{font-size:inherit!important;min-height:42px!important}.container{width:calc(100% - 24px)!important;max-width:none!important}.dialog-body{font-size:1rem!important}@media(max-width:760px){.ccflux-program-bar{padding:9px 12px;gap:9px}.ccflux-program-bar iframe{width:132px;height:72px;flex-basis:132px}.ccflux-program-bar span{display:none}}
  .ccflux-program-bar span{color:#a9c8d5;font-size:13px}
  body>header,main>section.card:first-of-type{display:none!important}
  .ccflux-program-bar{position:sticky!important}
  .ccflux-program-title{display:flex;align-items:center;gap:14px;min-width:0;flex:1 1 460px}
  .ccflux-program-title div{display:flex;flex-direction:column;min-width:0}
  .ccflux-program-title small{color:#a9c8d5;font-family:"Segoe UI",Arial,sans-serif;
    font-size:clamp(14px,.88vw,18px);line-height:1.35}
  .ccflux-program-actions{display:flex;align-items:center;gap:8px;flex-wrap:wrap}
  .ccflux-program-actions button{border:1px solid #3b6480;border-radius:8px;background:#10293d;color:#edf8ff;padding:7px 12px;font-weight:600;cursor:pointer}
  .ccflux-program-actions .ccflux-map-button{border-color:#39b99e}
  .container{padding:18px 0 0!important}.card{padding:20px!important}.panel h3{font-size:clamp(17px,1vw,21px)!important}.plot{height:470px!important}.plot.tall{height:530px!important}.correlation-plot{height:560px!important}
  .ccflux-credit{padding:14px 22px;text-align:center;background:#071827;color:#dcebf1;
    border-top:1px solid #1e4255;font-weight:700}
  .ccflux-map-button{background:linear-gradient(135deg,#0e7773,#159447)!important;color:white!important}
  .ccflux-refresh-button{border-color:#3dbbd8!important}
  #mapSyncDialog{max-width:560px}
  #mapSyncDialog .sync-progress{height:16px;background:#d8e5e5;border-radius:20px;overflow:hidden}
  #mapSyncFill{height:100%;width:0;background:linear-gradient(90deg,#0e7773,#35d5ff);
    transition:width .25s}
</style>
</head>""",
        )
        html = html.replace(
            "<body>",
            """<body>
<div class="ccflux-program-bar">
  <div class="ccflux-program-title"><iframe src="/campaign-logo.html" title="CC-FLUX 2026 campaign logo" scrolling="no"></iframe><div><strong>CC-FLUX Campaign 2026</strong><small>Integrated Post-Flight Scientific Payload Review &middot; MIRO Rack</small></div></div>
  <div class="ccflux-program-actions"><span id="ccfluxMainInterval">Using the main GUI time filter</span><button type="button" onclick="window.location.href='/'">Main GUI</button><button type="button" onclick="window.location.href='/#time-filter'">Main Time Filter</button><button type="button" id="ccfluxInvestigationFilter">Investigation Time Filter</button><button type="button" class="ccflux-refresh-button" id="ccfluxMiroRefresh">Refresh</button><button type="button" class="ccflux-map-button" id="ccfluxTraceGas" title="Attribute each species to cell temperature, altitude and time, and separate a drift from the atmosphere">Trace gas investigation</button><button type="button" class="ccflux-map-button" id="ccfluxMiroMap">Mapview</button></div>
</div>""",
            1,
        )
        html = html.replace(
            "</body>",
            """
<dialog id="mapSyncDialog">
  <div class="dialog-body">
    <h2>Mapview</h2>
    <p id="mapSyncMessage">Synchronizing and georeferencing...</p>
    <div class="sync-progress"><div id="mapSyncFill"></div></div>
    <p><strong id="mapSyncPercent">0%</strong></p>
    <div class="dialog-actions">
      <button id="mapSyncClose" onclick="closeMapSync()" disabled>Close</button><button class="primary" id="mapSyncOpen" onclick="openPreparedMap()" disabled>Open Mapview</button>
    </div>
  </div>
</dialog>

<script>
let ccfluxLegacyApplyFilters = null;
(() => {
  document.getElementById('ccfluxMiroRefresh').onclick = () => bootstrapFromMainProject();
  document.getElementById('ccfluxMiroMap').onclick = startMapSync;
  // Its own tab: an investigation is read alongside the workspace it questions,
  // not instead of it.
  document.getElementById('ccfluxTraceGas').onclick = () =>
    window.open('/miro_rack/trace_gas', '_blank', 'noopener');
  document.getElementById('ccfluxInvestigationFilter').onclick = openInvestigationTimeFilter;
  ccfluxLegacyApplyFilters = typeof window.applyFilters === 'function'
    ? window.applyFilters.bind(window) : null;
  const investigationApply = [...document.querySelectorAll('#filterDialog .dialog-actions button')]
    .find(button => button.textContent.trim() === 'Apply');
  if (investigationApply) {
    investigationApply.removeAttribute('onclick');
    investigationApply.onclick = applyInvestigationTimeFilter;
  }
  bootstrapFromMainProject();
  document.addEventListener('change', queueMiroRackStateSync);
  document.addEventListener('click', event => {
    if (event.target.closest('button')) setTimeout(queueMiroRackStateSync, 250);
  });
  window.addEventListener('pagehide', syncMiroRackState);
  setTimeout(queueMiroRackStateSync, 750);
})();
let miroRackStateTimer = null;
function queueMiroRackStateSync() {
  clearTimeout(miroRackStateTimer);
  miroRackStateTimer = setTimeout(syncMiroRackState, 250);
}
async function syncMiroRackState() {
  if (typeof currentState !== 'function') return;
  try {
    await fetch('/api/miro-rack/ui-state', {
      method:'POST', headers:{'Content-Type':'application/json'},
      body:JSON.stringify(currentState()), keepalive:true
    });
  } catch (_) {}
}
let ccfluxMainAnalysisKey = '';
let ccfluxMainFilterKey = '';
let ccfluxMainBounds = null;
function ccfluxUtcMilliseconds(value) {
  const text = String(value || '').trim().replace(' ', 'T');
  if (!text) return NaN;
  const explicitZone = /(?:Z|[+-][0-9]{2}:?[0-9]{2})$/i.test(text);
  return Date.parse(explicitZone ? text : `${text}Z`);
}
function ccfluxClampInterval(start, end, availableStart, availableEnd) {
  const low = new Date(Math.max(ccfluxUtcMilliseconds(start), ccfluxUtcMilliseconds(availableStart)));
  const high = new Date(Math.min(ccfluxUtcMilliseconds(end), ccfluxUtcMilliseconds(availableEnd)));
  return high > low ? [filterInput(low.toISOString()), filterInput(high.toISOString())] : null;
}
function ccfluxFilterInside(values, bounds) {
  if (!values || !bounds) return false;
  // Only the instruments this flight actually has are checked. An absent one
  // has no bounds to be inside of, and demanding them failed every check.
  const within = (name, startKey, endKey) => {
    const range = bounds[name];
    if (!range) return true;
    const low = parseFilter(values[startKey]), high = parseFilter(values[endKey]);
    if (!Number.isFinite(low) || !Number.isFinite(high)) return false;
    return low >= parseFilter(range[0]) && high <= parseFilter(range[1]);
  };
  if (!bounds.miro && !bounds.picarro) return false;
  return within('miro', 'miro_start', 'miro_end')
    && within('picarro', 'picarro_start', 'picarro_end');
}
function applyMainProjectTimeFilter(timeState) {
  if (!app?.meta || !timeState?.selected_analysis_start || !timeState?.selected_analysis_end) return '';
  // app.meta.<instrument> only exists once that instrument has been processed,
  // so reading .start off it threw for a flight carrying only one of them.
  const clampFor = name => {
    const info = app.meta[name];
    if (!info || !info.start || !info.end) return null;
    return ccfluxClampInterval(timeState.selected_analysis_start, timeState.selected_analysis_end, info.start, info.end);
  };
  const miroRange = clampFor('miro');
  const picarroRange = clampFor('picarro');
  if (!miroRange && !picarroRange) throw new Error('The main GUI Time Filter does not overlap MIRO or Picarro data.');
  ccfluxMainBounds = {miro:miroRange, picarro:picarroRange};
  const mainKey = JSON.stringify(ccfluxMainBounds);
  if (!app.filtersApplied || !ccfluxFilterInside(app.filters, ccfluxMainBounds)) {
    // Destructuring a null range threw; only what exists is filled in.
    if (miroRange) [miroStart.value, miroEnd.value] = miroRange;
    if (picarroRange) [picarroStart.value, picarroEnd.value] = picarroRange;
    app.filters = {
      miro_start: miroRange ? miroRange[0] : '', miro_end: miroRange ? miroRange[1] : '',
      picarro_start: picarroRange ? picarroRange[0] : '', picarro_end: picarroRange ? picarroRange[1] : ''
    };
    app.filtersApplied = true;
    app.mismatchAccepted = true;
    ccfluxMainAnalysisKey = '';
  }
  ccfluxMainFilterKey = mainKey;
  app.filterMessage = 'Investigation filter is constrained by the main GUI Time Filter';
  metaSummary();
  updateSetupState();
  const label = document.getElementById('ccfluxMainInterval');
  if (label) label.textContent = `${filterInput(timeState.selected_analysis_start)} to ${filterInput(timeState.selected_analysis_end)} UTC`;
  return JSON.stringify(app.filters);
}
function openInvestigationTimeFilter() {
  if (!app?.loaded) {
    alert('Process or load MIRO or Picarro data first.');
    return;
  }
  let note = document.getElementById('ccfluxInvestigationNote');
  if (!note) {
    note = document.createElement('p');
    note.id = 'ccfluxInvestigationNote';
    note.className = 'miro-note';
    filterDialog.querySelector('.dialog-grid').before(note);
  }
  const describe = (label, range) => range ? `${label}: ${range[0]} to ${range[1]}` : `${label}: no data available`;
  note.textContent = ccfluxMainBounds
    ? `Scientific investigation only. ${describe('MIRO', ccfluxMainBounds.miro)}; ${describe('Picarro', ccfluxMainBounds.picarro)}. The main GUI Time Filter is unchanged.`
    : 'Scientific investigation only. This filter never changes the main GUI Time Filter.';
  filterDialog.showModal();
}
function applyInvestigationTimeFilter() {
  const next = {miro_start:miroStart.value.trim(), miro_end:miroEnd.value.trim(), picarro_start:picarroStart.value.trim(), picarro_end:picarroEnd.value.trim()};
  if (!ccfluxFilterInside(next, ccfluxMainBounds)) {
    alert('The investigation interval must remain inside the selected main GUI Time Filter for the available instruments.');
    return;
  }
  if (ccfluxLegacyApplyFilters) ccfluxLegacyApplyFilters();
  app.filterMessage = 'Investigation-only Time Filter applied inside the main GUI interval';
  metaSummary();
  queueMiroRackStateSync();
}async function bootstrapFromMainProject() {
  try {
    const response = await fetch('/api/miro-rack/bootstrap', {cache:'no-store'});
    const state = await response.json();
    if (!response.ok) throw new Error(state.error || 'Main-project state unavailable');
    if (state.restored_session && state.rack_results_available && typeof afterProjectLoad === 'function' && !app.resultsCurrent) {
      await afterProjectLoad();
    }
    if (state.flight_no && !flightNo.value) flightNo.value = state.flight_no;
    if (state.output_path && !outputPath.value) {
      outputPath.value = state.output_path;
      outputSummary.textContent = `Output: ${state.output_path}`;
      outputSummary.title = state.output_path;
    }
    // Whichever analyzers this flight carried. Requiring both meant a
    // Picarro-only flight never reached loadData or runAnalysis below, so the
    // overview stayed empty while /api/results already held its analysis.
    const sourceChanged = Boolean(
      (state.miro_path && miroPath.value !== state.miro_path) ||
      (state.picarro_path && picarroPath.value !== state.picarro_path)
    );
    if (state.miro_path) miroPath.value = state.miro_path;
    if (state.picarro_path) picarroPath.value = state.picarro_path;
    updateSetupState();
    if (typeof app !== 'undefined' && (state.miro_path || state.picarro_path)) {
      if (sourceChanged) app.loaded = false;
      if (!app.loaded && !app.busy) {
        await loadData();
        setTimeout(bootstrapFromMainProject, 700);
        return;
      }
      if (app.busy) {
        setTimeout(bootstrapFromMainProject, 700);
        return;
      }
      const analysisKey = applyMainProjectTimeFilter(state.time_filter || {});
      if (analysisKey && analysisKey !== ccfluxMainAnalysisKey) {
        ccfluxMainAnalysisKey = analysisKey;
        app.resultsCurrent = false;
        await runAnalysis();
      }
    }
  } catch (error) {
    if (typeof reportClientError === 'function') reportClientError('main project bootstrap', error);
  }
}
let mapSyncTimer = null;
async function startMapSync() {
  const dialog = document.getElementById('mapSyncDialog');
  document.getElementById('mapSyncClose').disabled = true;
  document.getElementById('mapSyncOpen').disabled = true;
  document.getElementById('mapSyncMessage').textContent = 'Synchronizing and georeferencing...';
  document.getElementById('mapSyncFill').style.width = '0%';
  document.getElementById('mapSyncPercent').textContent = '0%';
  dialog.showModal();
  try {
    const response = await fetch('/api/miro-rack/map/start', {
      method:'POST', headers:{'Content-Type':'application/json'}, body:'{}'
    });
    const body = await response.json();
    if (!response.ok) throw new Error(body.error || 'Could not start Mapview');
    clearTimeout(mapSyncTimer);
    mapSyncTimer = setTimeout(pollMapSync, 150);
  } catch (error) {
    document.getElementById('mapSyncMessage').textContent = error.message;
    document.getElementById('mapSyncClose').disabled = false;
  }
}
async function pollMapSync() {
  try {
    const response = await fetch('/api/miro-rack/map/progress', {cache:'no-store'});
    const state = await response.json();
    if (!response.ok) throw new Error(state.error || 'Mapview status unavailable');
    const percent = Number(state.percent || 0);
    document.getElementById('mapSyncFill').style.width = percent + '%';
    document.getElementById('mapSyncPercent').textContent = percent.toFixed(1) + '%';
    document.getElementById('mapSyncMessage').textContent = state.message || 'Working...';
    if (state.running) { mapSyncTimer = setTimeout(pollMapSync, 300); return; }
    document.getElementById('mapSyncClose').disabled = false;
    if (state.error) throw new Error(state.error);
    document.getElementById('mapSyncOpen').disabled = !state.ready;
    if (state.ready) {
      document.getElementById('mapSyncMessage').textContent = 'Synchronization is ready. Open Mapview when you are ready.';
    }
  } catch (error) {
    document.getElementById('mapSyncMessage').textContent = error.message;
    document.getElementById('mapSyncClose').disabled = false;
  }
}
function openPreparedMap() {
  window.open('/miro_rack/map', '_blank', 'noopener');
  closeMapSync();
}
function closeMapSync() {
  clearTimeout(mapSyncTimer);
  document.getElementById('mapSyncDialog').close();
}
</script>
</body>""",
            1,
        )
        return html.encode("utf-8")

    def forward_get(
        self, relative_path: str, query: dict[str, list[str]] | None = None
    ) -> tuple[int, str, bytes, dict[str, str]]:
        if relative_path == "/api/navigation":
            self._publish_navigation()
        path = relative_path
        if query:
            pairs = [(key, item) for key, values in query.items() for item in values]
            path += "?" + urlencode(pairs)
        with self.module.app.test_client() as client:
            response = client.get(path)
            headers = {
                key: value
                for key, value in response.headers.items()
                if key.casefold() in {"content-disposition"}
            }
            return (
                int(response.status_code),
                response.content_type,
                bytes(response.data),
                headers,
            )

    def _publish_navigation(self) -> None:
        """Hand the workspace the flight's altitude record before it exports.

        The gas time series carry altitude on their right-hand scale, and the
        export runs inside the legacy workspace, which knows nothing of the
        other instruments. It is resolved here at the moment of export rather
        than snapshotted when MIRO was processed, because the Noseboom may be
        processed after the gases and the figure should still show the profile.
        """
        with self.module.LOCK:
            self.module.STORE["navigation"] = self._investigation_navigation()

    def forward_post(
        self, relative_path: str, body: dict[str, Any]
    ) -> tuple[int, str, bytes, dict[str, str]]:
        if relative_path == "/api/exit":
            payload = json.dumps(
                {"exiting": False, "message": "Close this MIRO Rack browser tab."}
            ).encode("utf-8")
            return 200, "application/json; charset=utf-8", payload, {}
        if relative_path == "/api/export":
            self._publish_navigation()
        with self.module.app.test_client() as client:
            response = client.post(relative_path, json=body)
            headers = {
                key: value
                for key, value in response.headers.items()
                if key.casefold() in {"content-disposition"}
            }
            if 200 <= int(response.status_code) < 300:
                self._remember_request_state(relative_path, body)
            return (
                int(response.status_code),
                response.content_type,
                bytes(response.data),
                headers,
            )

    def _remember_request_state(
        self, relative_path: str, body: dict[str, Any]
    ) -> None:
        state = body.get("parameters") if relative_path == "/api/export" else body
        if relative_path in {"/api/analyze", "/api/compare", "/api/export"}:
            with self._lock:
                self._ui_state.update(dict(state or {}))
        elif relative_path == "/api/load":
            with self._lock:
                self._ui_state.update(
                    {
                        "miro_path": body.get("miro_path", ""),
                        "picarro_path": body.get("picarro_path", ""),
                    }
                )

    def update_ui_state(self, state: dict[str, Any]) -> dict[str, Any]:
        with self._lock:
            self._ui_state = dict(state or {})
            return dict(self._ui_state)

    @staticmethod
    def _recorded_filter_value(value: Any) -> str | None:
        if value is None:
            return None
        stamp = pd.Timestamp(value)
        if stamp.tzinfo is not None:
            stamp = stamp.tz_convert("UTC").tz_localize(None)
        return stamp.strftime("%m-%d-%Y %H:%M")

    @staticmethod
    def _one_hz_series(
        timestamps: Any, values: Any
    ) -> list[dict[str, Any]]:
        frame = pd.DataFrame(
            {
                "timestamp": _naive_utc(timestamps),
                "value": pd.to_numeric(values, errors="coerce"),
            }
        ).dropna()
        if frame.empty:
            return []
        series = (
            frame.sort_values("timestamp")
            .drop_duplicates("timestamp")
            .set_index("timestamp")["value"]
            .resample("1s")
            .mean()
            .dropna()
        )
        return [
            {"time": stamp.isoformat(), "value": float(value)}
            for stamp, value in series.items()
            if np.isfinite(value)
        ]

    def publish_main_processing_instrument(
        self,
        *,
        instrument_id: str,
        data: pd.DataFrame,
        metadata: dict[str, Any],
        analysis: dict[str, Any],
        source_paths: tuple[Path, ...],
        selected_start: Any,
        selected_end: Any,
        output_root: Path,
        progress_callback: Any = None,
    ) -> dict[str, Any]:
        """Publish original science plus a separate map-only 1 Hz copy."""
        self._bind_current_project()
        if instrument_id not in {"miro", "picarro"}:
            raise ValueError(f"Unsupported MIRO Rack instrument: {instrument_id}")
        instrument_name = "MIRO" if instrument_id == "miro" else "Picarro"
        start = self._recorded_filter_value(selected_start)
        end = self._recorded_filter_value(selected_end)
        params = {
            "flight_no": self.dashboard_backend.snapshot().get("flight_id") or "",
            "miro_gas": "NO2 wet",
            "picarro_gas": "CO2 raw",
            "smooth_seconds": 300.0,
            "miro_start": start,
            "miro_end": end,
            "picarro_start": start,
            "picarro_end": end,
        }
        if progress_callback:
            progress_callback(0.03, f"{instrument_name}: retaining original-resolution scientific data")
        layers: dict[str, list[dict[str, Any]]] = {}
        units: dict[str, str] = {}
        if instrument_id == "miro":
            gases = [
                gas for gas in self.module.miro.GAS_COLUMNS
                if gas in data and str(gas).split()[0].upper() in MIRO_MAP_GASES
            ]
            for index, gas in enumerate(gases, start=1):
                if progress_callback:
                    progress_callback(
                        0.05 + 0.78 * (index - 1) / max(1, len(gases)),
                        f"MIRO Mapview copy: stable ambient {gas} at 1 Hz",
                    )
                try:
                    gas_result = self.module.miro.analyze(
                        data, gas, 300.0, start, end, 30.0
                    )
                except (KeyError, TypeError, ValueError) as exc:
                    self.dashboard_backend.logger.log(
                        LogLevel.WARNING,
                        "miro-rack-map-preparation",
                        f"MIRO {gas} omitted from the 1 Hz Mapview copy: {exc}",
                        instrument="miro",
                        processing_step="map-only-resample",
                    )
                    continue
                series = gas_result["series"]
                map_gas = str(gas).split()[0].upper()
                layers[map_gas] = self._one_hz_series(
                    series["time"], series["ambient"]
                )
                units[map_gas] = str(gas_result["unit"])
            for gas in MIRO_MAP_GASES:
                layers.setdefault(gas, [])
                units.setdefault(gas, MIRO_MAP_UNITS[gas])
        else:
            sliced = self.module.picarro._slice(data, start, end)
            gases = list(self.module.picarro.GAS_COLUMNS.items())
            for index, (gas, (column, unit)) in enumerate(gases, start=1):
                if column not in sliced:
                    continue
                if progress_callback:
                    progress_callback(
                        0.05 + 0.78 * (index - 1) / max(1, len(gases)),
                        f"Picarro Mapview copy: {gas} at 1 Hz",
                    )
                map_gas = str(gas).split()[0].upper()
                layers[map_gas] = self._one_hz_series(
                    sliced["timestamp"], sliced[column]
                )
                units[map_gas] = str(unit)
            for gas in PICARRO_MAP_GASES:
                layers.setdefault(gas, [])
                units.setdefault(gas, PICARRO_MAP_UNITS[gas])

        output_root = Path(output_root)
        output_root.mkdir(parents=True, exist_ok=True)
        analysis_path = output_root / f"{instrument_id}_analysis.json"
        analysis_path.write_text(
            json.dumps(analysis, indent=2, allow_nan=False), encoding="utf-8"
        )
        with self.module.LOCK:
            store_meta = dict(self.module.STORE.get("meta") or {})
            store_paths = dict(store_meta.get("paths") or {})
            store_paths[instrument_id] = str(Path(source_paths[0]).parent)
            store_meta[instrument_id] = dict(metadata)
            # Both keys always, so the page can read rows and gases for an
            # instrument this flight does not carry instead of failing on an
            # undefined one. Flight_CCT0803 has Picarro and no MIRO.
            for name in ("miro", "picarro"):
                store_meta.setdefault(name, _absent_instrument_meta())
            store_meta["paths"] = store_paths
            store_results = dict(self.module.STORE.get("results") or {})
            store_results[instrument_id] = analysis
            store_results.setdefault("comparison", {})
            store_results["parameters"] = {
                **dict(store_results.get("parameters") or {}), **params
            }
            self.module.STORE[instrument_id] = data
            self.module.STORE["meta"] = store_meta
            self.module.STORE["results"] = store_results
        with self._lock:
            self._map_series[instrument_name] = layers
            self._map_units[instrument_name] = units
            self._ui_state.update(params)
            self._ui_state["filters"] = {
                "miro_start": start,
                "miro_end": end,
                "picarro_start": start,
                "picarro_end": end,
            }
            self._map_payload = None
        if progress_callback:
            progress_callback(0.88, f"{instrument_name}: saving the 1 Hz Mapview preparation")
        with self._project_save_lock:
            map_series_path = self._save_map_series_project()
        map_result = self.prepare_map_during_main_processing(
            (
                lambda fraction, message: progress_callback(
                    0.89 + 0.05 * min(1.0, max(0.0, float(fraction))),
                    message,
                )
            )
            if progress_callback
            else None
        )
        project_saved = False
        with self.module.LOCK:
            # Whichever instruments this flight has. Requiring both meant a
            # flight carrying only one never saved its MIRO Rack products.
            any_loaded = any(
                self.module.STORE.get(name) is not None
                for name in ("miro", "picarro")
            )
        if any_loaded:
            if progress_callback:
                progress_callback(0.94, "Saving MIRO Rack project, please wait")
            with self._project_save_lock:
                saved = self.persist_main_project()
                project_saved = bool(saved.get("saved"))
                checkpoint = getattr(self.dashboard_backend, "_checkpoint_project", None)
                if callable(checkpoint):
                    checkpoint()
        if progress_callback:
            progress_callback(1.0, f"{instrument_name}: main processing preparation complete")
        return {
            "outputs": [
                analysis_path,
                *([map_series_path] if map_series_path is not None else []),
            ],
            "project_saved": project_saved,
            "map_layers": len(layers),
            "map_ready": bool(map_result.get("ready")),
        }

    def process_picarro_from_main(
        self,
        *,
        source_paths: tuple[Path, ...],
        selected_start: Any,
        selected_end: Any,
        output_root: Path,
        progress_callback: Any = None,
    ) -> dict[str, Any]:
        roots = {Path(path).parent.resolve() for path in source_paths}
        if len(roots) != 1:
            raise ValueError("A Picarro candidate must use files from one confirmed folder")
        root = roots.pop()
        if progress_callback:
            progress_callback(3.0, "Picarro: concatenating confirmed .dat files")
        data, metadata = self.module.picarro.load_folder(
            root,
            lambda fraction, message: progress_callback(
                3.0 + 52.0 * float(fraction), message
            ) if progress_callback else None,
        )
        start = self._recorded_filter_value(selected_start)
        end = self._recorded_filter_value(selected_end)
        if progress_callback:
            progress_callback(58.0, "Picarro: original-resolution concentration analysis")
        analysis = self.module.picarro.analyze(data, "CO2 raw", start, end)
        published = self.publish_main_processing_instrument(
            instrument_id="picarro",
            data=data,
            metadata=metadata,
            analysis=analysis,
            source_paths=source_paths,
            selected_start=selected_start,
            selected_end=selected_end,
            output_root=output_root,
            progress_callback=(
                (lambda fraction, message: progress_callback(
                    62.0 + 36.0 * float(fraction), message
                )) if progress_callback else None
            ),
        )
        # What the loader had to say about the delivery, not an empty list. The
        # column flavour it fell back to and any file it skipped both change how
        # the result should be read, so they belong on the instrument card
        # rather than only in the loader's own metadata.
        return {**published, "warnings": list(metadata.get("warnings", ()))}

    def _save_map_series_project(self) -> Path | None:
        with self.dashboard_backend._lock:
            project = self.dashboard_backend._flight_project
        if project is None:
            return None
        path = project.flight_output_root / "quicklooks" / "miro_rack_map_1hz.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        with self._lock:
            payload = {
                "version": 1,
                "resolution": "1 Hz mean; Mapview only",
                "scientific_data_policy": (
                    "All MIRO/Picarro plots and statistics retain original resolution"
                ),
                "layers": self._map_series,
                "units": self._map_units,
            }
        path.write_text(json.dumps(payload, separators=(",", ":")), encoding="utf-8")
        with self.dashboard_backend._lock:
            project.output_locations["miro_rack_map_1hz"] = path
        return path

    def _restore_map_series_project(self) -> bool:
        self._bind_current_project()
        with self.dashboard_backend._lock:
            project = self.dashboard_backend._flight_project
        if project is None:
            return False
        path = project.output_locations.get("miro_rack_map_1hz")
        if not path or not Path(path).is_file():
            return False
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
        with self._lock:
            self._map_series = {
                "MIRO": dict(payload.get("layers", {}).get("MIRO", {})),
                "Picarro": dict(payload.get("layers", {}).get("Picarro", {})),
            }
            self._map_units = {
                "MIRO": dict(payload.get("units", {}).get("MIRO", {})),
                "Picarro": dict(payload.get("units", {}).get("Picarro", {})),
            }
        return any(self._map_series.values())
    def persist_main_project(self) -> dict[str, Any]:
        with self.dashboard_backend._lock:
            project = self.dashboard_backend._flight_project
        if project is None:
            return {"saved": False, "reason": "No main Flight Project is active"}
        with self.module.LOCK:
            # Whichever analyzer this flight carried. Demanding both meant a
            # Picarro-only flight saved no session, so reopening its project
            # showed no Picarro data at all.
            loaded = self.module.STORE.get("meta") is not None and any(
                self.module.STORE.get(name) is not None
                for name in ("miro", "picarro")
            )
            results = self.module.STORE.get("results")
        existing = project.output_locations.get("miro_rack_session")
        if not loaded:
            return {
                "saved": False,
                "reason": "MIRO Rack data are not loaded",
                "path": str(existing) if existing else None,
                "preserved_existing": bool(existing and Path(existing).is_file()),
            }
        path = project.flight_output_root / "quicklooks" / "miro_rack_session.hdf"
        path.parent.mkdir(parents=True, exist_ok=True)
        with self._lock:
            state = dict(self._ui_state)
        with self.module.LOCK:
            prior = self.module.STORE.get("project") or {}
            if not state:
                state = dict(prior.get("state") or {})
        state["results_current"] = results is not None
        state["comparison_current"] = bool(
            isinstance(results, dict) and results.get("comparison")
        )
        try:
            self.module.save_project_worker(str(path), state)
        except Exception as exc:
            self.dashboard_backend.logger.capture_exception(
                "miro-rack-project",
                "MIRO Rack session could not be saved with Flight Project",
                exc,
                instrument="miro",
                processing_step="project-save",
            )
            raise RuntimeError(
                f"Flight Project was not saved because the MIRO Rack snapshot failed: {exc}"
            ) from exc
        with self.dashboard_backend._lock:
            project.output_locations["miro_rack_session"] = path
            for instrument_id in ("miro", "picarro"):
                saved = project.detected_instruments.get(instrument_id)
                if saved is not None and path not in saved.output_locations:
                    saved.output_locations.append(path)
        self.dashboard_backend.logger.log(
            LogLevel.SUCCESS,
            "miro-rack-project",
            "MIRO Rack data, analysis, controls, and logs saved with Flight Project",
            instrument="miro",
            file_path=path,
            processing_step="project-save",
        )
        return {
            "saved": True,
            "path": str(path),
            "results_saved": results is not None,
        }

    def restore_main_project(self) -> dict[str, Any]:
        with self.dashboard_backend._lock:
            project = self.dashboard_backend._flight_project
        if project is None:
            return {"restored": False, "reason": "No main Flight Project is active"}
        path = project.output_locations.get("miro_rack_session")
        if not path:
            return {"restored": False, "reason": "No MIRO Rack session was saved"}
        path = Path(path)
        if not path.is_file():
            message = f"Saved MIRO Rack session is missing: {path}"
            self.dashboard_backend.logger.log(
                LogLevel.WARNING,
                "miro-rack-project",
                message,
                instrument="miro",
                file_path=path,
                processing_step="project-load",
            )
            return {"restored": False, "reason": message, "path": str(path)}
        try:
            self.module.load_project_worker(str(path))
            map_series_restored = self._restore_map_series_project()
            map_payload_restored = self._restore_map_payload_project()
            with self.module.LOCK:
                saved = self.module.STORE.get("project") or {}
                results_available = bool(saved.get("results_available"))
                restored_state = dict(saved.get("state") or {})
            with self._lock:
                self._ui_state = restored_state
                self._map_job = {
                    "running": False,
                    "percent": 0.0,
                    "message": (
                        "Saved Mapview synchronization is ready."
                        if map_payload_restored else
                        "Project restored; Mapview preparation is ready."
                        if map_series_restored else
                        "Project restored; run Mapview synchronization."
                    ),
                    "error": None,
                    "ready": map_payload_restored,
                }
            self.dashboard_backend.logger.log(
                LogLevel.SUCCESS,
                "miro-rack-project",
                "MIRO Rack data, analysis, controls, and logs restored from Flight Project",
                instrument="miro",
                file_path=path,
                processing_step="project-load",
            )
            return {
                "restored": True,
                "path": str(path),
                "results_available": results_available,
            }
        except Exception as exc:
            self.dashboard_backend.logger.capture_exception(
                "miro-rack-project",
                "Saved MIRO Rack session could not be restored",
                exc,
                instrument="miro",
                processing_step="project-load",
            )
            return {"restored": False, "reason": str(exc), "path": str(path)}

    def bootstrap(self) -> dict[str, Any]:
        state = self.dashboard_backend.snapshot()
        instruments = state.get("instruments") or {}
        with self.module.LOCK:
            rack_project = dict(self.module.STORE.get("project") or {})

        def source_directory(instrument_id: str) -> str:
            item = instruments.get(instrument_id) or {}
            values = [Path(value) for value in item.get("candidate_paths") or []]
            if not values:
                return ""
            first = values[0]
            return str(first if first.is_dir() else first.parent)

        return {
            "flight_no": state.get("flight_id") or "",
            "miro_path": source_directory("miro"),
            "picarro_path": source_directory("picarro"),
            "output_path": state.get("selected_output_folder") or "",
            "time_filter": state.get("time_filter") or {},
            "restored_session": bool(rack_project.get("path")),
            "rack_results_available": bool(rack_project.get("results_available")),
            "rack_state": dict(rack_project.get("state") or {}),
        }

    def log_view(self, message: str) -> None:
        self.dashboard_backend.logger.log(
            LogLevel.INFO,
            "miro-rack-browser",
            message,
            instrument="miro",
            processing_step="browser-view",
        )
        self.dashboard_backend._persist_project_logs()

    def start_map_job(self) -> dict[str, Any]:
        with self._lock:
            if self._map_job["running"]:
                raise RuntimeError("Mapview synchronization is already running")
        if self._map_payload is None:
            self._restore_map_payload_project()
        if self._map_payload is not None:
            with self._lock:
                self._map_job = {
                    "running": False,
                    "percent": 100.0,
                    "message": "Saved Mapview synchronization is ready.",
                    "error": None,
                    "ready": True,
                }
            return self.map_progress()
        if not any(self._map_series.values()):
            self._restore_map_series_project()
        with self.module.LOCK:
            has_miro = self.module.STORE.get("miro") is not None
            has_picarro = self.module.STORE.get("picarro") is not None
        if not any(self._map_series.values()) and not (has_miro or has_picarro):
            raise RuntimeError(
                "Process MIRO or Picarro in the main GUI before opening Mapview"
            )
        with self._lock:
            self._map_job = {
                "running": True,
                "percent": 0.0,
                "message": "Reading the saved 1 Hz Mapview preparation...",
                "error": None,
                "ready": False,
            }
            self._map_payload = None
        thread = threading.Thread(
            target=self._map_worker, name="miro-rack-georeference", daemon=True
        )
        thread.start()
        self.log_view("MIRO Rack Mapview synchronization started")
        return self.map_progress()

    def map_progress(self) -> dict[str, Any]:
        with self._lock:
            return dict(self._map_job)

    def map_payload(self) -> dict[str, Any]:
        with self._lock:
            if self._map_payload is None:
                if not self._restore_map_payload_project():
                    raise RuntimeError("Run Mapview synchronization first")
            payload = dict(self._map_payload)
        payload["gases"] = {
            "MIRO": list(MIRO_MAP_GASES),
            "Picarro": list(PICARRO_MAP_GASES),
        }
        layers = {
            instrument: {
                gas: list(records)
                for gas, records in dict(
                    payload.get("layers", {}).get(instrument, {})
                ).items()
            }
            for instrument in ("MIRO", "Picarro")
        }
        units = {
            instrument: dict(payload.get("units", {}).get(instrument, {}))
            for instrument in ("MIRO", "Picarro")
        }
        for saved_gas, records in list(layers["MIRO"].items()):
            canonical = str(saved_gas).split()[0].upper()
            if canonical in MIRO_MAP_GASES and records:
                layers["MIRO"].setdefault(canonical, records)
                units["MIRO"].setdefault(
                    canonical, units["MIRO"].get(saved_gas, MIRO_MAP_UNITS[canonical])
                )
        for gas in MIRO_MAP_GASES:
            layers["MIRO"].setdefault(gas, [])
            units["MIRO"].setdefault(gas, MIRO_MAP_UNITS[gas])
        for gas in PICARRO_MAP_GASES:
            layers["Picarro"].setdefault(gas, [])
            units["Picarro"].setdefault(gas, PICARRO_MAP_UNITS[gas])
        payload["layers"] = layers
        payload["units"] = units
        snapshot_method = getattr(self.dashboard_backend, "snapshot", None)
        snapshot = snapshot_method() if callable(snapshot_method) else {}
        payload["flight_name"] = str(snapshot.get("flight_id") or "Flight")
        payload["selected_timeframe"] = dict(snapshot.get("time_filter") or {})
        if not payload.get("flight_track"):
            try:
                navigation, _ = self._navigation_from_main_project()
                payload["flight_track"] = self._flight_track_records(navigation)
            except (AttributeError, FileNotFoundError, RuntimeError, ValueError):
                payload["flight_track"] = self._track_from_layers(layers)
        return payload

    # -- Trace Gas Investigation -------------------------------------------
    #
    # The MIRO cell warmed 7.7 degrees over Flight_CC0806 and CO2 followed it
    # at 6.2 ppm per degree, which is fourteen times the atmospheric signal and
    # is why that channel disagreed with the Picarro at R2 = 0.05. Establishing
    # that took a day of one-off scripting; these two calls make it a page.

    def _loaded_gas_frames(self) -> tuple[Any, Any]:
        with self.module.LOCK:
            miro_data = self.module.STORE.get("miro")
            picarro_data = self.module.STORE.get("picarro")
        if miro_data is None:
            raise RuntimeError(
                "Process MIRO in the MIRO Rack workspace before opening the "
                "Trace Gas Investigation; it is the instrument whose state the "
                "investigation regresses against."
            )
        return miro_data, picarro_data

    def _investigation_navigation(self):
        """Noseboom altitude, when the flight project has it. Optional."""
        try:
            navigation, _ = self._navigation_from_main_project()
        except (FileNotFoundError, ValueError, RuntimeError, KeyError):
            return None
        return navigation if "altitude" in getattr(navigation, "columns", []) else None

    def trace_gas_investigation(self, request: dict[str, Any]) -> dict[str, Any]:
        from . import trace_gas_investigation as engine

        miro_data, picarro_data = self._loaded_gas_frames()
        filters = engine.parse_filters(request)
        payload = engine.investigate(
            miro_data, picarro_data, self._investigation_navigation(), filters,
            miro_module=self.module.miro, picarro_module=self.module.picarro,
        )
        self.log_view(
            "Trace Gas Investigation evaluated "
            f"{len(payload['species'])} species over {payload['window']['samples']} "
            f"samples at {filters.resolution_seconds} s"
        )
        return payload

    def export_trace_gas_figure(self, request: dict[str, Any]) -> dict[str, Any]:
        from . import trace_gas_export, trace_gas_investigation as engine

        miro_data, picarro_data = self._loaded_gas_frames()
        filters = engine.parse_filters(request)
        payload = engine.investigate(
            miro_data, picarro_data, self._investigation_navigation(), filters,
            miro_module=self.module.miro, picarro_module=self.module.picarro,
        )
        with self.dashboard_backend._lock:
            project = self.dashboard_backend._flight_project
        if project is None:
            raise RuntimeError(
                "Open or restore a Flight Project before exporting, so the "
                "figure is written beside the rest of the flight's products."
            )
        root = Path(project.flight_output_root) / "exports" / "trace_gas"
        flight = getattr(project, "flight_id", None) or "flight"
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        written = trace_gas_export.render(
            payload, root, f"TraceGas_{flight}_{stamp}",
            view=str(request.get("view") or "overview"),
            species=request.get("species"),
            driver=request.get("driver"),
            formats=request.get("formats"),
            dpi=request.get("dpi", 600),
        )
        self.log_view(
            f"Trace Gas Investigation exported {len(written)} figure file(s) to {root}"
        )
        return {
            "files": [path.name for path in written],
            "directory": str(root),
        }

    def export_map_figure(self, request: dict[str, Any]) -> tuple[str, bytes, str]:
        """Draw one map layer as a figure, from the numbers rather than the canvas.

        The old export sent the browser's own canvas to be wrapped in a PDF: one
        raster, no fonts, and whatever size the window happened to be - 17.33
        inches wide on Flight_CC0807, with nothing in it a reader could scale or
        search. This redraws the same layer server-side, so the file carries a
        titled axes in degrees, a colour bar naming the quantity and its unit, a
        scale bar, a north arrow and the basemap attribution, at the campaign's
        seven inches and nine point.
        """
        from core import scientific_map

        payload = self.map_payload()
        instrument = str(request.get("instrument") or "MIRO")
        layers = dict(payload.get("layers", {}).get(instrument, {}))
        gas = str(request.get("gas") or "")
        if gas not in layers:
            gas = next((name for name in layers if layers[name]), "")
        records = list(layers.get(gas, ()))
        if not records:
            raise ValueError(
                f"{instrument} has no georeferenced {gas or 'layer'} to draw. "
                "Run Mapview synchronization first."
            )
        unit = str(payload.get("units", {}).get(instrument, {}).get(gas, "")).strip()
        latitudes = [row.get("lat") for row in records]
        longitudes = [row.get("lon") for row in records]
        values = [row.get("value") for row in records]
        stamps = [str(row.get("time") or "") for row in records if row.get("time")]
        flight = self.dashboard_backend.snapshot().get("flight_id") or "Flight"
        window = ""
        if stamps:
            window = (
                f"{min(stamps).replace('T', ' ')} to "
                f"{max(stamps).replace('T', ' ')} UTC"
            )
        image_format = str(request.get("format") or "pdf").casefold()
        with tempfile.TemporaryDirectory() as directory:
            stem = (
                f"{safe_file_stem(flight)}_{instrument}_{gas}_map_"
                f"{datetime.now().strftime('%Y%m%d_%H%M%S')}"
            )
            written = scientific_map.render_track_map(
                latitudes, longitudes, values, Path(directory), stem,
                title=f"CC-FLUX · {flight} · {instrument} {gas}",
                subtitle=window,
                value_label=f"{gas} ({unit})" if unit else gas,
                colormap=str(request.get("colormap") or "viridis"),
                log_scale=bool(request.get("log_scale", False)),
                formats=(image_format,),
                dpi=int(request.get("dpi") or 300),
                cache_directory=self._basemap_cache(),
            )
            path = written[0]
            body = path.read_bytes()
        self.log_view(
            f"Mapview exported {instrument} {gas} as a {image_format.upper()} figure "
            f"from {len(records)} georeferenced sample(s)"
        )
        return path.name, body, scientific_map.media_type(image_format)

    def _basemap_cache(self) -> Path:
        with self.dashboard_backend._lock:
            project = self.dashboard_backend._flight_project
        root = (
            Path(project.flight_output_root) if project is not None
            else Path(self.root)
        )
        return root / "exports" / "_basemap_tiles"

    def export_map_pdf(self, request: dict[str, Any]) -> tuple[str, bytes]:
        """Convert the browser-composed, current visible map layout to a PDF."""
        data_url = str(request.get("image") or "")
        if not data_url.startswith("data:image/png;base64,"):
            raise ValueError("Map export must contain a PNG image")
        encoded = data_url.split(",", 1)[1]
        if len(encoded) > 64_000_000:
            raise ValueError("Map export image is too large")
        try:
            raw = base64.b64decode(encoded, validate=True)
        except (binascii.Error, ValueError) as exc:
            raise ValueError("Map export image is not valid base64 data") from exc
        try:
            image = Image.open(io.BytesIO(raw))
            image.load()
        except (OSError, UnidentifiedImageError) as exc:
            raise ValueError("Map export image is not a readable PNG") from exc
        if image.format != "PNG":
            raise ValueError("Map export image must use PNG format")
        width, height = image.size
        if width < 800 or height < 500 or width > 10_000 or height > 10_000:
            raise ValueError(
                "Map export image dimensions are outside the supported range"
            )
        if image.mode in {"RGBA", "LA"}:
            background = Image.new("RGB", image.size, "white")
            alpha = image.getchannel("A")
            background.paste(image.convert("RGB"), mask=alpha)
            image = background
        else:
            image = image.convert("RGB")

        snapshot_method = getattr(self.dashboard_backend, "snapshot", None)
        snapshot = snapshot_method() if callable(snapshot_method) else {}
        flight_name = str(
            request.get("flight_name") or snapshot.get("flight_id") or "Flight"
        ).strip()
        timeframe = str(request.get("timeframe") or "").strip()
        safe_name = re.sub(r"[^A-Za-z0-9._-]+", "_", flight_name).strip("._")
        safe_name = safe_name or "Flight"
        stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        filename = f"{safe_name}_MIRO_Rack_Map_{stamp}.pdf"
        buffer = io.BytesIO()
        image.save(
            buffer,
            format="PDF",
            resolution=300.0,
            quality=95,
            title=f"{flight_name} - MIRO Rack Map",
            author="Biplob Dey - Forschungszentrum Jülich GmbH",
            subject=(
                timeframe
                or "MIRO and Picarro georeferenced concentration map"
            ),
        )
        pdf = buffer.getvalue()

        backend_lock = getattr(self.dashboard_backend, "_lock", threading.RLock())
        with backend_lock:
            project = getattr(self.dashboard_backend, "_flight_project", None)
        destination: Path | None = None
        if project is not None:
            export_root = project.flight_output_root / "exports" / "miro_rack_map"
            export_root.mkdir(parents=True, exist_ok=True)
            destination = export_root / filename
            destination.write_bytes(pdf)
            with backend_lock:
                project.output_locations["miro_rack_map_exports"] = export_root
            checkpoint = getattr(self.dashboard_backend, "_checkpoint_project", None)
            if callable(checkpoint):
                checkpoint()
        self.dashboard_backend.logger.log(
            LogLevel.SUCCESS,
            "miro-rack-map-export",
            "High-resolution MIRO Rack map PDF exported",
            instrument="miro",
            file_path=destination,
            processing_step="map-export",
        )
        persist_logs = getattr(self.dashboard_backend, "_persist_project_logs", None)
        if callable(persist_logs):
            persist_logs()
        return filename, pdf

    def _set_map_progress(self, percent: float, message: str) -> None:
        with self._lock:
            self._map_job["percent"] = float(percent)
            self._map_job["message"] = message

    def _prepare_map_series_fallback(self) -> None:
        """Prepare legacy/restored projects once; new processing does this earlier."""
        with self.module.LOCK:
            miro_data = self.module.STORE.get("miro")
            picarro_data = self.module.STORE.get("picarro")
            results = dict(self.module.STORE.get("results") or {})
        parameters = dict(results.get("parameters") or {})
        miro_layers: dict[str, list[dict[str, Any]]] = {}
        miro_units: dict[str, str] = {}
        if miro_data is not None:
            gases = [
                gas for gas in self.module.miro.GAS_COLUMNS
                if gas in miro_data and str(gas).split()[0].upper() in MIRO_MAP_GASES
            ]
            for gas in gases:
                try:
                    analysis = self.module.miro.analyze(
                        miro_data,
                        gas,
                        float(parameters.get("smooth_seconds", 300.0)),
                        parameters.get("miro_start"),
                        parameters.get("miro_end"),
                        30.0,
                    )
                except (KeyError, TypeError, ValueError):
                    continue
                series = analysis["series"]
                map_gas = str(gas).split()[0].upper()
                records = self._one_hz_series(
                    series["time"], series["ambient"]
                )
                # Preserve the legacy key for saved-project compatibility while
                # exposing the canonical compound name used by the Mapview menu.
                miro_layers[gas] = records
                miro_layers[map_gas] = records
                miro_units[gas] = str(analysis["unit"])
                miro_units[map_gas] = str(analysis["unit"])
        picarro_layers: dict[str, list[dict[str, Any]]] = {}
        picarro_units: dict[str, str] = {}
        if picarro_data is not None:
            sliced = self.module.picarro._slice(
                picarro_data,
                parameters.get("picarro_start"),
                parameters.get("picarro_end"),
            )
            for gas, (column, unit) in self.module.picarro.GAS_COLUMNS.items():
                if column not in sliced:
                    continue
                map_gas = str(gas).split()[0].upper()
                picarro_layers[map_gas] = self._one_hz_series(
                    sliced["timestamp"], sliced[column]
                )
                picarro_units[map_gas] = str(unit)
        with self._lock:
            self._map_series = {"MIRO": miro_layers, "Picarro": picarro_layers}
            self._map_units = {"MIRO": miro_units, "Picarro": picarro_units}
            for gas in MIRO_MAP_GASES:
                self._map_series["MIRO"].setdefault(gas, [])
                self._map_units["MIRO"].setdefault(gas, MIRO_MAP_UNITS[gas])
            for gas in PICARRO_MAP_GASES:
                self._map_series["Picarro"].setdefault(gas, [])
                self._map_units["Picarro"].setdefault(gas, PICARRO_MAP_UNITS[gas])
        self._save_map_series_project()

    def _navigation_from_main_project(self) -> tuple[pd.DataFrame, str]:
        """Use the saved Noseboom browser quicklook as the authoritative GPS source."""
        with self.dashboard_backend._lock:
            project = self.dashboard_backend._flight_project
            quicklook_path = (
                project.output_locations.get("noseboom_quicklook")
                if project is not None
                else None
            )
        if quicklook_path and Path(quicklook_path).is_file():
            payload = json.loads(Path(quicklook_path).read_text(encoding="utf-8"))
            points = payload.get("points") or []
            if points:
                return (
                    self._prepare_navigation(pd.DataFrame.from_records(points)),
                    str(Path(quicklook_path)),
                )
        # Older Flight Projects may predate the browser quicklook. Retain the
        # processed export only as a compatibility fallback.
        nav_path = self.dashboard_backend.noseboom_export_file()
        return self._prepare_navigation(self._read_navigation(nav_path)), str(nav_path)

    def _build_map_payload(
        self, progress_callback: Any = None
    ) -> dict[str, Any]:
        started = time.monotonic()
        if progress_callback:
            progress_callback(0.05, "Reading saved Noseboom navigation")
        navigation, navigation_source = self._navigation_from_main_project()
        with self._lock:
            prepared = {
                instrument: {
                    gas: list(records) for gas, records in layers.items()
                }
                for instrument, layers in self._map_series.items()
            }
            units = {
                instrument: dict(values)
                for instrument, values in self._map_units.items()
            }
        layer_count = max(
            1,
            sum(len(instrument_layers) for instrument_layers in prepared.values()),
        )
        completed = 0
        map_layers: dict[str, dict[str, list[dict[str, Any]]]] = {
            "MIRO": {}, "Picarro": {}
        }
        for instrument, layers in prepared.items():
            for gas, records in layers.items():
                if progress_callback:
                    progress_callback(
                        0.10 + 0.78 * completed / layer_count,
                        f"{instrument}: aligning 1 Hz {gas} with Noseboom GPS",
                    )
                if records:
                    frame = pd.DataFrame.from_records(records)
                    map_layers[instrument][gas] = self._instrument_layers(
                        frame, instrument, {gas: "value"}, navigation
                    )[gas]
                else:
                    map_layers[instrument][gas] = []
                completed += 1
        if not any(map_layers["MIRO"].values()) and not any(
            map_layers["Picarro"].values()
        ):
            raise ValueError(
                "No 1 Hz MIRO or Picarro concentration values overlap "
                "the saved Noseboom navigation interval"
            )
        if progress_callback:
            progress_callback(0.92, "Saving the synchronized Mapview product")
        for gas in MIRO_MAP_GASES:
            map_layers["MIRO"].setdefault(gas, [])
            units["MIRO"].setdefault(gas, MIRO_MAP_UNITS[gas])
        for gas in PICARRO_MAP_GASES:
            map_layers["Picarro"].setdefault(gas, [])
            units["Picarro"].setdefault(gas, PICARRO_MAP_UNITS[gas])
        payload = {
            "ready": True,
            "source": navigation_source,
            "value_source": "main-processing 1 Hz Mapview-only concentration copies",
            "scientific_data_policy": (
                "Original-resolution data remain authoritative for time series, "
                "Allan deviation, detrending, correlation, and export"
            ),
            "synchronization": (
                "1 Hz mean aligned to nearest Noseboom timestamp within 1 second"
            ),
            "prepared_during": "main processing",
            "layers": map_layers,
            "gases": {
                "MIRO": list(MIRO_MAP_GASES),
                "Picarro": list(PICARRO_MAP_GASES),
            },
            # One instrument can be absent from a flight, and the other is still
            # worth mapping. Saying which produced layers lets the page offer
            # only those and name the missing one, rather than presenting an
            # instrument whose every layer is empty.
            "available": {
                instrument: bool(any(layers.values()))
                for instrument, layers in map_layers.items()
            },
            "units": units,
            "flight_track": self._flight_track_records(navigation),
            "elapsed_seconds": round(time.monotonic() - started, 3),
        }
        with self._lock:
            self._map_payload = payload
        saved_path = self._save_map_payload_project(payload)
        with self._lock:
            self._map_job = {
                "running": False,
                "percent": 100.0,
                "message": "Saved Mapview product is ready.",
                "error": None,
                "ready": True,
            }
        if progress_callback:
            progress_callback(1.0, "Saved MIRO Rack Mapview product is ready")
        return {
            "ready": True,
            "path": str(saved_path) if saved_path else None,
            "payload": payload,
        }

    def prepare_map_during_main_processing(
        self, progress_callback: Any = None
    ) -> dict[str, Any]:
        """Precompute georeferencing once while the selected jobs are running."""
        self._bind_current_project()
        if not any(self._map_series.values()):
            self._restore_map_series_project()
        if not any(self._map_series.values()):
            return {"ready": False, "reason": "MIRO/Picarro 1 Hz map data are not ready"}
        with self._map_build_lock:
            try:
                result = self._build_map_payload(progress_callback)
            except (FileNotFoundError, RuntimeError, ValueError) as exc:
                # Instrument tasks can finish before Noseboom. A later Noseboom,
                # MIRO, or Picarro completion invokes this method again.
                message = str(exc)
                if "Noseboom" in message or "navigation" in message:
                    return {"ready": False, "reason": message}
                raise
        self.log_view("MIRO Rack Mapview product prepared during main processing")
        return result

    def _map_worker(self) -> None:
        try:
            if not any(self._map_series.values()):
                self._set_map_progress(
                    4, "Preparing a 1 Hz Mapview copy for this older project"
                )
                self._prepare_map_series_fallback()
            with self._map_build_lock:
                self._build_map_payload(
                    lambda fraction, message: self._set_map_progress(
                        8 + 90 * float(fraction), message
                    )
                )
            self.log_view("MIRO Rack Mapview fallback preparation completed")
        except Exception as exc:
            with self._lock:
                self._map_job = {
                    "running": False,
                    "percent": float(self._map_job.get("percent", 0)),
                    "message": "Mapview preparation failed.",
                    "error": str(exc),
                    "ready": False,
                }
            self.dashboard_backend.logger.capture_exception(
                "miro-rack-map",
                "MIRO Rack Mapview preparation failed",
                exc,
                instrument="miro",
                processing_step="georeference",
            )
            self.dashboard_backend._persist_project_logs()
    def _save_map_payload_project(self, payload: dict[str, Any]) -> Path | None:
        with self.dashboard_backend._lock:
            project = self.dashboard_backend._flight_project
        if project is None:
            return None
        path = project.flight_output_root / "quicklooks" / "miro_rack_map.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload, separators=(",", ":")), encoding="utf-8")
        with self.dashboard_backend._lock:
            project.output_locations["miro_rack_map"] = path
        checkpoint = getattr(self.dashboard_backend, "_checkpoint_project", None)
        if callable(checkpoint):
            checkpoint()
        return path

    def _restore_map_payload_project(self) -> bool:
        self._bind_current_project()
        with self.dashboard_backend._lock:
            project = self.dashboard_backend._flight_project
        if project is None:
            return False
        path = project.output_locations.get("miro_rack_map")
        if not path or not Path(path).is_file():
            return False
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
        if not payload.get("ready"):
            return False
        with self._lock:
            self._map_payload = payload
        return True
    @staticmethod
    def _read_navigation(path: Path) -> pd.DataFrame:
        suffix = path.suffix.casefold()
        if suffix in {".h5", ".hdf", ".hdf5"}:
            return pd.read_hdf(path)
        return pd.read_csv(path, sep=None, engine="python")

    @staticmethod
    def _prepare_navigation(frame: pd.DataFrame) -> pd.DataFrame:
        lookup = {str(column).casefold(): column for column in frame.columns}
        time_column = next(
            (
                column
                for column in frame.columns
                if any(token in str(column).casefold() for token in ("time", "date"))
            ),
            None,
        )
        lat_column = next(
            (
                lookup.get(name)
                for name in ("plot_lat", "latitude", "lat", "gnss_lat")
                if lookup.get(name) is not None
            ),
            None,
        )
        lon_column = next(
            (
                lookup.get(name)
                for name in ("plot_lon", "longitude", "lon", "gnss_lon")
                if lookup.get(name) is not None
            ),
            None,
        )
        if time_column is None or lat_column is None or lon_column is None:
            raise ValueError(
                "Processed Noseboom export must contain timestamp, latitude, and longitude"
            )
        # Altitude is optional: the Mapview never needed it, and a delivery
        # without one still georeferences. The Trace Gas Investigation asks for
        # it so a drift can be separated from a vertical gradient, and reports
        # its absence rather than substituting anything.
        altitude_column = next(
            (
                lookup.get(name)
                for name in (
                    "plot_alt", "altitude", "alt", "alt_above_ground_m",
                    "height", "gnss_alt", "altitude_m",
                )
                if lookup.get(name) is not None
            ),
            None,
        )
        columns = {
            "timestamp": _naive_utc(frame[time_column]),
            "lat": pd.to_numeric(frame[lat_column], errors="coerce"),
            "lon": pd.to_numeric(frame[lon_column], errors="coerce"),
        }
        result = pd.DataFrame(columns)
        if altitude_column is not None:
            result["altitude"] = pd.to_numeric(
                frame[altitude_column], errors="coerce"
            )
        result = result.dropna(subset=["timestamp", "lat", "lon"])
        result = result[
            result["lat"].between(-90, 90) & result["lon"].between(-180, 180)
        ]
        if result.empty:
            raise ValueError("Processed Noseboom export contains no valid GPS samples")
        return result.sort_values("timestamp").drop_duplicates("timestamp")

    @staticmethod
    def _flight_track_records(
        navigation: pd.DataFrame, maximum: int = 5000
    ) -> list[dict[str, Any]]:
        step = max(1, math.ceil(len(navigation) / maximum))
        sampled = navigation.iloc[::step]
        return [
            {
                "time": row.timestamp.isoformat(),
                "lat": float(row.lat),
                "lon": float(row.lon),
            }
            for row in sampled.itertuples(index=False)
        ]

    @staticmethod
    def _track_from_layers(
        layers: dict[str, dict[str, list[dict[str, Any]]]]
    ) -> list[dict[str, Any]]:
        candidates = [
            records
            for instrument in ("MIRO", "Picarro")
            for records in layers.get(instrument, {}).values()
            if records
        ]
        if not candidates:
            return []
        longest = max(candidates, key=len)
        return [
            {
                "time": str(point.get("time") or ""),
                "lat": float(point["lat"]),
                "lon": float(point["lon"]),
            }
            for point in longest
            if np.isfinite(point.get("lat")) and np.isfinite(point.get("lon"))
        ]

    @staticmethod
    def _instrument_layers(
        frame: pd.DataFrame,
        instrument: str,
        mapping: dict[str, str],
        navigation: pd.DataFrame,
    ) -> dict[str, list[dict[str, Any]]]:
        timestamp_column = "timestamp" if "timestamp" in frame.columns else "time"
        if timestamp_column not in frame.columns:
            raise ValueError(f"{instrument} data do not contain a timestamp or time column")
        result: dict[str, list[dict[str, Any]]] = {}
        for gas, column in mapping.items():
            values = pd.DataFrame(
                {
                    "timestamp": _naive_utc(frame[timestamp_column]),
                    "value": pd.to_numeric(frame[column], errors="coerce"),
                }
            ).dropna()
            if values.empty:
                result[gas] = []
                continue
            step = max(1, math.ceil(len(values) / 2500))
            values = values.iloc[::step].sort_values("timestamp")
            joined = pd.merge_asof(
                values,
                navigation,
                on="timestamp",
                direction="nearest",
                tolerance=pd.Timedelta(seconds=1),
            ).dropna(subset=["lat", "lon", "value"])
            result[gas] = [
                {
                    "time": row.timestamp.isoformat(),
                    "lat": float(row.lat),
                    "lon": float(row.lon),
                    "value": float(row.value),
                }
                for row in joined.itertuples(index=False)
                if np.isfinite(row.value)
            ]
        return result


def _naive_utc(values: Any) -> pd.Series:
    parsed = pd.to_datetime(values, errors="coerce", utc=True)
    if isinstance(parsed, pd.Series):
        return parsed.dt.tz_localize(None)
    return pd.Series(parsed).dt.tz_localize(None)
