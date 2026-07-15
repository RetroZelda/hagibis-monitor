import re
import subprocess
from pathlib import Path

from PyQt6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QHBoxLayout, QVBoxLayout,
    QLabel, QSlider, QComboBox, QCheckBox, QPushButton, QGroupBox,
    QTabWidget, QTabBar, QScrollArea, QSizePolicy, QFrame,
    QColorDialog, QInputDialog, QMessageBox, QLineEdit
)
from PyQt6.QtCore import Qt, QPoint, QSettings, QTimer
from PyQt6.QtGui import QImage, QColor, QPalette

from workers import VideoWorker, AudioWorker, OutputWorker
from vu_meter import StereoVuMeter
from video import VideoDisplay, _scan_video_devices, _query_device_caps
from audio import _scan_audio_devices
from output import (
    _find_loopback_devices, _v4l2loopback_installed, _ModprobeWorker,
    _unload_v4l2loopback,
)
from settings import (
    AppSettings, OutputSettings,
    _DEFAULT_FORMATS, _DEFAULT_RESOLUTIONS, _DEFAULT_FRAMERATES,
    _OUTPUT_RESOLUTIONS, _OUTPUT_PIXEL_FORMATS, _OUTPUT_FPS,
)
from utils import _dev_key, _aspect_label, _slider_row, _db_label
from power import ScreenWakeInhibitor


