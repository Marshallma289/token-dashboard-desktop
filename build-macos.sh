#!/usr/bin/env sh
set -eu
script_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
python_command=${CODEX_DASHBOARD_PYTHON:-python3}
exec "$python_command" "$script_dir/build_desktop.py" "$@"
