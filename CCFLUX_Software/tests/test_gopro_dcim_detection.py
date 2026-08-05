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


def test_a_numbered_card_folder_is_a_gopro_folder(gopro_patterns):
    """100GOPRO, 101GOPRO and so on, which sit below DCIM."""
    assert "*GOPRO" in gopro_patterns.likely_folder_names


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
        matches = _folder_matches(folder, root, ("GoPro", "DCIM", "*GOPRO"))
        assert {name for _path, name in matches} >= {"DCIM", "*GOPRO"}

    def test_an_unrelated_tree_does_not_match(self, tmp_path):
        from core.scanner import _folder_matches

        folder = tmp_path / "influxdb" / "Nose_Boom_Windpy"
        folder.mkdir(parents=True)
        assert _folder_matches(folder, tmp_path, ("GoPro", "DCIM", "*GOPRO")) == ()
