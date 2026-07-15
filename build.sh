#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")"

VENV=".venv-build"

# ── system dependency check ───────────────────────────────────────────────────
check() { command -v "$1" &>/dev/null || { echo "ERROR: $1 not found. $2"; exit 1; }; }
check python3 "Install Python 3.10+  (sudo apt install python3)"
# Probe the venv module itself — the previous check re-ran `command -v python3`
# and so could never catch a missing python3-venv.
python3 -m venv --help &>/dev/null || {
    echo "ERROR: python3-venv not available. Install it: sudo apt install python3-venv"
    exit 1
}

# ── create / reuse build venv ─────────────────────────────────────────────────
if [[ ! -d "$VENV" ]]; then
    echo "Creating build venv at $VENV ..."
    python3 -m venv "$VENV"
fi

# ── install build deps into venv ──────────────────────────────────────────────
echo "Installing build dependencies..."
"$VENV/bin/pip" install --quiet --upgrade pip
# requirements.txt is the single source of truth for runtime deps; pyinstaller
# is a build-only tool so it stays explicit here.
"$VENV/bin/pip" install --quiet --upgrade pyinstaller -r requirements.txt

# ── build ─────────────────────────────────────────────────────────────────────
echo "Building hagibis-monitor..."
"$VENV/bin/python" -m PyInstaller hagibis-monitor.spec --clean --noconfirm

echo ""
echo "Done. Executable: dist/hagibis-monitor"
echo "Run it with:      ./dist/hagibis-monitor"
echo "To install:       ./install.sh"