# ── status bar ────────────────────────────────────────────────────────────────
class _StatusBar(QWidget):
    """Full-width status bar widget that locks the output status over the video display."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self._video_ref: QWidget | None = None

        self.signal_lbl = QLabel("Initialising…", self)
        self.fps_lbl    = QLabel("FPS: --", self)

        self._center = QWidget(self)
        row = QHBoxLayout(self._center)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(6)
        row.addWidget(QLabel("Output:", self._center))
        self.audio_lbl = QLabel("○ Audio", self._center)
        self.audio_lbl.setStyleSheet("color: #888888;")
        row.addWidget(self.audio_lbl)
        self.video_lbl = QLabel("○ Video", self._center)
        self.video_lbl.setStyleSheet("color: #888888;")
        row.addWidget(self.video_lbl)

    def set_video_ref(self, ref: QWidget):
        self._video_ref = ref

    def relayout(self):
        self._do_layout()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._do_layout()

    def _do_layout(self):
        W, H = self.width(), self.height()
        if W == 0 or H == 0:
            return

        fps_w = self.fps_lbl.sizeHint().width() + 8
        self.fps_lbl.setGeometry(W - fps_w, 0, fps_w, H)

        cw = self._center.sizeHint().width()
        if self._video_ref is not None and self._video_ref.isVisible():
            try:
                vx = self._video_ref.mapToGlobal(QPoint(self._video_ref.width() // 2, 0)).x()
                ox = self.mapToGlobal(QPoint(0, 0)).x()
                cx = vx - ox - cw // 2
                cx = max(4, min(W - fps_w - cw - 4, cx))
            except Exception:
                cx = max(4, (W - cw) // 2)
        else:
            cx = max(4, (W - cw) // 2)

        self._center.setGeometry(cx, 0, cw, H)
        self.signal_lbl.setGeometry(4, 0, max(0, cx - 8), H)


# ── main window ───────────────────────────────────────────────────────────────
class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Hagibis Monitor")
        self.setMinimumSize(960, 580)

        self._video_worker:    VideoWorker    | None = None
        self._audio_worker:    AudioWorker    | None = None
        self._output_worker:   OutputWorker   | None = None
        self._modprobe_worker: _ModprobeWorker | None = None
        self._v4l2_device:       str  = ""
        self._v4l2_loaded_by_us: bool = False
        self._caps:           dict = {}
        self._pa_sink_input:  int | None = None
        self._pa_poll_count:  int = 0
        self._current_profile: str  = "Default"
        self._dirty:           bool = False
        self._res_tab_selection: dict[str, tuple[int, int]] = {}
        self._output_settings: OutputSettings = OutputSettings()

        # Keep the screen awake while the app is open, like a video player.
        self._screen_wake = ScreenWakeInhibitor()

        self._build_ui()
        self._apply_dark_theme()
        self._load_settings()
        self._apply_screen_wake(self._keep_awake.isChecked())

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

        self._sb = _StatusBar()
        self._sb.set_video_ref(self._display)
        self.statusBar().addWidget(self._sb, 1)
        self._lbl_signal       = self._sb.signal_lbl
        self._lbl_fps          = self._sb.fps_lbl
        self._audio_status_lbl = self._sb.audio_lbl
        self._video_status_lbl = self._sb.video_lbl

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

        # Global (all-profiles) app setting, not part of the profile.
        self._keep_awake = QCheckBox("Keep screen awake while running")
        self._keep_awake.setChecked(True)
        self._keep_awake.setToolTip(
            "Prevent the screen from dimming, blanking, or sleeping while the app\n"
            "is open — like a video player.\n"
            "Saved globally and applies to every profile."
        )
        self._keep_awake.stateChanged.connect(self._on_keep_awake_changed)
        layout.addWidget(self._keep_awake)

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
        layout.addWidget(vol_grp)

        layout.addStretch()
        return w

    def _build_output_tab(self) -> QWidget:
        w = QWidget()
        layout = QVBoxLayout(w)
        layout.setContentsMargins(4, 6, 4, 4)
        layout.setSpacing(6)

        self._out_enabled = QCheckBox("Enable Output")
        self._out_enabled.setToolTip(
            "Stream video to a v4l2loopback virtual camera AND create a virtual\n"
            "audio input device. OBS captures the camera + audio input separately.\n"
            "Audio uses the Audio tab's volume and mono mix settings."
        )
        self._out_enabled.stateChanged.connect(self._on_out_enable_changed)
        layout.addWidget(self._out_enabled)

        obs_grp = QGroupBox("OBS Audio Setup")
        ol = QVBoxLayout(obs_grp)
        ol.addWidget(QLabel(
            "In OBS, add an Audio Input Capture source\n"
            "and select the device named below:"
        ))
        audio_dev_row = QHBoxLayout()
        self._output_device_edit = QLineEdit(AudioWorker.SOURCE_NAME)
        self._output_device_edit.setReadOnly(True)
        audio_dev_row.addWidget(self._output_device_edit, 1)
        copy_btn = QPushButton("Copy")
        copy_btn.setFixedWidth(52)
        copy_btn.setToolTip("Copy device name to clipboard")
        copy_btn.clicked.connect(
            lambda: QApplication.clipboard().setText(self._output_device_edit.text())
        )
        audio_dev_row.addWidget(copy_btn)
        ol.addLayout(audio_dev_row)
        note = QLabel(
            "Master volume, channel volumes, and mono mix\n"
            "from the Audio tab are all applied."
        )
        note.setStyleSheet("color: #888888; font-size: 11px;")
        ol.addWidget(note)
        layout.addWidget(obs_grp)

        vid_grp = QGroupBox("OBS Video Setup")
        vgl = QVBoxLayout(vid_grp)
        vgl.addWidget(QLabel(
            "In OBS, add a Video Capture Device source\n"
            "and select the device named below:"
        ))
        vid_dev_row = QHBoxLayout()
        self._output_video_combo = QComboBox()
        self._output_video_combo.setSizeAdjustPolicy(
            QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon
        )
        vid_dev_row.addWidget(self._output_video_combo, 1)
        refresh_vid_btn = QPushButton("↻")
        refresh_vid_btn.setFixedWidth(32)
        refresh_vid_btn.setToolTip("Re-scan for loopback devices")
        refresh_vid_btn.clicked.connect(self._refresh_output_video_devices)
        vid_dev_row.addWidget(refresh_vid_btn)
        copy_vid_btn = QPushButton("Copy")
        copy_vid_btn.setFixedWidth(52)
        copy_vid_btn.setToolTip("Copy selected device path to clipboard")
        copy_vid_btn.clicked.connect(
            lambda: QApplication.clipboard().setText(
                self._output_video_combo.currentData() or ""
            )
        )
        vid_dev_row.addWidget(copy_vid_btn)
        vgl.addLayout(vid_dev_row)
        self._out_setup_lbl = QLabel(
            "v4l2loopback module not installed.\n\n"
            "Install it with:\n"
            "  sudo apt install v4l2loopback-dkms\n\n"
            "Then re-open this app."
        )
        self._out_setup_lbl.setStyleSheet("color: #f0a000; font-size: 10px;")
        self._out_setup_lbl.setWordWrap(True)
        self._out_setup_lbl.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse
        )
        self._out_setup_lbl.setVisible(not _v4l2loopback_installed())
        vgl.addWidget(self._out_setup_lbl)
        layout.addWidget(vid_grp)

        res_grp = QGroupBox("Output Resolution")
        rgl = QVBoxLayout(res_grp)
        self._out_res_aspect_bar = QTabBar()
        self._out_res_aspect_bar.setExpanding(False)
        self._out_res_aspect_bar.setUsesScrollButtons(True)
        for aspect in _OUTPUT_RESOLUTIONS:
            self._out_res_aspect_bar.addTab(aspect)
        rgl.addWidget(self._out_res_aspect_bar)
        self._out_res_combo = QComboBox()
        rgl.addWidget(self._out_res_combo)
        self._out_res_aspect_bar.currentChanged.connect(self._on_out_res_aspect_changed)
        self._out_res_combo.currentIndexChanged.connect(self._on_out_res_changed)
        self._fill_out_res_combo()
        layout.addWidget(res_grp)

        fmt_grp = QGroupBox("Pixel Format")
        fgl = QVBoxLayout(fmt_grp)
        self._out_pix_fmt_combo = QComboBox()
        for label, key in _OUTPUT_PIXEL_FORMATS:
            self._out_pix_fmt_combo.addItem(label, key)
        self._out_pix_fmt_combo.currentIndexChanged.connect(self._on_out_settings_changed)
        fgl.addWidget(self._out_pix_fmt_combo)
        layout.addWidget(fmt_grp)

        fps_grp = QGroupBox("Frame Rate")
        fpgl = QVBoxLayout(fps_grp)
        self._out_fps_combo = QComboBox()
        for fps in _OUTPUT_FPS:
            self._out_fps_combo.addItem(f"{fps} fps", fps)
        self._out_fps_combo.currentIndexChanged.connect(self._on_out_settings_changed)
        fpgl.addWidget(self._out_fps_combo)
        layout.addWidget(fps_grp)

        disp_grp = QGroupBox("Scale && Crop")
        dgl = QVBoxLayout(disp_grp)
        dgl.addWidget(QLabel("Scale Mode:"))
        self._out_scale_combo = QComboBox()
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
            self._out_scale_combo.addItem(label, key)
        self._out_scale_combo.currentIndexChanged.connect(self._on_out_scale_mode_changed)
        dgl.addWidget(self._out_scale_combo)
        dgl.addWidget(QLabel("Crop:"))
        self._out_crop_combo = QComboBox()
        for label, key in [
            ("Full Image",    "full"),
            ("Crop to 10:9",  "10:9"),
            ("Crop to 5:4",   "5:4"),
            ("Crop to 4:3",   "4:3"),
        ]:
            self._out_crop_combo.addItem(label, key)
        self._out_crop_combo.currentIndexChanged.connect(self._on_out_crop_mode_changed)
        dgl.addWidget(self._out_crop_combo)
        layout.addWidget(disp_grp)

        pz_grp = QGroupBox("Pan / Zoom")
        pzl = QVBoxLayout(pz_grp)
        hint = QLabel("Drag the preview to pan · scroll to zoom")
        hint.setStyleSheet("color: #888; font-size: 10px;")
        pzl.addWidget(hint)
        pz_row = QHBoxLayout()
        pz_row.addWidget(QLabel("Pan X:"))
        self._out_pan_x_lbl = QLabel("0.0")
        self._out_pan_x_lbl.setFixedWidth(46)
        pz_row.addWidget(self._out_pan_x_lbl)
        pz_row.addWidget(QLabel("Y:"))
        self._out_pan_y_lbl = QLabel("0.0")
        self._out_pan_y_lbl.setFixedWidth(46)
        pz_row.addWidget(self._out_pan_y_lbl)
        pz_row.addWidget(QLabel("Zoom:"))
        self._out_zoom_lbl = QLabel("1.00×")
        self._out_zoom_lbl.setFixedWidth(46)
        pz_row.addWidget(self._out_zoom_lbl)
        pzl.addLayout(pz_row)
        reset_pz_btn = QPushButton("Reset Pan / Zoom")
        reset_pz_btn.clicked.connect(self._reset_output_pan_zoom)
        pzl.addWidget(reset_pz_btn)
        layout.addWidget(pz_grp)

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
            output_scale_mode = self._out_scale_combo.currentData() or "fit",
            output_crop_mode  = self._out_crop_combo.currentData() or "full",
            pan_x             = self._display._pan_x,
            pan_y             = self._display._pan_y,
            zoom              = self._display._zoom,
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

        def cap_int(key: str, default: int) -> int:
            try:
                return int(cap(key, default))
            except (TypeError, ValueError):
                try:
                    return int(float(cap(key, default)))  # tolerate "30.0"
                except (TypeError, ValueError):
                    return default

        def out_float(key: str, default: float) -> float:
            try:
                return float(_s(key, default) or default)
            except (TypeError, ValueError):
                return default

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
            video_fps     = cap_int("fps", 30),
            brightness    = cap_int("brightness", 50),
            contrast      = cap_int("contrast", 50),
            saturation    = cap_int("saturation", 50),
            hue           = cap_int("hue", 50),
            audio_device  = adev,
            audio_enabled = _b("audio/enabled", True),
            mono_mix      = aud("mono_mix",    False, bool),
            passthrough   = aud("passthrough", False, bool),
            volume_db     = aud("volume_db",   0,     int),
            volume_l_db   = aud("volume_l_db", 0,     int),
            volume_r_db   = aud("volume_r_db", 0,     int),
            output_scale_mode = _s("output/scale_mode", "fit"),
            output_crop_mode  = _s("output/crop_mode",  "full"),
            pan_x             = out_float("output/pan_x", 0.0),
            pan_y             = out_float("output/pan_y", 0.0),
            # Clamp to the same range the zoom wheel enforces, so a corrupt or
            # hand-edited profile can't request a pathologically large zoom.
            zoom              = max(0.1, min(20.0, out_float("output/zoom", 1.0))),
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
        s.setValue("output/scale_mode",    settings.output_scale_mode)
        s.setValue("output/crop_mode",     settings.output_crop_mode)
        s.setValue("output/pan_x",         settings.pan_x)
        s.setValue("output/pan_y",         settings.pan_y)
        s.setValue("output/zoom",          settings.zoom)
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
        video_found = self._select_combo_by_data(self._device_combo, settings.video_device)
        actual_video_dev = self._device_combo.currentData() or "/dev/video0"

        # Caps + format/res/fps combos — query the device actually selected, not
        # the saved one, so an unplugged card doesn't populate mismatched combos.
        self._caps = _query_device_caps(actual_video_dev)
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
        audio_found = self._select_combo_by_data(self._audio_device_combo, settings.audio_device)

        # Audio options (block signals to avoid premature restarts)
        for widget, val in [
            (self._audio_enabled,  settings.audio_enabled),
            (self._mono_mix,       settings.mono_mix),
            (self._passthrough,    settings.passthrough),
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

        # Output scale / crop / pan / zoom
        self._select_combo(self._out_scale_combo, settings.output_scale_mode)
        self._display.set_output_scale_mode(settings.output_scale_mode)
        self._select_combo(self._out_crop_combo, settings.output_crop_mode)
        self._display.set_output_crop_mode(settings.output_crop_mode)
        self._display.set_pan_zoom(settings.pan_x, settings.pan_y, settings.zoom)
        self._out_pan_x_lbl.setText(f"{settings.pan_x:.1f}")
        self._out_pan_y_lbl.setText(f"{settings.pan_y:.1f}")
        self._out_zoom_lbl.setText(f"{settings.zoom:.2f}×")

        # Restart streams
        self._restart_video()
        self._apply_v4l2_all()
        if settings.audio_enabled:
            self._start_audio()
        else:
            self._stop_audio()

        # Warn (last, so it isn't overwritten by the "Capturing…" message) if a
        # saved device is gone and we silently fell back to another one.
        missing = []
        if not video_found:
            missing.append(f"video device {settings.video_device}")
        if not audio_found:
            missing.append(f"audio device {settings.audio_device}")
        if missing:
            self._lbl_signal.setText("⚠ Saved " + " and ".join(missing) +
                                     " not found — using an available device instead")

    # ── combo helpers ─────────────────────────────────────────────────────────
    def _select_combo(self, combo: QComboBox, key: str) -> bool:
        combo.blockSignals(True)
        found = False
        for i in range(combo.count()):
            if combo.itemData(i) == key:
                combo.setCurrentIndex(i)
                found = True
                break
        combo.blockSignals(False)
        return found

    def _select_combo_by_data(self, combo: QComboBox, data) -> bool:
        combo.blockSignals(True)
        found = False
        for i in range(combo.count()):
            if combo.itemData(i) == data:
                combo.setCurrentIndex(i)
                found = True
                break
        combo.blockSignals(False)
        return found

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
                raw, ok = QInputDialog.getText(
                    self, "Save as New Profile", "Profile name:"
                )
                new_name = self._sanitize_profile_name(raw) if ok else None
                if new_name and new_name != name and self._confirm_overwrite(new_name):
                    self._save_to_disk(self._collect_settings(), new_name)
                    self._populate_profile_combo()

        self._switch_profile(name)

    def _sanitize_profile_name(self, name: str) -> str | None:
        """Return a safe profile name, or None if empty/reserved/illegal.

        The name is interpolated straight into a file path (_profile_path), so
        reject anything with path separators or traversal.
        """
        name = (name or "").strip()
        if not name or name == "Default":
            return None
        if "/" in name or "\\" in name or ".." in name or name != Path(name).name:
            return None
        return name

    def _confirm_overwrite(self, name: str) -> bool:
        """True if the profile may be written — asking first if it already exists."""
        if not self._profile_path(name).exists():
            return True
        return QMessageBox.question(
            self, "Overwrite Profile?",
            f'Profile "{name}" already exists. Overwrite it with the current settings?',
        ) == QMessageBox.StandardButton.Yes

    def _switch_profile(self, name: str):
        self._current_profile = name
        QSettings("HagibisMonitor", "HagibisMonitor").setValue("profile/current", name)
        self._del_profile_btn.setEnabled(name != "Default")
        settings = self._load_from_disk(name)
        self._apply_settings(settings)
        # Keep the combo showing the profile that is actually active (e.g. after
        # a "Save as New Profile…" switch that left it pointing at the old one).
        self._populate_profile_combo()
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
        raw, ok = QInputDialog.getText(self, "New Profile", "Profile name:")
        if not ok:
            return
        name = self._sanitize_profile_name(raw)
        if name is None:
            QMessageBox.warning(
                self, "Invalid Name",
                "Profile name can't be empty, 'Default', or contain path separators.",
            )
            return
        if not self._confirm_overwrite(name):
            return
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

        # Global app setting (not per-profile): keep the screen awake.
        keep_awake = gs.value("power/keep_awake", True, type=bool)
        self._keep_awake.blockSignals(True)
        self._keep_awake.setChecked(keep_awake)
        self._keep_awake.blockSignals(False)

        # Migrate old flat settings into Default.ini if it doesn't exist yet
        if not self._profile_path("Default").exists():
            ps = self._profile_settings("Default")
            for key in gs.allKeys():
                if (not key.startswith("window/") and not key.startswith("power/")
                        and key != "profile/current"):
                    ps.setValue(key, gs.value(key))
            ps.sync()

        self._current_profile = gs.value("profile/current", "Default")
        self._populate_profile_combo()
        settings = self._load_from_disk(self._current_profile)
        self._apply_settings(settings)
        self._clear_dirty()

        self._load_output_settings(gs)
        self._apply_output_settings_to_ui()
        self._display.output_changed.connect(self._on_output_changed)

    def _save_global(self):
        gs = QSettings("HagibisMonitor", "HagibisMonitor")
        gs.setValue("window/geometry",     self.saveGeometry())
        gs.setValue("window/state",        self.saveState())
        gs.setValue("window/panel_visible", self._panel_scroll.isVisible())
        gs.setValue("profile/current",     self._current_profile)
        gs.setValue("power/keep_awake",    self._keep_awake.isChecked())

    # ── screen-wake (global setting) ──────────────────────────────────────────
    def _on_keep_awake_changed(self, state: int):
        enabled = state == Qt.CheckState.Checked.value
        QSettings("HagibisMonitor", "HagibisMonitor").setValue("power/keep_awake", enabled)
        self._apply_screen_wake(enabled)

    def _apply_screen_wake(self, enabled: bool):
        if enabled:
            self._screen_wake.inhibit()
        else:
            self._screen_wake.release()

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
        # One slot handles both the preview and the output feed, then tells the
        # worker the frame is done so it can send the next one (backpressure).
        wk.frame_ready.connect(self._on_frame)
        wk.fps_updated.connect(lambda f: self._lbl_fps.setText(f"FPS: {f:.1f}"))
        wk.error.connect(lambda e: self._lbl_signal.setText(f"Error: {e}"))
        self._video_worker = wk
        wk.start()
        self._lbl_signal.setText(
            f"Capturing {w}×{h} @ {fps} fps  [{fmt.upper()}]  {dev}"
        )

    def _stop_video(self):
        if self._video_worker:
            wk = self._video_worker
            # Disconnect first so a thread that is slow to exit can't keep
            # delivering frames to the display after we've dropped it.
            try:
                wk.frame_ready.disconnect(self._on_frame)
            except TypeError:
                pass
            wk.stop()
            wk.wait(3000)
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
        wk.virtual_output  = self._out_enabled.isChecked()
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
        if wk.virtual_output:
            QTimer.singleShot(500, self._apply_virtual_source_volume)
        self._update_output_status()

    def _stop_audio(self, teardown_virtual: bool = False):
        self._pa_sink_input = None
        if self._audio_worker:
            self._audio_worker.stop()
            self._audio_worker.wait(3000)
            if teardown_virtual:
                self._audio_worker.teardown()
            self._audio_worker = None
        self._vu.set_levels(-96.0, -96.0)
        self._update_output_status()

    def _on_audio_toggle(self, state: int):
        if state == Qt.CheckState.Checked.value:
            self._start_audio()
        else:
            self._stop_audio()
        self._update_vol_slider_states()
        self._mark_dirty()

    def _on_audio_opt_change(self):
        if self._audio_enabled.isChecked():
            self._start_audio()
        self._mark_dirty()

    # ── output tab helpers ────────────────────────────────────────────────────

    def _populate_output_video_combo(self, select_dev: str = ""):
        self._output_video_combo.blockSignals(True)
        self._output_video_combo.clear()
        for dev in _find_loopback_devices():
            self._output_video_combo.addItem(dev, dev)
        if select_dev:
            for i in range(self._output_video_combo.count()):
                if self._output_video_combo.itemData(i) == select_dev:
                    self._output_video_combo.setCurrentIndex(i)
                    break
        self._output_video_combo.blockSignals(False)

    def _refresh_output_video_devices(self):
        current = self._output_video_combo.currentData() or ""
        self._populate_output_video_combo(current)

    def _fill_out_res_combo(self):
        self._out_res_combo.blockSignals(True)
        self._out_res_combo.clear()
        tab_idx = self._out_res_aspect_bar.currentIndex()
        if tab_idx >= 0:
            aspect = self._out_res_aspect_bar.tabText(tab_idx)
            for label, w, h in _OUTPUT_RESOLUTIONS.get(aspect, []):
                self._out_res_combo.addItem(label, (w, h))
        self._out_res_combo.blockSignals(False)

    def _on_out_res_aspect_changed(self):
        self._fill_out_res_combo()
        self._on_out_settings_changed()

    def _on_out_res_changed(self):
        self._on_out_settings_changed()

    def _on_out_scale_mode_changed(self):
        self._display.set_output_scale_mode(self._out_scale_combo.currentData())
        self._mark_dirty()

    def _on_out_crop_mode_changed(self):
        self._display.set_output_crop_mode(self._out_crop_combo.currentData())
        self._mark_dirty()

    def _on_out_settings_changed(self):
        if self._out_enabled.isChecked():
            self._restart_output()

    def _on_out_enable_changed(self, state: int):
        enabled = state == Qt.CheckState.Checked.value
        if enabled:
            selected = self._output_video_combo.currentData() or ""
            # Only reuse the selected device if it still exists; a previous
            # disable may have unloaded it, leaving a dead node in the combo.
            if selected and Path(selected).exists():
                self._on_v4l2_loaded(selected, False)
            else:
                self._lbl_signal.setText("Loading v4l2loopback…")
                worker = _ModprobeWorker()
                worker.done.connect(self._on_v4l2_loaded)
                self._modprobe_worker = worker
                worker.start()
        else:
            self._stop_output()
            if self._v4l2_loaded_by_us:
                _unload_v4l2loopback()
                self._v4l2_loaded_by_us = False
            self._v4l2_device = ""
            # Rescan so the just-unloaded device is dropped from the combo.
            self._populate_output_video_combo()
            w, h = self._out_res_combo.currentData() or (1920, 1080)
            self._display.set_output_mode(False, w, h)
            self._stop_audio(teardown_virtual=True)
            if self._audio_enabled.isChecked():
                self._start_audio()
            self._update_output_status()
            self._save_output_settings()

    def _on_v4l2_loaded(self, dev: str, loaded_by_us: bool):
        if dev:
            self._v4l2_device = dev
            self._v4l2_loaded_by_us = loaded_by_us
            self._populate_output_video_combo(dev)
            w, h = self._out_res_combo.currentData() or (1920, 1080)
            self._display.set_output_mode(True, w, h)
            self._start_output()
            if self._audio_enabled.isChecked():
                self._start_audio()
        else:
            self._v4l2_device = ""
            self._v4l2_loaded_by_us = False
            self._lbl_signal.setText(
                "Failed to load v4l2loopback — install v4l2loopback-dkms and grant permission"
            )
            w, h = self._out_res_combo.currentData() or (1920, 1080)
            self._display.set_output_mode(True, w, h)
            if self._audio_enabled.isChecked():
                self._start_audio()
        self._update_output_status()
        self._save_output_settings()

    def _update_output_status(self):
        audio_active = self._out_enabled.isChecked() and self._audio_worker is not None
        if audio_active:
            self._audio_status_lbl.setText("● Audio")
            self._audio_status_lbl.setStyleSheet("color: #50c878;")
        else:
            self._audio_status_lbl.setText("○ Audio")
            self._audio_status_lbl.setStyleSheet("color: #888888;")

        video_active = self._out_enabled.isChecked() and self._output_worker is not None
        if video_active:
            self._video_status_lbl.setText("● Video")
            self._video_status_lbl.setStyleSheet("color: #50c878;")
        else:
            self._video_status_lbl.setText("○ Video")
            self._video_status_lbl.setStyleSheet("color: #888888;")

    def _reset_output_pan_zoom(self):
        self._display.set_pan_zoom(0.0, 0.0, 1.0)
        self._out_pan_x_lbl.setText("0.0")
        self._out_pan_y_lbl.setText("0.0")
        self._out_zoom_lbl.setText("1.00×")
        self._mark_dirty()

    def _on_output_changed(self, pan_x: float, pan_y: float, zoom: float):
        self._out_pan_x_lbl.setText(f"{pan_x:.1f}")
        self._out_pan_y_lbl.setText(f"{pan_y:.1f}")
        self._out_zoom_lbl.setText(f"{zoom:.2f}×")
        self._mark_dirty()

    # ── output stream management ──────────────────────────────────────────────

    def _start_output(self):
        self._stop_output()
        dev = self._v4l2_device
        if not dev:
            self._update_output_status()
            return
        w, h = self._out_res_combo.currentData() or (1920, 1080)
        fmt  = self._out_pix_fmt_combo.currentData() or "yuyv422"
        fps  = self._out_fps_combo.currentData() or 30
        wk = OutputWorker(dev, w, h, fps, fmt)
        wk.error.connect(lambda e: self._lbl_signal.setText(f"Output error: {e}"))
        self._output_worker = wk
        wk.start()
        self._update_output_status()

    def _stop_output(self):
        if self._output_worker:
            self._output_worker.stop()
            self._output_worker.wait(3000)
            self._output_worker = None
        self._update_output_status()

    def _restart_output(self):
        if self._out_enabled.isChecked():
            w, h = self._out_res_combo.currentData() or (1920, 1080)
            self._display.set_output_mode(True, w, h)
            self._stop_output()  # close device now → triggers SOURCE_CHANGE to readers
            QTimer.singleShot(400, self._start_output)  # reopen after readers react

    def _on_frame(self, img: QImage):
        self._display.set_frame(img)
        self._feed_output(img)
        wk = self._video_worker
        if wk is not None:
            wk.frame_consumed()

    def _feed_output(self, img: QImage):
        if self._output_worker is not None:
            pan_x, pan_y, zoom = self._display.get_pan_zoom()
            self._output_worker.push_frame(
                img, pan_x, pan_y, zoom,
                self._display._bg_color,
                self._display._output_scale_mode,
                self._display._output_crop_mode,
            )

    # ── output global settings I/O ────────────────────────────────────────────

    def _load_output_settings(self, gs: QSettings):
        def _b(k, d): return gs.value(f"output/{k}", d, type=bool)
        def _i(k, d):
            try: return int(gs.value(f"output/{k}", d))
            except (TypeError, ValueError): return d
        def _s(k, d):
            v = gs.value(f"output/{k}"); return v if v is not None else d

        self._output_settings = OutputSettings(
            enabled      = _b("enabled",      False),
            device       = _s("device",       ""),
            width        = _i("width",         1920),
            height       = _i("height",        1080),
            pixel_format = _s("pixel_format",  "yuyv422"),
            fps          = _i("fps",           30),
        )

    def _apply_output_settings_to_ui(self):
        os = self._output_settings
        target_aspect = None
        for aspect, entries in _OUTPUT_RESOLUTIONS.items():
            for _, w, h in entries:
                if w == os.width and h == os.height:
                    target_aspect = aspect
                    break
            if target_aspect:
                break
        if target_aspect:
            for i in range(self._out_res_aspect_bar.count()):
                if self._out_res_aspect_bar.tabText(i) == target_aspect:
                    self._out_res_aspect_bar.blockSignals(True)
                    self._out_res_aspect_bar.setCurrentIndex(i)
                    self._out_res_aspect_bar.blockSignals(False)
                    break
            self._fill_out_res_combo()
            self._select_combo_by_data(self._out_res_combo, (os.width, os.height))

        self._populate_output_video_combo(os.device)
        self._select_combo(self._out_pix_fmt_combo, os.pixel_format)
        self._select_combo_by_data(self._out_fps_combo, os.fps)

        self._out_enabled.blockSignals(True)
        self._out_enabled.setChecked(False)
        self._out_enabled.blockSignals(False)

        # Restore the enabled state, but only when the saved loopback device is
        # already present — auto-enabling otherwise would fire a modprobe/pkexec
        # prompt at launch, which the user didn't ask for.
        if os.enabled and os.device and os.device in _find_loopback_devices():
            self._out_enabled.setChecked(True)  # unblocked → enables via existing device

    def _save_output_settings(self):
        w, h = self._out_res_combo.currentData() or (1920, 1080)
        # Fall back to the combo's device so disabling output (which clears
        # _v4l2_device) doesn't erase the remembered device.
        device = self._v4l2_device or (self._output_video_combo.currentData() or "")
        gs = QSettings("HagibisMonitor", "HagibisMonitor")
        gs.setValue("output/enabled",      self._out_enabled.isChecked())
        gs.setValue("output/device",       device)
        gs.setValue("output/width",        w)
        gs.setValue("output/height",       h)
        gs.setValue("output/pixel_format", self._out_pix_fmt_combo.currentData() or "yuyv422")
        gs.setValue("output/fps",          self._out_fps_combo.currentData() or 30)
        gs.sync()

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
        self._apply_virtual_source_volume()
        self._mark_dirty()

    def _on_vol_l_changed(self, v: int):
        self._vol_l_lbl.setText(_db_label(v))
        if self._audio_worker:
            self._audio_worker.volume_l_db = v
        self._apply_pa_volume()
        self._apply_virtual_source_volume()
        self._mark_dirty()

    def _on_vol_r_changed(self, v: int):
        self._vol_r_lbl.setText(_db_label(v))
        if self._audio_worker:
            self._audio_worker.volume_r_db = v
        self._apply_pa_volume()
        self._apply_virtual_source_volume()
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

    def _apply_virtual_source_volume(self):
        if not self._out_enabled.isChecked():
            return
        master = self._vol_slider.value()
        l_pct = max(0, int(100 * 10 ** ((master + self._vol_l_slider.value()) / 20.0)))
        r_pct = max(0, int(100 * 10 ** ((master + self._vol_r_slider.value()) / 20.0)))
        subprocess.Popen(
            ["pactl", "set-sink-volume", AudioWorker.BUS_SINK,
             f"{l_pct}%", f"{r_pct}%"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )

    # ── panel toggle ──────────────────────────────────────────────────────────
    def _toggle_panel(self):
        visible = not self._panel_scroll.isVisible()
        self._panel_scroll.setVisible(visible)
        self._panel_toggle.setText("◀" if visible else "▶")
        self._save_global()
        QTimer.singleShot(0, self._sb.relayout)

    # ── window close ──────────────────────────────────────────────────────────
    def closeEvent(self, event):
        self._save_output_settings()
        self._save_global()
        # Don't silently overwrite the profile with unsaved experimental changes
        # (that would defeat Revert). Ask; if clean, disk already matches.
        if self._dirty:
            resp = QMessageBox.question(
                self, "Unsaved Changes",
                f'Save changes to profile "{self._current_profile}" before quitting?',
                QMessageBox.StandardButton.Save | QMessageBox.StandardButton.Discard,
            )
            if resp == QMessageBox.StandardButton.Save:
                self._save_to_disk(self._collect_settings(), self._current_profile)
        self._stop_output()
        if self._v4l2_loaded_by_us:
            _unload_v4l2loopback(silent=True)  # no pkexec dialog on exit
            self._v4l2_loaded_by_us = False
        self._stop_audio(teardown_virtual=True)
        self._stop_video()
        self._screen_wake.release()  # let the screen sleep normally again
        event.accept()
