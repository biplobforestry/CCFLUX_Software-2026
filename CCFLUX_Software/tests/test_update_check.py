"""The update check must inform, never install, and never get in the way.

The software is expected to run on a laptop with a campaign disk and no
network. Every failure path here has to end in a quiet "did not check", because
an update notice is worth nothing next to the application refusing to work.
"""

import io
import json
import re
import urllib.error
from pathlib import Path

import pytest

from app.scan_backend import DashboardScanBackend
from core.update_check import (
    UpdateStatus,
    check_for_update,
    is_newer,
    update_check_enabled,
    version_tuple,
)
from core.version import SOFTWARE_VERSION, UPDATE_MANIFEST_URL


def _manifest(**overrides):
    payload = {
        "latest_version": "1.1.0",
        "released_utc": "2026-08-03T22:00:00Z",
        "notice": "Parallel processing.",
        "download_url": "https://example.invalid/download",
    }
    payload.update(overrides)

    def opener(request, timeout=None):
        class Response(io.BytesIO):
            def __enter__(self):
                return self

            def __exit__(self, *args):
                self.close()

        return Response(json.dumps(payload).encode("utf-8"))

    return opener


def test_declared_version_matches_the_package():
    """A check comparing the wrong number is worse than no check."""
    # tomllib is 3.11+; the supported floor is 3.10, so read the field directly.
    text = (Path(__file__).parents[1] / "pyproject.toml").read_text(encoding="utf-8")
    declared = re.search(r'^version = "([^"]+)"', text, re.MULTILINE)
    assert declared and declared.group(1) == SOFTWARE_VERSION


def test_manifest_url_points_at_this_repository():
    assert UPDATE_MANIFEST_URL.startswith("https://raw.githubusercontent.com/")
    assert UPDATE_MANIFEST_URL.endswith("update_manifest.json")


def test_published_manifest_is_valid_and_current():
    """The manifest served to every installation must parse and name a version."""
    manifest = Path(__file__).parents[2] / "update_manifest.json"
    if not manifest.is_file():
        pytest.skip("manifest is published from the repository root")
    payload = json.loads(manifest.read_text(encoding="utf-8"))
    assert version_tuple(payload["latest_version"])
    # Announcing a version older than the one shipped would be incoherent.
    assert not is_newer(SOFTWARE_VERSION, payload["latest_version"])


@pytest.mark.parametrize(
    "candidate, installed, expected",
    [
        ("1.0.1", "1.0.0", True),
        ("1.1.0", "1.0.9", True),
        ("1.10.0", "1.9.0", True),      # numeric, not lexicographic
        ("2.0", "1.9.9", True),
        ("v1.2.0", "1.1.0", True),      # a leading v is tolerated
        ("1.0.0", "1.0.0", False),
        ("0.9.9", "1.0.0", False),
        ("", "1.0.0", False),
        ("not-a-version", "1.0.0", False),
    ],
)
def test_version_comparison(candidate, installed, expected):
    assert is_newer(candidate, installed) is expected


def test_newer_release_is_reported_with_its_notice():
    status = check_for_update(current_version="1.0.0", opener=_manifest())

    assert status.checked and status.update_available
    assert status.latest_version == "1.1.0"
    assert status.notice == "Parallel processing."
    assert status.download_url == "https://example.invalid/download"


def test_same_version_is_not_an_update():
    status = check_for_update(current_version="1.1.0", opener=_manifest())

    assert status.checked
    assert status.update_available is False


def test_offline_is_quiet_and_not_an_update():
    def refuse(request, timeout=None):
        raise urllib.error.URLError("network is unreachable")

    status = check_for_update(current_version="1.0.0", opener=refuse)

    assert status.checked is False
    assert status.update_available is False
    assert "could be retrieved" in status.reason


