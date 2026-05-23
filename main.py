import glob
import re
import sys
import subprocess
from dataclasses import dataclass
from math import gcd
from pathlib import Path

from PyQt6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QHBoxLayout, QVBoxLayout,
    QLabel, QSlider, QComboBox, QCheckBox, QPushButton, QGroupBox,
    QTabWidget, QTabBar, QScrollArea, QSizePolicy, QFrame,
    QColorDialog, QInputDialog, QMessageBox, QLineEdit,
)
from PyQt6.QtCore import Qt, QPoint, QRect, QSettings, QTimer
from PyQt6.QtGui import QImage, QPixmap, QPainter, QColor, QFont, QPalette

from workers import VideoWorker, AudioWorker
from vu_meter import StereoVuMeter

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


# ── settings dataclass ────────────────────────────────────────────────────────
@dataclass
class AppSettings:
    scale_mode:    str   = "fit"
    crop_mode:     str   = "full"
    bg_color:      str   = "#1f1f1f"
    # Capture
    video_device:  str   = "/dev/video0"
    video_fmt:     str   = "mjpeg"
    video_res:     str   = "1280x720"
    video_fps:     int   = 30
    brightness:    int   = 50
    contrast:      int   = 50
    saturation:    int   = 50
    hue:           int   = 50
    # Audio
    audio_device:  str   = "plughw:Hagibis,0"
    audio_enabled: bool  = True
    mono_mix:      bool  = False
    passthrough:   bool  = False
    volume_db:     int   = 0
    volume_l_db:   int   = 0
    volume_r_db:   int   = 0
    # Output
    output_enabled: bool = False


# ── module-level helpers ──────────────────────────────────────────────────────
def _dev_key(path: str) -> str:
    return re.sub(r"[^A-Za-z0-9]", "_", path)


def _aspect_label(w: int, h: int) -> str:
    g = gcd(w, h)
    aw, ah = w // g, h // g
    return "16:10" if (aw, ah) == (8, 5) else f"{aw}:{ah}"


def _scan_video_devices() -> list[tuple[str, str]]:
    try:
        out = subprocess.check_output(
            ["v4l2-ctl", "--list-devices"], stderr=subprocess.DEVNULL, timeout=2,
        ).decode(errors="replace")
        devices, name = [], ""
        for line in out.splitlines():
            if line and not line[0].isspace():
                name = line.split("(")[0].strip().rstrip(":")
            elif line.strip().startswith("/dev/video"):
                path = line.strip()
                devices.append((f"{name}  ({path})", path))
        return devices or [("/dev/video0", "/dev/video0")]
    except Exception:
        paths = sorted(glob.glob("/dev/video*"))
        return [(p, p) for p in paths] or [("/dev/video0", "/dev/video0")]


def _scan_audio_devices() -> list[tuple[str, str]]:
    # Prefer PulseAudio/PipeWire sources — works even when PipeWire owns ALSA
    try:
        out = subprocess.check_output(
            ["pactl", "list", "sources"], stderr=subprocess.DEVNULL, timeout=3,
        ).decode(errors="replace")
        devices, name, desc = [], None, None
        for line in out.splitlines():
            line = line.strip()
            m = re.match(r"Name:\s+(.+)", line)
            if m:
                name, desc = m.group(1), None
                continue
            m = re.match(r"Description:\s+(.+)", line)
            if m:
                desc = m.group(1)
                if name and not name.endswith(".monitor"):
                    devices.append((desc, name))
        if devices:
            return devices
    except Exception:
        pass

    # Fall back to direct ALSA
    try:
        out = subprocess.check_output(
            ["arecord", "-l"], stderr=subprocess.DEVNULL, timeout=2,
        ).decode(errors="replace")
        devices = []
        for line in out.splitlines():
            m = re.match(r"card \d+: (\w+) \[(.+?)\], device (\d+): \S+ \[(.+?)\]", line)
            if m:
                short, full, dev_num, dev_full = m.groups()
                alsa = f"plughw:{short},{dev_num}"
                devices.append((f"{full} — {dev_full}  ({alsa})", alsa))
        return devices or [("plughw:Hagibis,0", "plughw:Hagibis,0")]
    except Exception:
        return [("plughw:Hagibis,0", "plughw:Hagibis,0")]


def _query_device_caps(dev: str) -> dict:
    FMT_MAP = {"MJPG": ("mjpeg", "MJPEG"), "YUY2": ("yuyv422", "YUYV")}
    caps: dict = {}
    try:
        out = subprocess.check_output(
            ["v4l2-ctl", "-d", dev, "--list-formats-ext"],
            stderr=subprocess.DEVNULL, timeout=3,
        ).decode(errors="replace")
    except Exception:
        return {}
    current_fmt = current_size = None
    for line in out.splitlines():
        line = line.strip()
        m = re.match(r"\[\d+\]: '([A-Z0-9]+)'", line)
        if m:
            v4l2_fmt = m.group(1)
            if v4l2_fmt in FMT_MAP:
                ff, label = FMT_MAP[v4l2_fmt]
                caps[ff] = {"label": label, "sizes": {}}
                current_fmt, current_size = ff, None
            else:
                current_fmt = current_size = None
            continue
        m = re.match(r"Size: Discrete (\d+)x(\d+)", line)
        if m and current_fmt:
            current_size = (int(m.group(1)), int(m.group(2)))
            caps[current_fmt]["sizes"][current_size] = []
            continue
        m = re.match(r"Interval: Discrete .+\((\d+\.\d+) fps\)", line)
        if m and current_fmt and current_size:
            caps[current_fmt]["sizes"][current_size].append(round(float(m.group(1))))
    return caps


