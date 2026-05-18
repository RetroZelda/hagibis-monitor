#!/usr/bin/env bash

# This was the original script I made in order to see and hear the output.  
# Its only in this repo for reference and backup and generally shouldnt need to be used

set -euo pipefail

# ==================== CONFIGURATION ====================
NATIVE_WIDTH=1440
NATIVE_HEIGHT=1080
#NATIVE_WIDTH=2560
#NATIVE_HEIGHT=1440
FRAMERATE=60

# ---------- COLOR TWEAKS ----------
BRIGHTNESS=0.05
CONTRAST=1.1
SATURATION=1.15
HUE=0.0
# ======================================================

# --- Auto-detect Hagibis/MacroSilicon video device ---
detect_hagibis_video() {
    for dev in /dev/video*; do
        [ -c "$dev" ] || continue
        if udevadm info --name="$dev" | grep -iqE "hagibis|macrosilicon|ms21|2130|534d:2109|345f:2130"; then
            echo "$dev"
            return 0
        fi
    done
    return 1
}

VIDEO_DEVICE=$(detect_hagibis_video) || {
    echo "ERROR: Hagibis/MacroSilicon capture card not found!" >&2
    echo "Available video devices:" >&2
    ls -l /dev/video* 2>/dev/null || echo "None"
    exit 1
}

# --- Better audio source detection (MS2130 appears as USB Audio) ---
AUDIO_SOURCE=$(pactl list short sources 2>/dev/null | \
    grep -iE 'hagibis|macrosilicon|2130|usb.*audio|hdmi|capture' | \
    head -n1 | awk '{print $2}' || true)

# If nothing was found, you can still force the default source (or leave empty)
if [ -z "$AUDIO_SOURCE" ]; then
    echo "Warning: Could not auto-detect audio source."
    echo "         Run: pactl list short sources"
    echo "         and set AUDIO_SOURCE= manually if needed."
    AUDIO_PIPE=""
else
    AUDIO_PIPE="pulsesrc device=\"${AUDIO_SOURCE}\" ! audioconvert ! audioresample ! queue ! autoaudiosink"
    echo "Audio source : ${AUDIO_SOURCE}"
fi

echo "=== Hagibis Capture (Resizable + 16:9 Locked + AUDIO) ==="
echo "Video device : $VIDEO_DEVICE"
echo "Resolution   : ${NATIVE_WIDTH}x${NATIVE_HEIGHT}@${FRAMERATE}"
echo "Color tweaks : brightness=$BRIGHTNESS contrast=$CONTRAST saturation=$SATURATION"
echo "→ Window resizable, aspect ratio locked with black bars"
echo "Press Ctrl+C to stop"
echo

# Build and run the full pipeline (video + optional audio)
exec gst-launch-1.0 -e \
    v4l2src device="$VIDEO_DEVICE" ! \
        videoconvert ! \
        videobalance \
            brightness=$BRIGHTNESS \
            contrast=$CONTRAST \
            saturation=$SATURATION \
            hue=$HUE ! \
        videoscale add-borders=true ! \
        video/x-raw,width=${NATIVE_WIDTH},height=${NATIVE_HEIGHT} ! \
        glimagesink sync=false force-aspect-ratio=true \
    $AUDIO_PIPE
