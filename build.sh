#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")"

VENV=".venv-build"

# ── system dependency check ───────────────────────────────────────────────────
check() { command -v "$1" &>/dev/null || { echo "ERROR: $1 not found. $2"; exit 1; }; }
check python3 "Install Python 3.10+  (sudo apt install python3)"
check python3 "Check python3-venv:   sudo apt install python3-venv"

# ── create / reuse build venv ─────────────────────────────────────────────────
if [[ ! -d "$VENV" ]]; then
    echo "Creating build venv at $VENV ..."
    python3 -m venv "$VENV"
fi

# ── install build deps into venv ──────────────────────────────────────────────
echo "Installing build dependencies..."
"$VENV/bin/pip" install --quiet --upgrade pip
"$VENV/bin/pip" install --quiet --upgrade pyinstaller PyQt6 numpy

# ── build ─────────────────────────────────────────────────────────────────────
echo "Building hagibis-monitor..."
"$VENV/bin/python" -m PyInstaller hagibis-monitor.spec --clean --noconfirm

echo ""
echo "Done. Executable: dist/hagibis-monitor"
echo "Run it with:      ./dist/hagibis-monitor"
echo "To install:       ./install.sh"
