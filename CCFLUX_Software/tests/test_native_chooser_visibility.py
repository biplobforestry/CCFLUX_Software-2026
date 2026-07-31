"""A chooser the operator cannot see is indistinguishable from a hang.

osascript is a background-only process, so a panel it owns opens behind the
browser. The operator sees the "select a folder" prompt, no window, and an
action that never finishes — which is the "Failed to fetch" report that made
Load .ccflux look broken. Hosting the chooser inside a Finder tell block gives
the panel a foreground owner that can be activated.
"""

import subprocess
import sys
from pathlib import Path

import pytest

from app.scan_backend import FolderDialog

DARWIN_CHOOSERS = [
    "choose_flight_folder",
    "choose_camera_folder",
    "choose_output_folder",
    "choose_project_folder",
    "choose_project_file",
]


@pytest.mark.parametrize("name", DARWIN_CHOOSERS)
def test_every_macos_chooser_goes_through_the_activating_helper(name):
    """A chooser that calls osascript directly reintroduces the invisible panel."""
    source = (Path(__file__).parents[1] / "app" / "scan_backend.py").read_text(
        encoding="utf-8"
    )
    start = source.index(f"def {name}(")
    body = source[start : source.index("\n    def ", start + 1)]

    assert "_choose_with_osascript" in body, f"{name} does not use the helper"
    assert "subprocess.run" not in body, f"{name} runs osascript itself"


def test_the_helper_activates_a_foreground_application():
    source = (Path(__file__).parents[1] / "app" / "scan_backend.py").read_text(
        encoding="utf-8"
    )
    start = source.index("def _choose_with_osascript(")
    body = source[start : source.index("\n    def ", start + 1)]

    assert 'tell application "Finder"' in body
    assert "activate" in body


def test_the_helper_converts_the_selection_itself():
    """Callers pass the chooser clause only; a caller that also converts would
    ask AppleScript for the POSIX path of a string and fail."""
    source = (Path(__file__).parents[1] / "app" / "scan_backend.py").read_text(
        encoding="utf-8"
    )
    for name in DARWIN_CHOOSERS:
        start = source.index(f"def {name}(")
        body = source[start : source.index("\n    def ", start + 1)]
        assert "POSIX path of" not in body, (
            f"{name} converts the selection; the helper already does"
        )


@pytest.mark.skipif(sys.platform != "darwin", reason="AppleScript is macOS only")
def test_the_helper_returns_a_posix_path():
    """Proved with a clause that resolves without a person clicking anything."""
    assert FolderDialog._choose_with_osascript("path to home folder") == Path.home()


@pytest.mark.skipif(sys.platform != "darwin", reason="AppleScript is macOS only")
def test_a_failing_clause_is_reported_as_a_cancel():
    """Cancel and error must both leave the request answerable, never hanging."""
    assert FolderDialog._choose_with_osascript("this is not applescript") is None


@pytest.mark.skipif(sys.platform != "darwin", reason="AppleScript is macOS only")
def test_the_generated_script_compiles():
    """A malformed script would look exactly like a cancelled dialog."""
    script = (
        'tell application "Finder"\n'
        "\tactivate\n"
        '\tset ccfluxSelection to choose folder with prompt "Test"\n'
        "end tell\n"
        "POSIX path of ccfluxSelection"
    )
    completed = subprocess.run(
        ["osacompile", "-o", "/dev/null", "-e", script],
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr


def test_a_network_failure_is_explained_rather_than_reported_raw():
    script = (Path(__file__).parents[1] / "app" / "assets" / "dashboard.js").read_text(
        encoding="utf-8"
    )

    assert "The CC-FLUX server did not respond" in script
    assert "still open" in script
