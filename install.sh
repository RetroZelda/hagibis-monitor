#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")"

EXE="dist/hagibis-monitor"
DESKTOP="hagibis-monitor.desktop"

if [[ ! -f "$EXE" ]]; then
    echo "ERROR: $EXE not found. Run ./build.sh first."
    exit 1
fi

# ── choose install scope ──────────────────────────────────────────────────────
if [[ "${1:-}" == "--system" ]]; then
    BIN_DIR="/usr/local/bin"
    APP_DIR="/usr/local/share/applications"
    SUDO="sudo"
else
    BIN_DIR="$HOME/.local/bin"
    APP_DIR="$HOME/.local/share/applications"
    SUDO=""
    echo "Installing for current user only (pass --system for a system-wide install)."
fi

# ── install ───────────────────────────────────────────────────────────────────
$SUDO mkdir -p "$BIN_DIR" "$APP_DIR"
$SUDO install -m 755 "$EXE" "$BIN_DIR/hagibis-monitor"

# Update Exec= path in the desktop file and install it
sed "s|^Exec=.*|Exec=$BIN_DIR/hagibis-monitor|" "$DESKTOP" \
    | $SUDO tee "$APP_DIR/hagibis-monitor.desktop" > /dev/null
$SUDO chmod 644 "$APP_DIR/hagibis-monitor.desktop"

# Refresh application menu if possible
if command -v update-desktop-database &>/dev/null; then
    $SUDO update-desktop-database "$APP_DIR" 2>/dev/null || true
fi

echo ""
echo "Installed:"
echo "  Binary:       $BIN_DIR/hagibis-monitor"
echo "  Desktop entry: $APP_DIR/hagibis-monitor.desktop"
echo ""
echo "You may need to re-login or run:"
echo "  export PATH=\"\$HOME/.local/bin:\$PATH\""
