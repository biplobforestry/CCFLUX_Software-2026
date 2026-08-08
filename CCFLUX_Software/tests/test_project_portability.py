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

# The point of these paths is that they belong to the machine that wrote the
# project and not to this one. C:\Output\Flight_2707\... was a real tree on an
# operator's own disk, so relocate_product_path rightly returned that file
# untouched and three cases failed on the very platform they describe. The
# folder name below is the fixture's own, and no campaign writes it.
WINDOWS = r"C:\CCFLUX_ForeignProjectFixture\Flight_2707\quicklooks\flir_browser.json"
POSIX = "/home/ops/CCFLUX_ForeignProjectFixture/Flight_2707/quicklooks/flir_browser.json"


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
    """The defect this guards: on POSIX the whole Windows path is `.name`.

    Stated with an explicit POSIX parser rather than with Path, because Path is
    this platform's flavour: on Windows it splits the string correctly and the
    premise read as false, so the case failed on the very platform whose paths
    it is about.
    """
    from pathlib import PurePosixPath

    assert PurePosixPath(WINDOWS).name == WINDOWS

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


# ------------------------------------------------- products read from the file
import json
import zipfile

from core.flight_project import read_bundled_product


def _archive(tmp_path, entries):
    path = tmp_path / "Flight_2707.ccflux"
    with zipfile.ZipFile(path, "w") as archive:
        for name, body in entries.items():
            archive.writestr(name, body)
    return path


def test_a_product_is_read_from_the_archive(tmp_path):
    """Extraction assumes the project's own location is writable and still
    attached. Read-only media, or a volume unplugged afterwards, restores
    nothing - and every workspace then reports that its instrument was never
    processed, with the products inside the file the whole time."""
    project = _archive(tmp_path, {
        "products/quicklooks/flir_browser.json": json.dumps({"available": True}),
    })

    raw = read_bundled_product(project, r"C:\Output\Flight_2707\quicklooks\flir_browser.json")

    assert raw is not None
    assert json.loads(raw)["available"] is True


def test_a_deep_product_is_found_in_the_archive(tmp_path):
    project = _archive(tmp_path, {
        "products/processed/flir/runs/x/level2/summary.json": "{}",
    })

    assert read_bundled_product(
        project, r"D:\out\F\processed\flir\runs\x\level2\summary.json"
    ) == b"{}"


def test_a_product_that_is_not_in_the_archive_is_reported_absent(tmp_path):
    project = _archive(tmp_path, {"products/quicklooks/a.json": "{}"})

    assert read_bundled_product(project, r"C:\o\F\quicklooks\missing.json") is None


@pytest.mark.parametrize("bad", [None, ""])
def test_nothing_to_read_is_harmless(bad, tmp_path):
    project = _archive(tmp_path, {"products/quicklooks/a.json": "{}"})

    assert read_bundled_product(project, bad) is None
    assert read_bundled_product(bad, r"C:\o\F\quicklooks\a.json") is None


def test_a_file_that_is_not_a_project_is_refused(tmp_path):
    plain = tmp_path / "notes.txt"
    plain.write_text("not an archive", encoding="utf-8")

    assert read_bundled_product(plain, r"C:\o\F\quicklooks\a.json") is None


def test_the_extracted_copy_is_preferred_when_it_is_there(tmp_path, output_root):
    """The archive is the fallback, not the first choice: a product rewritten by
    a later run must win over the copy sealed into the project."""
    project = _archive(tmp_path, {
        "products/quicklooks/flir_browser.json": json.dumps({"from": "archive"}),
    })
    newer = output_root / "quicklooks" / "flir_browser.json"
    newer.write_text(json.dumps({"from": "disk"}), encoding="utf-8")

    resolved = relocate_product_path(WINDOWS, output_root)

    assert json.loads(resolved.read_text(encoding="utf-8"))["from"] == "disk"
    assert json.loads(read_bundled_product(project, resolved))["from"] == "archive"


# ------------------------------------------- rescanning when a project is opened
def test_opening_a_project_rescans_its_recorded_folders(tmp_path, monkeypatch):
    """A loaded project restores its products but not its link to the raw files:
    those paths were recorded on the machine that processed them, so the scan
    report is absent and anything reading source data refuses. The Noseboom
    download failed exactly there - "Complete Initial Check and apply the Time
    Filter before downloading" - on a project that had just been loaded."""
    from app.scan_backend import DashboardScanBackend

    flight = tmp_path / "Flight_2707"
    flight.mkdir()
    backend = DashboardScanBackend(tmp_path)

    started = {}
    monkeypatch.setattr(
        backend, "start_scan",
        lambda folder, camera_folder=None, include_camera=True: started.update(
            {"folder": folder, "camera": camera_folder, "include": include_camera}
        ),
    )

    class Project:
        flight_folder_path = flight
        camera_folder_path = None

    result = backend._rescan_after_open(Project())

    assert result["started"] is True
    assert started["folder"] == flight


