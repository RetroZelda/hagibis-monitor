import glob
import re
import subprocess
import sys
from collections import Counter

from PyQt6.QtCore import Qt, QPoint, QRect, QRectF, pyqtSignal
from PyQt6.QtGui import QColor, QFont, QImage, QPainter, QPen, QPixmap
from PyQt6.QtWidgets import QLabel, QSizePolicy

_IS_WINDOWS = sys.platform == "win32"
# Suppress the transient console window each ffmpeg spawn would otherwise flash
# under the windowed (console=False) Windows build.
_NO_WINDOW = {"creationflags": subprocess.CREATE_NO_WINDOW} if _IS_WINDOWS else {}

# DirectShow parsing regexes (Windows). ffmpeg's log-line prefix varies by
# version — current builds print "[in#0 @ 0x..]", older ones "[dshow @ 0x..]" —
# so anchor on a generic "[...]" prefix, never the literal "dshow".
_DSHOW_DEV_RE  = re.compile(r'^\[[^\]]+\]\s+"(?P<name>.+)"\s+\((?P<types>[^)]*)\)\s*$')
_DSHOW_ALT_RE  = re.compile(r'^\[[^\]]+\]\s+Alternative name\s+"(?P<alt>.+)"\s*$')
_DSHOW_MODE_RE = re.compile(
    r'(?P<kind>vcodec|pixel_format)=(?P<val>\w+)\s+'
    r'min s=(?P<minw>\d+)x(?P<minh>\d+) fps=(?P<minfps>[\d.]+)\s+'
    r'max s=(?P<maxw>\d+)x(?P<maxh>\d+) fps=(?P<maxfps>[\d.]+)')
# Discrete framerates offered in the UI, intersected with each mode's reported
# [min, max] fps range (the ceiling is always added on top).
_WIN_FPS_CANDIDATES = (60, 50, 30, 25, 20, 15, 10)


def _scan_video_devices() -> list[tuple[str, str]]:
    return _scan_video_devices_windows() if _IS_WINDOWS else _scan_video_devices_linux()


def _query_device_caps(dev: str) -> dict:
    return _query_device_caps_windows(dev) if _IS_WINDOWS else _query_device_caps_linux(dev)


# ── Linux (V4L2) ──────────────────────────────────────────────────────────────
def _scan_video_devices_linux() -> list[tuple[str, str]]:
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


def _query_device_caps_linux(dev: str) -> dict:
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


# ── Windows (DirectShow via ffmpeg) ───────────────────────────────────────────
def _list_dshow_devices(kind: str) -> list[tuple[str, str]]:
    """Return [(friendly_name, alternative_name)] for dshow devices of `kind`
    ("video" or "audio"). alternative_name is "" if none was printed.

    ffmpeg emits the device list to stderr and always exits non-zero on the
    "dummy" open, so parse stderr regardless of return code. Third-party driver
    logging (e.g. NVIDIA Broadcast) is interleaved and simply doesn't match the
    device/alt-name regexes. (audio.py shares this parser via kind="audio".)
    """
    try:
        r = subprocess.run(
            ["ffmpeg", "-hide_banner", "-list_devices", "true", "-f", "dshow", "-i", "dummy"],
            capture_output=True, timeout=10, **_NO_WINDOW,
        )
        text = r.stderr.decode(errors="replace")
    except Exception:
        return []
    out: list[list[str] | None] = []
    for line in text.splitlines():
        m = _DSHOW_DEV_RE.match(line)
        if m:
            types = {t.strip() for t in m["types"].split(",")}
            # Keep a slot (matching kind) or a None placeholder, so a following
            # Alternative-name line only ever attaches to the device above it.
            out.append([m["name"], ""] if kind in types else None)
            continue
        m = _DSHOW_ALT_RE.match(line)
        if m and out and out[-1] is not None and out[-1][1] == "":
            out[-1][1] = m["alt"]
    return [(e[0], e[1]) for e in out if e is not None]


def _apply_duplicate_name_policy(entries: list[tuple[str, str]]) -> list[tuple[str, str]]:
    """Map [(friendly, alt)] to [(display, device_id)].

    Prefer the friendly name as the stored id (human-readable in profile INIs,
    survives replug — mirrors the Linux `plughw:Name` decision). Fall back to
    the stable @device alternative name only when a friendly name is ambiguous
    (appears more than once), displaying it as "Name #N".
    """
    counts = Counter(name for name, _ in entries)
    seen: Counter = Counter()
    devices: list[tuple[str, str]] = []
    for name, alt in entries:
        if counts[name] > 1 and alt:
            seen[name] += 1
            devices.append((f"{name} #{seen[name]}", alt))
        else:
            devices.append((name, name))
    return devices


def _scan_video_devices_windows() -> list[tuple[str, str]]:
    # No fake fallback row on Windows — an empty list means "no camera", which
    # ui.py surfaces as a rescan hint rather than a bogus /dev/video0 entry.
    return _apply_duplicate_name_policy(_list_dshow_devices("video"))


