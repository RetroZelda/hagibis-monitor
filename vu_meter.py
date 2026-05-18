from PyQt6.QtWidgets import QWidget, QHBoxLayout, QSizePolicy
from PyQt6.QtCore import Qt, QRect
from PyQt6.QtGui import QPainter, QColor, QFont


class VuMeter(QWidget):
    DB_MIN = -60.0
    DB_MAX = 0.0
    SEGMENTS = 30
    PEAK_HOLD_FRAMES = 90
    PEAK_DECAY = 0.4  # dB per update

    def __init__(self, label="L", parent=None):
        super().__init__(parent)
        self.label = label
        self.level = self.DB_MIN
        self.peak = self.DB_MIN
        self._peak_hold = 0
        self.setMinimumSize(18, 120)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)

    def set_level(self, db: float):
        self.level = max(self.DB_MIN, min(self.DB_MAX, db))
        if self.level >= self.peak:
            self.peak = self.level
            self._peak_hold = self.PEAK_HOLD_FRAMES
        else:
            if self._peak_hold > 0:
                self._peak_hold -= 1
            else:
                self.peak = max(self.DB_MIN, self.peak - self.PEAK_DECAY)
        self.update()

    def paintEvent(self, event):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing, False)
        w, h = self.width(), self.height()
        label_h = 14
        meter_h = h - label_h
        db_range = self.DB_MAX - self.DB_MIN
        seg_h_f = meter_h / self.SEGMENTS

        p.fillRect(0, 0, w, h, QColor("#111111"))

        for i in range(self.SEGMENTS):
            db_at = self.DB_MIN + (i / self.SEGMENTS) * db_range
            y = meter_h - int((i + 1) * seg_h_f)
            seg_h = max(1, int(seg_h_f) - 1)

            if db_at < self.level:
                if db_at >= -6:
                    color = QColor("#ff2020")
                elif db_at >= -12:
                    color = QColor("#ff9900")
                elif db_at >= -24:
                    color = QColor("#bbff00")
                else:
                    color = QColor("#00cc44")
            else:
                if db_at >= -6:
                    color = QColor("#3a0808")
                elif db_at >= -12:
                    color = QColor("#3a2500")
                elif db_at >= -24:
                    color = QColor("#2d3800")
                else:
                    color = QColor("#003318")

            p.fillRect(2, y, w - 4, seg_h, color)

        # Peak hold bar
        if self.peak > self.DB_MIN:
            peak_frac = (self.peak - self.DB_MIN) / db_range
            py = meter_h - int(peak_frac * meter_h) - 1
            py = max(0, min(meter_h - 2, py))
            if self.peak >= -6:
                pk_col = QColor("#ff4040")
            elif self.peak >= -12:
                pk_col = QColor("#ffbb00")
            else:
                pk_col = QColor("#44ff88")
            p.fillRect(1, py, w - 2, 2, pk_col)

        p.setPen(QColor("#777777"))
        p.setFont(QFont("Monospace", 7))
        p.drawText(QRect(0, meter_h + 1, w, label_h - 1), Qt.AlignmentFlag.AlignCenter, self.label)


class DbScale(QWidget):
    DB_MIN = -60.0
    DB_MAX = 0.0
    TICKS = [0, -3, -6, -12, -20, -40, -60]

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFixedWidth(28)
        self.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Expanding)

    def paintEvent(self, event):
        p = QPainter(self)
        w, h = self.width(), self.height()
        label_h = 14
        meter_h = h - label_h
        db_range = self.DB_MAX - self.DB_MIN

        p.fillRect(0, 0, w, h, QColor("#111111"))
        p.setFont(QFont("Monospace", 6))

        for db in self.TICKS:
            frac = (db - self.DB_MIN) / db_range
            y = meter_h - int(frac * meter_h)
            p.setPen(QColor("#555555"))
            p.drawLine(0, y, 3, y)
            p.drawLine(w - 3, y, w, y)
            p.setPen(QColor("#666666"))
            text = "0" if db == 0 else str(db)
            p.drawText(3, y - 5, w - 6, 11, Qt.AlignmentFlag.AlignCenter, text)


class StereoVuMeter(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setMinimumHeight(160)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(4, 4, 4, 4)
        layout.setSpacing(2)

        self._left = VuMeter("L")
        self._scale = DbScale()
        self._right = VuMeter("R")

        layout.addWidget(self._left)
        layout.addWidget(self._scale)
        layout.addWidget(self._right)

    def set_levels(self, l_db: float, r_db: float):
        self._left.set_level(l_db)
        self._right.set_level(r_db)
