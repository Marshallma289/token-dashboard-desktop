#!/bin/sh
set -eu
cd "$(dirname "$0")"
exec "${CODEX_DASHBOARD_PYTHON:-python3}" desktop.py "$@"
