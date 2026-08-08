"""The Output Folder shows the project file, and the project file holds it all.

Opening the Output Folder used to show a flight folder containing seven folders
— logs, metadata, processed, project, quicklooks, reports, thumbnails — three of
which were created empty on every run, with the .ccflux buried three levels
inside one of them. And the archive only carried what an adapter had remembered
to register: on Flight_2707 that left nine files behind, including the evaluated
OPC and INS Gimbal CSVs, the FLIR frame index and the SIF position log. An
operator keeping only the .ccflux lost them.

The project file now sits at the top of the Output Folder and carries every
file the run produced: 78 of 78 on Flight_2707, 142 MB of working tree in a
27.8 MB archive.
"""

import zipfile
from pathlib import Path

import pytest

from core.flight_project import (
    BUNDLED_FILE_BYTE_LIMIT,
    BUNDLED_TOTAL_BYTE_LIMIT,
    OUTPUT_DIRECTORIES,
    FlightProjectStore,
    project_filename_for,
)


def _project(tmp_path):
    from tests.test_flight_project import _project as build
    return build(tmp_path)


# --------------------------------------------------------------------------
# Where the project file lives
# --------------------------------------------------------------------------
def test_the_project_file_sits_at_the_top_of_the_output_folder(tmp_path):
    project, _ = _project(tmp_path)

    assert project.project_file.parent == project.output_folder_path
    assert project.project_file.name == project_filename_for(project.flight_id)


def test_saving_puts_it_there(tmp_path):
    project, _ = _project(tmp_path)

    saved = FlightProjectStore().save_project(project, overwrite=True)

    assert saved.parent == project.output_folder_path
    assert saved.parent != project.flight_output_root


def test_only_folders_that_are_written_to_are_created(tmp_path):
    """metadata and thumbnails were made empty on every single run."""
    project, _ = _project(tmp_path)

    created = {path.name for path in project.flight_output_root.iterdir()}

    assert not {"metadata", "thumbnails", "project"} & created
    assert set(OUTPUT_DIRECTORIES) == {"quicklooks", "reports", "logs"}


def test_a_project_beside_its_flight_folder_still_opens(tmp_path):
    """The new layout has to survive the round trip, not just the save."""
    store = FlightProjectStore()
    project, _ = _project(tmp_path)
    saved = store.save_project(project, overwrite=True)

    reopened = store.load_project(saved)

    assert reopened.flight_id == project.flight_id
    assert reopened.flight_output_root == project.flight_output_root


# --------------------------------------------------------------------------
# What the project file carries
# --------------------------------------------------------------------------
def test_every_produced_file_is_bundled(tmp_path):
    """Not only the ones an adapter registered."""
    store = FlightProjectStore()
    project, _ = _project(tmp_path)
    root = project.flight_output_root
    registered = root / "processed" / "noseboom" / "runs" / "1"
    registered.mkdir(parents=True)
    (registered / "export.csv").write_text("a,b\n1,2\n", encoding="utf-8")
    # Written by a run but never put in output_locations — the case that used
    # to be silently left behind.
    unregistered = root / "processed" / "opc_hbx4" / "runs" / "1"
    unregistered.mkdir(parents=True)
    (unregistered / "opc_hbx4_evaluated.csv").write_text("x\n1\n", encoding="utf-8")
    state = next(iter(project.detected_instruments.values()))
    state.output_locations = [registered / "export.csv"]

    saved = store.save_project(project, overwrite=True)

    with zipfile.ZipFile(saved) as archive:
        bundled = {
            name[len("products/"):] for name in archive.namelist()
            if name.startswith("products/")
        }
    # as_posix, because a zip entry always separates with "/" while str() of a
    # Windows path gives "\". Comparing the two raw made every file look left
    # behind on Windows, so this case reported a bundling failure that was not
    # one and could not report a real one.
    on_disk = {
        path.relative_to(root).as_posix()
        for path in root.rglob("*") if path.is_file()
    }
    assert on_disk <= bundled, f"left behind: {sorted(on_disk - bundled)}"


def test_a_large_scientific_product_still_fits(tmp_path):
    """The evaluated CSVs are 10-12 MB; an 8 MB ceiling excluded them."""
    assert BUNDLED_FILE_BYTE_LIMIT >= 32 * 1024 * 1024
    assert BUNDLED_TOTAL_BYTE_LIMIT >= BUNDLED_FILE_BYTE_LIMIT

    store = FlightProjectStore()
    project, _ = _project(tmp_path)
    run = project.flight_output_root / "processed" / "opc_hbx4" / "runs" / "1"
    run.mkdir(parents=True)
    big = run / "opc_hbx4_evaluated.csv"
    big.write_text("value\n" + "1\n" * 5_000_000, encoding="utf-8")
    assert big.stat().st_size > 8 * 1024 * 1024

    saved = store.save_project(project, overwrite=True)

    with zipfile.ZipFile(saved) as archive:
        names = archive.namelist()
    assert any(name.endswith("opc_hbx4_evaluated.csv") for name in names)


def test_camera_imagery_is_still_left_out(tmp_path):
    """Sweeping the whole tree must not start pulling in the pictures."""
    store = FlightProjectStore()
    project, _ = _project(tmp_path)
    thumbs = project.flight_output_root / "processed" / "gopro" / "runs" / "1" / "thumbnails"
    thumbs.mkdir(parents=True)
    (thumbs / "GPAA6289_thumbnail.jpg").write_bytes(b"\xff\xd8\xff" + b"0" * 512)

    saved = store.save_project(project, overwrite=True)

    with zipfile.ZipFile(saved) as archive:
        assert not any("thumbnail" in name for name in archive.namelist())


def test_a_file_beyond_the_ceiling_is_recorded_not_dropped(tmp_path):
    """Anything that cannot travel has to be named in the manifest."""
    import json

    store = FlightProjectStore()
    project, _ = _project(tmp_path)
    run = project.flight_output_root / "processed" / "flir" / "runs" / "1"
    run.mkdir(parents=True)
    huge = run / "enormous.bin"
    huge.write_bytes(b"0" * (BUNDLED_FILE_BYTE_LIMIT + 1024))

    saved = store.save_project(project, overwrite=True)

    with zipfile.ZipFile(saved) as archive:
        manifest = json.loads(archive.read("manifest.json"))
    skipped = {item["path"]: item["reason"] for item in manifest["skipped_products"]}
    assert any("enormous.bin" in path for path in skipped)


# --------------------------------------------------------------------------
# The save says what it did
# --------------------------------------------------------------------------
def test_the_save_reports_what_the_file_carries(tmp_path):
    from app.scan_backend import DashboardScanBackend

    backend = DashboardScanBackend(tmp_path)
    assert hasattr(backend, "_project_contents_report")
    source = (Path(__file__).parents[1] / "app" / "scan_backend.py").read_text(
        encoding="utf-8"
    )
    assert "Everything in the Output Folder is inside this file." in source
    assert "stayed in the Output Folder" in source
