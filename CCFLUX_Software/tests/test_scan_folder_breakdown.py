"""The scan window must show that every camera instrument is being read.

A camera delivery is 3,651 GoPro frames, 536 MicaSense files and one 36 GB FLIR
export. The window shows a single "current file" line, so for almost the whole
scan it reads a GoPro path and it looks as though FLIR and MicaSense are being
skipped - they are not, but nothing on screen says so.

The per-folder tally makes it visible. It has to add up to the total, because a
count that disagrees with "files scanned" would be worse than none at all.
"""

from pathlib import Path

import pytest

from app.scan_backend import DashboardScanBackend


ROOT = Path("/Volumes/external_HD/Camera_System")


@pytest.mark.parametrize("current_file, expected", [
    (ROOT / "GoPro/DCIM/168GOPRO/GPAA7287.JPG", "GoPro"),
    (ROOT / "MicaSense_Zeppelin/0001SET/000/IMG_0000_1.tif", "MicaSense_Zeppelin"),
    (ROOT / "camera.FLIR_Zeppelin.json", "camera.FLIR_Zeppelin.json"),
])
def test_a_file_is_attributed_to_its_top_level_entry(current_file, expected):
    assert DashboardScanBackend._scan_group_name(ROOT, current_file) == expected


def test_an_update_without_a_file_has_no_group():
    """Those files belong to wherever the walk was, not a bucket of their own."""
    assert DashboardScanBackend._scan_group_name(ROOT, None) is None


def test_a_file_outside_the_root_falls_back_to_its_name():
    name = DashboardScanBackend._scan_group_name(ROOT, Path("/elsewhere/odd.json"))
    assert name == "odd.json"


def _channel(tmp_path):
    return DashboardScanBackend._new_scan_channel("camera", ROOT, running=True)


def _apply(backend, channel, files_scanned, current_file):
    """The attribution the progress handler performs, in isolation."""
    delta = max(0, int(files_scanned) - int(channel["files_scanned"]))
    if delta:
        group = backend._scan_group_name(channel["root"], current_file)
        if group is None:
            group = channel["last_group"] or "(counting)"
        channel["last_group"] = group
        channel["folder_counts"][group] += delta
    channel["files_scanned"] = files_scanned


def test_the_tally_always_equals_the_total(tmp_path):
    backend = DashboardScanBackend(tmp_path)
    channel = _channel(tmp_path)

    steps = [
        (128, ROOT / "GoPro/DCIM/100GOPRO/a.JPG"),
        (300, None),                                    # a fileless update
        (836, ROOT / "MicaSense_Zeppelin/0001SET/b.tif"),
        (837, ROOT / "camera.FLIR_Zeppelin.json"),
        (4188, ROOT / "GoPro/DCIM/168GOPRO/z.JPG"),
    ]
    for scanned, current in steps:
        _apply(backend, channel, scanned, current)
        assert sum(channel["folder_counts"].values()) == channel["files_scanned"]

    assert channel["files_scanned"] == 4188
    assert set(channel["folder_counts"]) == {
        "GoPro", "MicaSense_Zeppelin", "camera.FLIR_Zeppelin.json"
    }
    assert "(counting)" not in channel["folder_counts"]


def test_fileless_updates_do_not_create_a_bucket_of_their_own(tmp_path):
    """They are attributed to the folder the walk was last reading."""
    backend = DashboardScanBackend(tmp_path)
    channel = _channel(tmp_path)

    _apply(backend, channel, 100, ROOT / "MicaSense_Zeppelin/a.tif")
    _apply(backend, channel, 536, None)

    assert channel["folder_counts"]["MicaSense_Zeppelin"] == 536


def test_the_snapshot_orders_the_biggest_first(tmp_path):
    backend = DashboardScanBackend(tmp_path)
    channel = _channel(tmp_path)
    _apply(backend, channel, 1, ROOT / "camera.FLIR_Zeppelin.json")
    _apply(backend, channel, 537, ROOT / "MicaSense_Zeppelin/a.tif")
    _apply(backend, channel, 4188, ROOT / "GoPro/a.JPG")

    counts = backend._scan_channel_snapshot(channel)["folder_counts"]

    assert [entry["name"] for entry in counts] == [
        "GoPro", "MicaSense_Zeppelin", "camera.FLIR_Zeppelin.json"
    ]
    assert sum(entry["files"] for entry in counts) == 4188


def test_a_fresh_scan_starts_from_zero(tmp_path):
    """A second scan must not add to the first one's numbers."""
    first = DashboardScanBackend._new_scan_channel("camera", ROOT, running=True)
    first["folder_counts"]["GoPro"] += 3651
    second = DashboardScanBackend._new_scan_channel("camera", ROOT, running=True)

    assert dict(second["folder_counts"]) == {}
    assert second["last_group"] is None


def test_both_scan_windows_render_the_breakdown():
    assets = Path(__file__).parents[1] / "app" / "assets"
    markup = (assets / "dashboard.html").read_text(encoding="utf-8")
    script = (assets / "dashboard.js").read_text(encoding="utf-8")

    for source in ("flight", "camera"):
        assert f'id="{source}ScanBreakdown"' in markup, f"{source} window has no row"
    assert "folder_counts" in script
    assert "Breakdown" in script