def _query_device_caps_windows(dev: str) -> dict:
    FMT_MAP = {("vcodec", "mjpeg"):         ("mjpeg",   "MJPEG"),
               ("pixel_format", "yuyv422"): ("yuyv422", "YUYV")}
    try:
        r = subprocess.run(
            ["ffmpeg", "-hide_banner", "-list_options", "true", "-f", "dshow",
             "-i", f"video={dev}"],
            capture_output=True, timeout=10, **_NO_WINDOW,
        )
        text = r.stderr.decode(errors="replace")
    except Exception:
        return {}
    caps: dict = {}
    for m in _DSHOW_MODE_RE.finditer(text):
        key = (m["kind"], m["val"])
        if key not in FMT_MAP:
            continue  # nv12 etc. skipped, same intent as the Linux FMT_MAP
        ff, label = FMT_MAP[key]
        size = (int(m["maxw"]), int(m["maxh"]))  # min s == max s for discrete UVC modes
        minf, maxf = float(m["minfps"]), round(float(m["maxfps"]))  # 60.0002 → 60
        fps = {f for f in _WIN_FPS_CANDIDATES if minf <= f <= maxf}
        fps.add(maxf)  # always offer the reported ceiling
        entry = caps.setdefault(ff, {"label": label, "sizes": {}})
        # set() dedups the doubled mode lines (plain + "(tv, …)" suffix).
        entry["sizes"].setdefault(size, set()).update(fps)
    # Convert the fps sets to the sorted-list shape ui.py's combos expect.
    for entry in caps.values():
        entry["sizes"] = {sz: sorted(fl, reverse=True) for sz, fl in entry["sizes"].items()}
    return caps


