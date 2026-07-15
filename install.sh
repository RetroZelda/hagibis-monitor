#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")"

DESKTOP="hagibis-monitor.desktop"

# Local build puts the binary in dist/; the release tarball puts it at the root.
EXE="dist/hagibis-monitor"
[[ -f "$EXE" ]] || EXE="./hagibis-monitor"

if [[ ! -f "$EXE" ]]; then
    echo "ERROR: hagibis-monitor binary not found (looked in dist/ and ./)."
    echo "       Run ./build.sh first, or extract the release tarball here."
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

# Update Exec= path in the desktop file and install it. Build the Exec line
# without sed (so a BIN_DIR with & or | can't corrupt the replacement) and
# quote the path per the Desktop Entry spec so paths with spaces still launch.
{
    grep -v '^Exec=' "$DESKTOP"
    printf 'Exec="%s/hagibis-monitor"\n' "$BIN_DIR"
} | $SUDO tee "$APP_DIR/hagibis-monitor.desktop" > /dev/null
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
