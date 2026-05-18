import glob
import re
import sys
import subprocess
from math import gcd

from PyQt6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QHBoxLayout, QVBoxLayout,
    QLabel, QSlider, QComboBox, QCheckBox, QPushButton, QGroupBox,
    QTabWidget, QTabBar, QScrollArea, QSizePolicy, QFrame, QColorDialog,
)
from PyQt6.QtCore import Qt, QPoint, QRect, QSettings, QTimer
from PyQt6.QtGui import QImage, QPixmap, QPainter, QColor, QFont, QPalette

from workers import VideoWorker, AudioWorker
from vu_meter import StereoVuMeter

# ── fallback tables used when v4l2-ctl is unavailable ───────────────────────
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


# ── helpers ──────────────────────────────────────────────────────────────────
def _dev_key(path: str) -> str:
    """Sanitize a device path into a safe QSettings key segment."""
    return re.sub(r"[^A-Za-z0-9]", "_", path)


def _aspect_label(w: int, h: int) -> str:
    """Return a human-readable aspect-ratio string for (w, h)."""
    g = gcd(w, h)
    aw, ah = w // g, h // g
    if (aw, ah) == (8, 5):
        return "16:10"   # normalise 8:5
    return f"{aw}:{ah}"


def _scan_video_devices() -> list[tuple[str, str]]:
    """Return (label, path) pairs for available V4L2 video devices."""
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
    """Return (label, alsa_device) pairs for ALSA capture devices."""
    try:
        out = subprocess.check_output(
            ["arecord", "-l"], stderr=subprocess.DEVNULL, timeout=2,
        ).decode(errors="replace")
        devices = []
        for line in out.splitlines():
            m = re.match(
                r"card \d+: (\w+) \[(.+?)\], device (\d+): \S+ \[(.+?)\]", line
            )
            if m:
                short, full, dev_num, dev_full = m.groups()
                alsa = f"plughw:{short},{dev_num}"
                devices.append((f"{full} — {dev_full}  ({alsa})", alsa))
        return devices or [("plughw:Hagibis,0", "plughw:Hagibis,0")]
    except Exception:
        return [("plughw:Hagibis,0", "plughw:Hagibis,0")]


def _query_device_caps(dev: str) -> dict:
    """Query v4l2-ctl for device format/resolution/framerate capabilities.

    Returns {ff_fmt: {"label": str, "sizes": {(w, h): [fps, ...]}}}
    """
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


