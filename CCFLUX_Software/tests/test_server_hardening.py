"""The local server must not act on requests from other pages, or hold whole files.

CC-FLUX listens on 127.0.0.1, which every page open in the same browser can also
reach, and none of the endpoints need credentials. Without an origin check a
website could POST to /api/processing/start, /api/reset or /api/exit while a
campaign run was under way and the request would simply be carried out.

_send_file served both 20 KB dashboard assets and instrument exports running to
hundreds of megabytes, and read the whole file into memory before writing a
byte.
"""

import json
import threading
import time
import urllib.error
import urllib.request
from http import HTTPStatus
from pathlib import Path

import pytest

from app.server import DashboardRequestHandler, create_server

APPLICATION_ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(scope="module")
def server():
    instance = create_server(host="127.0.0.1", port=0,
                            application_root=APPLICATION_ROOT)
    thread = threading.Thread(target=instance.serve_forever, daemon=True)
    thread.start()
    time.sleep(0.2)
    yield instance
    instance.shutdown()
    instance.server_close()


def _address(server):
    host, port = server.server_address[:2]
    return f"http://{host}:{port}"


def _post(server, path, origin=None, referer=None):
    request = urllib.request.Request(
        _address(server) + path, data=b"{}", method="POST",
        headers={"Content-Type": "application/json"},
    )
    if origin:
        request.add_header("Origin", origin)
    if referer:
        request.add_header("Referer", referer)
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            return response.status, response.read()
    except urllib.error.HTTPError as error:
        return error.code, error.read()


def test_a_post_from_another_site_is_refused(server):
    status, body = _post(server, "/api/reset", origin="https://example.invalid")

    assert status == HTTPStatus.FORBIDDEN
    assert "did not come from the CC-FLUX dashboard" in json.loads(body)["error"]


def test_a_post_with_no_origin_at_all_is_refused(server):
    """A cross-site form POST is exactly the case that sends neither header."""
    status, _ = _post(server, "/api/reset")

    assert status == HTTPStatus.FORBIDDEN


def test_a_post_from_the_dashboard_is_allowed(server):
    status, _ = _post(server, "/api/logs/clear", origin=_address(server))

    assert status != HTTPStatus.FORBIDDEN


def test_referer_alone_is_accepted(server):
    """Some browsers send Referer without Origin on same-origin requests."""
    status, _ = _post(server, "/api/logs/clear",
                      referer=_address(server) + "/index.html")

    assert status != HTTPStatus.FORBIDDEN


def test_a_lookalike_host_is_refused(server):
    """127.0.0.1:PORT.evil.test must not pass as 127.0.0.1:PORT."""
    status, _ = _post(server, "/api/reset", origin=_address(server) + ".evil.test")

    assert status == HTTPStatus.FORBIDDEN


def test_the_dangerous_endpoints_are_all_behind_the_check(server):
    for path in ("/api/processing/start", "/api/reset", "/api/exit",
                 "/api/project/save", "/api/scan"):
        status, _ = _post(server, path, origin="https://example.invalid")
        assert status == HTTPStatus.FORBIDDEN, f"{path} accepted a foreign origin"


def test_reads_are_not_blocked(server):
    """GET is safe and the pages must keep loading normally."""
    with urllib.request.urlopen(_address(server) + "/api/scan", timeout=10) as r:
        assert r.status == HTTPStatus.OK


def test_files_are_streamed_not_read_whole():
    source = (APPLICATION_ROOT / "app" / "server.py").read_text(encoding="utf-8")
    start = source.index("def _send_file")
    body = source[start: source.index("\ndef create_server")]

    assert "read_bytes()" not in body, "the whole file is still being loaded"
    assert "copyfileobj" in body
    assert "Content-Length" in body, "the browser still needs the size"
    assert DashboardRequestHandler.FILE_CHUNK_BYTES <= 1024 * 1024


def test_a_cancelled_download_does_not_raise():
    source = (APPLICATION_ROOT / "app" / "server.py").read_text(encoding="utf-8")
    start = source.index("def _send_file")
    body = source[start: source.index("\ndef create_server")]

    assert "BrokenPipeError" in body and "ConnectionResetError" in body


def test_a_served_file_arrives_intact(server, tmp_path):
    """Chunking must not truncate or corrupt what it sends."""
    with urllib.request.urlopen(_address(server) + "/manual.text", timeout=10) as r:
        served = r.read()
        declared = int(r.headers["Content-Length"])

    on_disk = (APPLICATION_ROOT / "manual.text").read_bytes()
    assert served == on_disk
    assert declared == len(on_disk)