# ── video preview widget ──────────────────────────────────────────────────────
class VideoDisplay(QLabel):
    output_changed = pyqtSignal(float, float, float)  # pan_x, pan_y, zoom

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setMinimumSize(640, 360)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self._pixmap:    QPixmap | None = None
        self._render_px: QPixmap | None = None
        self._render_pt: QPoint = QPoint(0, 0)
        # Output mode draws _render_px (the unscaled source) into this target
        # rect via QPainter sampling, so a large zoom never materialises a
        # multi-GB scaled pixmap. None in normal (non-output) mode.
        self._render_target: QRectF | None = None
        self._scale_mode:        str = "fit"
        self._crop_mode:         str = "full"
        self._output_scale_mode: str = "fit"
        self._output_crop_mode:  str = "full"
        self._bg_color:   QColor = QColor("#1f1f1f")

        self._output_enabled: bool  = False
        self._output_w:       int   = 1920
        self._output_h:       int   = 1080
        self._pan_x:          float = 0.0
        self._pan_y:          float = 0.0
        self._zoom:           float = 1.0

        self._drag_pos:    QPoint | None            = None
        self._drag_pan:    tuple[float, float] | None = None
        self._canvas_rect: QRect = QRect()

    def set_scale_mode(self, mode: str):
        self._scale_mode = mode
        self._refresh()

    def set_crop_mode(self, mode: str):
        self._crop_mode = mode
        self._refresh()

    def set_output_scale_mode(self, mode: str):
        self._output_scale_mode = mode
        self._refresh()

    def set_output_crop_mode(self, mode: str):
        self._output_crop_mode = mode
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

    def set_output_mode(self, enabled: bool, w: int, h: int):
        self._output_enabled = enabled
        self._output_w = w
        self._output_h = h
        self.setMouseTracking(enabled)
        self._refresh()

    def set_pan_zoom(self, pan_x: float, pan_y: float, zoom: float):
        self._pan_x = pan_x
        self._pan_y = pan_y
        self._zoom  = zoom
        self._refresh()

    def get_pan_zoom(self) -> tuple[float, float, float]:
        return self._pan_x, self._pan_y, self._zoom

    def _cropped(self, px: QPixmap, mode: str | None = None) -> QPixmap:
        mode = mode if mode is not None else self._crop_mode
        if mode == "full":
            return px
        try:
            rw, rh = (int(p) for p in mode.split(":"))
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
        W, H = self.width(), self.height()
        if W == 0 or H == 0:
            return
        if not self._pixmap:
            self._render_px = None
            if self._output_enabled:
                self._refresh_output(W, H)
            else:
                self._canvas_rect = QRect()
            self.update()
            return
        if self._output_enabled:
            self._refresh_output(W, H)
        else:
            self._canvas_rect = QRect()
            self._refresh_normal(W, H)
        self.update()

    def _refresh_normal(self, W: int, H: int):
        self._render_target = None
        px   = self._cropped(self._pixmap)
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

    def _refresh_output(self, W: int, H: int):
        """Position source video inside the output canvas area."""
        out_w, out_h = self._output_w, self._output_h
        disp_scale = min(W / out_w, H / out_h)
        canvas_w   = int(out_w * disp_scale)
        canvas_h   = int(out_h * disp_scale)
        canvas_x   = (W - canvas_w) // 2
        canvas_y   = (H - canvas_h) // 2
        self._canvas_rect = QRect(canvas_x, canvas_y, canvas_w, canvas_h)

        if not self._pixmap:
            self._render_px = None
            self._render_target = None
            return

        px = self._cropped(self._pixmap, self._output_crop_mode)
        src_w, src_h = px.width(), px.height()
        if src_w == 0 or src_h == 0:
            self._render_px = None
            self._render_target = None
            return

        mode   = self._output_scale_mode
        cw, ch = canvas_w, canvas_h

        if mode == "stretch":
            dw = max(1, int(cw * self._zoom))
            dh = max(1, int(ch * self._zoom))
        elif mode == "fill":
            s = max(cw / src_w, ch / src_h) * self._zoom
            dw, dh = max(1, int(src_w * s)), max(1, int(src_h * s))
        elif mode == "native":
            dw, dh = max(1, int(src_w * self._zoom)), max(1, int(src_h * self._zoom))
        elif mode.startswith("area_"):
            rw, rh = (int(v) for v in mode[5:].split("_"))
            as_ = min(cw / rw, ch / rh)
            s   = min(rw * as_ / src_w, rh * as_ / src_h) * self._zoom
            dw, dh = max(1, int(src_w * s)), max(1, int(src_h * s))
        elif mode.startswith("stretch_"):
            rw, rh = (int(v) for v in mode[8:].split("_"))
            as_ = min(cw / rw, ch / rh)
            dw, dh = max(1, int(rw * as_ * self._zoom)), max(1, int(rh * as_ * self._zoom))
        else:  # "fit"
            s = min(cw / src_w, ch / src_h) * self._zoom
            dw, dh = max(1, int(src_w * s)), max(1, int(src_h * s))

        cx = canvas_x + cw / 2
        cy = canvas_y + ch / 2
        dx = cx - dw / 2 + self._pan_x * disp_scale
        dy = cy - dh / 2 + self._pan_y * disp_scale

        # Keep the unscaled source; paintEvent samples it into this target rect
        # clipped to the canvas, so zoom never allocates a giant pixmap here.
        self._render_px = px
        self._render_target = QRectF(dx, dy, float(dw), float(dh))

    def resizeEvent(self, event):
        self._refresh()

    def paintEvent(self, event):
        p = QPainter(self)
        p.fillRect(self.rect(), self._bg_color)

        if self._output_enabled and not self._canvas_rect.isNull():
            p.fillRect(self._canvas_rect, self._bg_color)
            if self._render_px is not None and self._render_target is not None:
                p.setClipRect(self._canvas_rect)
                p.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, True)
                p.drawPixmap(self._render_target, self._render_px,
                             QRectF(self._render_px.rect()))
                p.setClipping(False)
            pen = QPen(QColor("#00e5a0"), 2)
            p.setPen(pen)
            p.drawRect(self._canvas_rect.adjusted(1, 1, -1, -1))
            p.setPen(QColor("#00e5a0"))
            p.setFont(QFont("sans-serif", 9))
            p.drawText(
                self._canvas_rect.x() + 6,
                self._canvas_rect.y() + 14,
                f"{self._output_w}×{self._output_h}  {self._zoom:.2f}×",
            )
        elif self._render_px is not None:
            p.drawPixmap(self._render_pt, self._render_px)

        if self._render_px is None:
            lum = (0.299 * self._bg_color.redF() +
                   0.587 * self._bg_color.greenF() +
                   0.114 * self._bg_color.blueF())
            p.setPen(QColor("#404040") if lum > 0.5 else QColor("#606060"))
            p.setFont(QFont("sans-serif", 20))
            p.drawText(self.rect(), Qt.AlignmentFlag.AlignCenter, "NO SIGNAL")

    def mousePressEvent(self, event):
        if self._output_enabled and event.button() == Qt.MouseButton.LeftButton:
            self._drag_pos = event.pos()
            self._drag_pan = (self._pan_x, self._pan_y)
            event.accept()
            return
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event):
        if self._output_enabled and self._drag_pos is not None:
            if not self._canvas_rect.isNull() and self._output_w > 0:
                disp_scale = self._canvas_rect.width() / self._output_w
                delta = event.pos() - self._drag_pos
                self._pan_x = self._drag_pan[0] + delta.x() / disp_scale
                self._pan_y = self._drag_pan[1] + delta.y() / disp_scale
                self._refresh()
                self.output_changed.emit(self._pan_x, self._pan_y, self._zoom)
            event.accept()
            return
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event):
        if self._output_enabled and event.button() == Qt.MouseButton.LeftButton:
            self._drag_pos = None
            self._drag_pan = None
            event.accept()
            return
        super().mouseReleaseEvent(event)

    def wheelEvent(self, event):
        if self._output_enabled:
            factor = 1.1 if event.angleDelta().y() > 0 else 1 / 1.1
            self._zoom = max(0.1, min(20.0, self._zoom * factor))
            self._refresh()
            self.output_changed.emit(self._pan_x, self._pan_y, self._zoom)
            event.accept()
            return
        super().wheelEvent(event)
