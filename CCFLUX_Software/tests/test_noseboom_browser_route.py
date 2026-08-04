import json
import threading
import urllib.request
from pathlib import Path

from app.scan_backend import DashboardScanBackend
from app.server import create_server
from core.flight_project import FlightProject, FlightProjectStore, InstrumentProjectState
from core.logging_manager import ProcessingLogManager


def _payload():
    return {
        "available": True,
        "points": [
            {"time": "2026-07-27T05:19:52+00:00", "lat": 50.90, "lon": 6.40, "wind_mps": 4.0, "heading_deg": 90.0, "altitude_m": 320.0, "straight": False, "straight_leg_id": 0},
            {"time": "2026-07-27T05:19:53+00:00", "lat": 50.91, "lon": 6.42, "wind_mps": 6.0, "heading_deg": 91.0, "altitude_m": 325.0, "straight": True, "straight_leg_id": 1},
            {"time": "2026-07-27T05:19:54+00:00", "lat": 50.92, "lon": 6.44, "wind_mps": 8.0, "heading_deg": 92.0, "altitude_m": 330.0, "straight": True, "straight_leg_id": 1},
            {"time": "2026-07-27T05:19:55+00:00", "lat": 50.93, "lon": 6.46, "wind_mps": 7.0, "heading_deg": 93.0, "altitude_m": 326.0, "straight": False, "straight_leg_id": 0},
        ],
        "sample_interval_seconds": 1,
        "source": "unchanged legacy Noseboom processor",
        "hist": {"wind_mps": [4.0, 6.0, 8.0, 7.0]},
        "frequency": [{"time": "2026-07-27T05:19:52+00:00", "frequency_hz": 10.0, "sample_count": 10}],
        "spectra": {"wind_mps": {"label": "Wind speed", "column": "wind_mps", "frequency_hz": [0.1, 0.2], "psd": [2.0, 1.0]}},
        "straight_settings": {"minimum_ground_speed_mps": 10.0, "minimum_segment_duration_s": 90.0, "maximum_roll_angle_deg": 12.0},
    }


def _saved_project(tmp_path: Path):
    raw = tmp_path / "raw"
    output = tmp_path / "output"
    raw.mkdir(); output.mkdir()
    quicklook = output / "flight" / "quicklooks" / "noseboom_browser.json"
    quicklook.parent.mkdir(parents=True)
    quicklook.write_text(json.dumps(_payload()), encoding="utf-8")
    export = output / "flight" / "processed" / "noseboom" / "flight.csv"
    export.parent.mkdir(parents=True)
    export.write_text("time,wind\n0,4\n", encoding="utf-8")
    project = FlightProject(
        flight_id="flight",
        flight_folder_path=raw,
        output_folder_path=output,
        detected_instruments={
            "noseboom": InstrumentProjectState(
                instrument_id="noseboom",
                output_locations=[export, quicklook],
            )
        },
        completed_jobs=["noseboom"],
        output_locations={"noseboom_quicklook": quicklook},
    )
    project_file = FlightProjectStore().save_project(project, overwrite=True)
    return project_file, export, quicklook


def test_noseboom_assets_are_integrated_readable_and_new_tab_is_explicit_only():
    root = Path(__file__).resolve().parents[1]
    html = (root / "app/assets/noseboom.html").read_text(encoding="utf-8")
    script = (root / "app/assets/noseboom.js").read_text(encoding="utf-8")
    dashboard = (root / "app/assets/dashboard.js").read_text(encoding="utf-8")
    dashboard_html = (root / "app/assets/dashboard.html").read_text(encoding="utf-8")

    assert "CC-FLUX Campaign 2026" in html
    # The standalone subpages embed the shared airship mark directly; the
    # iframe treatment survives only in the injected MIRO Rack header.
    assert 'src="/campaign-main-airship.svg"' in html
    assert "let browserPoints = [];" in script
    assert "clamp(16px" in html
    assert "Map buffer [m]" in html
    assert "Map colours" in html
    assert "View full screen" in html
    assert "More view options" not in html
    assert 'id="statsFullscreenBtn"' not in html
    assert 'id="downloadModal"' in html
    assert "Download CSV" in html
    assert "Save settings and Proceed" in script
    assert "Reset settings" in script
    assert "Frequency distribution curve" in script
    assert "DTM display floor" in script
    assert "Histogram" in html
    assert "Frequency" in html
    assert "Altitude profile" in html
    assert "Wind spectra" in html
    assert 'id="statisticsExportBtn"' in html
    assert 'name="exportFormat" value="pdf"' in html
    assert 'name="exportFormat" value="svg"' in html
    assert 'name="exportFormat" value="png"' in html
    assert 'id="exportDpi"' in html
    assert "grid-template-columns:repeat(2,minmax(0,1fr))" in html
    assert "Load data" not in html
    assert "unpkg.com/leaflet" not in html
    assert 'href="/noseboom" target="_blank" rel="noopener"' in dashboard_html
    assert "link.href = '/noseboom'" in dashboard
    assert "link.target = '_blank'" in dashboard
    assert "L.tileLayer" in script
    assert "deriveStraightLegs" in script
    assert "windRoseSvg" in script
    assert "Plotly.react" in script
    assert "#d62828" in script
    assert "altitude_profile" in script
    assert "-5/3" in script
    assert "showBusy" in script
    assert "window.history.pushState" in script
    assert "viewFromPath" in script
    assert "window.open(viewMenuContext.path, '_blank', 'noopener')" in script
    assert "map.setZoom(Math.min(19" not in script
    assert "map.setZoom(Math.max(1" not in script
    for route in ("/noseboom/flight-route", "/noseboom/wind-speed", "/noseboom/straight-flight", "/noseboom/statistics/histogram"):
        assert f'href="{route}"' in html
        assert f'href="{route}" target="_blank"' not in html


