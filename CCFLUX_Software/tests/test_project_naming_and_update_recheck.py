"""A project file must say which flight it holds, and a check must be forceable.

Several campaign projects end up in one folder, or attached to one message.
When every one of them is called flight_project.ccflux they can only be told
apart by opening them. The update check is answered from a launch-time cache,
so a colleague told "a new version is out" needs a way to ask again without
restarting the software.
"""

from pathlib import Path

import pytest

from app.scan_backend import DashboardScanBackend
from core.flight_project import (
    LEGACY_PROJECT_FILENAME,
    PROJECT_FILENAME,
    PROJECT_SUFFIX,
    project_filename_for,
)
from core.update_check import UpdateStatus


@pytest.mark.parametrize(
    "flight_id, expected",
    [
        ("Flight_2707", "Flight_2707.ccflux"),
        ("Flight_2124", "Flight_2124.ccflux"),
        ("2707", "2707.ccflux"),
        ("Flight 2707", "Flight_2707.ccflux"),        # spaces are not separators
        ("Flight/2707", "Flight_2707.ccflux"),        # never escapes its folder
        ("../secrets", "secrets.ccflux"),             # leading dots stripped too
        ("", "flight_project.ccflux"),                # a blank id still saves
    ],
)
def test_the_file_is_named_after_the_flight(flight_id, expected):
    assert project_filename_for(flight_id) == expected


def test_the_name_can_never_contain_a_path_separator():
    for hostile in ("a/b", "a\\b", "../../etc/passwd", "a\0b"):
        name = project_filename_for(hostile)
        assert "/" not in name and "\\" not in name and "\0" not in name


def _project(tmp_path: Path, flight_id: str = "Flight_2707"):
    from tests.test_flight_project import _project as build

    project, raw = build(tmp_path)
    project.flight_id = flight_id
    return project, raw


def test_saving_uses_the_flight_name(tmp_path: Path):
    from core.flight_project import FlightProjectStore

    project, _ = _project(tmp_path)
    saved = FlightProjectStore().save_project(project)

    assert saved.name == "Flight_2707.ccflux"


def test_an_existing_fixed_name_project_is_renamed_not_duplicated(tmp_path: Path):
    """Two files for one flight, one of them stale, is worse than either name."""
    from core.flight_project import FlightProjectStore

    store = FlightProjectStore()
    project, _ = _project(tmp_path)
    saved = store.save_project(project)
    fixed = saved.with_name(PROJECT_FILENAME)
    saved.replace(fixed)

    again = store.save_project(project, overwrite=True)

    assert again.name == "Flight_2707.ccflux"
    assert not fixed.exists(), "the superseded fixed-name file must be removed"
    assert sorted(p.name for p in again.parent.glob("*.ccflux")) == [
        "Flight_2707.ccflux"
    ]


def test_a_superseded_file_is_only_removed_after_a_good_save(tmp_path: Path):
    """A failed save must never leave the flight with no project at all."""
    from core.flight_project import FlightProjectStore

    store = FlightProjectStore()
    project, _ = _project(tmp_path)
    saved = store.save_project(project)
    fixed = saved.with_name(PROJECT_FILENAME)
    saved.replace(fixed)

    project.flight_id = ""          # rejected by validate, so nothing is written
    with pytest.raises(Exception):
        store.save_project(project, overwrite=True)

    assert fixed.exists(), "the existing project was removed by a failed save"


def test_a_legacy_json_project_is_migrated_rather_than_kept(tmp_path: Path):
    from core.flight_project import FlightProjectStore

    store = FlightProjectStore()
    project, _ = _project(tmp_path)
    saved = store.save_project(project)
    saved.replace(saved.with_name(LEGACY_PROJECT_FILENAME))

    migrated = store.save_project(project, overwrite=True)

    assert migrated.name == "Flight_2707.ccflux"
    assert migrated.suffix == PROJECT_SUFFIX


def test_discovery_finds_flight_named_projects(tmp_path: Path):
    """Discovery matched one fixed filename; a named project must still appear."""
    backend = DashboardScanBackend(tmp_path)
    for name in ("Flight_2707.ccflux", "Flight_2124.ccflux", PROJECT_FILENAME):
        folder = tmp_path / "search" / name.split(".")[0] / "project"
        folder.mkdir(parents=True)
        (folder / name).write_text("{}", encoding="utf-8")

    result = backend.discover_saved_projects(tmp_path / "search")

    # All three are found; none parse, so all three are counted as invalid.
    assert result["invalid_count"] == 3
    assert result["valid_count"] == 0


def test_discovery_ignores_a_legacy_json_beside_a_ccflux(tmp_path: Path):
    """That pair is one project written twice, not two projects."""
    backend = DashboardScanBackend(tmp_path)
    folder = tmp_path / "search" / "Flight_2707" / "project"
    folder.mkdir(parents=True)
    (folder / "Flight_2707.ccflux").write_text("{}", encoding="utf-8")
    (folder / LEGACY_PROJECT_FILENAME).write_text("{}", encoding="utf-8")

    result = backend.discover_saved_projects(tmp_path / "search")

    assert result["invalid_count"] == 1


def test_a_forced_check_contacts_the_server_again(tmp_path, monkeypatch):
    backend = DashboardScanBackend(tmp_path)
    calls = []

    def counting(**kwargs):
        calls.append(1)
        return UpdateStatus(
            current_version="1.0.0", latest_version="1.0.0",
            update_available=False, checked=True,
        )

    monkeypatch.setattr("app.scan_backend.check_for_update", counting)

    backend.update_status()
    backend.update_status()
    assert len(calls) == 1, "the cached answer must serve repeat opens"

    backend.update_status(refresh=True)
    assert len(calls) == 2, "Check Again must not be served from the cache"


def test_the_dialog_offers_a_forced_recheck():
    script = (Path(__file__).parents[1] / "app" / "assets" / "dashboard.js").read_text(
        encoding="utf-8"
    )

    assert "Check Again" in script
    assert "recheckUpdate" in script
    # It must ask the server, not redisplay the launch-time answer.
    assert "'?refresh=1'" in script or '"?refresh=1"' in script
    assert "force: true" in script
    # An unreachable server must be reported, not silently shown as current.
    assert "could not be reached" in script
