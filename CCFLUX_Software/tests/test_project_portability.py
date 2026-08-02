"""A project must find its own products after it is moved.

A .ccflux carries every product inside it, but records where each one was when
it was written. Process on Windows, open the project on a Mac or from a USB
stick, and every recorded path is dead: the FLIR map, the SIF map, the Noseboom
and GoPro views and the rest all reported that their instrument had not been
processed, while the products sat correctly extracted beside the project.

The recorded separators are not necessarily this platform's either. A Windows
path parsed here is one long meaningless filename - `Path(r"C:\\Output\\x.json").name`
is the whole string - so a fix that only joins `.name` to the new root finds
nothing for a product stored in a subdirectory.
"""

from pathlib import Path

import pytest

from core.flight_project import (
    PRODUCT_DIRECTORIES,
    relocate_output_locations,
    relocate_product_path,
)

WINDOWS = r"C:\Output\Flight_2707\quicklooks\flir_browser.json"
POSIX = "/home/ops/Output/Flight_2707/quicklooks/flir_browser.json"


@pytest.fixture
def output_root(tmp_path):
    root = tmp_path / "Flight_2707"
    (root / "quicklooks").mkdir(parents=True)
    (root / "quicklooks" / "flir_browser.json").write_text("{}", encoding="utf-8")
    (root / "logs").mkdir()
    (root / "logs" / "processing.jsonl").write_text("", encoding="utf-8")
    deep = root / "processed" / "flir" / "runs" / "20260802T153514_273006Z" / "level2"
    deep.mkdir(parents=True)
    (deep / "temperature_frames.csv").write_text("a,b\n", encoding="utf-8")
    return root


@pytest.mark.parametrize("recorded", [WINDOWS, POSIX])
def test_a_foreign_path_is_resolved_against_the_extracted_tree(recorded, output_root):
    resolved = relocate_product_path(recorded, output_root)

    assert resolved == output_root / "quicklooks" / "flir_browser.json"
    assert resolved.is_file()


def test_a_windows_path_is_not_treated_as_one_filename(output_root):
    """The defect this guards: on POSIX the whole Windows path is `.name`."""
    assert Path(WINDOWS).name == WINDOWS.replace("/", "\\")

    assert relocate_product_path(WINDOWS, output_root).name == "flir_browser.json"


def test_a_product_in_a_deep_subdirectory_is_found(output_root):
    recorded = (
        r"C:\Output\Flight_2707\processed\flir\runs"
        r"\20260802T153514_273006Z\level2\temperature_frames.csv"
    )

    resolved = relocate_product_path(recorded, output_root)

    assert resolved.is_file()
    assert resolved.parts[-2:] == ("level2", "temperature_frames.csv")


def test_a_path_that_still_resolves_is_left_alone(output_root, tmp_path):
    """Every same-machine case must behave exactly as before."""
    existing = output_root / "quicklooks" / "flir_browser.json"

    assert relocate_product_path(existing, tmp_path / "somewhere-else") == existing


def test_a_product_that_is_genuinely_absent_is_returned_unchanged(output_root):
    """So the caller's own is_file() check still reports it missing, rather than
    this quietly inventing a path that does not exist either."""
    recorded = r"C:\Output\Flight_2707\quicklooks\gone.json"

    resolved = relocate_product_path(recorded, output_root)

    assert not resolved.is_file()
    assert str(resolved) == recorded


@pytest.mark.parametrize("empty", [None, "", Path("")])
def test_an_empty_recorded_path_is_harmless(empty, output_root):
    assert relocate_product_path(empty, output_root) == Path("")


def test_every_output_location_is_relocated(output_root):
    locations = {
        "flir_browser": WINDOWS,
        "processing_log": r"C:\Output\Flight_2707\logs\processing.jsonl",
        "missing": r"C:\Output\Flight_2707\quicklooks\nope.json",
    }

    moved = relocate_output_locations(locations, output_root)

    assert moved["flir_browser"].is_file()
    assert moved["processing_log"].is_file()
    assert not moved["missing"].is_file(), "absent products stay absent"


def test_the_product_directories_cover_what_a_project_writes():
    for name in ("quicklooks", "logs", "reports", "processed", "exports"):
        assert name in PRODUCT_DIRECTORIES


def test_the_last_matching_directory_wins(tmp_path):
    """A flight folder may itself be called 'processed'; the product directory
    nearest the file is the one that identifies it."""
    root = tmp_path / "Flight"
    target = root / "processed" / "sif" / "runs" / "x" / "sif_browser.json"
    target.parent.mkdir(parents=True)
    target.write_text("{}", encoding="utf-8")

    recorded = r"D:\processed\Flight\processed\sif\runs\x\sif_browser.json"

    assert relocate_product_path(recorded, root) == target
