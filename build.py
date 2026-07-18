#!/usr/bin/env python3
"""Cross-platform build script for hagibis-monitor.

Detects the OS, creates (or reuses) an isolated build venv, installs the build +
runtime dependencies into it, and runs PyInstaller against the shared spec file.
Produces a single-file executable in ``dist/`` — ``hagibis-monitor`` on Linux,
``hagibis-monitor.exe`` on Windows (PyInstaller appends the extension).

Run it directly on either platform:

    python build.py          # Windows
    ./build.sh               # Linux (thin wrapper around this script)
    python3 build.py         # Linux (equivalent)
"""
import subprocess
import sys
from pathlib import Path

IS_WINDOWS = sys.platform == "win32"
ROOT = Path(__file__).resolve().parent
VENV = ROOT / ".venv-build"


def venv_python() -> Path:
    # Windows venvs put the interpreter in Scripts/, POSIX in bin/.
    return VENV / ("Scripts/python.exe" if IS_WINDOWS else "bin/python")


def run(cmd) -> None:
    print("+", " ".join(str(c) for c in cmd))
    subprocess.run([str(c) for c in cmd], check=True, cwd=ROOT)


def main() -> int:
    if sys.version_info < (3, 10):
        print("ERROR: Python 3.10+ is required (found %d.%d)." % sys.version_info[:2])
        return 1

    if not VENV.is_dir():
        print(f"Creating build venv at {VENV.name} ...")
        try:
            # Use the invoking interpreter so we don't depend on a `python3` /
            # `py` launcher being resolvable in a particular way.
            run([sys.executable, "-m", "venv", str(VENV)])
        except subprocess.CalledProcessError:
            if IS_WINDOWS:
                print("ERROR: could not create a venv. Reinstall Python from "
                      "python.org and ensure pip + venv are included.")
            else:
                print("ERROR: python3-venv not available. Install it: "
                      "sudo apt install python3-venv")
            return 1

    py = venv_python()
    print("Installing build dependencies...")
    # Always invoke pip via `-m pip`: on Windows a bare pip.exe cannot upgrade
    # itself while running (the file is locked).
    run([py, "-m", "pip", "install", "--quiet", "--upgrade", "pip"])
    # requirements.txt is the single source of truth for runtime deps; pyinstaller
    # is a build-only tool so it stays explicit here.
    run([py, "-m", "pip", "install", "--quiet", "--upgrade",
         "pyinstaller", "-r", "requirements.txt"])

    print("Building hagibis-monitor...")
    run([py, "-m", "PyInstaller", "hagibis-monitor.spec", "--clean", "--noconfirm"])

    exe = "dist/hagibis-monitor.exe" if IS_WINDOWS else "dist/hagibis-monitor"
    print()
    print(f"Done. Executable: {exe}")
    if not IS_WINDOWS:
        print("Run it with:      ./dist/hagibis-monitor")
        print("To install:       ./install.sh")
    return 0


if __name__ == "__main__":
    sys.exit(main())