def test_the_camera_folder_joins_the_rescan_when_it_is_there(tmp_path, monkeypatch):
    from app.scan_backend import DashboardScanBackend

    flight = tmp_path / "Flight"
    camera = tmp_path / "Camera_System"
    flight.mkdir()
    camera.mkdir()
    backend = DashboardScanBackend(tmp_path)
    started = {}
    monkeypatch.setattr(
        backend, "start_scan",
        lambda folder, camera_folder=None, include_camera=True: started.update(
            {"camera": camera_folder, "include": include_camera}
        ),
    )

    class Project:
        flight_folder_path = flight
        camera_folder_path = camera

    result = backend._rescan_after_open(Project())

    assert result["camera_included"] is True
    assert started["camera"] == camera


def test_a_missing_flight_folder_is_named_rather_than_raising(tmp_path):
    """A project opened away from its raw data is still good for looking at
    results, so this reports instead of refusing to open."""
    from app.scan_backend import DashboardScanBackend

    backend = DashboardScanBackend(tmp_path)

    class Project:
        flight_folder_path = Path(r"C:\CCFLUX_2026\Flight_2707")
        camera_folder_path = None

    result = backend._rescan_after_open(Project())

    assert result["started"] is False
    assert "Flight_2707" in result["reason"]
    assert "not on this computer" in result["reason"]


def test_a_camera_folder_that_is_gone_does_not_stop_the_flight_rescan(tmp_path, monkeypatch):
    from app.scan_backend import DashboardScanBackend

    flight = tmp_path / "Flight"
    flight.mkdir()
    backend = DashboardScanBackend(tmp_path)
    monkeypatch.setattr(backend, "start_scan", lambda *a, **k: None)

    class Project:
        flight_folder_path = flight
        camera_folder_path = Path(r"D:\Camera_System")

    result = backend._rescan_after_open(Project())

    assert result["started"] is True
    assert result["camera_included"] is False


def test_a_scan_that_will_not_start_is_reported_not_raised(tmp_path, monkeypatch):
    from app.scan_backend import DashboardScanBackend

    flight = tmp_path / "Flight"
    flight.mkdir()
    backend = DashboardScanBackend(tmp_path)

    def refuse(*args, **kwargs):
        raise RuntimeError("A Flight Folder scan is already running")

    monkeypatch.setattr(backend, "start_scan", refuse)

    class Project:
        flight_folder_path = flight
        camera_folder_path = None

    result = backend._rescan_after_open(Project())

    assert result["started"] is False
    assert "already running" in result["reason"]


def test_the_dashboard_reports_the_rescan():
    dashboard = (
        Path(__file__).resolve().parents[1] / "app" / "assets" / "dashboard.js"
    ).read_text(encoding="utf-8")

    assert "result.auto_rescan" in dashboard
    assert "startPolling()" in dashboard
    assert "openMissingSourcesDialog" in dashboard


def test_an_intact_saved_scan_is_left_alone(tmp_path, monkeypatch):
    """Rescanning a project whose raw files are where it left them can only
    replace good recorded detection with whatever this machine happens to find,
    and it already has a usable scan report."""
    from app.scan_backend import DashboardScanBackend

    backend = DashboardScanBackend(tmp_path)
    called = []
    monkeypatch.setattr(backend, "_rescan_after_open", lambda project: called.append(project))

    source = (Path(__file__).resolve().parents[1] / "app" / "scan_backend.py").read_text(
        encoding="utf-8"
    )

    assert "if opened.reused_saved_scan" in source
    assert '"needed": False' in source


def test_every_rescan_outcome_says_whether_it_was_needed():
    """The dashboard shows a "raw data not found" dialog only for a rescan that
    was needed and could not run; without the flag it would show it for a
    project that is perfectly healthy."""
    source = (Path(__file__).resolve().parents[1] / "app" / "scan_backend.py").read_text(
        encoding="utf-8"
    )
    block = source[source.index("def _rescan_after_open"):]
    block = block[: block.index("\n    def ", 10)]

    assert block.count('"needed": True') == 3, "each outcome must state it"

    dashboard = (
        Path(__file__).resolve().parents[1] / "app" / "assets" / "dashboard.js"
    ).read_text(encoding="utf-8")
    assert "rescan.needed === false" in dashboard


def test_restored_products_survive_a_rescan(tmp_path):
    """A scan re-detects instruments and knows nothing about results, so on its
    own it leaves every workspace looking unprocessed - the state the operator
    opened the project to get out of."""
    from app.scan_backend import DashboardScanBackend

    backend = DashboardScanBackend(tmp_path)
    backend._restored_products = {
        "noseboom": ({"available": True, "points": [1, 2]}, "complete", ["/x/a.csv"]),
    }
    backend._instruments["noseboom"].quicklook = {}

    backend._reapply_restored_products()

    state = backend._instruments["noseboom"]
    assert state.quicklook["available"] is True
    assert state.processing_status == "complete"
    assert state.output_files == ["/x/a.csv"]


def test_a_newer_result_is_never_replaced_by_the_restored_one(tmp_path):
    from app.scan_backend import DashboardScanBackend

    backend = DashboardScanBackend(tmp_path)
    backend._restored_products = {"noseboom": ({"from": "project"}, "complete", [])}
    backend._instruments["noseboom"].quicklook = {"from": "this session"}

    backend._reapply_restored_products()

    assert backend._instruments["noseboom"].quicklook == {"from": "this session"}

