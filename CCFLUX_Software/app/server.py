"""Local-only HTTP bridge between the preserved dashboard and Python backend."""

from __future__ import annotations

import argparse
import json
import mimetypes
import os
import shutil
import threading
import webbrowser
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

from core.logging_manager import LogLevel
from core.version import SOFTWARE_VERSION, build_fingerprint

from .miro_rack_bridge import MiroRackBridge
from .scan_backend import DashboardScanBackend


class DashboardHTTPServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(
        self,
        address: tuple[str, int],
        backend: DashboardScanBackend,
        dashboard_file: Path,
    ) -> None:
        self.backend = backend
        self.dashboard_file = dashboard_file
        self.miro_rack = MiroRackBridge(dashboard_file.parents[1], backend)
        self.backend.attach_miro_rack_bridge(self.miro_rack)
        super().__init__(address, DashboardRequestHandler)

    def server_close(self) -> None:
        self.backend.shutdown()
        super().server_close()


class DashboardRequestHandler(BaseHTTPRequestHandler):
    server: DashboardHTTPServer

    def do_GET(self) -> None:  # noqa: N802
        try:
            self._route_get()
        except ConnectionError:
            # The browser closed the tab or navigated away mid-response; there
            # is no socket left to report an error on.
            return
        except FileNotFoundError as exc:
            self._send_json(HTTPStatus.NOT_FOUND, {"error": str(exc)})
        except (KeyError, TypeError, ValueError, RuntimeError) as exc:
            # Routes such as the Noseboom export raise ValueError as their
            # normal "not processed yet" signal; the operator must see the
            # message rather than a dropped connection.
            self._send_json(HTTPStatus.BAD_REQUEST, {"error": str(exc)})
        except Exception:
            self._send_json(
                HTTPStatus.INTERNAL_SERVER_ERROR,
                {"error": "The local application could not complete the request"},
            )

    def _route_get(self) -> None:
        parsed = urlparse(self.path)
        path = parsed.path
        if path in {"/", "/index.html"}:
            self._send_file(self.server.dashboard_file)
        elif path == "/dashboard.js":
            self._send_file(self.server.dashboard_file.with_name("dashboard.js"))
        elif path in {"/data_products.txt", "/software_update.txt"}:
            self._send_file(
                self.server.dashboard_file.with_name(path.removeprefix("/"))
            )
        elif path in {"/License.txt", "/manual.text"}:
            self._send_file(
                self.server.dashboard_file.parents[2] / path.removeprefix("/")
            )
        elif path == "/noseboom" or path.startswith("/noseboom/"):
            self.server.backend.log_noseboom_view_event(
                f"Noseboom browser view opened: {path}"
            )
            self._send_file(self.server.dashboard_file.with_name("noseboom.html"))
        elif path == "/noseboom.js":
            self._send_javascript_bundle(
                self.server.dashboard_file.parent / "vendor" / "leaflet" / "leaflet.js",
                self.server.dashboard_file.with_name("noseboom.js"),
            )
        elif path == "/gopro" or path.startswith("/gopro/"):
            self.server.backend.log_gopro_view_event(
                f"GoPro capture map opened: {path}"
            )
            self._send_file(self.server.dashboard_file.with_name("gopro.html"))
        elif path == "/gopro.js":
            self._send_javascript_bundle(
                self.server.dashboard_file.parent / "vendor" / "leaflet" / "leaflet.js",
                self.server.dashboard_file.with_name("gopro.js"),
            )
        elif path == "/flir" or path.startswith("/flir/"):
            self.server.backend.log_flir_view_event(
                f"FLIR temperature workspace opened: {path}"
            )
            self._send_file(self.server.dashboard_file.with_name("flir.html"))
        elif path == "/flir.js":
            self._send_javascript_bundle(
                self.server.dashboard_file.parent / "vendor" / "leaflet" / "leaflet.js",
                self.server.dashboard_file.with_name("flir.js"),
            )
        elif path == "/opc" or path.startswith("/opc/"):
            self.server.backend.log_hatchbox_view_event(
                "opc", f"Combined OPC browser view opened: {path}"
            )
            self._send_file(self.server.dashboard_file.with_name("opc.html"))
        elif path == "/partector" or path.startswith("/partector/"):
            self.server.backend.log_hatchbox_view_event(
                "partector", f"Partector Pro browser view opened: {path}"
            )
            self._send_file(self.server.dashboard_file.with_name("partector.html"))
        elif path == "/ins_gimbal" or path.startswith("/ins_gimbal/"):
            self.server.backend.log_hatchbox_view_event(
                "ins_gimbal", f"INS Gimbal browser view opened: {path}"
            )
            self._send_file(self.server.dashboard_file.with_name("ins_gimbal.html"))
        elif path == "/micasense" or path.startswith("/micasense/"):
            self.server.backend.log_hatchbox_view_event(
                "micasense", f"MicaSense browser view opened: {path}"
            )
            self._send_file(self.server.dashboard_file.with_name("micasense.html"))
        elif path == "/micasense.js":
            self._send_file(self.server.dashboard_file.with_name("micasense.js"))
        elif path == "/sif" or path.startswith("/sif/"):
            self.server.backend.log_hatchbox_view_event(
                "sif", f"SIF / FLOX browser view opened: {path}"
            )
            self._send_file(self.server.dashboard_file.with_name("sif.html"))
        elif path == "/sif.js":
            self._send_javascript_bundle(
                self.server.dashboard_file.parent / "vendor" / "leaflet" / "leaflet.js",
                self.server.dashboard_file.with_name("sif.js"),
            )
        elif path in {"/opc.js", "/partector.js"}:
            self._send_javascript_bundle(
                self.server.dashboard_file.parent / "vendor" / "leaflet" / "leaflet.js",
                self.server.dashboard_file.with_name("size_map.js"),
                self.server.dashboard_file.with_name(path.removeprefix("/")),
            )
        elif path in {"/ins_gimbal.js", "/hatchbox_science.css"}:
            self._send_file(self.server.dashboard_file.with_name(path.removeprefix("/")))
        elif path == "/miro_rack":
            self.server.miro_rack.log_view(
                "MIRO Rack browser workspace opened from the main GUI"
            )
            self._send_bytes(
                HTTPStatus.OK,
                self.server.miro_rack.page_html(),
                "text/html; charset=utf-8",
            )
        elif path == "/miro_rack/map":
            self.server.miro_rack.log_view("MIRO Rack Mapview page opened")
            self._send_file(
                self.server.dashboard_file.with_name("miro_rack_map.html")
            )
        elif path == "/miro_rack/map.js":
            self._send_javascript_bundle(
                self.server.dashboard_file.parent / "vendor" / "leaflet" / "leaflet.js",
                self.server.dashboard_file.with_name("miro_rack_map.js"),
            )
        elif path in {"/miro_rack/plotly.min.js", "/vendor/plotly.min.js"}:
            status, content_type, body, headers = self.server.miro_rack.forward_get(
                "/plotly.min.js"
            )
            self._send_bytes(status, body, content_type, headers)
        elif path == "/api/build":
            # Answers "am I running the code I just pulled?" without a terminal.
            self._send_json(
                HTTPStatus.OK,
                {
                    "version": SOFTWARE_VERSION,
                    # dashboard.html sits at <root>/app/assets/, so the
                    # application root is two levels above its directory.
                    "build": build_fingerprint(self.server.dashboard_file.parents[2]),
                },
            )
        elif path == "/campaign-logo.html":
            self._send_file(self.server.dashboard_file.with_name("campaign-logo.html"))
        elif path in {
            "/campaign-logo.svg",
            "/campaign-airship.svg",
            "/campaign-main-airship.svg",
        }:
            asset_name = {
                "/campaign-logo.svg": "campaign-logo.svg",
                "/campaign-airship.svg": "campaign-airship.svg",
                "/campaign-main-airship.svg": "campaign-main-airship.svg",
            }[path]
            self._send_file(self.server.dashboard_file.with_name(asset_name))
        elif path.startswith("/logos/"):
            # Partner logos are bundled so the dashboard renders with no network
            # access. Resolve and containment-check before serving.
            name = path.removeprefix("/logos/")
            directory = (self.server.dashboard_file.parent / "logos").resolve()
            asset = (directory / name).resolve()
            if (
                Path(name).name != name
                or not asset.is_relative_to(directory)
                or not asset.is_file()
            ):
                self._send_json(HTTPStatus.NOT_FOUND, {"error": "Unknown logo"})
            else:
                self._send_file(asset)
        elif path == "/vendor/leaflet/leaflet.js":
            self._send_file(
                self.server.dashboard_file.parent / "vendor" / "leaflet" / "leaflet.js"
            )
        elif path == "/vendor/leaflet/leaflet.css":
            self._send_file(
                self.server.dashboard_file.parent / "vendor" / "leaflet" / "leaflet.css"
            )
        elif path == "/api/noseboom":
            self._send_json(HTTPStatus.OK, self.server.backend.noseboom_view())
        elif path == "/api/noseboom/qc":
            self._send_json(HTTPStatus.OK, self.server.backend.noseboom_qc_view())
        elif path == "/api/gopro":
            self._send_json(HTTPStatus.OK, self.server.backend.gopro_view())
        elif path.startswith("/api/gopro/image/"):
            capture_id = path.removeprefix("/api/gopro/image/")
            try:
                image_file = self.server.backend.gopro_image_file(capture_id)
            except ValueError as exc:
                self._send_json(HTTPStatus.NOT_FOUND, {"error": str(exc)})
            else:
                self._send_file(
                    image_file,
                    download=parse_qs(parsed.query).get("download") == ["1"],
                )
        elif path == "/api/gopro/media-status":
            self._send_json(HTTPStatus.OK, self.server.backend.gopro_media_status())
        elif path == "/api/flir":
            self._send_json(HTTPStatus.OK, self.server.backend.flir_view())
        elif path == "/api/flir/exports":
            self._send_json(HTTPStatus.OK, {"exports": self.server.backend.flir_exports()})
        elif path.startswith("/api/flir/asset/"):
            name = path.removeprefix("/api/flir/asset/")
            try:
                asset_file = self.server.backend.flir_asset_file(name)
            except ValueError as exc:
                self._send_json(HTTPStatus.NOT_FOUND, {"error": str(exc)})
            else:
                self._send_file(
                    asset_file,
                    download=parse_qs(parsed.query).get("download") == ["1"],
                )
        elif path == "/api/noseboom/straight-settings/progress":
            self._send_json(
                HTTPStatus.OK,
                self.server.backend.noseboom_straight_recalculation_progress(),
            )
        elif path == "/api/noseboom/export":
            self._send_file(self.server.backend.noseboom_export_file(), download=True)
        elif path == "/api/noseboom/data-export/progress":
            # Polled while the download request itself is still streaming.
            self._send_json(
                HTTPStatus.OK,
                self.server.backend.noseboom_data_export_progress(),
            )
        elif path == "/api/noseboom/statistics/export/progress":
            self._send_json(
                HTTPStatus.OK,
                self.server.backend.noseboom_statistics_export_progress(),
            )
        elif path.startswith("/api/noseboom/statistics/export/download/"):
            name = path.removeprefix(
                "/api/noseboom/statistics/export/download/"
            )
            self._send_file(
                self.server.backend.noseboom_statistics_export_file(name),
                download=True,
            )
        elif path == "/api/opc":
            self._send_json(HTTPStatus.OK, self.server.backend.hatchbox_view("opc"))
        elif path in {"/api/opc/map", "/api/partector/map"}:
            self._send_json(
                HTTPStatus.OK,
                self.server.backend.size_distribution_map_view(
                    path.split("/")[2]
                ),
            )
        elif path == "/api/partector":
            self._send_json(HTTPStatus.OK, self.server.backend.hatchbox_view("partector"))
        elif path == "/api/ins-gimbal":
            self._send_json(HTTPStatus.OK, self.server.backend.hatchbox_view("ins_gimbal"))
        elif path == "/api/micasense":
            self._send_json(HTTPStatus.OK, self.server.backend.hatchbox_view("micasense"))
        elif path.startswith("/api/micasense/thumbnail/"):
            self._send_file(
                self.server.backend.micasense_thumbnail_file(
                    path.removeprefix("/api/micasense/thumbnail/")
                )
            )
        elif path == "/api/sif":
            self._send_json(HTTPStatus.OK, self.server.backend.hatchbox_view("sif"))
        elif path == "/api/miro-rack/bootstrap":
            self._send_json(HTTPStatus.OK, self.server.miro_rack.bootstrap())
        elif path == "/api/miro-rack/map/progress":
            self._send_json(HTTPStatus.OK, self.server.miro_rack.map_progress())
        elif path == "/api/miro-rack/map/data":
            try:
                self._send_json(HTTPStatus.OK, self.server.miro_rack.map_payload())
            except RuntimeError as exc:
                self._send_json(HTTPStatus.CONFLICT, {"error": str(exc)})
        elif path.startswith("/api/miro-rack/"):
            legacy_path = "/api/" + path.removeprefix("/api/miro-rack/")
            status, content_type, body, headers = self.server.miro_rack.forward_get(
                legacy_path, parse_qs(parsed.query)
            )
            self._send_bytes(status, body, content_type, headers)
        elif path == "/api/scan":
            self._send_json(HTTPStatus.OK, self.server.backend.snapshot())
        elif path == "/api/hybrid/state":
            self._send_json(HTTPStatus.OK, self.server.backend.hybrid_state())
        elif path == "/api/remote-sensing/coverage":
            self._send_json(HTTPStatus.OK, self.server.backend.camera_coverage())
        elif path == "/api/sif/progress":
            self._send_json(HTTPStatus.OK, self.server.backend.sif_progress())
        elif path == "/api/update/status":
            self._send_json(
                HTTPStatus.OK,
                self.server.backend.update_status(
                    refresh=parse_qs(parsed.query).get("refresh") == ["1"]
                ),
            )
        elif path == "/api/logs":
            self._send_json(
                HTTPStatus.OK, {"records": self.server.backend.visible_logs()}
            )
        else:
            self._send_json(HTTPStatus.NOT_FOUND, {"error": "Route not found"})

    def _request_origin_is_trusted(self) -> bool:
        """Whether this POST came from the dashboard rather than another page.

        CC-FLUX listens on 127.0.0.1, which any page open in the same browser can
        also reach. Without this check a website could quietly POST to
        /api/processing/start, /api/reset or /api/exit while a campaign run was
        under way - the browser attaches no credentials, but none are needed
        here, so the request would simply be carried out.

        A same-origin fetch from the dashboard sends Origin; some browsers send
        only Referer. Either must name this server. A request with neither is
        refused, because a form POST from another page is exactly the case that
        omits both.
        """
        host = self.headers.get("Host", "")
        allowed = {f"http://{host}", f"https://{host}"}
        origin = self.headers.get("Origin")
        if origin is not None:
            return origin in allowed
        referer = self.headers.get("Referer")
        if referer:
            parsed = urlparse(referer)
            return f"{parsed.scheme}://{parsed.netloc}" in allowed
        return False

    def do_POST(self) -> None:  # noqa: N802
        path = urlparse(self.path).path
        if not self._request_origin_is_trusted():
            self.server.backend.logger.log(
                LogLevel.WARNING,
                "server",
                f"Refused a cross-origin request to {path} from "
                f"{self.headers.get('Origin') or self.headers.get('Referer') or 'an unnamed page'}",
                processing_step="request-origin",
            )
            self._send_json(
                HTTPStatus.FORBIDDEN,
                {
                    "error": (
                        "This request did not come from the CC-FLUX dashboard and "
                        "was refused. Open the dashboard from the launcher."
                    )
                },
            )
            return
        try:
            if path == "/api/select-flight-folder":
                result = self.server.backend.select_and_start()
                self._send_json(HTTPStatus.OK, result)
            elif path == "/api/select-scan-folders":
                result = self.server.backend.select_folders(
                    self._json_body().get("folder")
                )
                self._send_json(HTTPStatus.OK, result)
            elif path == "/api/select-camera-folder":
                result = self.server.backend.select_camera_folder(
                    self._json_body().get("folder")
                )
                self._send_json(HTTPStatus.OK, result)
            elif path == "/api/scan":
                body = self._json_body()
                result = self.server.backend.start_scan(
                    Path(str(body["folder"])),
                    camera_folder=(
                        Path(str(body["camera_folder"]))
                        if body.get("camera_folder")
                        else None
                    ),
                    include_camera=bool(body.get("include_camera", True)),
                )
                self._send_json(HTTPStatus.ACCEPTED, result)
            elif path == "/api/select-output-folder":
                self._send_json(
                    HTTPStatus.OK,
                    self.server.backend.select_output_folder(
                        self._json_body().get("folder")
                    ),
                )
            elif path == "/api/project/save":
                miro_rack = self.server.miro_rack.persist_main_project()
                project_file = self.server.backend.save_project()
                self._send_json(
                    HTTPStatus.OK,
                    {
                        "saved": True,
                        "project_file": str(project_file),
                        "miro_rack": miro_rack,
                    },
                )
            elif path == "/api/project/discover":
                body = self._json_body()
                result = (
                    self.server.backend.discover_saved_projects(
                        Path(str(body["folder"]))
                    )
                    if body.get("folder")
                    else self.server.backend.select_project_folder()
                )
                self._send_json(HTTPStatus.OK, result)
            elif path == "/api/project/open":
                body = self._json_body()
                result = self.server.backend.open_project(
                    Path(str(body["project_file"]))
                    if body.get("project_file") else None
                )
                if not result.get("cancelled"):
                    result["miro_rack"] = self.server.miro_rack.restore_main_project()
                    result["state"] = self.server.backend.snapshot()
                self._send_json(HTTPStatus.OK, result)
            elif path == "/api/hatchbox/log":
                body = self._json_body()
                page = str(body.get("page") or "opc")
                self.server.backend.log_hatchbox_view_event(
                    page, str(body.get("message") or "Hatchbox browser interaction")
                )
                self._send_json(HTTPStatus.OK, {"logged": True})
            elif path == "/api/flir/level2-options":
                body = self._json_body()
                self._send_json(
                    HTTPStatus.OK,
                    {
                        "saved": True,
                        "options": self.server.backend.update_flir_level2_options(body),
                    },
                )
            elif path == "/api/sif/select-file":
                body = self._json_body()
                self._send_json(
                    HTTPStatus.OK,
                    self.server.backend.select_sif_essential_file(
                        str(body.get("kind", ""))
                    ),
                )
            elif path == "/api/sif/options":
                body = self._json_body()
                self._send_json(
                    HTTPStatus.OK,
                    {
                        "saved": True,
                        "options": self.server.backend.update_sif_options(body),
                    },
                )
            elif path == "/api/noseboom/log":
                body = self._json_body()
                self.server.backend.log_noseboom_view_event(str(body.get("message", "")))
                self._send_json(HTTPStatus.OK, {"logged": True})
            elif path == "/api/gopro/reconnect":
                body = self._json_body()
                self._send_json(
                    HTTPStatus.OK, self.server.backend.reconnect_gopro_media(body)
                )
            elif path == "/api/gopro/log":
                body = self._json_body()
                self.server.backend.log_gopro_view_event(str(body.get("message", "")))
                self._send_json(HTTPStatus.OK, {"logged": True})
            elif path == "/api/noseboom/straight-settings":
                body = self._json_body()
                action = str(body.get("action") or "legacy-save")
                if action == "preview":
                    result = self.server.backend.start_noseboom_straight_recalculation(
                        body.get("settings")
                    )
                elif action == "save-preview":
                    result = self.server.backend.save_noseboom_straight_preview()
                else:
                    settings = self.server.backend.update_noseboom_straight_settings(
                        body.get("settings")
                    )
                    result = {"saved": True, "settings": settings}
                self._send_json(HTTPStatus.OK, result)
            elif path == "/api/noseboom/data-export":
                body = self._json_body()
                self._send_file(
                    self.server.backend.export_noseboom_data(body), download=True
                )
            elif path == "/api/noseboom/statistics/export":
                body = self._json_body()
                result = self.server.backend.start_noseboom_statistics_export(body)
                self._send_json(HTTPStatus.ACCEPTED, result)
            elif path in {"/api/opc/map/export", "/api/partector/map/export"}:
                body = self._json_body()
                filename, pdf = self.server.backend.export_size_distribution_map_pdf(
                    path.split("/")[2], body
                )
                self._send_bytes(
                    HTTPStatus.OK,
                    pdf,
                    "application/pdf",
                    {"Content-Disposition": f'attachment; filename="{filename}"'},
                )
            elif path == "/api/miro-rack/map/export":
                body = self._json_body()
                filename, pdf = self.server.miro_rack.export_map_pdf(body)
                self._send_bytes(
                    HTTPStatus.OK,
                    pdf,
                    "application/pdf",
                    {"Content-Disposition": f'attachment; filename="{filename}"'},
                )
            elif path == "/api/miro-rack/map/start":
                self._json_body()
                self._send_json(
                    HTTPStatus.ACCEPTED, self.server.miro_rack.start_map_job()
                )
            elif path == "/api/miro-rack/log":
                body = self._json_body()
                self.server.miro_rack.log_view(
                    str(body.get("message") or "MIRO Rack browser interaction")
                )
                self._send_json(HTTPStatus.OK, {"logged": True})
            elif path == "/api/miro-rack/ui-state":
                body = self._json_body()
                self._send_json(
                    HTTPStatus.OK,
                    {"saved": True, "state": self.server.miro_rack.update_ui_state(body)},
                )
            elif path.startswith("/api/miro-rack/"):
                body = self._json_body()
                legacy_path = "/api/" + path.removeprefix("/api/miro-rack/")
                status, content_type, response, headers = (
                    self.server.miro_rack.forward_post(legacy_path, body)
                )
                self._send_bytes(status, response, content_type, headers)
            elif path == "/api/scan/cancel":
                body = self._json_body()
                self._send_json(
                    HTTPStatus.OK,
                    {
                        "cancelled": self.server.backend.cancel(
                            str(body["source"]) if body.get("source") else None
                        )
                    },
                )
            elif path == "/api/logs/clear":
                self.server.backend.clear_visible_logs()
                self._send_json(HTTPStatus.OK, {"cleared": True})
            elif path == "/api/scan/candidates/confirm":
                body = self._json_body()
                self.server.backend.confirm_candidate(
                    str(body["instrument_id"]), Path(str(body["candidate_path"]))
                )
                self._send_json(HTTPStatus.OK, {"confirmed": True})
            elif path == "/api/time-filter":
                body = self._json_body()
                self.server.backend.update_time_filter(body)
                self._send_json(
                    HTTPStatus.OK,
                    {"time_filter": self.server.backend.snapshot()["time_filter"]},
                )
            elif path == "/api/time-filter/instrument":
                body = self._json_body()
                self.server.backend.update_instrument_time_override(
                    str(body["instrument_id"]),
                    body.get("start"),
                    body.get("end"),
                )
                self._send_json(
                    HTTPStatus.OK,
                    {"time_filter": self.server.backend.snapshot()["time_filter"]},
                )
            elif path == "/api/resources":
                body = self._json_body()
                self.server.backend.update_resources(
                    worker_count=body.get("worker_count"),
                    memory_bytes=body.get("memory_bytes"),
                )
                self._send_json(
                    HTTPStatus.OK,
                    {"resources": self.server.backend.snapshot()["resources"]},
                )
            elif path == "/api/queue":
                body = self._json_body()
                self.server.backend.update_queue(body)
                self._send_json(
                    HTTPStatus.OK,
                    {
                        "processing_queue": self.server.backend.snapshot()[
                            "processing_queue"
                        ]
                    },
                )
            elif path == "/api/processing/start":
                body = self._json_body()
                self.server.backend.start_processing(
                    confirmed_limited_coverage=body.get("confirmed_limited_coverage") is True
                )
                self._send_json(
                    HTTPStatus.ACCEPTED,
                    {"started": True, "state": self.server.backend.snapshot()},
                )
            elif path == "/api/remote-sensing/log":
                body = self._json_body()
                self.server.backend.log_remote_sensing_workflow(body)
                self._send_json(HTTPStatus.OK, {"logged": True})
            elif path == "/api/hybrid/create":
                body = self._json_body()
                self._send_json(
                    HTTPStatus.OK, self.server.backend.create_hybrid_packages(body)
                )
            elif path == "/api/hybrid/load":
                body = self._json_body()
                self._send_json(
                    HTTPStatus.OK,
                    self.server.backend.load_work_package(
                        Path(str(body.get("path", ""))),
                        str(body.get("passphrase", "")),
                    ),
                )
            elif path == "/api/hybrid/export":
                body = self._json_body()
                self._send_json(
                    HTTPStatus.OK, self.server.backend.export_hybrid_results(body)
                )
            elif path == "/api/hybrid/fusion/review":
                body = self._json_body()
                self._send_json(
                    HTTPStatus.OK, self.server.backend.review_hybrid_fusion(body)
                )
            elif path == "/api/hybrid/fusion/start":
                body = self._json_body()
                self._send_json(
                    HTTPStatus.OK, self.server.backend.fuse_hybrid_results(body)
                )
            elif path == "/api/remote-sensing/preview":
                body = self._json_body()
                self._send_json(
                    HTTPStatus.OK,
                    self.server.backend.preview_remote_sensing(body),
                )
            elif path == "/api/remote-sensing/start":
                body = self._json_body()
                jobs = self.server.backend.start_remote_sensing(body)
                self._send_json(
                    HTTPStatus.ACCEPTED,
                    {
                        "started": True,
                        "jobs": jobs,
                        "state": self.server.backend.snapshot(),
                    },
                )
            elif path == "/api/application/reset":
                self._send_json(
                    HTTPStatus.OK,
                    {"reset": True, "state": self.server.backend.reset_system()},
                )
            elif path == "/api/application/exit":
                self._send_json(HTTPStatus.OK, {"exiting": True})
                threading.Thread(
                    target=self.server.shutdown,
                    name="ccflux-server-shutdown",
                    daemon=True,
                ).start()
            else:
                self._send_json(HTTPStatus.NOT_FOUND, {"error": "Route not found"})
        except ConnectionError:
            return
        except (KeyError, TypeError, ValueError, RuntimeError) as exc:
            self._send_json(HTTPStatus.BAD_REQUEST, {"error": str(exc)})
        except Exception:
            self._send_json(
                HTTPStatus.INTERNAL_SERVER_ERROR,
                {"error": "The local application could not complete the request"},
            )

    def log_message(self, format: str, *args: object) -> None:
        return

    def _json_body(self) -> dict[str, Any]:
        try:
            length = int(self.headers.get("Content-Length", "0"))
            value = json.loads(self.rfile.read(length) or b"{}")
        except (ValueError, json.JSONDecodeError) as exc:
            raise ValueError("Request body must be valid JSON") from exc
        if not isinstance(value, dict):
            raise ValueError("Request body must be a JSON object")
        return value

    def _send_json(self, status: HTTPStatus, value: object) -> None:
        body = json.dumps(value, ensure_ascii=False).encode("utf-8")
        self._send_bytes(status, body, "application/json; charset=utf-8")

    def _send_bytes(
        self,
        status: int | HTTPStatus,
        body: bytes,
        content_type: str,
        headers: dict[str, str] | None = None,
    ) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for key, value in (headers or {}).items():
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(body)

    def _send_javascript_bundle(self, *paths: Path) -> None:
        try:
            body = b"\n;\n".join(path.read_bytes() for path in paths)
        except OSError:
            self._send_json(
                HTTPStatus.INTERNAL_SERVER_ERROR,
                {"error": "Noseboom browser bundle is unavailable"},
            )
            return
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/javascript; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)
    # Streamed rather than read whole: the same helper serves 20 KB dashboard
    # assets and instrument exports that run to hundreds of megabytes, and
    # read_bytes() allocated the entire file before writing a single byte.
    FILE_CHUNK_BYTES = 64 * 1024

    def _send_file(self, path: Path, *, download: bool = False) -> None:
        try:
            size = path.stat().st_size
            stream = path.open("rb")
        except OSError:
            self._send_json(
                HTTPStatus.INTERNAL_SERVER_ERROR,
                {"error": "Dashboard asset is unavailable"},
            )
            return
        with stream:
            self.send_response(HTTPStatus.OK)
            self.send_header(
                "Content-Type",
                mimetypes.guess_type(path.name)[0] or "application/octet-stream",
            )
            self.send_header("Content-Length", str(size))
            self.send_header("Cache-Control", "no-store")
            if download:
                self.send_header(
                    "Content-Disposition", f'attachment; filename="{path.name}"'
                )
            self.end_headers()
            try:
                shutil.copyfileobj(stream, self.wfile, self.FILE_CHUNK_BYTES)
            except (BrokenPipeError, ConnectionResetError):
                # The browser navigated away or cancelled the download. The
                # headers are already out, so there is nothing to report.
                pass


