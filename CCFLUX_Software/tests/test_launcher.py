from pathlib import Path


def test_double_click_launcher_prepares_environment_and_starts_dashboard():
    launcher = Path(__file__).resolve().parents[1] / "Start_CCFLUX_Dashboard.bat"
    content = launcher.read_text(encoding="utf-8")

    assert '-m venv ".venv"' in content
    assert '-e ".[noseboom,miro,partector,ins-gimbal,sif,micasense,flir,gopro]"' in content
    assert 'import numpy, pandas, PIL, yaml, scipy, matplotlib, tables, flask, plotly, werkzeug' in content
    pyproject = (launcher.parent / "pyproject.toml").read_text(encoding="utf-8")
    assert '"Flask>=3.0,<4"' in pyproject
    assert '"plotly>=5.20,<7"' in pyproject
    assert '"%DASHBOARD_PYTHON%" -m app.main --port 0' in content
    assert 'start "CC-FLUX Zeppelin Dashboard" /min' not in content
    assert 'Keep this command window open while using CC-FLUX.' in content
    assert 'If startup fails, the error will remain visible here.' in content
    assert 'logs\\launcher.log' in content
    assert 'EnableDelayedExpansion' in content
    assert '[!date! !time!]' in content
    assert '© 2026 Biplob Dey - Forschungszentrum Jülich GmbH' in content
    assert 'set "OMP_NUM_THREADS=1"' in content
    assert 'set "OPENBLAS_NUM_THREADS=1"' in content
    assert 'Python 3.10 or newer is required.' in content
    assert content.count('pause') >= 4


def test_macos_launcher_matches_windows_startup_contract():
    launcher = Path(__file__).resolve().parents[1] / "Start_CCFLUX_Dashboard.sh"
    content = launcher.read_text(encoding="utf-8")

    assert content.startswith("#!/usr/bin/env bash")
    assert 'python3 python' in content
    assert 'VENV_DIRECTORY="$SCRIPT_DIR/.venv"' in content
    assert 'VENV_DIRECTORY="$SCRIPT_DIR/.venv-macos"' in content
    assert '-m venv "$VENV_DIRECTORY"' in content
    assert "-e '.[noseboom,miro,partector,ins-gimbal,sif,micasense,flir,gopro]'" in content
    assert "-m app.main --port 0" in content
    assert "logs/launcher.log" in content
    assert "export OMP_NUM_THREADS=1" in content
    assert "export OPENBLAS_NUM_THREADS=1" in content
    assert "© 2026 Biplob Dey - Forschungszentrum Jülich GmbH" in content
    assert "Keep this Terminal window open while using CC-FLUX." in content


def test_macos_finder_launcher_delegates_to_checked_shell_launcher():
    root = Path(__file__).resolve().parents[1]
    launcher = root / "Start_CCFLUX_Dashboard.command"
    content = launcher.read_text(encoding="utf-8")

    assert content.startswith("#!/usr/bin/env bash")
    assert 'SCRIPT_DIR="$(CDPATH= cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"' in content
    assert 'exec /bin/bash "$SCRIPT_DIR/Start_CCFLUX_Dashboard.sh"' in content
