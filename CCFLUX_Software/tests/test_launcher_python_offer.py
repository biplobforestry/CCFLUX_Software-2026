"""A colleague with an old Python must be offered a way forward, not a refusal.

These are static checks. The launchers cannot be executed here — the batch
files need cmd.exe, and the macOS path would open a real installer — so the
properties that matter are asserted against the scripts themselves: every jump
resolves, nothing installs without consent, and no path silently continues on
an unsupported interpreter.
"""

import re
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
BATCH_LAUNCHERS = ["Windows_CCFLUX.bat", "Start_CCFLUX_Dashboard.bat"]
SHELL_LAUNCHERS = ["Mac_CCFLUX.command", "Start_CCFLUX_Dashboard.sh"]
ALL_LAUNCHERS = BATCH_LAUNCHERS + SHELL_LAUNCHERS

REQUIRED_PYTHON = "3.10"
OFFERED_PYTHON = "3.12.7"


def read(name):
    return (ROOT / name).read_text(encoding="utf-8")


@pytest.mark.parametrize("name", ALL_LAUNCHERS)
def test_the_offer_is_made_before_giving_up(name):
    content = read(name)

    assert "Download and open the Python installer now? [y/N]" in content, (
        f"{name} does not ask; it only refuses"
    )
    assert OFFERED_PYTHON in content
    assert "Nothing is installed without" in content


@pytest.mark.parametrize("name", ALL_LAUNCHERS)
def test_the_installer_comes_from_python_org_over_https(name):
    content = read(name)

    url = re.search(r"https://www\.python\.org/ftp/python/\S+", content)
    assert url, f"{name} does not download from python.org"
    assert "http://" not in content


@pytest.mark.parametrize("name", ALL_LAUNCHERS)
def test_declining_stops_the_launcher(name):
    """Continuing on an unsupported Python would fail later and less clearly."""
    content = read(name)

    if name in SHELL_LAUNCHERS:
        # The decline branch of the case statement ends in fail_and_wait.
        offer = content[content.index("Download and open the Python installer"):]
        branch = offer[: offer.index("architecture=")]
        assert "fail_and_wait" in branch
    else:
        assert 'if /i not "!INSTALL_PYTHON:~0,1!"=="y" goto :python_missing' in content


@pytest.mark.parametrize("name", ALL_LAUNCHERS)
def test_a_supported_python_is_never_replaced(name):
    """The offer must be skipped entirely when a usable interpreter exists."""
    content = read(name)

    if name in SHELL_LAUNCHERS:
        assert "if ! find_supported_python; then\n  offer_python_installation\nfi" in content
    else:
        assert "call :detect_python\nif defined PYTHON_COMMAND goto :python_ready" in content


@pytest.mark.parametrize("name", BATCH_LAUNCHERS)
def test_every_batch_jump_has_a_destination(name):
    content = read(name)

    labels = set(re.findall(r"^:([a-zA-Z_][\w]*)", content, re.MULTILINE))
    targets = set(re.findall(r"goto :([a-zA-Z_][\w]*)", content))
    called = set(re.findall(r"call :([a-zA-Z_][\w]*)", content))

    assert (targets | called) - labels - {"eof"} == set(), (
        f"{name} jumps to a label that does not exist"
    )


@pytest.mark.parametrize("name", BATCH_LAUNCHERS)
def test_the_normal_run_cannot_fall_into_a_subroutine(name):
    """:detect_python sits in the error block; reaching it by fall-through
    would re-run detection after the dashboard exits."""
    content = read(name)
    lines = content.splitlines()

    detect = next(i for i, line in enumerate(lines) if line.strip() == ":detect_python")
    preceding = [line.strip() for line in lines[:detect] if line.strip()]
    assert preceding[-1].startswith("exit /b"), (
        f"{name} can fall through into :detect_python"
    )


@pytest.mark.parametrize("name", BATCH_LAUNCHERS)
def test_the_subroutine_only_accepts_a_supported_interpreter(name):
    """Detection that ignores the version defeats the whole point."""
    content = read(name)
    subroutine = content[content.index("\n:detect_python"):]
    subroutine = subroutine[: subroutine.index("goto :eof")]

    assert subroutine.count("sys.version_info >= (3, 10)") == 2, (
        "both py -3 and python must be version-checked before being accepted"
    )


@pytest.mark.parametrize("name", SHELL_LAUNCHERS)
def test_shell_launchers_are_syntactically_valid(name):
    result = subprocess.run(
        ["bash", "-n", str(ROOT / name)], capture_output=True, text=True
    )
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize("name", SHELL_LAUNCHERS)
def test_versioned_interpreters_are_searched_before_offering(name):
    """A supported Python is often installed beside an older default python3."""
    content = read(name)

    assert "python3.13 python3.12 python3.11 python3.10 python3 python" in content


@pytest.mark.parametrize("name", SHELL_LAUNCHERS)
def test_the_downloaded_installer_is_removed_again(name):
    content = read(name)

    assert content.count('rm -f "$installer"') >= 2


def test_the_distribution_readme_describes_the_offer():
    readme = read("DISTRIBUTION_README.txt")

    assert "offers to install a supported release" in readme
    assert "Nothing is installed without" in readme
    assert "portable colleague-distribution package" not in readme


def test_the_manual_describes_the_offer():
    manual = read("manual.text")

    assert f"Python {REQUIRED_PYTHON} or newer is required" in manual
    assert "offers to download" in manual or "offers to install" in manual
