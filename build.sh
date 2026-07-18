#!/usr/bin/env bash
# Thin wrapper — all build logic lives in the cross-platform build.py so Linux
# and Windows share one code path. Run `./build.sh` (Linux) or `python build.py`
# (either platform) directly.
set -euo pipefail
cd "$(dirname "$0")"
exec python3 build.py
