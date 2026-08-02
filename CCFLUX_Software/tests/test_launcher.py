"""The two shipped launchers must each prepare an environment and start the app.

There were once five launcher files for two platforms, building three
differently-named environments. Double-clicking the wrong one built a second
complete environment beside the first, so updates landed somewhere other than
where the software was being started from -- indistinguishable, from the
outside, from an update that simply did not work. Only the two files the manual
documents remain, and these assertions moved onto them.
"""

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_windows_launcher_prepares_environment_and_starts_dashboard():
    content = (ROOT / "Windows_CCFLUX.bat").read_text(encoding="utf-8")

    assert '-m venv ".venv-windows"' in content
    assert '-e ".[noseboom,miro,partector,ins-gimbal,sif,micasense,flir,gopro]"' in content
    assert (
        "import numpy, pandas, PIL, yaml, scipy, matplotlib, tables, flask, plotly, werkzeug"
        in content
    )
    pyproject = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    assert '"Flask>=3.0,<4"' in pyproject
    assert '"plotly>=5.20,<7"' in pyproject
    assert '"%DASHBOARD_PYTHON%" -m app.main --port 0' in content
    assert 'start "CC-FLUX Zeppelin Dashboard" /min' not in content
    assert "Keep this command window open while using the software." in content
    assert "logs\\launcher.log" in content
    assert "EnableDelayedExpansion" in content
    assert "[!date! !time!]" in content
    assert "© 2026 Biplob Dey - Forschungszentrum Jülich GmbH" in content
    assert 'set "OMP_NUM_THREADS=1"' in content
    assert 'set "OPENBLAS_NUM_THREADS=1"' in content
    assert "Python 3.10 or newer is required." in content
    # Every failure path has to hold the window open, or the operator sees a
    # console flash and no message at all.
    assert content.count("pause") >= 4


def test_macos_launcher_matches_the_windows_startup_contract():
    content = (ROOT / "Mac_CCFLUX.command").read_text(encoding="utf-8")

    assert content.startswith("#!/usr/bin/env bash")
    assert "python3.13 python3.12 python3.11 python3.10 python3 python" in content
    assert 'VENV_DIRECTORY="$SCRIPT_DIR/.venv-macos"' in content
    assert '-m venv "$VENV_DIRECTORY"' in content
    assert "-e '.[noseboom,miro,partector,ins-gimbal,sif,micasense,flir,gopro]'" in content
    assert "-m app.main --port 0" in content
    assert "logs/launcher.log" in content
    assert "export OMP_NUM_THREADS=1" in content
    assert "export OPENBLAS_NUM_THREADS=1" in content
    assert "© 2026 Biplob Dey - Forschungszentrum Jülich GmbH" in content
    assert "Keep this Terminal window open while using the software." in content


def test_the_two_launchers_do_not_share_an_environment():
    """Separate names are what let one machine hold both without collision."""
    windows = (ROOT / "Windows_CCFLUX.bat").read_text(encoding="utf-8")
    macos = (ROOT / "Mac_CCFLUX.command").read_text(encoding="utf-8")

    assert ".venv-windows" in windows and ".venv-macos" not in windows
    assert ".venv-macos" in macos and ".venv-windows" not in macos


def test_no_superseded_launcher_returns():
    """Restoring one would resurrect the duplicate-environment trap, and the
    documentation no longer explains which of a pair to run."""
    shipped = {path.name for path in ROOT.iterdir() if path.is_file()}
    launchers = {name for name in shipped if name.endswith((".bat", ".command", ".sh"))}

    assert launchers == {"Windows_CCFLUX.bat", "Mac_CCFLUX.command"}
