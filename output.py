import glob
import platform
import subprocess
import time
from pathlib import Path

from PyQt6.QtCore import QThread, pyqtSignal

from utils import _sbin


def _find_loopback_devices() -> list[str]:
    """Return /dev/videoN paths that belong to a v4l2loopback device."""
    devs = []
    for sys_path in sorted(glob.glob("/sys/class/video4linux/video*")):
        try:
            real = Path(sys_path).resolve()
            # v4l2loopback exposes a max_openers attribute; real cameras never do
            if (real / "max_openers").exists():
                devs.append(f"/dev/{Path(sys_path).name}")
                continue
            # Fallback: platform path or device name contains a recognisable keyword
            if "v4l2loopback" in str(real):
                devs.append(f"/dev/{Path(sys_path).name}")
                continue
            name = (real / "name").read_text().strip().lower()
            if "dummy" in name or "loopback" in name:
                devs.append(f"/dev/{Path(sys_path).name}")
        except OSError:
            pass
    return devs


def _v4l2loopback_installed() -> bool:
    # Check for the .ko file under the running kernel — no subprocess needed.
    kernel = platform.uname().release
    if glob.glob(f"/lib/modules/{kernel}/**/v4l2loopback.ko*", recursive=True):
        return True
    # Also true if the module is already loaded.
    return Path("/sys/module/v4l2loopback").exists()


def _load_v4l2loopback() -> tuple[str, bool]:
    """Return (device_path, loaded_by_us). Empty string on failure."""
    existing = _find_loopback_devices()
    if existing:
        return existing[0], False
    modprobe = _sbin("modprobe")
    for prefix in ([], ["pkexec"]):
        cmd = prefix + [
            modprobe, "v4l2loopback",
            "devices=1", "video_nr=10",
            "card_label=HagibisMonitor",
        ]
        try:
            r = subprocess.run(cmd, capture_output=True, timeout=15)
            if r.returncode == 0:
                time.sleep(0.3)
                devs = _find_loopback_devices()
                return (devs[0], True) if devs else ("", True)
        except (FileNotFoundError, subprocess.TimeoutExpired):
            continue
    return "", False


def _unload_v4l2loopback():
    modprobe = _sbin("modprobe")
    for prefix in ([], ["pkexec"]):
        cmd = prefix + [modprobe, "-r", "v4l2loopback"]
        try:
            if subprocess.run(cmd, capture_output=True, timeout=10).returncode == 0:
                return
        except (FileNotFoundError, subprocess.TimeoutExpired):
            continue


# ── modprobe worker ───────────────────────────────────────────────────────────
class _ModprobeWorker(QThread):
    done = pyqtSignal(str, bool)  # (device_path_or_empty, loaded_by_us)

    def __init__(self):
        super().__init__()

    def run(self):
        dev, by_us = _load_v4l2loopback()
        self.done.emit(dev, by_us)
