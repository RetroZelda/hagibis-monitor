# -*- mode: python ; coding: utf-8 -*-
# PyInstaller spec for hagibis-monitor
# Build with:  pyinstaller hagibis-monitor.spec

from PyInstaller.utils.hooks import collect_all

# Collect all PyQt6 plugins (GL, image formats, platform plugins, etc.)
qt_datas, qt_binaries, qt_hidden = collect_all("PyQt6")

a = Analysis(
    ["main.py"],
    pathex=[],
    binaries=qt_binaries,
    datas=qt_datas,
    hiddenimports=qt_hidden + [
        "workers",
        "vu_meter",
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
    name="hagibis-monitor",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=True,   # keep True so errors are visible when launched from a terminal
    disable_windowed_traceback=False,
    target_arch=None,
)
