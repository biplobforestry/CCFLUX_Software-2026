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
    assert FolderDialog()._choose_with_osascript("path to home folder") == Path.home()


@pytest.mark.skipif(sys.platform != "darwin", reason="AppleScript is macOS only")
def test_a_cancel_is_a_cancel_and_a_failure_is_reported():
    """These used to be the same thing.

    Every non-zero exit became "cancelled", so a machine that refused the
    Automation permission looked exactly like an operator changing their mind:
    folder selection did nothing, with no way to find out why.
    """
    dialog = FolderDialog()

    # AppleScript reports a cancel as error -128. Nothing is raised.
    assert dialog._run_chooser_script("error number -128") == (None, None)

    # Anything else is a failure with a reason attached.
    selection, failure = dialog._run_chooser_script("this is not applescript")
    assert selection is None
    assert failure, "a real error must not be reported as a cancel"

    # And a clause that cannot work anywhere is raised, not swallowed.
    with pytest.raises(RuntimeError, match="could not be opened"):
        dialog._choose_with_osascript("this is not applescript")


@pytest.mark.skipif(sys.platform != "darwin", reason="AppleScript is macOS only")
def test_the_finder_route_falls_back_when_it_cannot_be_used():
    """Hosting in Finder costs an Automation permission a machine can refuse.
    A window behind the browser beats no window at all."""
    dialog = FolderDialog()
    attempts = []
    real = dialog._run_chooser_script

    def recording(script):
        attempts.append(script)
        if 'tell application "Finder"' in script:
            return None, "Not authorised to send Apple events to Finder."
        return real(script)

    dialog._run_chooser_script = recording
    assert dialog._choose_with_osascript("path to home folder") == Path.home()
    assert len(attempts) == 2, "the plain chooser was never tried"
    assert 'tell application "Finder"' not in attempts[1]


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


# --------------------------------------------------------------------------
# A typed path, because the native window cannot be relied on to be visible
# --------------------------------------------------------------------------
def test_a_typed_path_selects_without_any_window(tmp_path):
    """Measured on macOS: the same request from a launcher-started server
    sometimes leaves the Finder window behind the browser and sometimes brings
    it forward. Focus is the operating system's decision, so selection cannot
    depend on it."""
    from app.scan_backend import DashboardScanBackend

    class _NeverCalled:
        def choose_flight_folder(self):
            raise AssertionError("the native window must not be opened")
        def choose_camera_folder(self): return self.choose_flight_folder()
        def choose_output_folder(self): return self.choose_flight_folder()

    backend = DashboardScanBackend(tmp_path, folder_dialog=_NeverCalled())
    flight = tmp_path / "flight"; flight.mkdir()

    result = backend.select_folders(flight)

    assert result["cancelled"] is False
    assert result["folder"] == str(flight.resolve())


def test_a_typed_path_is_checked(tmp_path):
    from app.scan_backend import DashboardScanBackend

    backend = DashboardScanBackend(tmp_path)

    with pytest.raises(ValueError, match="No such folder"):
        backend.select_folders(tmp_path / "absent")


def test_a_home_relative_path_is_expanded(tmp_path):
    from app.scan_backend import DashboardScanBackend

    backend = DashboardScanBackend(tmp_path)

    assert backend._typed_folder("flight-folder", "~") == Path.home().resolve()


def test_every_folder_button_offers_both(tmp_path):
    """The path box has to be on all three, or one of them is still a dead end."""
    script = (Path(__file__).parents[1] / "app" / "assets" / "dashboard.js").read_text(
        encoding="utf-8"
    )

    assert script.count("chooseFolder(") >= 4      # the helper plus three callers
    for endpoint in ("/api/select-scan-folders", "/api/select-camera-folder",
                     "/api/select-output-folder"):
        assert f"chooseFolder('{endpoint}'" in script, endpoint
    assert "It can appear behind this" in script, "the operator must be told where it is"