def test_a_broken_manifest_never_raises():
    def rubbish(request, timeout=None):
        class Response(io.BytesIO):
            def __enter__(self):
                return self

            def __exit__(self, *args):
                self.close()

        return Response(b"<html>not json</html>")

    status = check_for_update(current_version="1.0.0", opener=rubbish)

    assert status.checked is False
    assert status.update_available is False


def test_a_manifest_without_a_version_is_ignored():
    status = check_for_update(current_version="1.0.0", opener=_manifest(latest_version=""))

    assert status.update_available is False
    assert "does not name a version" in status.reason


@pytest.mark.parametrize("value", ["off", "0", "false", "no", "disabled", "OFF"])
def test_the_check_can_be_switched_off(value):
    environment = {"CCFLUX_UPDATE_CHECK": value}

    assert update_check_enabled(environment) is False
    status = check_for_update(environment=environment, opener=_manifest())
    assert status.enabled is False
    assert status.checked is False
    assert "switched off" in status.reason


def test_the_check_is_on_by_default():
    assert update_check_enabled({}) is True


def test_backend_caches_the_answer_for_the_launch(tmp_path, monkeypatch):
    backend = DashboardScanBackend(tmp_path)
    calls = []

    def counting(**kwargs):
        calls.append(1)
        return UpdateStatus(current_version="1.0.0", latest_version="1.1.0",
                            update_available=True, checked=True)

    monkeypatch.setattr("app.scan_backend.check_for_update", counting)

    first = backend.update_status()
    second = backend.update_status()

    assert first["update_available"] is True
    assert second == first
    assert len(calls) == 1, "opening the dialog must not contact the server again"

    backend.update_status(refresh=True)
    assert len(calls) == 2


def test_the_running_version_is_in_the_dashboard_state(tmp_path):
    backend = DashboardScanBackend(tmp_path)

    assert backend.snapshot()["software_version"] == SOFTWARE_VERSION


def test_nothing_is_downloaded_or_installed():
    """The dialog links to a download page; it must not fetch or run anything."""
    source = (Path(__file__).parents[1] / "core" / "update_check.py").read_text(
        encoding="utf-8"
    )
    for forbidden in ("urlretrieve", "subprocess", "shutil.move", "os.replace", "zipfile"):
        assert forbidden not in source


def test_the_manual_states_what_the_check_discloses():
    manual = (Path(__file__).parents[1] / "manual.text").read_text(encoding="utf-8")

    assert "Software updates and privacy" in manual
    assert "CCFLUX_UPDATE_CHECK=off" in manual
    assert "Nothing is downloaded or installed automatically" in manual
    # Be explicit about what leaves the machine, and what does not.
    assert "IP address" in manual
    assert "No\nflight data" in manual or "No flight data" in manual


def test_every_page_footer_shows_the_running_version():
    """A footer that drifts from the real version misleads bug reports."""
    assets = Path(__file__).parents[1] / "app" / "assets"
    pages = [p for p in sorted(assets.glob("*.html")) if p.name != "campaign-logo.html"]
    assert pages
    for page in pages:
        text = page.read_text(encoding="utf-8")
        assert "app-footer" in text or "<footer" in text, page.name
        assert f"Version {SOFTWARE_VERSION}" in text, (
            f"{page.name} does not show version {SOFTWARE_VERSION}"
        )


def test_injected_miro_rack_footer_shows_the_version():
    bridge = (Path(__file__).parents[1] / "app" / "miro_rack_bridge.py").read_text(
        encoding="utf-8"
    )
    assert f"Version {SOFTWARE_VERSION}" in bridge


def test_update_dialog_shows_the_current_version_and_what_changed():
    script = (Path(__file__).parents[1] / "app" / "assets" / "dashboard.js").read_text(
        encoding="utf-8"
    )
    # Current version is always stated, whether or not an update exists.
    assert "status.current_version" in script
    # When one exists, its version and its notice are shown, with a link only.
    assert "status.latest_version" in script
    assert "status.notice" in script
    assert "status.download_url" in script
    assert "Update available" in script
    assert "Nothing is downloaded or installed automatically" in script