# ── video preview widget ─────────────────────────────────────────────────────
class VideoDisplay(QLabel):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setMinimumSize(640, 360)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self._pixmap: QPixmap | None = None
        self._render_px: QPixmap | None = None
        self._render_pt: QPoint = QPoint(0, 0)
        self._scale_mode: str = "fit"
        self._crop_mode: str = "full"
        self._bg_color: QColor = QColor("#000000")

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
            # e.g. "area_16_9" → constrain output to a 16:9 sub-rect
            rw, rh = (int(v) for v in mode[5:].split("_"))
            scale  = min(W / rw, H / rh)
            aw, ah = int(rw * scale), int(rh * scale)
            s = px.scaled(aw, ah, Qt.AspectRatioMode.KeepAspectRatio, fast)
            self._render_px = s
            self._render_pt = QPoint((W - s.width()) // 2, (H - s.height()) // 2)
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


# ── helper: labelled slider row ───────────────────────────────────────────────
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
        self._caps: dict = {}
        self._current_video_dev: str | None = None
        self._current_audio_dev: str | None = None
        self._pa_sink_input: int | None = None
        self._pa_poll_count: int = 0

        self._build_ui()
        self._apply_dark_theme()
        self._load_settings()
        self._start_video()

    # ── UI construction ───────────────────────────────────────────────────────
    def _build_ui(self):
        root = QWidget()
        self.setCentralWidget(root)
        root_layout = QHBoxLayout(root)
        root_layout.setContentsMargins(0, 0, 0, 0)
        root_layout.setSpacing(0)

        self._display = VideoDisplay()
        root_layout.addWidget(self._display, stretch=1)

        # Thin toggle strip between video and panel
        self._panel_toggle = QPushButton("◀")
        self._panel_toggle.setFixedWidth(16)
        self._panel_toggle.setSizePolicy(
            QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Expanding
        )
        self._panel_toggle.setToolTip("Hide / show control panel")
        self._panel_toggle.setStyleSheet("""
            QPushButton {
                background: #252525;
                border: none;
                border-left: 1px solid #3a3a3a;
                border-radius: 0;
                padding: 0;
                color: #666;
                font-size: 9px;
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
        panel_layout.setSpacing(6)

        tabs = QTabWidget()
        tabs.addTab(self._build_video_tab(), "Video")
        tabs.addTab(self._build_audio_tab(), "Audio")
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

        # ── Capture settings ──────────────────────────────────────────────
        cap = QGroupBox("Capture Settings")
        cl = QVBoxLayout(cap)

        cl.addWidget(QLabel("Device:"))
        self._video_devices = _scan_video_devices()
        self._device_combo = QComboBox()
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

        apply_btn = QPushButton("Apply && Restart Capture")
        apply_btn.clicked.connect(self._restart_video)
        cl.addWidget(apply_btn)
        layout.addWidget(cap)

        # ── Display ───────────────────────────────────────────────────────
        disp = QGroupBox("Display")
        dl = QVBoxLayout(disp)
        dl.addWidget(QLabel("Scale Mode:"))
        self._scale_combo = QComboBox()
        for label, key in [
            ("Fit (Keep Aspect)",   "fit"),
            ("Stretch to Fill",     "stretch"),
            ("Zoom to Fill (Crop)", "fill"),
            ("Native (1:1 Pixels)", "native"),
            ("Fit to 16:9 Area",    "area_16_9"),
            ("Fit to 10:9 Area",    "area_10_9"),
            ("Fit to 5:4 Area",     "area_5_4"),
            ("Fit to 4:3 Area",     "area_4_3"),
        ]:
            self._scale_combo.addItem(label, key)
        self._scale_combo.currentIndexChanged.connect(self._on_scale_mode_changed)
        dl.addWidget(self._scale_combo)

        dl.addWidget(QLabel("Crop:"))
        self._crop_combo = QComboBox()
        for label, key in [
            ("Full Image",   "full"),
            ("Crop to 10:9", "10:9"),
            ("Crop to 5:4",  "5:4"),
            ("Crop to 4:3",  "4:3"),
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

        # ── Image controls ────────────────────────────────────────────────
        img = QGroupBox("Image Controls")
        il = QVBoxLayout(img)
        self._v4l2_sliders: dict[str, QSlider] = {}

        for ctrl, label in [
            ("brightness", "Brightness"),
            ("contrast",   "Contrast"),
            ("saturation", "Saturation"),
            ("hue",        "Hue"),
        ]:
            il.addWidget(QLabel(label + ":"))
            row, slider, _ = _slider_row(
                0, 100, 50,
                lambda v, c=ctrl: self._set_v4l2(c, v)
            )
            il.addLayout(row)
            self._v4l2_sliders[ctrl] = slider

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

        # ── Audio device ──────────────────────────────────────────────────
        dev_grp = QGroupBox("Audio Device")
        dl = QVBoxLayout(dev_grp)
        self._audio_devices = _scan_audio_devices()
        self._audio_device_combo = QComboBox()
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

        # ── Enable + meters ───────────────────────────────────────────────
        self._audio_enabled = QCheckBox("Enable Audio Monitor")
        self._audio_enabled.stateChanged.connect(self._on_audio_toggle)
        layout.addWidget(self._audio_enabled)

        self._vu = StereoVuMeter()
        layout.addWidget(self._vu)

        # ── Audio options ─────────────────────────────────────────────────
        opts = QGroupBox("Audio Options")
        ol = QVBoxLayout(opts)

        self._mono_mix = QCheckBox("Force Mono Mix")
        self._mono_mix.setToolTip(
            "Mix L+R into a single mono signal and send it to both\n"
            "output channels. Useful when the source sends audio\n"
            "on only one channel (e.g. single-channel HDMI audio)."
        )
        self._mono_mix.stateChanged.connect(self._on_audio_opt_change)
        ol.addWidget(self._mono_mix)

        self._passthrough = QCheckBox("Passthrough to System Audio")
        self._passthrough.setToolTip(
            "Route captured audio to the default PulseAudio/PipeWire\n"
            "output device. Mono Mix applies to passthrough too."
        )
        self._passthrough.stateChanged.connect(self._on_audio_opt_change)
        ol.addWidget(self._passthrough)
        layout.addWidget(opts)

        # ── Volume controls ───────────────────────────────────────────────
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
        self._vol_slider.sliderReleased.connect(self._save_settings)
        self._vol_l_slider.sliderReleased.connect(self._save_settings)
        self._vol_r_slider.sliderReleased.connect(self._save_settings)
        layout.addWidget(vol_grp)

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
                border: 1px solid #3a3a3a;
                border-radius: 4px;
                margin-top: 8px;
                padding-top: 4px;
            }
            QGroupBox::title {
                subcontrol-origin: margin;
                left: 8px;
                color: #aaaaaa;
            }
            QSlider::groove:horizontal {
                height: 4px;
                background: #3a3a3a;
                border-radius: 2px;
            }
            QSlider::handle:horizontal {
                background: #0078d4;
                width: 14px;
                height: 14px;
                margin: -5px 0;
                border-radius: 7px;
            }
            QSlider::sub-page:horizontal {
                background: #0078d4;
                border-radius: 2px;
            }
            QScrollArea { background: #252525; }
            QTabWidget::pane { border: 1px solid #3a3a3a; }
            QTabBar::tab {
                background: #2a2a2a;
                color: #aaaaaa;
                padding: 5px 12px;
                border: 1px solid #3a3a3a;
            }
            QTabBar::tab:selected { background: #1e1e1e; color: #ffffff; }
            QPushButton {
                background: #3a3a3a;
                border: 1px solid #555;
                border-radius: 3px;
                padding: 4px 8px;
                color: #dddddd;
            }
            QPushButton:hover  { background: #484848; }
            QPushButton:pressed { background: #0078d4; }
            QComboBox {
                background: #2a2a2a;
                border: 1px solid #3a3a3a;
                border-radius: 3px;
                padding: 3px 6px;
            }
            QComboBox::drop-down { border: none; }
            QCheckBox { spacing: 6px; }
            QStatusBar { background: #161616; color: #888888; }
        """)

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
        ff_fmt = self._fmt_combo.currentData()
        if self._caps and ff_fmt and ff_fmt in self._caps:
            all_sizes = sorted(self._caps[ff_fmt]["sizes"].keys(),
                               key=lambda s: s[0] * s[1], reverse=True)
        else:
            all_sizes = [(w, h) for _, w, h in _DEFAULT_RESOLUTIONS]

        # Group by aspect ratio (preserving insertion order = largest-first within each group)
        groups: dict[str, list[tuple[int, int]]] = {}
        for w, h in all_sizes:
            groups.setdefault(_aspect_label(w, h), []).append((w, h))
        self._res_aspect_groups = groups

        # Rebuild tab bar
        self._res_aspect_bar.blockSignals(True)
        while self._res_aspect_bar.count():
            self._res_aspect_bar.removeTab(0)
        for label in groups:
            self._res_aspect_bar.addTab(label)
        self._res_aspect_bar.blockSignals(False)

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

    def _on_res_changed(self):
        self._populate_fps_combo()

    # ── per-device video settings ─────────────────────────────────────────────
    def _save_video_device_settings(self, dev: str):
        s = QSettings("HagibisMonitor", "HagibisMonitor")
        k = _dev_key(dev)
        fmt = self._fmt_combo.currentData()
        res = self._res_combo.currentData()
        fps = self._fps_combo.currentData()
        if fmt:
            s.setValue(f"cap/{k}/fmt", fmt)
        if res:
            s.setValue(f"cap/{k}/res", f"{res[0]}x{res[1]}")
        if fps:
            s.setValue(f"cap/{k}/fps", fps)
        for ctrl, slider in self._v4l2_sliders.items():
            s.setValue(f"cap/{k}/{ctrl}", slider.value())

    def _load_video_device_settings(self, dev: str):
        s = QSettings("HagibisMonitor", "HagibisMonitor")
        k = _dev_key(dev)

        saved_fmt = s.value(f"cap/{k}/fmt", "")
        self._fmt_combo.blockSignals(True)
        for i in range(self._fmt_combo.count()):
            if self._fmt_combo.itemData(i) == saved_fmt:
                self._fmt_combo.setCurrentIndex(i)
                break
        self._fmt_combo.blockSignals(False)
        self._populate_res_combo()

        saved_res = s.value(f"cap/{k}/res", "")
        if saved_res:
            try:
                rw, rh = map(int, saved_res.split("x"))
                # Switch to the matching aspect tab first
                target_aspect = _aspect_label(rw, rh)
                self._res_aspect_bar.blockSignals(True)
                for i in range(self._res_aspect_bar.count()):
                    if self._res_aspect_bar.tabText(i) == target_aspect:
                        self._res_aspect_bar.setCurrentIndex(i)
                        break
                self._res_aspect_bar.blockSignals(False)
                self._fill_res_combo_for_tab()
                # Now select the resolution
                self._res_combo.blockSignals(True)
                for i in range(self._res_combo.count()):
                    if self._res_combo.itemData(i) == (rw, rh):
                        self._res_combo.setCurrentIndex(i)
                        break
                self._res_combo.blockSignals(False)
            except (ValueError, AttributeError):
                pass
        self._populate_fps_combo()

        saved_fps = s.value(f"cap/{k}/fps", 0, type=int)
        self._fps_combo.blockSignals(True)
        if saved_fps:
            for i in range(self._fps_combo.count()):
                if self._fps_combo.itemData(i) == saved_fps:
                    self._fps_combo.setCurrentIndex(i)
                    break
        self._fps_combo.blockSignals(False)

        for ctrl, slider in self._v4l2_sliders.items():
            slider.blockSignals(True)
            slider.setValue(int(s.value(f"cap/{k}/{ctrl}", 50)))
            slider.blockSignals(False)

    def _on_video_device_changed(self):
        if self._current_video_dev:
            self._save_video_device_settings(self._current_video_dev)
        dev = self._device_combo.currentData() or "/dev/video0"
        self._current_video_dev = dev
        QSettings("HagibisMonitor", "HagibisMonitor").setValue("cap/device", dev)
        self._caps = _query_device_caps(dev)
        self._populate_fmt_combo()
        self._load_video_device_settings(dev)

    def _on_scale_mode_changed(self):
        self._display.set_scale_mode(self._scale_combo.currentData())
        self._save_settings()

    def _on_crop_mode_changed(self):
        self._display.set_crop_mode(self._crop_combo.currentData())
        self._save_settings()

    def _pick_bg_color(self):
        color = QColorDialog.getColor(
            self._display._bg_color, self, "Choose Background Color"
        )
        if color.isValid():
            self._apply_bg_color(color)
            self._save_settings()

    def _apply_bg_color(self, color: QColor):
        self._display.set_bg_color(color)
        self._bg_color_btn.setStyleSheet(
            f"background: {color.name()};"
            f"border: 1px solid #555;"
            f"border-radius: 3px;"
        )

    def _refresh_video_devices(self):
        current = self._device_combo.currentData()
        self._video_devices = _scan_video_devices()
        self._device_combo.blockSignals(True)
        self._device_combo.clear()
        for label, path in self._video_devices:
            self._device_combo.addItem(label, path)
        for i in range(self._device_combo.count()):
            if self._device_combo.itemData(i) == current:
                self._device_combo.setCurrentIndex(i)
                break
        self._device_combo.blockSignals(False)

    # ── per-device audio settings ─────────────────────────────────────────────
    def _save_audio_device_settings(self, dev: str):
        s = QSettings("HagibisMonitor", "HagibisMonitor")
        k = _dev_key(dev)
        s.setValue(f"audio/{k}/mono_mix",    self._mono_mix.isChecked())
        s.setValue(f"audio/{k}/passthrough", self._passthrough.isChecked())
        s.setValue(f"audio/{k}/volume_db",   self._vol_slider.value())
        s.setValue(f"audio/{k}/volume_l_db", self._vol_l_slider.value())
        s.setValue(f"audio/{k}/volume_r_db", self._vol_r_slider.value())

    def _load_audio_device_settings(self, dev: str):
        s = QSettings("HagibisMonitor", "HagibisMonitor")
        k = _dev_key(dev)
        self._mono_mix.blockSignals(True)
        self._passthrough.blockSignals(True)
        self._mono_mix.setChecked(s.value(f"audio/{k}/mono_mix", False, type=bool))
        self._passthrough.setChecked(s.value(f"audio/{k}/passthrough", False, type=bool))
        self._mono_mix.blockSignals(False)
        self._passthrough.blockSignals(False)
        self._vol_slider.setValue(int(s.value(f"audio/{k}/volume_db", 0)))
        self._vol_l_slider.setValue(int(s.value(f"audio/{k}/volume_l_db", 0)))
        self._vol_r_slider.setValue(int(s.value(f"audio/{k}/volume_r_db", 0)))

    def _on_audio_device_changed(self):
        if self._current_audio_dev:
            self._save_audio_device_settings(self._current_audio_dev)
        dev = self._audio_device_combo.currentData() or "plughw:Hagibis,0"
        self._current_audio_dev = dev
        self._load_audio_device_settings(dev)
        if self._audio_enabled.isChecked():
            self._start_audio()
        self._save_settings()

    def _refresh_audio_devices(self):
        current = self._audio_device_combo.currentData()
        self._audio_devices = _scan_audio_devices()
        self._audio_device_combo.blockSignals(True)
        self._audio_device_combo.clear()
        for label, path in self._audio_devices:
            self._audio_device_combo.addItem(label, path)
        for i in range(self._audio_device_combo.count()):
            if self._audio_device_combo.itemData(i) == current:
                self._audio_device_combo.setCurrentIndex(i)
                break
        self._audio_device_combo.blockSignals(False)

    # ── global settings ───────────────────────────────────────────────────────
    def _load_settings(self):
        s = QSettings("HagibisMonitor", "HagibisMonitor")

        geom = s.value("window/geometry")
        if geom:
            self.restoreGeometry(geom)
        state = s.value("window/state")
        if state:
            self.restoreState(state)
        panel_visible = s.value("window/panel_visible", True, type=bool)
        self._panel_scroll.setVisible(panel_visible)
        self._panel_toggle.setText("◀" if panel_visible else "▶")
        saved_mode = s.value("display/scale_mode", "fit")
        for i in range(self._scale_combo.count()):
            if self._scale_combo.itemData(i) == saved_mode:
                self._scale_combo.setCurrentIndex(i)
                break
        self._display.set_scale_mode(self._scale_combo.currentData())
        saved_crop = s.value("display/crop_mode", "full")
        for i in range(self._crop_combo.count()):
            if self._crop_combo.itemData(i) == saved_crop:
                self._crop_combo.setCurrentIndex(i)
                break
        self._display.set_crop_mode(self._crop_combo.currentData())
        self._apply_bg_color(QColor(s.value("display/bg_color", "#000000")))

        # Video device — block signal, set index, then call handler explicitly
        self._device_combo.blockSignals(True)
        saved_dev = s.value("cap/device", "")
        for i in range(self._device_combo.count()):
            if self._device_combo.itemData(i) == saved_dev:
                self._device_combo.setCurrentIndex(i)
                break
        self._device_combo.blockSignals(False)
        self._on_video_device_changed()

        # Audio device
        self._audio_device_combo.blockSignals(True)
        saved_audio = s.value("audio/device", "")
        for i in range(self._audio_device_combo.count()):
            if self._audio_device_combo.itemData(i) == saved_audio:
                self._audio_device_combo.setCurrentIndex(i)
                break
        self._audio_device_combo.blockSignals(False)
        self._on_audio_device_changed()

        # Audio enabled (block signal, restore, then start manually if needed)
        self._audio_enabled.blockSignals(True)
        self._audio_enabled.setChecked(s.value("audio/enabled", True, type=bool))
        self._audio_enabled.blockSignals(False)
        self._update_vol_slider_states()
        if self._audio_enabled.isChecked():
            self._start_audio()

    def _save_settings(self):
        s = QSettings("HagibisMonitor", "HagibisMonitor")
        s.setValue("window/geometry",      self.saveGeometry())
        s.setValue("window/state",         self.saveState())
        s.setValue("window/panel_visible", self._panel_scroll.isVisible())
        s.setValue("display/scale_mode",   self._scale_combo.currentData())
        s.setValue("display/crop_mode",    self._crop_combo.currentData())
        s.setValue("display/bg_color",     self._display._bg_color.name())
        s.setValue("cap/device",      self._device_combo.currentData() or "/dev/video0")
        s.setValue("audio/device",    self._audio_device_combo.currentData() or "plughw:Hagibis,0")
        s.setValue("audio/enabled",   self._audio_enabled.isChecked())
        if self._current_video_dev:
            self._save_video_device_settings(self._current_video_dev)
        if self._current_audio_dev:
            self._save_audio_device_settings(self._current_audio_dev)

    # ── capture params ────────────────────────────────────────────────────────
    def _cap_params(self) -> tuple[int, int, int, str, str]:
        w, h = self._res_combo.currentData() or (1280, 720)
        fps  = self._fps_combo.currentData() or 30
        fmt  = self._fmt_combo.currentData() or "mjpeg"
        dev  = self._device_combo.currentData() or "/dev/video0"
        return w, h, fps, fmt, dev

    # ── video management ──────────────────────────────────────────────────────
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
        self._save_settings()

    # ── V4L2 controls ─────────────────────────────────────────────────────────
    def _set_v4l2(self, ctrl: str, value: int):
        dev = self._device_combo.currentData() or "/dev/video0"
        subprocess.Popen(
            ["v4l2-ctl", "-d", dev, f"--set-ctrl={ctrl}={value}"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        self._save_settings()

    def _reset_v4l2(self):
        for slider in self._v4l2_sliders.values():
            slider.setValue(50)

    # ── audio management ──────────────────────────────────────────────────────
    def _start_audio(self):
        self._stop_audio()
        wk = AudioWorker()
        wk.device      = self._audio_device_combo.currentData() or "plughw:Hagibis,0"
        wk.mono_mix    = self._mono_mix.isChecked()
        wk.passthrough = self._passthrough.isChecked()
        wk.volume_db   = self._vol_slider.value()
        wk.volume_l_db = self._vol_l_slider.value()
        wk.volume_r_db = self._vol_r_slider.value()
        wk.levels_updated.connect(self._vu.set_levels)
        wk.error.connect(lambda e: self._lbl_signal.setText(f"Audio error: {e}"))
        self._audio_worker = wk
        wk.start()
        self._pa_sink_input = None
        self._pa_poll_count = 0
        QTimer.singleShot(80, self._poll_pa_sink_input)

    def _stop_audio(self):
        self._pa_sink_input = None
        if self._audio_worker:
            self._audio_worker.stop()
            self._audio_worker.wait(3000)
            self._audio_worker = None
        self._vu.set_levels(-96.0, -96.0)

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

    def _on_audio_toggle(self, state: int):
        if state == Qt.CheckState.Checked.value:
            self._start_audio()
        else:
            self._stop_audio()
        self._update_vol_slider_states()
        self._save_settings()

    def _on_audio_opt_change(self):
        if self._audio_enabled.isChecked():
            self._start_audio()
        self._save_settings()

    def _update_vol_slider_states(self):
        on = self._audio_enabled.isChecked()
        for w in (self._vol_slider, self._vol_lbl,
                  self._vol_l_slider, self._vol_l_lbl,
                  self._vol_r_slider, self._vol_r_lbl):
            w.setEnabled(on)

    # ── volume handlers (real-time VU updates while dragging) ─────────────────
    def _on_vol_changed(self, v: int):
        self._vol_lbl.setText(_db_label(v))
        if self._audio_worker:
            self._audio_worker.volume_db = v
        self._apply_pa_volume()

    def _on_vol_l_changed(self, v: int):
        self._vol_l_lbl.setText(_db_label(v))
        if self._audio_worker:
            self._audio_worker.volume_l_db = v
        self._apply_pa_volume()

    def _on_vol_r_changed(self, v: int):
        self._vol_r_lbl.setText(_db_label(v))
        if self._audio_worker:
            self._audio_worker.volume_r_db = v
        self._apply_pa_volume()

    # ── panel visibility ──────────────────────────────────────────────────────
    def _toggle_panel(self):
        visible = not self._panel_scroll.isVisible()
        self._panel_scroll.setVisible(visible)
        self._panel_toggle.setText("◀" if visible else "▶")
        self._save_settings()

    # ── window close ─────────────────────────────────────────────────────────
    def closeEvent(self, event):
        self._save_settings()
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
