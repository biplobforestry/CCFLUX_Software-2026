#!/usr/bin/env bash

# Double-clickable macOS entry point. The implementation stays in the .sh file
# so Terminal and Finder launches always use the same checked workflow.

SCRIPT_DIR="$(CDPATH= cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
exec /bin/bash "$SCRIPT_DIR/Start_CCFLUX_Dashboard.sh"