# ── video preview widget ──────────────────────────────────────────────────────
class VideoDisplay(QLabel):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setMinimumSize(640, 360)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self._pixmap:    QPixmap | None = None
        self._render_px: QPixmap | None = None
        self._render_pt: QPoint = QPoint(0, 0)
        self._scale_mode: str = "fit"
        self._crop_mode:  str = "full"
        self._bg_color:   QColor = QColor("#1f1f1f")

    def set_scale_mode(self, mode: str):
        self._scale_mode = mode
        self._refresh()

    def set_crop_mode(self, mode: str):
        self._crop_mode = mode
        self._refresh()

    def set_bg_color(self, color: QColor):
        self._bg_color = color
        self.update()

    def set_frame(self, img: QImage):
        self._pixmap = QPixmap.fromImage(img)
        self._refresh()

    def clear_signal(self):
        self._pixmap = None
        self._render_px = None
        self.update()

    def _cropped(self, px: QPixmap) -> QPixmap:
        if self._crop_mode == "full":
            return px
        try:
            rw, rh = (int(p) for p in self._crop_mode.split(":"))
        except ValueError:
            return px
        sw, sh = px.width(), px.height()
        if sw == 0 or sh == 0:
            return px
        tgt = rw / rh
        src = sw / sh
        if abs(src - tgt) < 0.001:
            return px
        if src > tgt:
            nw = int(sh * tgt)
            return px.copy((sw - nw) // 2, 0, nw, sh)
        else:
            nh = int(sw / tgt)
            return px.copy(0, (sh - nh) // 2, sw, nh)

    def _refresh(self):
        if not self._pixmap:
            self._render_px = None
            self.update()
            return
        px   = self._cropped(self._pixmap)
        W, H = self.width(), self.height()
        if W == 0 or H == 0:
            return
        fast = Qt.TransformationMode.FastTransformation
        mode = self._scale_mode

        if mode == "stretch":
            self._render_px = px.scaled(W, H, Qt.AspectRatioMode.IgnoreAspectRatio, fast)
            self._render_pt = QPoint(0, 0)
        elif mode == "fill":
            s = px.scaled(W, H, Qt.AspectRatioMode.KeepAspectRatioByExpanding, fast)
            self._render_px = s.copy(
                max(0, (s.width() - W) // 2), max(0, (s.height() - H) // 2), W, H
            )
            self._render_pt = QPoint(0, 0)
        elif mode == "native":
            self._render_px = px
            self._render_pt = QPoint(max(0, (W - px.width()) // 2),
                                     max(0, (H - px.height()) // 2))
        elif mode.startswith("area_"):
            rw, rh = (int(v) for v in mode[5:].split("_"))
            scale  = min(W / rw, H / rh)
            aw, ah = int(rw * scale), int(rh * scale)
            s = px.scaled(aw, ah, Qt.AspectRatioMode.KeepAspectRatio, fast)
            self._render_px = s
            self._render_pt = QPoint((W - s.width()) // 2, (H - s.height()) // 2)
        elif mode.startswith("stretch_"):
            rw, rh = (int(v) for v in mode[8:].split("_"))
            scale  = min(W / rw, H / rh)
            aw, ah = int(rw * scale), int(rh * scale)
            self._render_px = px.scaled(aw, ah, Qt.AspectRatioMode.IgnoreAspectRatio, fast)
            self._render_pt = QPoint((W - aw) // 2, (H - ah) // 2)
        else:  # "fit"
            s = px.scaled(W, H, Qt.AspectRatioMode.KeepAspectRatio, fast)
            self._render_px = s
            self._render_pt = QPoint((W - s.width()) // 2, (H - s.height()) // 2)

        self.update()

    def resizeEvent(self, event):
        self._refresh()

    def paintEvent(self, event):
        p = QPainter(self)
        p.fillRect(self.rect(), self._bg_color)
        if self._render_px is not None:
            p.drawPixmap(self._render_pt, self._render_px)
        else:
            lum = (0.299 * self._bg_color.redF() +
                   0.587 * self._bg_color.greenF() +
                   0.114 * self._bg_color.blueF())
            p.setPen(QColor("#404040") if lum > 0.5 else QColor("#606060"))
            p.setFont(QFont("sans-serif", 20))
            p.drawText(self.rect(), Qt.AlignmentFlag.AlignCenter, "NO SIGNAL")


# ── helpers ───────────────────────────────────────────────────────────────────
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


# ── main window ───────────────────────────────────────────────────────────────
class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Hagibis Monitor")
        self.setMinimumSize(960, 580)

        self._video_worker: VideoWorker | None = None
        self._audio_worker: AudioWorker | None = None
        self._caps:           dict = {}
        self._pa_sink_input:  int | None = None
        self._pa_poll_count:  int = 0
        self._current_profile: str  = "Default"
        self._dirty:           bool = False
        self._res_tab_selection: dict[str, tuple[int, int]] = {}

        self._build_ui()
        self._apply_dark_theme()
        self._load_settings()

    # ── UI construction ───────────────────────────────────────────────────────
    def _build_ui(self):
        root = QWidget()
        self.setCentralWidget(root)
        root_layout = QHBoxLayout(root)
        root_layout.setContentsMargins(0, 0, 0, 0)
        root_layout.setSpacing(0)

        self._display = VideoDisplay()
        root_layout.addWidget(self._display, stretch=1)

        self._panel_toggle = QPushButton("◀")
        self._panel_toggle.setFixedWidth(16)
        self._panel_toggle.setSizePolicy(
            QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Expanding
        )
        self._panel_toggle.setToolTip("Hide / show control panel")
        self._panel_toggle.setStyleSheet("""
            QPushButton {
                background: #252525; border: none;
                border-left: 1px solid #3a3a3a; border-radius: 0;
                padding: 0; color: #666; font-size: 9px;
            }
            QPushButton:hover { background: #333; color: #ccc; }
        """)
        self._panel_toggle.clicked.connect(self._toggle_panel)
        root_layout.addWidget(self._panel_toggle)

        self._panel_scroll = QScrollArea()
        self._panel_scroll.setWidgetResizable(True)
        self._panel_scroll.setFixedWidth(380)
        self._panel_scroll.setFrameShape(QFrame.Shape.NoFrame)
        self._panel_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)

        panel = QWidget()
        panel_layout = QVBoxLayout(panel)
        panel_layout.setContentsMargins(8, 8, 8, 8)
        panel_layout.setSpacing(4)

        # ── Profile bar ───────────────────────────────────────────────────
        row1 = QHBoxLayout()
        row1.setSpacing(4)
        row1.addWidget(QLabel("Profile:"))
        self._profile_combo = QComboBox()
        self._profile_combo.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred
        )
        row1.addWidget(self._profile_combo, 1)
        new_btn = QPushButton("+")
        new_btn.setFixedWidth(28)
        new_btn.setToolTip("Create new profile from current settings")
        new_btn.clicked.connect(self._new_profile)
        row1.addWidget(new_btn)
        self._del_profile_btn = QPushButton("✕")
        self._del_profile_btn.setFixedWidth(28)
        self._del_profile_btn.setToolTip("Delete selected profile")
        self._del_profile_btn.clicked.connect(self._delete_profile)
        row1.addWidget(self._del_profile_btn)
        panel_layout.addLayout(row1)

        row2 = QHBoxLayout()
        row2.setSpacing(4)
        save_btn = QPushButton("Save Profile")
        save_btn.clicked.connect(self._save_profile)
        save_btn.setToolTip("Save current settings to the active profile")
        row2.addWidget(save_btn)
        revert_btn = QPushButton("Revert")
        revert_btn.clicked.connect(self._revert_profile)
        revert_btn.setToolTip("Discard unsaved changes and reload the active profile")
        row2.addWidget(revert_btn)
        folder_btn = QPushButton("⎆")
        folder_btn.setFixedWidth(28)
        folder_btn.setToolTip("Open config folder")
        folder_btn.clicked.connect(self._open_config_folder)
        row2.addWidget(folder_btn)
        self._dirty_lbl = QLabel("● unsaved")
        self._dirty_lbl.setStyleSheet("color: #f0a000; font-size: 11px;")
        self._dirty_lbl.setVisible(False)
        row2.addWidget(self._dirty_lbl)
        row2.addStretch()
        panel_layout.addLayout(row2)

        self._profile_combo.currentIndexChanged.connect(self._on_profile_changed)

        tabs = QTabWidget()
        tabs.addTab(self._build_video_tab(), "Video")
        tabs.addTab(self._build_audio_tab(), "Audio")
        tabs.addTab(self._build_output_tab(), "Output")
        panel_layout.addWidget(tabs)
        panel_layout.addStretch()

        self._panel_scroll.setWidget(panel)
        root_layout.addWidget(self._panel_scroll)

        self._lbl_signal = QLabel("Initialising…")
        self._lbl_fps    = QLabel("FPS: --")
        sb = self.statusBar()
        sb.addWidget(self._lbl_signal, 1)
        sb.addPermanentWidget(self._lbl_fps)

    def _build_video_tab(self) -> QWidget:
        w = QWidget()
        layout = QVBoxLayout(w)
        layout.setContentsMargins(4, 6, 4, 4)
        layout.setSpacing(6)

        cap = QGroupBox("Capture Settings")
        cl = QVBoxLayout(cap)

        cl.addWidget(QLabel("Device:"))
        self._video_devices = _scan_video_devices()
        self._device_combo = QComboBox()
        self._device_combo.setSizeAdjustPolicy(
            QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon
        )
        for label, path in self._video_devices:
            self._device_combo.addItem(label, path)
        refresh_btn = QPushButton("↻")
        refresh_btn.setFixedWidth(32)
        refresh_btn.setToolTip("Re-scan video devices")
        refresh_btn.clicked.connect(self._refresh_video_devices)
        dev_row = QHBoxLayout()
        dev_row.addWidget(self._device_combo, 1)
        dev_row.addWidget(refresh_btn)
        cl.addLayout(dev_row)
        self._device_combo.currentIndexChanged.connect(self._on_video_device_changed)

        cl.addWidget(QLabel("Format:"))
        self._fmt_combo = QComboBox()
        cl.addWidget(self._fmt_combo)
        self._fmt_combo.currentIndexChanged.connect(self._on_fmt_changed)

        cl.addWidget(QLabel("Resolution:"))
        self._res_aspect_bar = QTabBar()
        self._res_aspect_bar.setExpanding(False)
        self._res_aspect_bar.setUsesScrollButtons(True)
        cl.addWidget(self._res_aspect_bar)
        self._res_combo = QComboBox()
        cl.addWidget(self._res_combo)
        self._res_aspect_bar.currentChanged.connect(self._on_res_aspect_changed)
        self._res_combo.currentIndexChanged.connect(self._on_res_changed)
        self._res_aspect_groups: dict[str, list[tuple[int, int]]] = {}

        cl.addWidget(QLabel("Frame Rate:"))
        self._fps_combo = QComboBox()
        cl.addWidget(self._fps_combo)
        self._fps_combo.currentIndexChanged.connect(lambda _: self._mark_dirty())

        apply_btn = QPushButton("Apply && Restart Capture")
        apply_btn.clicked.connect(self._restart_video)
        cl.addWidget(apply_btn)
        layout.addWidget(cap)

        disp = QGroupBox("Display")
        dl = QVBoxLayout(disp)
        dl.addWidget(QLabel("Scale Mode:"))
        self._scale_combo = QComboBox()
        for label, key in [
            ("Fit (Keep Aspect)",       "fit"),
            ("Stretch to Fill",         "stretch"),
            ("Zoom to Fill (Crop)",     "fill"),
            ("Native (1:1 Pixels)",     "native"),
            ("Fit to 16:9 Area",        "area_16_9"),
            ("Fit to 10:9 Area",        "area_10_9"),
            ("Fit to 5:4 Area",         "area_5_4"),
            ("Fit to 4:3 Area",         "area_4_3"),
            ("Stretch to 16:9 Area",    "stretch_16_9"),
            ("Stretch to 10:9 Area",    "stretch_10_9"),
            ("Stretch to 5:4 Area",     "stretch_5_4"),
            ("Stretch to 4:3 Area",     "stretch_4_3"),
        ]:
            self._scale_combo.addItem(label, key)
        self._scale_combo.currentIndexChanged.connect(self._on_scale_mode_changed)
        dl.addWidget(self._scale_combo)

        dl.addWidget(QLabel("Crop:"))
        self._crop_combo = QComboBox()
        for label, key in [
            ("Full Image",    "full"),
            ("Crop to 10:9",  "10:9"),
            ("Crop to 5:4",   "5:4"),
            ("Crop to 4:3",   "4:3"),
        ]:
            self._crop_combo.addItem(label, key)
        self._crop_combo.currentIndexChanged.connect(self._on_crop_mode_changed)
        dl.addWidget(self._crop_combo)

        dl.addWidget(QLabel("Background Color:"))
        self._bg_color_btn = QPushButton()
        self._bg_color_btn.setFixedHeight(26)
        self._bg_color_btn.setToolTip("Click to choose background colour")
        self._bg_color_btn.clicked.connect(self._pick_bg_color)
        dl.addWidget(self._bg_color_btn)
        layout.addWidget(disp)

        img = QGroupBox("Image Controls")
        il = QVBoxLayout(img)
        self._v4l2_sliders: dict[str, QSlider] = {}
        self._v4l2_labels:  dict[str, QLabel]  = {}
        for ctrl, label in [
            ("brightness", "Brightness"),
            ("contrast",   "Contrast"),
            ("saturation", "Saturation"),
            ("hue",        "Hue"),
        ]:
            il.addWidget(QLabel(label + ":"))
            row, slider, lbl = _slider_row(
                0, 100, 50,
                lambda v, c=ctrl: self._set_v4l2(c, v)
            )
            il.addLayout(row)
            self._v4l2_sliders[ctrl] = slider
            self._v4l2_labels[ctrl]  = lbl

        reset_btn = QPushButton("Reset to Defaults")
        reset_btn.clicked.connect(self._reset_v4l2)
        il.addWidget(reset_btn)
        layout.addWidget(img)

        layout.addStretch()
        return w

    def _build_audio_tab(self) -> QWidget:
        w = QWidget()
        layout = QVBoxLayout(w)
        layout.setContentsMargins(4, 6, 4, 4)
        layout.setSpacing(6)

        dev_grp = QGroupBox("Audio Device")
        dl = QVBoxLayout(dev_grp)
        self._audio_devices = _scan_audio_devices()
        self._audio_device_combo = QComboBox()
        self._audio_device_combo.setSizeAdjustPolicy(
            QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon
        )
        for label, path in self._audio_devices:
            self._audio_device_combo.addItem(label, path)
        audio_refresh_btn = QPushButton("↻")
        audio_refresh_btn.setFixedWidth(32)
        audio_refresh_btn.setToolTip("Re-scan audio devices")
        audio_refresh_btn.clicked.connect(self._refresh_audio_devices)
        audio_dev_row = QHBoxLayout()
        audio_dev_row.addWidget(self._audio_device_combo, 1)
        audio_dev_row.addWidget(audio_refresh_btn)
        dl.addLayout(audio_dev_row)
        self._audio_device_combo.currentIndexChanged.connect(self._on_audio_device_changed)
        layout.addWidget(dev_grp)

        self._audio_enabled = QCheckBox("Enable Audio Monitor")
        self._audio_enabled.stateChanged.connect(self._on_audio_toggle)
        layout.addWidget(self._audio_enabled)

        self._vu = StereoVuMeter()
        layout.addWidget(self._vu)

        opts = QGroupBox("Audio Options")
        ol = QVBoxLayout(opts)
        self._mono_mix = QCheckBox("Force Mono Mix")
        self._mono_mix.setToolTip(
            "Mix L+R into a single mono signal sent to both channels.\n"
            "Useful when the source sends audio on only one channel."
        )
        self._mono_mix.stateChanged.connect(self._on_audio_opt_change)
        ol.addWidget(self._mono_mix)
        self._passthrough = QCheckBox("Passthrough to System Audio")
        self._passthrough.setToolTip(
            "Route captured audio to the default PulseAudio/PipeWire output."
        )
        self._passthrough.stateChanged.connect(self._on_audio_opt_change)
        ol.addWidget(self._passthrough)
        layout.addWidget(opts)

        vol_grp = QGroupBox("Volume")
        vl = QVBoxLayout(vol_grp)
        for attr, label_text, lo, hi in [
            ("_vol_slider",   "Master:", -40, 10),
            ("_vol_l_slider", "Left:",   -20, 20),
            ("_vol_r_slider", "Right:",  -20, 20),
        ]:
            vl.addWidget(QLabel(label_text))
            slider = QSlider(Qt.Orientation.Horizontal)
            slider.setRange(lo, hi)
            slider.setValue(0)
            slider.setEnabled(False)
            lbl = QLabel("0 dB")
            lbl.setFixedWidth(50)
            lbl.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
            setattr(self, attr, slider)
            setattr(self, attr.replace("_slider", "_lbl"), lbl)
            row = QHBoxLayout()
            row.addWidget(slider)
            row.addWidget(lbl)
            vl.addLayout(row)

        self._vol_slider.valueChanged.connect(self._on_vol_changed)
        self._vol_l_slider.valueChanged.connect(self._on_vol_l_changed)
        self._vol_r_slider.valueChanged.connect(self._on_vol_r_changed)
        self._vol_slider.sliderReleased.connect(self._on_audio_opt_change)
        self._vol_l_slider.sliderReleased.connect(self._on_audio_opt_change)
        self._vol_r_slider.sliderReleased.connect(self._on_audio_opt_change)
        layout.addWidget(vol_grp)

        layout.addStretch()
        return w

    def _build_output_tab(self) -> QWidget:
        w = QWidget()
        layout = QVBoxLayout(w)
        layout.setContentsMargins(4, 6, 4, 4)
        layout.setSpacing(6)

        self._output_enabled = QCheckBox("Enable Virtual Microphone Output")
        self._output_enabled.setToolTip(
            "Creates a virtual microphone in PulseAudio/PipeWire.\n"
            "Audio from the selected input device is routed through it\n"
            "with all volume and mono settings from the Audio tab applied."
        )
        self._output_enabled.stateChanged.connect(self._on_output_toggle)
        layout.addWidget(self._output_enabled)

        obs_grp = QGroupBox("OBS Setup")
        ol = QVBoxLayout(obs_grp)
        ol.addWidget(QLabel(
            "In OBS, add an Audio Input Capture source\n"
            "and select the device named below:"
        ))

        device_row = QHBoxLayout()
        self._output_device_edit = QLineEdit(AudioWorker.SOURCE_NAME)
        self._output_device_edit.setReadOnly(True)
        device_row.addWidget(self._output_device_edit, 1)
        copy_btn = QPushButton("Copy")
        copy_btn.setFixedWidth(52)
        copy_btn.setToolTip("Copy device name to clipboard")
        copy_btn.clicked.connect(
            lambda: QApplication.clipboard().setText(self._output_device_edit.text())
        )
        device_row.addWidget(copy_btn)
        ol.addLayout(device_row)

        note = QLabel(
            "In OBS select Sources → Audio Input Capture\n"
            "and look for \"Hagibis Virtual Microphone\".\n\n"
            "Master volume, channel volumes, and mono mix\n"
            "from the Audio tab are all applied."
        )
        note.setStyleSheet("color: #888888; font-size: 11px;")
        ol.addWidget(note)
        layout.addWidget(obs_grp)

        self._output_status_lbl = QLabel("○ Inactive")
        self._output_status_lbl.setStyleSheet("color: #888888;")
        layout.addWidget(self._output_status_lbl)

        layout.addStretch()
        return w

    # ── theming ───────────────────────────────────────────────────────────────
    def _apply_dark_theme(self):
        app = QApplication.instance()
        pal = QPalette()
        c = {
            QPalette.ColorRole.Window:          "#1e1e1e",
            QPalette.ColorRole.WindowText:      "#dddddd",
            QPalette.ColorRole.Base:            "#2a2a2a",
            QPalette.ColorRole.AlternateBase:   "#333333",
            QPalette.ColorRole.Text:            "#dddddd",
            QPalette.ColorRole.Button:          "#333333",
            QPalette.ColorRole.ButtonText:      "#dddddd",
            QPalette.ColorRole.Highlight:       "#0078d4",
            QPalette.ColorRole.HighlightedText: "#ffffff",
            QPalette.ColorRole.ToolTipBase:     "#2a2a2a",
            QPalette.ColorRole.ToolTipText:     "#cccccc",
        }
        for role, hex_color in c.items():
            pal.setColor(role, QColor(hex_color))
        app.setPalette(pal)
        app.setStyleSheet("""
            QGroupBox {
                border: 1px solid #3a3a3a; border-radius: 4px;
                margin-top: 8px; padding-top: 4px;
            }
            QGroupBox::title { subcontrol-origin: margin; left: 8px; color: #aaaaaa; }
            QSlider::groove:horizontal {
                height: 4px; background: #3a3a3a; border-radius: 2px;
            }
            QSlider::handle:horizontal {
                background: #0078d4; width: 14px; height: 14px;
                margin: -5px 0; border-radius: 7px;
            }
            QSlider::sub-page:horizontal { background: #0078d4; border-radius: 2px; }
            QScrollArea { background: #252525; }
            QTabWidget::pane { border: 1px solid #3a3a3a; }
            QTabBar::tab {
                background: #2a2a2a; color: #aaaaaa;
                padding: 5px 12px; border: 1px solid #3a3a3a;
            }
            QTabBar::tab:selected { background: #1e1e1e; color: #ffffff; }
            QPushButton {
                background: #3a3a3a; border: 1px solid #555;
                border-radius: 3px; padding: 4px 8px; color: #dddddd;
            }
            QPushButton:hover  { background: #484848; }
            QPushButton:pressed { background: #0078d4; }
            QComboBox {
                background: #2a2a2a; border: 1px solid #3a3a3a;
                border-radius: 3px; padding: 3px 6px;
            }
            QComboBox::drop-down { border: none; }
            QCheckBox { spacing: 6px; }
            QStatusBar { background: #161616; color: #888888; }
        """)

    # ── profile file helpers ──────────────────────────────────────────────────
    def _profiles_dir(self) -> Path:
        ini = QSettings("HagibisMonitor", "HagibisMonitor").fileName()
        return Path(ini).parent / "profiles"

    def _profile_path(self, name: str) -> Path:
        return self._profiles_dir() / f"{name}.ini"

    def _profile_settings(self, name: str) -> QSettings:
        self._profiles_dir().mkdir(parents=True, exist_ok=True)
        return QSettings(str(self._profile_path(name)), QSettings.Format.IniFormat)

    def _open_config_folder(self):
        subprocess.Popen(["xdg-open", str(self._profiles_dir())],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    def _list_profiles(self) -> list[str]:
        d = self._profiles_dir()
        names = ["Default"]
        if d.exists():
            names += sorted(p.stem for p in d.glob("*.ini") if p.stem != "Default")
        return names

    # ── dirty tracking ────────────────────────────────────────────────────────
    def _mark_dirty(self):
        self._dirty = True
        self._dirty_lbl.setVisible(True)

    def _clear_dirty(self):
        self._dirty = False
        self._dirty_lbl.setVisible(False)

    # ── settings I/O ─────────────────────────────────────────────────────────
    def _collect_settings(self) -> AppSettings:
        w, h = self._res_combo.currentData() or (1280, 720)
        return AppSettings(
            scale_mode    = self._scale_combo.currentData() or "fit",
            crop_mode     = self._crop_combo.currentData() or "full",
            bg_color      = self._display._bg_color.name(),
            video_device  = self._device_combo.currentData() or "/dev/video0",
            video_fmt     = self._fmt_combo.currentData() or "mjpeg",
            video_res     = f"{w}x{h}",
            video_fps     = self._fps_combo.currentData() or 30,
            brightness    = self._v4l2_sliders["brightness"].value(),
            contrast      = self._v4l2_sliders["contrast"].value(),
            saturation    = self._v4l2_sliders["saturation"].value(),
            hue           = self._v4l2_sliders["hue"].value(),
            audio_device  = self._audio_device_combo.currentData() or "plughw:Hagibis,0",
            audio_enabled = self._audio_enabled.isChecked(),
            mono_mix      = self._mono_mix.isChecked(),
            passthrough   = self._passthrough.isChecked(),
            volume_db     = self._vol_slider.value(),
            volume_l_db   = self._vol_l_slider.value(),
            volume_r_db   = self._vol_r_slider.value(),
            output_enabled = self._output_enabled.isChecked(),
        )

    def _load_from_disk(self, name: str) -> AppSettings:
        s = self._profile_settings(name)

        def _b(key: str, default: bool) -> bool:
            return s.value(key, default, type=bool)

        def _i(key: str, default: int) -> int:
            try:
                return int(s.value(key, default))
            except (TypeError, ValueError):
                return default

        def _s(key: str, default: str) -> str:
            v = s.value(key)
            return v if v is not None else default

        dev = _s("cap/device", "/dev/video0")
        adev = _s("audio/device", "plughw:Hagibis,0")

        # Support the old per-device-keyed format for migration
        dk = _dev_key(dev)
        ak = _dev_key(adev)

        def cap(key: str, default) -> str:
            v = s.value(f"cap/{key}")
            if v is None:
                v = s.value(f"cap/{dk}/{key}")
            return v if v is not None else default

        def aud(key: str, default, cast=str):
            v = s.value(f"audio/{key}")
            if v is None:
                v = s.value(f"audio/{ak}/{key}")
            if v is None:
                return default
            try:
                if cast is bool:
                    return str(v).lower() in ("true", "1", "yes")
                return cast(v)
            except (TypeError, ValueError):
                return default

        return AppSettings(
            scale_mode    = _s("display/scale_mode", "fit"),
            crop_mode     = _s("display/crop_mode", "full"),
            bg_color      = _s("display/bg_color", "#1f1f1f"),
            video_device  = dev,
            video_fmt     = cap("fmt", "mjpeg"),
            video_res     = cap("res", "1280x720"),
            video_fps     = int(cap("fps", 30)),
            brightness    = int(cap("brightness", 50)),
            contrast      = int(cap("contrast", 50)),
            saturation    = int(cap("saturation", 50)),
            hue           = int(cap("hue", 50)),
            audio_device  = adev,
            audio_enabled = _b("audio/enabled", True),
            mono_mix      = aud("mono_mix",    False, bool),
            passthrough   = aud("passthrough", False, bool),
            volume_db     = aud("volume_db",   0,     int),
            volume_l_db   = aud("volume_l_db", 0,     int),
            volume_r_db   = aud("volume_r_db", 0,     int),
            output_enabled = aud("output_enabled", False, bool),
        )

    def _save_to_disk(self, settings: AppSettings, name: str):
        s = self._profile_settings(name)
        s.setValue("display/scale_mode",   settings.scale_mode)
        s.setValue("display/crop_mode",    settings.crop_mode)
        s.setValue("display/bg_color",     settings.bg_color)
        s.setValue("cap/device",           settings.video_device)
        s.setValue("cap/fmt",              settings.video_fmt)
        s.setValue("cap/res",              settings.video_res)
        s.setValue("cap/fps",              settings.video_fps)
        s.setValue("cap/brightness",       settings.brightness)
        s.setValue("cap/contrast",         settings.contrast)
        s.setValue("cap/saturation",       settings.saturation)
        s.setValue("cap/hue",              settings.hue)
        s.setValue("audio/device",         settings.audio_device)
        s.setValue("audio/enabled",        settings.audio_enabled)
        s.setValue("audio/mono_mix",       settings.mono_mix)
        s.setValue("audio/passthrough",    settings.passthrough)
        s.setValue("audio/volume_db",      settings.volume_db)
        s.setValue("audio/volume_l_db",    settings.volume_l_db)
        s.setValue("audio/volume_r_db",    settings.volume_r_db)
        s.setValue("audio/output_enabled", settings.output_enabled)
        s.sync()

    def _apply_settings(self, settings: AppSettings):
        """Apply an AppSettings struct to all UI widgets then restart streams."""
        # Scale / crop / bg
        self._select_combo(self._scale_combo, settings.scale_mode)
        self._display.set_scale_mode(settings.scale_mode)
        self._select_combo(self._crop_combo, settings.crop_mode)
        self._display.set_crop_mode(settings.crop_mode)
        self._apply_bg_color(QColor(settings.bg_color))

        # Video device
        self._select_combo_by_data(self._device_combo, settings.video_device)

        # Caps + format/res/fps combos
        self._caps = _query_device_caps(settings.video_device)
        self._populate_fmt_combo()

        self._select_combo(self._fmt_combo, settings.video_fmt)
        self._populate_res_combo()

        try:
            rw, rh = map(int, settings.video_res.split("x"))
        except (ValueError, AttributeError):
            rw, rh = 1280, 720
        target_aspect = _aspect_label(rw, rh)
        self._res_aspect_bar.blockSignals(True)
        for i in range(self._res_aspect_bar.count()):
            if self._res_aspect_bar.tabText(i) == target_aspect:
                self._res_aspect_bar.setCurrentIndex(i)
                break
        self._res_aspect_bar.blockSignals(False)
        self._res_tab_selection[target_aspect] = (rw, rh)
        self._fill_res_combo_for_tab()
        self._select_combo_by_data(self._res_combo, (rw, rh))
        self._populate_fps_combo()
        self._select_combo_by_data(self._fps_combo, settings.video_fps)

        # Image controls (no signal → hardware applied separately)
        for ctrl in ("brightness", "contrast", "saturation", "hue"):
            val = getattr(settings, ctrl)
            sl  = self._v4l2_sliders[ctrl]
            sl.blockSignals(True)
            sl.setValue(val)
            sl.blockSignals(False)
            self._v4l2_labels[ctrl].setText(str(val))

        # Audio device
        self._select_combo_by_data(self._audio_device_combo, settings.audio_device)

        # Audio options (block signals to avoid premature restarts)
        for widget, val in [
            (self._audio_enabled,  settings.audio_enabled),
            (self._mono_mix,       settings.mono_mix),
            (self._passthrough,    settings.passthrough),
            (self._output_enabled, settings.output_enabled),
        ]:
            widget.blockSignals(True)
            widget.setChecked(val)
            widget.blockSignals(False)

        for slider, val in [
            (self._vol_slider,   settings.volume_db),
            (self._vol_l_slider, settings.volume_l_db),
            (self._vol_r_slider, settings.volume_r_db),
        ]:
            slider.blockSignals(True)
            slider.setValue(val)
            slider.blockSignals(False)

        self._vol_lbl.setText(_db_label(settings.volume_db))
        self._vol_l_lbl.setText(_db_label(settings.volume_l_db))
        self._vol_r_lbl.setText(_db_label(settings.volume_r_db))
        self._update_vol_slider_states()

        # Restart streams
        self._restart_video()
        self._apply_v4l2_all()
        if settings.audio_enabled:
            self._start_audio()
        else:
            self._stop_audio()

    # ── combo helpers ─────────────────────────────────────────────────────────
    def _select_combo(self, combo: QComboBox, key: str):
        combo.blockSignals(True)
        for i in range(combo.count()):
            if combo.itemData(i) == key:
                combo.setCurrentIndex(i)
                break
        combo.blockSignals(False)

    def _select_combo_by_data(self, combo: QComboBox, data):
        combo.blockSignals(True)
        for i in range(combo.count()):
            if combo.itemData(i) == data:
                combo.setCurrentIndex(i)
                break
        combo.blockSignals(False)

    # ── profile operations ────────────────────────────────────────────────────
    def _populate_profile_combo(self):
        self._profile_combo.blockSignals(True)
        self._profile_combo.clear()
        for name in self._list_profiles():
            self._profile_combo.addItem(name)
        idx = self._profile_combo.findText(self._current_profile)
        self._profile_combo.setCurrentIndex(max(0, idx))
        self._del_profile_btn.setEnabled(self._current_profile != "Default")
        self._profile_combo.blockSignals(False)

    def _on_profile_changed(self):
        name = self._profile_combo.currentText()
        if name == self._current_profile:
            return

        if self._dirty:
            msg = QMessageBox(self)
            msg.setWindowTitle("Unsaved Changes")
            msg.setText(
                f'Profile "{self._current_profile}" has unsaved changes.\n'
                "What would you like to do?"
            )
            discard  = msg.addButton("Discard & Switch",     QMessageBox.ButtonRole.DestructiveRole)
            save_sw  = msg.addButton("Save & Switch",        QMessageBox.ButtonRole.AcceptRole)
            save_new = msg.addButton("Save as New Profile…", QMessageBox.ButtonRole.ActionRole)
            cancel   = msg.addButton("Cancel",               QMessageBox.ButtonRole.RejectRole)
            msg.exec()
            clicked = msg.clickedButton()

            if clicked == cancel:
                self._profile_combo.blockSignals(True)
                self._profile_combo.setCurrentIndex(
                    max(0, self._profile_combo.findText(self._current_profile))
                )
                self._profile_combo.blockSignals(False)
                return
            if clicked == save_sw:
                self._save_to_disk(self._collect_settings(), self._current_profile)
            elif clicked == save_new:
                new_name, ok = QInputDialog.getText(
                    self, "Save as New Profile", "Profile name:"
                )
                if ok and new_name.strip() and new_name.strip() != name:
                    self._save_to_disk(self._collect_settings(), new_name.strip())
                    self._populate_profile_combo()

        self._switch_profile(name)

    def _switch_profile(self, name: str):
        self._current_profile = name
        QSettings("HagibisMonitor", "HagibisMonitor").setValue("profile/current", name)
        self._del_profile_btn.setEnabled(name != "Default")
        settings = self._load_from_disk(name)
        self._apply_settings(settings)
        self._clear_dirty()

    def _save_profile(self):
        self._save_to_disk(self._collect_settings(), self._current_profile)
        QSettings("HagibisMonitor", "HagibisMonitor").setValue(
            "profile/current", self._current_profile
        )
        self._clear_dirty()

    def _revert_profile(self):
        settings = self._load_from_disk(self._current_profile)
        self._apply_settings(settings)
        self._clear_dirty()

    def _new_profile(self):
        name, ok = QInputDialog.getText(self, "New Profile", "Profile name:")
        if not ok or not name.strip():
            return
        name = name.strip()
        self._save_to_disk(self._collect_settings(), name)
        self._current_profile = name
        self._populate_profile_combo()
        self._clear_dirty()
        QSettings("HagibisMonitor", "HagibisMonitor").setValue("profile/current", name)

    def _delete_profile(self):
        name = self._current_profile
        if name == "Default":
            return
        path = self._profile_path(name)
        if path.exists():
            path.unlink()
        self._switch_profile("Default")
        self._populate_profile_combo()

    # ── global settings (window geometry + active profile only) ───────────────
    def _load_settings(self):
        gs = QSettings("HagibisMonitor", "HagibisMonitor")
        geom = gs.value("window/geometry")
        if geom:
            self.restoreGeometry(geom)
        state = gs.value("window/state")
        if state:
            self.restoreState(state)
        panel_visible = gs.value("window/panel_visible", True, type=bool)
        self._panel_scroll.setVisible(panel_visible)
        self._panel_toggle.setText("◀" if panel_visible else "▶")

        # Migrate old flat settings into Default.ini if it doesn't exist yet
        if not self._profile_path("Default").exists():
            ps = self._profile_settings("Default")
            for key in gs.allKeys():
                if not key.startswith("window/") and key != "profile/current":
                    ps.setValue(key, gs.value(key))
            ps.sync()

        self._current_profile = gs.value("profile/current", "Default")
        self._populate_profile_combo()
        settings = self._load_from_disk(self._current_profile)
        self._apply_settings(settings)
        self._clear_dirty()

    def _save_global(self):
        gs = QSettings("HagibisMonitor", "HagibisMonitor")
        gs.setValue("window/geometry",     self.saveGeometry())
        gs.setValue("window/state",        self.saveState())
        gs.setValue("window/panel_visible", self._panel_scroll.isVisible())
        gs.setValue("profile/current",     self._current_profile)

    # ── dynamic combo population ──────────────────────────────────────────────
    def _populate_fmt_combo(self):
        self._fmt_combo.blockSignals(True)
        self._fmt_combo.clear()
        if self._caps:
            for ff_fmt, info in self._caps.items():
                self._fmt_combo.addItem(info["label"], ff_fmt)
        else:
            for label, ff_fmt in _DEFAULT_FORMATS:
                self._fmt_combo.addItem(label, ff_fmt)
        self._fmt_combo.blockSignals(False)
        self._populate_res_combo()

    def _populate_res_combo(self):
        self._res_combo.blockSignals(True)
        self._res_combo.clear()
        ff_fmt = self._fmt_combo.currentData()
        if self._caps and ff_fmt and ff_fmt in self._caps:
            all_sizes = sorted(self._caps[ff_fmt]["sizes"].keys(),
                               key=lambda s: s[0] * s[1], reverse=True)
        else:
            all_sizes = [(w, h) for _, w, h in _DEFAULT_RESOLUTIONS]

        groups: dict[str, list[tuple[int, int]]] = {}
        for w, h in all_sizes:
            groups.setdefault(_aspect_label(w, h), []).append((w, h))
        self._res_aspect_groups = groups

        self._res_aspect_bar.blockSignals(True)
        while self._res_aspect_bar.count():
            self._res_aspect_bar.removeTab(0)
        for label in groups:
            self._res_aspect_bar.addTab(label)
        self._res_aspect_bar.blockSignals(False)

        self._res_combo.blockSignals(False)
        self._fill_res_combo_for_tab()
        self._populate_fps_combo()

    def _fill_res_combo_for_tab(self):
        self._res_combo.blockSignals(True)
        self._res_combo.clear()
        tab_idx = self._res_aspect_bar.currentIndex()
        if tab_idx >= 0:
            label = self._res_aspect_bar.tabText(tab_idx)
            for w, h in self._res_aspect_groups.get(label, []):
                self._res_combo.addItem(f"{w}×{h}", (w, h))
            saved = self._res_tab_selection.get(label)
            if saved:
                for i in range(self._res_combo.count()):
                    if self._res_combo.itemData(i) == saved:
                        self._res_combo.setCurrentIndex(i)
                        break
        self._res_combo.blockSignals(False)

    def _on_res_aspect_changed(self):
        self._fill_res_combo_for_tab()
        self._populate_fps_combo()

    def _populate_fps_combo(self):
        self._fps_combo.blockSignals(True)
        self._fps_combo.clear()
        ff_fmt = self._fmt_combo.currentData()
        res    = self._res_combo.currentData()
        if self._caps and ff_fmt and ff_fmt in self._caps and res:
            fps_list = sorted(self._caps[ff_fmt]["sizes"].get(res, []), reverse=True)
            for fps in fps_list:
                self._fps_combo.addItem(f"{fps} fps", fps)
        else:
            for fps in _DEFAULT_FRAMERATES:
                self._fps_combo.addItem(f"{fps} fps", fps)
        self._fps_combo.blockSignals(False)

    def _on_fmt_changed(self):
        self._populate_res_combo()
        self._mark_dirty()

    def _on_res_changed(self):
        tab_idx = self._res_aspect_bar.currentIndex()
        if tab_idx >= 0:
            label = self._res_aspect_bar.tabText(tab_idx)
            data = self._res_combo.currentData()
            if data:
                self._res_tab_selection[label] = data
        self._populate_fps_combo()
        self._mark_dirty()

    # ── display controls ──────────────────────────────────────────────────────
    def _on_scale_mode_changed(self):
        self._display.set_scale_mode(self._scale_combo.currentData())
        self._mark_dirty()

    def _on_crop_mode_changed(self):
        self._display.set_crop_mode(self._crop_combo.currentData())
        self._mark_dirty()

    def _pick_bg_color(self):
        color = QColorDialog.getColor(self._display._bg_color, self, "Choose Background Color")
        if color.isValid():
            self._apply_bg_color(color)
            self._mark_dirty()

    def _apply_bg_color(self, color: QColor):
        self._display.set_bg_color(color)
        self._bg_color_btn.setStyleSheet(
            f"background: {color.name()}; border: 1px solid #555; border-radius: 3px;"
        )

    # ── video device ──────────────────────────────────────────────────────────
    def _on_video_device_changed(self):
        dev = self._device_combo.currentData() or "/dev/video0"
        self._caps = _query_device_caps(dev)
        self._populate_fmt_combo()
        self._mark_dirty()

    def _refresh_video_devices(self):
        current = self._device_combo.currentData()
        self._video_devices = _scan_video_devices()
        self._device_combo.blockSignals(True)
        self._device_combo.clear()
        for label, path in self._video_devices:
            self._device_combo.addItem(label, path)
        self._select_combo_by_data(self._device_combo, current)
        self._device_combo.blockSignals(False)

    # ── capture params + video management ────────────────────────────────────
    def _cap_params(self) -> tuple[int, int, int, str, str]:
        w, h = self._res_combo.currentData() or (1280, 720)
        fps  = self._fps_combo.currentData() or 30
        fmt  = self._fmt_combo.currentData() or "mjpeg"
        dev  = self._device_combo.currentData() or "/dev/video0"
        return w, h, fps, fmt, dev

    def _start_video(self):
        w, h, fps, fmt, dev = self._cap_params()
        wk = VideoWorker()
        wk.configure(w, h, fps, fmt, dev)
        wk.frame_ready.connect(self._display.set_frame)
        wk.fps_updated.connect(lambda f: self._lbl_fps.setText(f"FPS: {f:.1f}"))
        wk.error.connect(lambda e: self._lbl_signal.setText(f"Error: {e}"))
        self._video_worker = wk
        wk.start()
        self._lbl_signal.setText(
            f"Capturing {w}×{h} @ {fps} fps  [{fmt.upper()}]  {dev}"
        )

    def _stop_video(self):
        if self._video_worker:
            self._video_worker.stop()
            self._video_worker.wait(3000)
            self._video_worker = None
        self._display.clear_signal()
        self._lbl_fps.setText("FPS: --")

    def _restart_video(self):
        self._stop_video()
        self._start_video()

    # ── V4L2 controls ─────────────────────────────────────────────────────────
    def _set_v4l2(self, ctrl: str, value: int):
        dev = self._device_combo.currentData() or "/dev/video0"
        subprocess.Popen(
            ["v4l2-ctl", "-d", dev, f"--set-ctrl={ctrl}={value}"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        self._mark_dirty()

    def _reset_v4l2(self):
        for slider in self._v4l2_sliders.values():
            slider.setValue(50)

    def _apply_v4l2_all(self):
        dev = self._device_combo.currentData() or "/dev/video0"
        for ctrl, slider in self._v4l2_sliders.items():
            subprocess.Popen(
                ["v4l2-ctl", "-d", dev, f"--set-ctrl={ctrl}={slider.value()}"],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )

    # ── audio device ──────────────────────────────────────────────────────────
    def _on_audio_device_changed(self):
        self._mark_dirty()
        if self._audio_enabled.isChecked():
            self._start_audio()

    def _refresh_audio_devices(self):
        current = self._audio_device_combo.currentData()
        self._audio_devices = _scan_audio_devices()
        self._audio_device_combo.blockSignals(True)
        self._audio_device_combo.clear()
        for label, path in self._audio_devices:
            self._audio_device_combo.addItem(label, path)
        self._select_combo_by_data(self._audio_device_combo, current)
        self._audio_device_combo.blockSignals(False)

    # ── audio management ──────────────────────────────────────────────────────
    def _start_audio(self):
        self._stop_audio()
        wk = AudioWorker()
        wk.device          = self._audio_device_combo.currentData() or "plughw:Hagibis,0"
        wk.mono_mix        = self._mono_mix.isChecked()
        wk.passthrough     = self._passthrough.isChecked()
        wk.virtual_output  = self._output_enabled.isChecked()
        wk.volume_db       = self._vol_slider.value()
        wk.volume_l_db     = self._vol_l_slider.value()
        wk.volume_r_db     = self._vol_r_slider.value()
        wk.levels_updated.connect(self._vu.set_levels)
        wk.error.connect(lambda e: self._lbl_signal.setText(f"Audio error: {e}"))
        self._audio_worker = wk
        wk.start()
        self._pa_sink_input = None
        self._pa_poll_count = 0
        QTimer.singleShot(80, self._poll_pa_sink_input)
        self._update_output_status()

    def _stop_audio(self):
        self._pa_sink_input = None
        if self._audio_worker:
            self._audio_worker.stop()
            self._audio_worker.wait(3000)
            self._audio_worker = None
        self._vu.set_levels(-96.0, -96.0)
        self._update_output_status()

    def _on_audio_toggle(self, state: int):
        if state == Qt.CheckState.Checked.value:
            self._start_audio()
        else:
            self._stop_audio()
        self._update_vol_slider_states()
        self._update_output_status()
        self._mark_dirty()

    def _on_audio_opt_change(self):
        if self._audio_enabled.isChecked():
            self._start_audio()
        self._mark_dirty()

    # ── virtual output ────────────────────────────────────────────────────────
    def _on_output_toggle(self, state: int):
        if self._audio_enabled.isChecked():
            self._start_audio()  # restart worker with new virtual_output value
        self._update_output_status()
        self._mark_dirty()

    def _update_output_status(self):
        active = self._audio_enabled.isChecked() and self._output_enabled.isChecked()
        if active:
            self._output_status_lbl.setText("● Active")
            self._output_status_lbl.setStyleSheet("color: #50c878;")
        else:
            self._output_status_lbl.setText("○ Inactive")
            self._output_status_lbl.setStyleSheet("color: #888888;")

    def _update_vol_slider_states(self):
        on = self._audio_enabled.isChecked()
        for widget in (self._vol_slider, self._vol_lbl,
                       self._vol_l_slider, self._vol_l_lbl,
                       self._vol_r_slider, self._vol_r_lbl):
            widget.setEnabled(on)

    def _on_vol_changed(self, v: int):
        self._vol_lbl.setText(_db_label(v))
        if self._audio_worker:
            self._audio_worker.volume_db = v
        self._apply_pa_volume()
        self._mark_dirty()

    def _on_vol_l_changed(self, v: int):
        self._vol_l_lbl.setText(_db_label(v))
        if self._audio_worker:
            self._audio_worker.volume_l_db = v
        self._apply_pa_volume()
        self._mark_dirty()

    def _on_vol_r_changed(self, v: int):
        self._vol_r_lbl.setText(_db_label(v))
        if self._audio_worker:
            self._audio_worker.volume_r_db = v
        self._apply_pa_volume()
        self._mark_dirty()

    # ── pactl real-time speaker volume ────────────────────────────────────────
    def _poll_pa_sink_input(self):
        if not self._audio_worker or not self._passthrough.isChecked():
            return
        idx = self._find_pa_sink_input()
        if idx is not None:
            self._pa_sink_input = idx
            self._apply_pa_volume()
        elif self._pa_poll_count < 40:
            self._pa_poll_count += 1
            QTimer.singleShot(80, self._poll_pa_sink_input)

    def _find_pa_sink_input(self) -> int | None:
        if not self._audio_worker:
            return None
        pid = self._audio_worker.proc_pid
        if pid is None:
            return None
        try:
            out = subprocess.check_output(
                ["pactl", "list", "sink-inputs"],
                stderr=subprocess.DEVNULL, timeout=2,
            ).decode(errors="replace")
            current_index = None
            for line in out.splitlines():
                m = re.match(r"\s*Sink Input #(\d+)", line)
                if m:
                    current_index = int(m.group(1))
                if current_index is not None and f'application.process.id = "{pid}"' in line:
                    return current_index
        except Exception:
            pass
        return None

    def _apply_pa_volume(self):
        if self._pa_sink_input is None or not self._passthrough.isChecked():
            return
        master = self._vol_slider.value()
        l_pct = max(0, int(100 * 10 ** ((master + self._vol_l_slider.value()) / 20.0)))
        r_pct = max(0, int(100 * 10 ** ((master + self._vol_r_slider.value()) / 20.0)))
        subprocess.Popen(
            ["pactl", "set-sink-input-volume", str(self._pa_sink_input),
             f"{l_pct}%", f"{r_pct}%"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )

    # ── panel toggle ──────────────────────────────────────────────────────────
    def _toggle_panel(self):
        visible = not self._panel_scroll.isVisible()
        self._panel_scroll.setVisible(visible)
        self._panel_toggle.setText("◀" if visible else "▶")
        self._save_global()

    # ── window close ──────────────────────────────────────────────────────────
    def closeEvent(self, event):
        self._save_global()
        self._save_to_disk(self._collect_settings(), self._current_profile)
        self._stop_audio()
        self._stop_video()
        event.accept()


# ── entry point ───────────────────────────────────────────────────────────────
def main():
    app = QApplication(sys.argv)
    app.setStyle("Fusion")
    win = MainWindow()
    win.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
