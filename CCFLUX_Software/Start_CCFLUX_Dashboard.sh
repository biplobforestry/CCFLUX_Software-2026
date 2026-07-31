#!/usr/bin/env bash

# CC-FLUX 2026 Zeppelin Dashboard launcher for macOS.
# Run with: bash Start_CCFLUX_Dashboard.sh

set -u

SCRIPT_DIR="$(CDPATH= cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR" || exit 1

mkdir -p logs
LAUNCH_LOG="$SCRIPT_DIR/logs/launcher.log"

timestamp() {
  date '+%Y-%m-%d %H:%M:%S'
}

log_event() {
  printf '[%s] %s\n' "$(timestamp)" "$1" >> "$LAUNCH_LOG"
}

fail_and_wait() {
  local message="$1"
  printf '\nERROR: %s\n' "$message"
  printf 'Review the messages above and:\n%s\n' "$LAUNCH_LOG"
  log_event "ERROR: $message"
  if [[ -t 0 ]]; then
    printf '\nPress Return to close...'
    read -r _
  fi
  exit 1
}

printf '\n'
printf '%s\n' '================================================================'
printf '%s\n' 'CC-FLUX 2026 Zeppelin Dashboard'
printf '%s\n' '© 2026 Biplob Dey - Forschungszentrum Jülich GmbH'
printf '%s\n' 'macOS launcher - automatic environment check and browser startup'
printf '%s\n' '================================================================'
printf '\n'
log_event 'macOS launcher started.'

PYTHON_COMMAND=''
for candidate in python3 python; do
  if command -v "$candidate" >/dev/null 2>&1; then
    if "$candidate" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 10) else 1)' >/dev/null 2>&1; then
      PYTHON_COMMAND="$candidate"
      break
    fi
  fi
done

if [[ -z "$PYTHON_COMMAND" ]]; then
  fail_and_wait 'Python 3.10 or newer was not found. Install it from python.org or Homebrew, then run this launcher again.'
fi

VENV_DIRECTORY="$SCRIPT_DIR/.venv"
# A project copied from Windows may already contain .venv/Scripts but no
# executable macOS interpreter. Keep both environments intact and portable.
if [[ -d "$VENV_DIRECTORY/Scripts" && ! -x "$VENV_DIRECTORY/bin/python" ]]; then
  VENV_DIRECTORY="$SCRIPT_DIR/.venv-macos"
  log_event 'Windows-style .venv detected; using separate .venv-macos.'
fi
VENV_PYTHON="$VENV_DIRECTORY/bin/python"
if [[ ! -x "$VENV_PYTHON" ]]; then
  printf '%s\n' 'Creating the private CC-FLUX Python environment...'
  log_event 'Creating .venv for macOS.'
  "$PYTHON_COMMAND" -m venv "$VENV_DIRECTORY" ||
    fail_and_wait 'The private Python environment could not be created.'
fi

if ! "$VENV_PYTHON" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 10) else 1)' >/dev/null 2>&1; then
  fail_and_wait 'The private environment uses Python older than 3.10. Remove .venv and run this launcher again after installing a newer Python.'
fi

printf '%s\n' 'Checking dashboard libraries...'
if ! "$VENV_PYTHON" -c 'import numpy, pandas, PIL, yaml, scipy, matplotlib, tables, flask, plotly, werkzeug' >/dev/null 2>&1; then
  printf '%s\n' 'One or more required libraries are missing. Installing them now...'
  log_event 'Missing dependencies detected; installation started.'
  "$VENV_PYTHON" -m pip install --disable-pip-version-check --upgrade pip ||
    fail_and_wait 'pip could not be updated.'
  "$VENV_PYTHON" -m pip install \
    --disable-pip-version-check \
    --upgrade-strategy only-if-needed \
    -e '.[noseboom,miro,partector,ins-gimbal,sif,micasense,flir,gopro]' ||
    fail_and_wait 'The dashboard libraries could not be installed.'
  log_event 'Dependency installation completed.'
else
  printf '%s\n' 'Required libraries are ready.'
fi

# Keep numerical backends from claiming every CPU core. The dashboard still
# uses its operator-selected processing allocation.
export PYTHONUNBUFFERED=1
export OMP_NUM_THREADS=1
export OPENBLAS_NUM_THREADS=1
export MKL_NUM_THREADS=1
export NUMEXPR_NUM_THREADS=1

printf '\n'
printf '%s\n' 'Starting the dashboard and opening the default browser...'
printf '%s\n' 'Keep this Terminal window open while using CC-FLUX.'
printf '%s\n' 'Press Control-C here to stop the dashboard.'
printf '%s\n' 'CPU-intensive numerical libraries are limited to one thread per job'
printf '%s\n' 'so the computer remains responsive during scanning and processing.'
printf '\n'
log_event 'Starting dashboard server with an automatically selected free port.'

"$VENV_PYTHON" -m app.main --port 0
DASHBOARD_EXIT=$?

if [[ $DASHBOARD_EXIT -ne 0 ]]; then
  fail_and_wait "The dashboard stopped during startup with exit code $DASHBOARD_EXIT."
fi

printf '\n%s\n' 'The CC-FLUX Dashboard has been closed.'
log_event 'Dashboard closed normally.'
