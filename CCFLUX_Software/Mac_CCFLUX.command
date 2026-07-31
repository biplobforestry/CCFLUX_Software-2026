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

REQUIRED_PYTHON='3.10'
OFFERED_PYTHON='3.12.7'

find_supported_python() {
  PYTHON_COMMAND=''
  # Look at the versioned interpreters too: a supported Python is often already
  # installed alongside an older default.
  for candidate in python3.13 python3.12 python3.11 python3.10 python3 python; do
    if command -v "$candidate" >/dev/null 2>&1 &&
       "$candidate" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 10) else 1)' >/dev/null 2>&1; then
      PYTHON_COMMAND="$candidate"
      return 0
    fi
  done
  return 1
}

installed_python_version() {
  for candidate in python3 python; do
    if command -v "$candidate" >/dev/null 2>&1; then
      "$candidate" -c 'import sys; print("%d.%d.%d" % sys.version_info[:3])' 2>/dev/null && return 0
    fi
  done
  printf '%s' 'none'
}

offer_python_installation() {
  local found architecture installer url
  found="$(installed_python_version)"
  printf '\n%s\n' '----------------------------------------------------------------'
  if [[ "$found" == 'none' ]]; then
    printf '%s\n' 'Python was not found on this computer.'
  else
    printf '%s\n' "Python $found was found. CC-FLUX requires $REQUIRED_PYTHON or newer."
  fi
  printf '%s\n' "CC-FLUX can download the official Python $OFFERED_PYTHON installer"
  printf '%s\n' 'from python.org and open it for you. Nothing is installed without'
  printf '%s\n' 'your confirmation, and your existing Python is left in place.'
  printf '%s\n' '----------------------------------------------------------------'
  printf '%s' 'Download and open the Python installer now? [y/N] '
  read -r reply
  case "$reply" in
    [Yy]*) ;;
    *)
      fail_and_wait "Python $REQUIRED_PYTHON or newer is required. Install it from https://www.python.org/downloads/ and run this file again."
      ;;
  esac

  architecture="$(uname -m)"
  installer="$SCRIPT_DIR/python-$OFFERED_PYTHON-macos.pkg"
  url="https://www.python.org/ftp/python/$OFFERED_PYTHON/python-$OFFERED_PYTHON-macos11.pkg"
  printf '\n%s\n' "Downloading Python $OFFERED_PYTHON for $architecture..."
  log_event "Downloading Python $OFFERED_PYTHON from $url"
  if ! curl -fSL --retry 2 -o "$installer" "$url"; then
    rm -f "$installer"
    fail_and_wait "The Python installer could not be downloaded. Install Python $REQUIRED_PYTHON or newer from https://www.python.org/downloads/ and run this file again."
  fi

  printf '%s\n' 'Opening the installer. Complete it, then return to this window.'
  log_event 'Opening the downloaded Python installer.'
  open -W "$installer" || fail_and_wait 'The Python installer could not be opened.'
  rm -f "$installer"

  if ! find_supported_python; then
    fail_and_wait "Python $REQUIRED_PYTHON or newer was still not found after installation. Open a new Terminal window and run this file again."
  fi
  printf '%s\n\n' "Python $("$PYTHON_COMMAND" -c 'import sys; print("%d.%d.%d" % sys.version_info[:3])') is now in use."
  log_event "Continuing with $PYTHON_COMMAND after installation."
}

if ! find_supported_python; then
  offer_python_installation
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

if ! "$VENV_PYTHON" -c 'import numpy, pandas, PIL, yaml, scipy, matplotlib, tables, flask, plotly, werkzeug, openpyxl' >/dev/null 2>&1; then
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
