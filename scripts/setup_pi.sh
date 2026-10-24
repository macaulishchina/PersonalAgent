#!/bin/sh
# Thin Linux entry; installation logic is shared with native Windows.
set -eu
exec python3 "$(dirname "$0")/setup_pi.py" "$@"
