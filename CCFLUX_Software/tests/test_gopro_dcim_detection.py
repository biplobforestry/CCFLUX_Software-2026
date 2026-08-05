"""A camera card is a DCIM tree, whatever the folder above it is called.

Flight_CCT0803 happened to keep its card under a folder named GoPro, so the
delivery was found. A card copied straight off the camera - card_01/DCIM/
100GOPRO - matched no configured folder name and the instrument went missing.
DCIM is what every camera writes, so that is what the scan looks for.
"""
from pathlib import Path

import pytest

from core.detection_configuration import load_detection_configuration

CONFIGS = Path(__file__).resolve().parents[1] / "configs"


@pytest.fixture(scope="module")
def gopro_patterns():
    configuration = load_detection_configuration(
        CONFIGS / "instrument_detection.yaml", CONFIGS / "file_patterns.yaml"
    )
    return configuration.patterns_for("gopro")


def test_dcim_is_a_gopro_folder(gopro_patterns):
    assert "DCIM" in gopro_patterns.likely_folder_names


def test_the_numbered_card_folders_are_not_separate_deliveries(gopro_patterns):
    """100GOPRO, 101GOPRO and 102GOPRO are one card, not three.

    Matching them individually made each its own candidate, so the scan asked
    which to use and the instrument never loaded. DCIM is their single parent,
    so matching DCIM gathers the whole card as one delivery.
    """
    assert "*GOPRO" not in gopro_patterns.likely_folder_names
    assert "DCIM" in gopro_patterns.likely_folder_names


def test_the_named_folders_still_match(gopro_patterns):
    for name in ("GoPro", "GOPRO", "gopro"):
        assert name in gopro_patterns.likely_folder_names


def test_the_card_extensions_are_unchanged(gopro_patterns):
    for extension in (".jpg", ".mp4"):
        assert extension in gopro_patterns.file_extensions


class TestFolderChainMatching:
    """The matcher walks the whole parent chain, so DCIM covers what is under it."""

    def test_a_folder_below_dcim_matches_through_its_parent(self, tmp_path):
        from core.scanner import _folder_matches

        root = tmp_path
        folder = root / "camera_system" / "card_01" / "DCIM" / "100GOPRO"
        folder.mkdir(parents=True)
        matches = _folder_matches(folder, root, ("GoPro", "DCIM"))
        # The innermost match becomes the candidate: one DCIM, one delivery.
        assert [name for _path, name in matches] == ["DCIM"]

    def test_an_unrelated_tree_does_not_match(self, tmp_path):
        from core.scanner import _folder_matches

        folder = tmp_path / "influxdb" / "Nose_Boom_Windpy"
        folder.mkdir(parents=True)
        assert _folder_matches(folder, tmp_path, ("GoPro", "DCIM")) == ()


class TestMicaSenseWorkspace:
    """MicaSense processed but had nowhere to be seen: no page, no route."""

    from pathlib import Path as _Path

    ASSETS = _Path(__file__).resolve().parents[1] / "app" / "assets"
    APP = _Path(__file__).resolve().parents[1] / "app"

    def test_the_workspace_page_exists(self):
        assert (self.ASSETS / "micasense.html").is_file()
        assert (self.ASSETS / "micasense.js").is_file()

    def test_the_page_and_its_data_are_served(self):
        server = (self.APP / "server.py").read_text(encoding="utf-8")
        assert 'path == "/micasense"' in server
        assert 'path == "/api/micasense"' in server
        assert '/api/micasense/thumbnail/' in server

    def test_processing_writes_what_the_page_reads(self):
        backend = (self.APP / "scan_backend.py").read_text(encoding="utf-8")
        assert "_micasense_browser_payload" in backend
        assert '"micasense": "micasense_browser",' in backend

    def test_the_dashboard_card_opens_it(self):
        markup = (self.ASSETS / "dashboard.html").read_text(encoding="utf-8")
        assert 'href="/micasense"' in markup

    def test_a_thumbnail_cannot_escape_its_folder(self):
        backend = (self.APP / "scan_backend.py").read_text(encoding="utf-8")
        assert "safe = Path(str(name)).name" in backend

    def test_unreadable_files_are_reported_not_fatal(self):
        """A camera file written badly must not hide the rest of the delivery."""
        script = (self.ASSETS / "micasense.js").read_text(encoding="utf-8")
        assert "could not be read and were skipped" in script
        assert "onerror=" in script
