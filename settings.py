import sys
from dataclasses import dataclass

from PyQt6.QtCore import QSettings

IS_WINDOWS = sys.platform == "win32"

# Per-platform default capture devices. On Linux these are the well-known V4L2 /
# ALSA addresses. Windows has no stable well-known name (DirectShow devices are
# friendly-name strings), so the default is empty, meaning "auto — use the first
# enumerated device". ui.py treats an empty saved value as "first available" and
# suppresses the missing-device warning for it.
_DEFAULT_VIDEO_DEVICE = "" if IS_WINDOWS else "/dev/video0"
_DEFAULT_AUDIO_DEVICE = "" if IS_WINDOWS else "plughw:Hagibis,0"


def global_qsettings() -> QSettings:
    """The single global settings store, constructed correctly per-platform.

    On Linux the historical NativeFormat store (``~/.config/HagibisMonitor/
    HagibisMonitor.conf``) is kept so existing users' settings never move.
    On Windows NativeFormat is the *registry*, which would make
    ``QSettings.fileName()`` a registry path and break ``_profiles_dir()`` — so
    Windows uses an explicit IniFormat/UserScope store at
    ``%APPDATA%\\HagibisMonitor\\HagibisMonitor.ini``, next to which the
    ``profiles/`` directory can live.

    Note: ``setDefaultFormat(IniFormat)`` is deliberately NOT used — it only
    affects the parent-less constructor, not ``QSettings(org, app)``.
    """
    if IS_WINDOWS:
        return QSettings(QSettings.Format.IniFormat, QSettings.Scope.UserScope,
                         "HagibisMonitor", "HagibisMonitor")
    return QSettings("HagibisMonitor", "HagibisMonitor")


# ── fallback tables used when v4l2-ctl is unavailable ────────────────────────
_DEFAULT_RESOLUTIONS = [
    ("2560×1440", 2560, 1440),  # 16:9
    ("1920×1080", 1920, 1080),  # 16:9
    ("1280×1152", 1280, 1152),  # 10:9
    ("1280×1024", 1280, 1024),  # 5:4
    ("1280×720",  1280,  720),  # 16:9
    ("1024×768",  1024,  768),  # 4:3
    ("800×600",    800,  600),  # 4:3
    ("640×576",    640,  576),  # 10:9
    ("640×480",    640,  480),  # 4:3
]
_DEFAULT_FRAMERATES = [60, 50, 30, 20, 10]
_DEFAULT_FORMATS    = [("MJPEG", "mjpeg"), ("YUYV", "yuyv422")]

_OUTPUT_RESOLUTIONS: dict[str, list[tuple[str, int, int]]] = {
    "16:9":  [("3840×2160", 3840, 2160), ("2560×1440", 2560, 1440),
              ("1920×1080", 1920, 1080), ("1280×720",  1280,  720),
              ("854×480",    854,  480),  ("640×360",    640,  360)],
    "4:3":   [("1600×1200", 1600, 1200), ("1024×768",  1024,  768),
              ("800×600",    800,  600),  ("640×480",    640,  480)],
    "5:4":   [("1280×1024", 1280, 1024), ("960×768",    960,  768)],
    "10:9":  [("1280×1152", 1280, 1152), ("640×576",    640,  576)],
    "21:9":  [("2560×1080", 2560, 1080), ("1720×720",  1720,  720)],
    "1:1":   [("1080×1080", 1080, 1080), ("720×720",    720,  720)],
}

_OUTPUT_PIXEL_FORMATS = [
    ("YUYV (YUY2) — most compatible", "yuyv422"),
    ("NV12 — hardware-friendly",      "nv12"),
    ("RGB24 — raw / lossless",        "rgb24"),
    ("MJPEG — compressed",            "mjpeg"),
]

_OUTPUT_FPS = [60, 30, 25, 24, 15]


# ── settings dataclasses ──────────────────────────────────────────────────────
@dataclass
class OutputSettings:
    """Persisted globally (not per-profile) in HagibisMonitor.ini."""
    enabled:      bool  = False
    device:       str   = ""
    width:        int   = 1920
    height:       int   = 1080
    pixel_format: str   = "yuyv422"
    fps:          int   = 30


@dataclass
class AppSettings:
    scale_mode:    str   = "fit"
    crop_mode:     str   = "full"
    bg_color:      str   = "#1f1f1f"
    # Capture
    video_device:  str   = _DEFAULT_VIDEO_DEVICE
    video_fmt:     str   = "mjpeg"
    video_res:     str   = "1280x720"
    video_fps:     int   = 30
    brightness:    int   = 50
    contrast:      int   = 50
    saturation:    int   = 50
    hue:           int   = 50
    # Audio
    audio_device:  str   = _DEFAULT_AUDIO_DEVICE
    audio_enabled: bool  = True
    mono_mix:      bool  = False
    passthrough:   bool  = False
    volume_db:     int   = 0
    volume_l_db:   int   = 0
    volume_r_db:   int   = 0
    # Output scale / crop / pan / zoom
    output_scale_mode: str   = "fit"
    output_crop_mode:  str   = "full"
    pan_x:             float = 0.0
    pan_y:             float = 0.0
    zoom:              float = 1.0
