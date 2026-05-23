import re
import shutil
from math import gcd
from pathlib import Path

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import QHBoxLayout, QLabel, QSlider


def _dev_key(path: str) -> str:
    return re.sub(r"[^A-Za-z0-9]", "_", path)


def _aspect_label(w: int, h: int) -> str:
    g = gcd(w, h)
    aw, ah = w // g, h // g
    return "16:10" if (aw, ah) == (8, 5) else f"{aw}:{ah}"


def _sbin(cmd: str) -> str:
    """Resolve a command that may live in /usr/sbin even when PATH is minimal."""
    found = shutil.which(cmd)
    if found:
        return found
    for prefix in ("/usr/sbin", "/sbin"):
        full = f"{prefix}/{cmd}"
        if Path(full).exists():
            return full
    return cmd


def _slider_row(lo: int, hi: int, val: int, on_change) -> tuple[QHBoxLayout, QSlider, QLabel]:
    slider = QSlider(Qt.Orientation.Horizontal)
    slider.setRange(lo, hi)
    slider.setValue(val)
    lbl = QLabel(str(val))
    lbl.setFixedWidth(30)
    lbl.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
    slider.valueChanged.connect(lambda v: (lbl.setText(str(v)), on_change(v)))
    row = QHBoxLayout()
    row.addWidget(slider)
    row.addWidget(lbl)
    return row, slider, lbl


def _db_label(v: int) -> str:
    return "0 dB" if v == 0 else f"{v:+d} dB"
