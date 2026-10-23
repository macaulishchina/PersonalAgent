#!/bin/sh
# Explicit installation from the reviewed transitive lock. Startup never installs packages.
set -eu
source_dir=$(CDPATH= cd -- "$(dirname -- "$0")/../deploy/pi" && pwd)
runtime_dir=${1:-${XDG_DATA_HOME:-"$HOME/.local/share"}/personal-agent/pi}
mkdir -p "$runtime_dir"
cp "$source_dir/package.json" "$source_dir/package-lock.json" "$runtime_dir/"
npm --prefix "$runtime_dir" ci --no-audit --no-fund
"$runtime_dir/node_modules/.bin/pi" --version
printf 'Set pi_binary to %s\n' "$runtime_dir/node_modules/.bin/pi"
