from __future__ import annotations

import http.client
import json
import threading
from pathlib import Path

import pandas as pd

from app.scan_backend import DashboardScanBackend
from app.server import create_server
from core.flight_project import FlightProject, InstrumentProjectState


def _request(server, method: str, path: str, body: dict | None = None):
    connection = http.client.HTTPConnection(
        "127.0.0.1", server.server_port, timeout=20
    )
    payload = json.dumps(body or {}).encode("utf-8") if method == "POST" else None
    # A same-origin POST from the dashboard carries Origin; the server refuses
    # one that does not, so the harness has to behave like the browser.
    headers = (
        {
            "Content-Type": "application/json",
            "Origin": f"http://127.0.0.1:{server.server_port}",
        }
        if payload is not None
        else {}
    )
    connection.request(method, path, body=payload, headers=headers)
    response = connection.getresponse()
    data = json.loads(response.read().decode("utf-8"))
    connection.close()
    assert response.status < 400, data
    return data


def _start(server):
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return thread


def test_http_main_save_restart_and_open_restores_noseboom_and_miro_rack(
    tmp_path: Path,
):
    raw = tmp_path / "raw"
    output = tmp_path / "output"
    raw.mkdir()
    output.mkdir()
    miro_source = raw / "miro.txt"
    picarro_source = raw / "picarro.dat"
    noseboom_source = raw / "noseboom.tdms"
    for source in (miro_source, picarro_source, noseboom_source):
        source.write_text("source", encoding="utf-8")
    project = FlightProject(
        flight_id="http-roundtrip",
        flight_folder_path=raw,
        output_folder_path=output,
        detected_instruments={
            "noseboom": InstrumentProjectState(
                "noseboom", selected_source_files=[noseboom_source]
            ),
            "miro": InstrumentProjectState(
                "miro", selected_source_files=[miro_source]
            ),
            "picarro": InstrumentProjectState(
                "picarro", selected_source_files=[picarro_source]
            ),
        },
        completed_jobs=["noseboom", "miro", "picarro"],
    )
    quicklook = project.flight_output_root / "quicklooks" / "noseboom_browser.json"
    noseboom_export = (
        project.flight_output_root / "processed" / "noseboom" / "noseboom.csv"
    )
    quicklook.parent.mkdir(parents=True, exist_ok=True)
    noseboom_export.parent.mkdir(parents=True, exist_ok=True)
    noseboom_payload = {
        "available": True,
        "points": [{"lat": 50.9, "lon": 6.3, "wind_mps": 3.4}],
        "sample_interval_seconds": 1,
    }
    quicklook.write_text(json.dumps(noseboom_payload), encoding="utf-8")
    noseboom_export.write_text(
        "timestamp,plot_lat,plot_lon\n2026-07-20T10:00:00Z,50.9,6.3\n",
        encoding="utf-8",
    )
    project.output_locations["noseboom_quicklook"] = quicklook
    project.detected_instruments["noseboom"].output_locations = [
        noseboom_export,
        quicklook,
    ]

    backend = DashboardScanBackend(Path(__file__).resolve().parents[1])
    backend._selected_folder = raw
    backend._selected_output_folder = output
    backend._flight_project = project
    backend._instruments["noseboom"].quicklook = noseboom_payload
    backend._instruments["noseboom"].output_files = [str(noseboom_export)]
    server = create_server(port=0, backend=backend)
    timestamps = pd.date_range("2026-07-20 10:00:00", periods=2, freq="s")
    with server.miro_rack.module.LOCK:
        server.miro_rack.module.STORE.update(
            {
                "miro": pd.DataFrame(
                    {"timestamp": timestamps, "CO2 wet": [410.0, 411.0]}
                ),
                "picarro": pd.DataFrame(
                    {"timestamp": timestamps, "CO2_sync": [409.8, 410.8]}
                ),
                "meta": {
                    "paths": {"miro": str(raw), "picarro": str(raw)},
                    "miro": {
                        "rows": 2,
                        "start": timestamps[0].isoformat(),
                        "end": timestamps[-1].isoformat(),
                    },
                    "picarro": {
                        "rows": 2,
                        "start": timestamps[0].isoformat(),
                        "end": timestamps[-1].isoformat(),
                    },
                },
                "results": {"comparison": {"CO2": {"count": 2}}},
            }
        )
    server.miro_rack.update_ui_state(
        {"miro_gas": "CO2 wet", "picarro_gas": "CO2 raw"}
    )
    _start(server)
    try:
        save_result = _request(server, "POST", "/api/project/save")
        project_file = Path(save_result["project_file"])
        assert save_result["miro_rack"]["saved"] is True
    finally:
        server.shutdown()
        server.server_close()

    restored_backend = DashboardScanBackend(Path(__file__).resolve().parents[1])
    restored_server = create_server(port=0, backend=restored_backend)
    _start(restored_server)
    try:
        discovered = _request(
            restored_server,
            "POST",
            "/api/project/discover",
            {"folder": str(output)},
        )
        opened = _request(
            restored_server,
            "POST",
            "/api/project/open",
            {"project_file": discovered["projects"][0]["project_file"]},
        )
        meta = _request(restored_server, "GET", "/api/miro-rack/meta")
        noseboom = _request(restored_server, "GET", "/api/noseboom")
        scan = _request(restored_server, "GET", "/api/scan")
    finally:
        restored_server.shutdown()
        restored_server.server_close()

    assert discovered["valid_count"] == 1
    assert discovered["projects"][0]["flight_id"] == "http-roundtrip"
    assert Path(discovered["projects"][0]["project_file"]) == project_file.resolve()
    assert scan["phase"] == "complete"
    assert scan["processing_queue"]["workflow"]["scan_ready"] is True
    assert scan["processing_queue"]["can_start"] is False
    assert opened["miro_rack"]["restored"] is True
    assert opened["miro_rack"]["results_available"] is True
    assert meta["miro"]["rows"] == 2
    assert meta["picarro"]["rows"] == 2
    assert noseboom["ready"] is True
    assert noseboom["data"] == noseboom_payload
    assert scan["instruments"]["miro"]["candidate_paths"] == [str(miro_source)]
    assert scan["instruments"]["picarro"]["candidate_paths"] == [str(picarro_source)]
