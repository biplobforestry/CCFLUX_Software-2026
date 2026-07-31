from __future__ import annotations

import json
import threading
import time
from pathlib import Path

import pandas as pd

from app.miro_rack_bridge import MiroRackBridge
from app.scan_backend import DashboardScanBackend
from core.flight_project import (
    FlightProject,
    FlightProjectStore,
    InstrumentProjectState,
)


class _Logger:
    def __init__(self) -> None:
        self.messages: list[str] = []

    def log(self, _level, _component, message, **_kwargs) -> None:
        self.messages.append(str(message))

    def capture_exception(self, _component, message, _error, **_kwargs) -> None:
        self.messages.append(str(message))


class _BridgeBackend:
    def __init__(self, project: FlightProject) -> None:
        self._lock = threading.RLock()
        self._flight_project = project
        self.logger = _Logger()

    def _persist_project_logs(self):
        return None

    def snapshot(self):
        return {
            "flight_id": self._flight_project.flight_id,
            "selected_output_folder": str(self._flight_project.output_folder_path),
            "instruments": {},
            "time_filter": {},
        }


def _project(tmp_path: Path) -> FlightProject:
    raw = tmp_path / "raw"
    output = tmp_path / "output"
    raw.mkdir()
    output.mkdir()
    return FlightProject(
        flight_id="save-load-flight",
        flight_folder_path=raw,
        output_folder_path=output,
        detected_instruments={
            "miro": InstrumentProjectState("miro"),
            "picarro": InstrumentProjectState("picarro"),
        },
    )


def test_main_project_snapshot_restores_miro_picarro_data_results_state_and_logs(
    tmp_path: Path,
):
    project = _project(tmp_path)
    backend = _BridgeBackend(project)
    bridge = MiroRackBridge(tmp_path, backend)
    timestamps = pd.date_range("2026-07-20 10:00:00", periods=3, freq="s")
    miro = pd.DataFrame({"timestamp": timestamps, "CO2 wet": [410.0, 411.0, 412.0]})
    picarro = pd.DataFrame({"timestamp": timestamps, "CO2_sync": [409.5, 410.5, 411.5]})
    meta = {
        "paths": {"miro": "miro-source", "picarro": "picarro-source"},
        "miro": {"rows": 3, "start": timestamps[0].isoformat(), "end": timestamps[-1].isoformat()},
        "picarro": {"rows": 3, "start": timestamps[0].isoformat(), "end": timestamps[-1].isoformat()},
    }
    results = {
        "miro": {"gas": "CO2 wet", "series": {"time": [value.isoformat() for value in timestamps]}},
        "picarro": {"gas": "CO2 raw", "series": {"time": [value.isoformat() for value in timestamps]}},
        "comparison": {"CO2": {"count": 3}},
    }
    with bridge.module.LOCK:
        bridge.module.STORE.update(
            {"miro": miro, "picarro": picarro, "meta": meta, "results": results}
        )
        bridge.module.LOGS[:] = [
            {
                "timestamp_utc": "2026-07-20T10:00:00Z",
                "level": "INFO",
                "context": "test",
                "message": "saved rack log",
                "details": "",
            }
        ]
    bridge.update_ui_state(
        {
            "flight_no": "Flight Save Load",
            "miro_gas": "CO2 wet",
            "picarro_gas": "CO2 raw",
            "smooth_seconds": 300,
            "filters": {"miro_start": "07-20-2026 10:00"},
        }
    )

    saved = bridge.persist_main_project()
    project_file = FlightProjectStore().save_project(project, overwrite=True)

    assert saved["saved"] is True
    snapshot = Path(saved["path"])
    assert snapshot.is_file()
    assert project.output_locations["miro_rack_session"] == snapshot
    assert snapshot in project.detected_instruments["miro"].output_locations
    assert snapshot in project.detected_instruments["picarro"].output_locations

    reopened = FlightProjectStore().open_project(project_file).project
    restored_backend = _BridgeBackend(reopened)
    restored_bridge = MiroRackBridge(tmp_path, restored_backend)
    restored = restored_bridge.restore_main_project()

    assert restored["restored"] is True
    assert restored["results_available"] is True
    assert restored_bridge.bootstrap()["restored_session"] is True
    with restored_bridge.module.LOCK:
        assert restored_bridge.module.STORE["miro"]["CO2 wet"].tolist() == [410.0, 411.0, 412.0]
        assert restored_bridge.module.STORE["picarro"]["CO2_sync"].tolist() == [409.5, 410.5, 411.5]
        assert restored_bridge.module.STORE["results"]["comparison"]["CO2"]["count"] == 3
        state = restored_bridge.module.STORE["project"]["state"]
        assert state["miro_gas"] == "CO2 wet"
        assert state["picarro_gas"] == "CO2 raw"
        assert any(entry["message"] == "saved rack log" for entry in restored_bridge.module.LOGS)

    navigation = tmp_path / "restored-noseboom.csv"
    pd.DataFrame(
        {
            "timestamp": timestamps,
            "plot_lat": [50.8, 50.81, 50.82],
            "plot_lon": [6.2, 6.21, 6.22],
        }
    ).to_csv(navigation, index=False)
    restored_backend.noseboom_export_file = lambda: navigation
    restored_bridge.start_map_job()
    deadline = time.monotonic() + 5
    while restored_bridge.map_progress()["running"] and time.monotonic() < deadline:
        time.sleep(0.02)
    assert restored_bridge.map_progress()["ready"] is True, restored_bridge.map_progress()
    assert restored_bridge.map_payload()["layers"]["MIRO"]["CO2 wet"]
    assert restored_bridge.map_payload()["layers"]["Picarro"]["CO2"]


def test_noseboom_browser_payload_and_export_restore_from_main_project(
    tmp_path: Path,
):
    raw = tmp_path / "raw"
    output = tmp_path / "output"
    raw.mkdir()
    output.mkdir()
    project = FlightProject(
        flight_id="noseboom-save-load",
        flight_folder_path=raw,
        output_folder_path=output,
        detected_instruments={"noseboom": InstrumentProjectState("noseboom")},
        completed_jobs=["noseboom"],
    )
    quicklook = project.flight_output_root / "quicklooks" / "noseboom_browser.json"
    export = project.flight_output_root / "processed" / "noseboom" / "noseboom.csv"
    quicklook.parent.mkdir(parents=True, exist_ok=True)
    export.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "available": True,
        "points": [{"lat": 50.9, "lon": 6.3, "wind_mps": 4.2, "straight": True}],
        "sample_interval_seconds": 1,
    }
    quicklook.write_text(json.dumps(payload), encoding="utf-8")
    export.write_text("timestamp,plot_lat,plot_lon\n2026-07-20T10:00:00Z,50.9,6.3\n", encoding="utf-8")
    project.output_locations["noseboom_quicklook"] = quicklook
    project.detected_instruments["noseboom"].output_locations = [export, quicklook]
    project_file = FlightProjectStore().save_project(project, overwrite=True)

    backend = DashboardScanBackend(Path(__file__).resolve().parents[1])
    opened = backend.open_project(project_file)
    restored = backend.noseboom_view()

    assert opened["cancelled"] is False
    assert restored["ready"] is True
    assert restored["data"] == payload
    assert Path(backend.noseboom_export_file()) == export
    assert restored["processing_status"] == "complete"
