#!/usr/bin/env bash

# CC-FLUX 2026 portable macOS launcher.
# Double-click this file in Finder.

set -u

SCRIPT_DIR="$(CDPATH= cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR" || exit 1

mkdir -p "$SCRIPT_DIR/logs"
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
  printf 'Launcher log: %s\n' "$LAUNCH_LOG"
  log_event "ERROR: $message"
  if [[ -t 0 ]]; then
    printf '\nPress Return to close...'
    read -r _
  fi
  exit 1
}

printf '\n%s\n' '================================================================'
printf '%s\n' 'CC-FLUX 2026 Zeppelin Software'
printf '%s\n' '© 2026 Biplob Dey - Forschungszentrum Jülich GmbH'
printf '%s\n' 'macOS automatic setup and launch'
printf '%s\n' '================================================================'
printf '\n'
log_event 'Mac_CCFLUX.command started.'

PYTHON_COMMAND=''
for candidate in python3 python; do
  if command -v "$candidate" >/dev/null 2>&1 &&
     "$candidate" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 10) else 1)' >/dev/null 2>&1; then
    PYTHON_COMMAND="$candidate"
    break
  fi
done

if [[ -z "$PYTHON_COMMAND" ]]; then
  fail_and_wait 'Python 3.10 or newer was not found. Install Python from https://www.python.org/downloads/ and run this file again.'
fi

VENV_DIRECTORY="$SCRIPT_DIR/.venv-macos"
VENV_PYTHON="$VENV_DIRECTORY/bin/python"

if [[ ! -x "$VENV_PYTHON" ]]; then
  printf '%s\n' 'Creating the private CC-FLUX Python environment...'
  log_event 'Creating .venv-macos.'
  "$PYTHON_COMMAND" -m venv "$VENV_DIRECTORY" ||
    fail_and_wait 'The private Python environment could not be created.'
fi

if ! "$VENV_PYTHON" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 10) else 1)' >/dev/null 2>&1; then
  fail_and_wait 'The private environment uses Python older than 3.10. Remove .venv-macos after installing a newer Python, then try again.'
fi

printf '%s\n' 'Installing and checking all required CC-FLUX libraries...'
log_event 'Dependency installation and validation started.'
"$VENV_PYTHON" -m pip install \
  --disable-pip-version-check \
  --upgrade-strategy only-if-needed \
  -e '.[noseboom,miro,partector,ins-gimbal,sif,micasense,flir,gopro]' ||
  fail_and_wait 'Required libraries could not be installed. Check the internet connection and available disk space.'

if ! "$VENV_PYTHON" -c 'import numpy, pandas, PIL, yaml, scipy, matplotlib, tables, flask, plotly, werkzeug' >/dev/null 2>&1; then
  fail_and_wait 'Library verification failed after installation.'
fi
log_event 'All required libraries are ready.'

export PYTHONUNBUFFERED=1
export OMP_NUM_THREADS=1
export OPENBLAS_NUM_THREADS=1
export MKL_NUM_THREADS=1
export NUMEXPR_NUM_THREADS=1

printf '\n%s\n' 'Starting CC-FLUX and opening the default browser...'
printf '%s\n' 'Keep this Terminal window open while using the software.'
printf '%s\n' 'Press Control-C here to stop CC-FLUX.'
printf '\n'
log_event 'Starting dashboard on an automatically selected free port.'

"$VENV_PYTHON" -m app.main --port 0
DASHBOARD_EXIT=$?

if [[ $DASHBOARD_EXIT -ne 0 ]]; then
  fail_and_wait "CC-FLUX stopped with exit code $DASHBOARD_EXIT."
fi

printf '\n%s\n' 'CC-FLUX has been closed.'
log_event 'CC-FLUX closed normally.'
