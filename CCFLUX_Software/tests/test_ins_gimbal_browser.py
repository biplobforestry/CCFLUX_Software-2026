import json
import threading
import urllib.request
from pathlib import Path

from app.scan_backend import DashboardScanBackend
from app.server import create_server
from core.flight_project import FlightProject, FlightProjectStore, InstrumentProjectState
from core.logging_manager import ProcessingLogManager


ROOT = Path(__file__).resolve().parents[1]
ASSETS = ROOT / "app" / "assets"


def _browser_payload():
    return {
        "schema": "ccflux-ins-gimbal-browser-v1",
        "available": True,
        "flight_id": "flight",
        "instrument_id": "ins_gimbal",
        "time_basis": "UTC as recorded by the CC-FLUX campaign instrument",
        "summary": {"dataset": {"rows_evaluated": 2}, "sampling": {}, "metrics": {}, "configuration": {}, "limitations": []},
        "sessions": [],
        "series": {"time": [], "session": [], "acc_norm_g": []},
        "spectrogram": {"sessions": [], "color_limits_db": [None, None]},
        "asd": {"acceleration": {"frequency_hz": [], "amplitude_g_sqrt_hz": []}, "angular_rate": {"frequency_hz": [], "amplitude_dps_sqrt_hz": []}},
    }


def test_ins_gimbal_assets_and_partector_axis_contract():
    html = (ASSETS / "ins_gimbal.html").read_text(encoding="utf-8")
    script = (ASSETS / "ins_gimbal.js").read_text(encoding="utf-8")
    dashboard = (ASSETS / "dashboard.html").read_text(encoding="utf-8")
    dashboard_script = (ASSETS / "dashboard.js").read_text(encoding="utf-8")
    partector = (ASSETS / "partector.js").read_text(encoding="utf-8")

    # The standalone subpages embed the shared airship mark directly; the
    # iframe treatment survives only in the injected MIRO Rack header.
    assert 'src="/campaign-main-airship.svg"' in html
    assert "CC-FLUX Campaign 2026" in html
    assert "INS Gimbal" in html
    for plot_id in ("accelerationPlot", "angularRatePlot", "accelerationMotionPlot", "angularMotionPlot", "spectrogramPlot", "asdPlot"):
        assert f'id="{plot_id}"' in html
        assert plot_id in script
    assert "responsive:true" in script
    assert "full_flight" not in script
    assert "Duration-weighted spectra use every acquisition session" in script
    assert 'href="/ins_gimbal/overview" target="_blank"' in dashboard
    assert "'ins_gimbal'" in dashboard_script
    assert "exponentformat:'none'" in partector
    assert "separatethousands:true" in partector
    assert "colour indicates measured value" in partector
    assert "colorscale:colorScale" in partector
    assert "tickvals:diameters" in partector


def test_saved_project_rehydrates_ins_gimbal_browser_payload(tmp_path: Path):
    raw = tmp_path / "raw"
    output = tmp_path / "output"
    raw.mkdir(); output.mkdir()
    quicklook = output / "flight" / "quicklooks" / "ins_gimbal_browser.json"
    quicklook.parent.mkdir(parents=True)
    quicklook.write_text(json.dumps(_browser_payload()), encoding="utf-8")
    project = FlightProject(
        flight_id="flight",
        flight_folder_path=raw,
        output_folder_path=output,
        detected_instruments={"ins_gimbal": InstrumentProjectState(instrument_id="ins_gimbal", output_locations=[quicklook])},
        completed_jobs=["ins_gimbal"],
        output_locations={"ins_gimbal_browser": quicklook},
    )
    project_file = FlightProjectStore().save_project(project, overwrite=True)
    backend = DashboardScanBackend(ROOT, logger=ProcessingLogManager(tmp_path / "application-log.jsonl"))

    result = backend.open_project(project_file)
    view = backend.hatchbox_view("ins_gimbal")

    assert not result["cancelled"]
    assert view["ready"]
    assert view["flight_id"] == "flight"
    assert view["data"]["schema"] == "ccflux-ins-gimbal-browser-v1"


def test_http_ins_gimbal_routes_and_api(tmp_path: Path):
    backend = DashboardScanBackend(ROOT, logger=ProcessingLogManager(tmp_path / "application-log.jsonl"))
    server = create_server(port=0, application_root=ROOT, backend=backend)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_port}"
    try:
        pages = []
        for route in ("/ins_gimbal", "/ins_gimbal/overview", "/ins_gimbal/motion", "/ins_gimbal/frequency", "/ins_gimbal/quality"):
            with urllib.request.urlopen(base + route) as response:
                pages.append(response.read().decode("utf-8"))
        with urllib.request.urlopen(base + "/ins_gimbal.js") as response:
            javascript = response.read().decode("utf-8")
        with urllib.request.urlopen(base + "/api/ins-gimbal") as response:
            state = json.load(response)

        assert all(page == pages[0] for page in pages)
        assert "INS Gimbal" in pages[0]
        assert "Plotly.react('spectrogramPlot'" in javascript
        assert state["ready"] is False
        assert "Process INS Gimbal from the Main GUI first" in state["message"]
    finally:
        server.shutdown(); server.server_close(); thread.join(timeout=2)