def create_server(
    *,
    host: str = "127.0.0.1",
    port: int = 8765,
    application_root: Path | None = None,
    backend: DashboardScanBackend | None = None,
) -> DashboardHTTPServer:
    root = application_root or Path(__file__).resolve().parents[1]
    active_backend = backend or DashboardScanBackend(root)
    return DashboardHTTPServer(
        (host, port), active_backend, root / "app" / "assets" / "dashboard.html"
    )


def open_dashboard_in_browser(server: DashboardHTTPServer, url: str) -> None:
    """Open the local dashboard in the operator's default browser."""
    try:
        opened = webbrowser.open(url, new=2, autoraise=True)
        if not opened and os.name == "nt":
            os.startfile(url)  # type: ignore[attr-defined]
            opened = True
        level = LogLevel.INFO if opened else LogLevel.WARNING
        message = (
            f"Default browser opened automatically: {url}"
            if opened
            else f"Default browser did not confirm opening; browse to {url}"
        )
        try:
            server.backend.logger.log(level, "dashboard-startup", message)
        except OSError:
            pass
    except Exception as error:
        try:
            server.backend.logger.capture_exception(
                "dashboard-startup",
                f"Automatic browser launch failed; browse to {url}",
                error,
            )
        except OSError:
            pass


def main() -> int:
    parser = argparse.ArgumentParser(description="CCFLUX Zeppelin local dashboard")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", default=8765, type=int)
    parser.add_argument(
        "--no-browser",
        action="store_true",
        help="Start the server without opening the default browser.",
    )
    arguments = parser.parse_args()
    server = create_server(host=arguments.host, port=arguments.port)
    # Contacts GitHub once, on a daemon thread; CCFLUX_UPDATE_CHECK=off skips it.
    server.backend.start_background_update_check()
    dashboard_url = f"http://{arguments.host}:{server.server_port}/"
    print(f"CCFLUX dashboard: {dashboard_url}")
    if not arguments.no_browser:
        browser_timer = threading.Timer(
            0.35, open_dashboard_in_browser, args=(server, dashboard_url)
        )
        browser_timer.daemon = True
        browser_timer.start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
