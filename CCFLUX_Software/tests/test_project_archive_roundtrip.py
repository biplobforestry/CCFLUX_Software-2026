"""A .ccflux must survive being handed to someone else, complete."""

import json
import shutil
import zipfile
from pathlib import Path

from core.flight_project import (
    FlightProject,
    FlightProjectStore,
    InstrumentProjectState,
)

PAYLOADS = {
    "noseboom_browser.json": {"available": True, "points": [{"lat": 50.9, "lon": 6.4}]},
    "flir_browser.json": {"available": True, "temperature_available": False},
    "gopro_browser.json": {"available": True, "captures": [{"capture_id": "1"}]},
}


def _processed_project(tmp_path: Path) -> tuple[FlightProjectStore, FlightProject, Path]:
    raw = tmp_path / "raw" / "Flight_2707"
    raw.mkdir(parents=True)
    output = tmp_path / "outputs"
    store = FlightProjectStore()
    project = FlightProject(
        flight_id="Flight_2707", flight_folder_path=raw, output_folder_path=output
    )
    store._create_output_structure(project)
    quicklooks = project.flight_output_root / "quicklooks"
    for name, body in PAYLOADS.items():
        (quicklooks / name).write_text(json.dumps(body), encoding="utf-8")
    project.output_locations["noseboom_quicklook"] = quicklooks / "noseboom_browser.json"
    project.detected_instruments["noseboom"] = InstrumentProjectState(
        instrument_id="noseboom",
        output_locations=[quicklooks / "noseboom_browser.json"],
    )
    return store, project, store.save_project(project, overwrite=True)


def test_products_are_bundled_into_the_archive(tmp_path: Path):
    store, _, saved = _processed_project(tmp_path)

    assert store.is_compressed(saved)
    bundled = store.bundled_products(saved)
    for name in PAYLOADS:
        assert f"quicklooks/{name}" in bundled
    with zipfile.ZipFile(saved) as archive:
        manifest = json.loads(archive.read("manifest.json"))
    assert manifest["flight_id"] == "Flight_2707"
    assert len(manifest["bundled_products"]) == len(PAYLOADS)


def test_bare_archive_handed_over_restores_every_product(tmp_path: Path):
    store, _, saved = _processed_project(tmp_path)
    recipient = tmp_path / "recipient"
    recipient.mkdir()
    shutil.copy2(saved, recipient / "flight_project.ccflux")

    opened = store.open_project(recipient / "flight_project.ccflux")

    # The output root is adopted from where the file now sits, not from the
    # absolute path recorded on the machine that saved it.
    assert opened.project.flight_output_root == recipient / "Flight_2707"
    assert len(opened.restored_products) == len(PAYLOADS)
    for name, expected in PAYLOADS.items():
        restored = opened.project.flight_output_root / "quicklooks" / name
        assert json.loads(restored.read_text(encoding="utf-8")) == expected


def test_reopening_in_place_does_not_overwrite_newer_products(tmp_path: Path):
    store, project, saved = _processed_project(tmp_path)
    live = project.flight_output_root / "quicklooks" / "noseboom_browser.json"
    live.write_text(json.dumps({"available": True, "points": []}), encoding="utf-8")

    store.open_project(saved)

    assert json.loads(live.read_text(encoding="utf-8"))["points"] == []


def test_archive_entries_cannot_escape_the_output_root(tmp_path: Path):
    store, project, saved = _processed_project(tmp_path)
    hostile = tmp_path / "hostile.ccflux"
    with zipfile.ZipFile(saved) as source, zipfile.ZipFile(hostile, "w") as target:
        for item in source.infolist():
            target.writestr(item, source.read(item.filename))
        target.writestr("products/../../escaped.json", '{"escaped": true}')

    restored = store.extract_products(hostile, project)

    assert not (tmp_path / "escaped.json").exists()
    assert all(
        project.flight_output_root in path.parents for path in restored
    )


def test_moved_project_reanchors_its_stored_output_paths(tmp_path: Path):
    store, project, saved = _processed_project(tmp_path)
    moved_root = tmp_path / "elsewhere"
    moved_root.mkdir()
    shutil.copytree(project.flight_output_root, moved_root / "Flight_2707")
    shutil.copy2(saved, moved_root / saved.name)

    reopened = store.load_project(moved_root / "Flight_2707.ccflux")

    assert reopened.flight_output_root == moved_root / "Flight_2707"
    quicklook = reopened.output_locations["noseboom_quicklook"]
    assert quicklook.is_file()
    assert moved_root in quicklook.parents
