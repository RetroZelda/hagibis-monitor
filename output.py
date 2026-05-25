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


def _hagibis_exclusive_caps_ok() -> bool:
    """Return True if the HagibisMonitor device (video_nr=10) has exclusive_caps=1."""
    try:
        nr_list = Path("/sys/module/v4l2loopback/parameters/video_nr").read_text().strip().split(",")
        ex_list = Path("/sys/module/v4l2loopback/parameters/exclusive_caps").read_text().strip().split(",")
        for i, nr in enumerate(nr_list):
            if nr.strip() == "10" and i < len(ex_list):
                return ex_list[i].strip().upper() in ("Y", "1", "TRUE")
    except OSError:
        pass
    return False


def _load_v4l2loopback() -> tuple[str, bool]:
    """Return (device_path, loaded_by_us). Empty string on failure."""
    modprobe = _sbin("modprobe")
    existing = _find_loopback_devices()
    if existing:
        if _hagibis_exclusive_caps_ok():
            return existing[0], False
        # Device exists but without exclusive_caps=1; unload and reload.
        unloaded = False
        for prefix in ([], ["pkexec"]):
            try:
                if subprocess.run(prefix + [modprobe, "-r", "v4l2loopback"],
                                  capture_output=True, timeout=10).returncode == 0:
                    unloaded = True
                    break
            except (FileNotFoundError, subprocess.TimeoutExpired):
                continue
        if not unloaded:
            return existing[0], False  # device in use or no perms; use as-is
        time.sleep(0.2)
    for prefix in ([], ["pkexec"]):
        cmd = prefix + [
            modprobe, "v4l2loopback",
            "devices=1", "video_nr=10",
            "card_label=HagibisMonitor",
            "exclusive_caps=1",
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


def _unload_v4l2loopback(silent: bool = False):
    """Unload the v4l2loopback module. If silent=True, skip pkexec (no UI prompts)."""
    modprobe = _sbin("modprobe")
    prefixes: tuple = ([],) if silent else ([], ["pkexec"])
    for prefix in prefixes:
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
