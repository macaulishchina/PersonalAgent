#!/bin/sh
# Explicit, pinned external runtime installation. Application startup never installs packages.
set -eu
npm install --global @earendil-works/pi-coding-agent@1.4.0
pi --version
