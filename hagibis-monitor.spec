# -*- mode: python ; coding: utf-8 -*-
# PyInstaller spec for hagibis-monitor
# Build with:  python build.py   (or:  pyinstaller hagibis-monitor.spec)

import sys
from PyInstaller.utils.hooks import collect_all

IS_WINDOWS = sys.platform == "win32"

# Collect all PyQt6 plugins (GL, image formats, platform plugins, etc.)
qt_datas, qt_binaries, qt_hidden = collect_all("PyQt6")

# Platform-specific hidden imports:
#   Linux   — QtDBus backs power.py's screen-wake inhibitor (freedesktop D-Bus).
#   Windows — sounddevice (audio output) + comtypes (UVC image controls) are the
#             Windows-only runtime deps installed via requirements.txt markers.
platform_hidden = (
    ["sounddevice", "comtypes"] if IS_WINDOWS else ["PyQt6.QtDBus"]
)

a = Analysis(
    ["main.py"],
    pathex=[],
    binaries=qt_binaries,
    datas=qt_datas,
    hiddenimports=qt_hidden + platform_hidden + [
        "ui",
        "video",
        "audio",
        "output",
        "power",
        "settings",
        "utils",
        "workers",
        "vu_meter",
        "camera_controls",
        "numpy",
        "numpy.core",
        "numpy.core._multiarray_umath",
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        "tkinter",
        "matplotlib",
        "scipy",
        "PIL",
        "cv2",
    ],
    noarchive=False,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="hagibis-monitor",  # PyInstaller appends .exe on Windows
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    # UPX stays on for Linux (smaller tar.gz, no downside) but is forced off on
    # Windows: UPX-packed Qt DLLs trip antivirus false positives and can corrupt
    # Control-Flow-Guard DLLs. (CI never installs UPX on Windows anyway.)
    upx=not IS_WINDOWS,
    upx_exclude=[],
    runtime_tmpdir=None,
    # Linux keeps console=True so errors are visible when launched from a
    # terminal. Windows uses a windowed build (no console window behind the GUI);
    # runtime errors already surface in-UI via the workers' error signals and the
    # ffmpeg-missing startup dialog.
    console=not IS_WINDOWS,
    disable_windowed_traceback=False,
    target_arch=None,
    # icon= intentionally omitted — no .ico asset exists in the repo yet. Add an
    # .ico plus icon="..." here (Windows) when an art asset is created; deferred.
)
