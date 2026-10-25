#!/bin/sh
# Thin entry point; do not synchronize the project before install.py runs.
set -eu
if ! command -v uv >/dev/null 2>&1; then
    echo "uv is required. Install uv and reopen the terminal." >&2
    exit 1
fi
exec uv run --isolated --no-project --python 3.12 "$(dirname "$0")/install.py" "$@"