def test_saved_project_rehydrates_noseboom_settings_and_mirrors_browser_logs(tmp_path: Path):
    project_file, export, quicklook = _saved_project(tmp_path)
    logger = ProcessingLogManager(tmp_path / "application-log.jsonl")
    backend = DashboardScanBackend(Path(__file__).resolve().parents[1], logger=logger)

    result = backend.open_project(project_file)
    view = backend.noseboom_view()
    settings = backend.update_noseboom_straight_settings({"minimum_ground_speed_mps": 11.0})
    backend.log_noseboom_view_event("buffer changed to 700 m")

    assert not result["cancelled"]
    assert view["ready"]
    assert len(view["data"]["points"]) == 4
    assert settings["minimum_ground_speed_mps"] == 11.0
    assert json.loads(quicklook.read_text(encoding="utf-8"))["straight_settings"]["minimum_ground_speed_mps"] == 11.0
    assert backend.noseboom_export_file() == export
    project_log = tmp_path / "output" / "flight" / "logs" / "processing.jsonl"
    assert project_log.is_file()
    assert "buffer changed to 700 m" in project_log.read_text(encoding="utf-8")


def test_http_noseboom_routes_api_export_logo_and_settings(tmp_path: Path):
    project_file, export, quicklook = _saved_project(tmp_path)
    backend = DashboardScanBackend(
        Path(__file__).resolve().parents[1],
        logger=ProcessingLogManager(tmp_path / "application-log.jsonl"),
    )
    backend.open_project(project_file)
    server = create_server(port=0, application_root=Path(__file__).resolve().parents[1], backend=backend)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_port}"
    try:
        direct_routes = [
            "/noseboom", "/noseboom/flight-route", "/noseboom/wind-speed",
            "/noseboom/straight-flight", "/noseboom/statistics/histogram",
            "/noseboom/statistics/frequency", "/noseboom/statistics/altitude-profile",
            "/noseboom/statistics/wind-spectra",
        ]
        html_by_route = {}
        for route in direct_routes:
            with urllib.request.urlopen(base + route) as response:
                html_by_route[route] = response.read().decode("utf-8")
        html = html_by_route["/noseboom"]
        with urllib.request.urlopen(base + "/noseboom.js") as response:
            javascript = response.read().decode("utf-8")
        with urllib.request.urlopen(base + "/api/noseboom") as response:
            state = json.load(response)
        with urllib.request.urlopen(base + "/api/noseboom/export") as response:
            downloaded = response.read()
            disposition = response.headers["Content-Disposition"]
        request = urllib.request.Request(
            base + "/api/noseboom/straight-settings",
            data=json.dumps({"settings": {"minimum_ground_speed_mps": 12.0}}).encode("utf-8"),
            headers={"Content-Type": "application/json", "Origin": base},
            method="POST",
        )
        with urllib.request.urlopen(request) as response:
            saved_settings = json.load(response)
        with urllib.request.urlopen(base + "/campaign-logo.html") as response:
            logo_html = response.read()
        with urllib.request.urlopen(base + "/campaign-logo.svg") as response:
            logo = response.read()
        with urllib.request.urlopen(base + "/campaign-airship.svg") as response:
            airship_logo = response.read()

        assert "Interactive Noseboom flight map" in html
        assert all(content == html for content in html_by_route.values())
        assert "Leaflet 1.9.4" in javascript
        assert "deriveStraightLegs" in javascript
        assert state["ready"]
        assert state["data"]["points"][1]["straight"] is True
        assert state["data"]["hist"]["wind_mps"]
        assert saved_settings["saved"]
        assert saved_settings["settings"]["minimum_ground_speed_mps"] == 12.0
        assert json.loads(quicklook.read_text(encoding="utf-8"))["straight_settings"]["minimum_ground_speed_mps"] == 12.0
        assert b'class="ccflux-logo"' in logo_html
        assert b"<iframe" not in logo_html
        assert logo.startswith(b"<svg")
        assert airship_logo.startswith(b"<svg")
        assert b"CC-FLUX 2026" not in airship_logo
        assert downloaded == export.read_bytes()
        assert "attachment" in disposition
    finally:
        server.shutdown(); server.server_close(); thread.join(timeout=2)