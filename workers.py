import subprocess
import time
import numpy as np
from PyQt6.QtCore import QThread, pyqtSignal
from PyQt6.QtGui import QImage


class VideoWorker(QThread):
    frame_ready = pyqtSignal(QImage)
    fps_updated = pyqtSignal(float)
    error = pyqtSignal(str)

    def __init__(self):
        super().__init__()
        self.width = 1280
        self.height = 720
        self.fps = 30
        self.input_format = "mjpeg"
        self.device = "/dev/video0"
        self._running = False
        self._proc = None

    def configure(self, width: int, height: int, fps: int, input_format: str, device: str = "/dev/video0"):
        self.width = width
        self.height = height
        self.fps = fps
        self.input_format = input_format
        self.device = device

    def run(self):
        self._running = True
        cmd = [
            "ffmpeg", "-loglevel", "quiet",
            "-f", "v4l2",
            "-input_format", self.input_format,
            "-video_size", f"{self.width}x{self.height}",
            "-framerate", str(self.fps),
            "-i", self.device,
            "-f", "rawvideo",
            "-pix_fmt", "rgb24",
            "-",
        ]
        try:
            self._proc = subprocess.Popen(
                cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, bufsize=0
            )
        except FileNotFoundError:
            self.error.emit("ffmpeg not found")
            return

        frame_size = self.width * self.height * 3
        prev = time.monotonic()
        count = 0

        while self._running:
            raw = b""
            while len(raw) < frame_size and self._running:
                chunk = self._proc.stdout.read(frame_size - len(raw))
                if not chunk:
                    self._running = False
                    break
                raw += chunk

            if not self._running or len(raw) < frame_size:
                break

            count += 1
            now = time.monotonic()
            if now - prev >= 1.0:
                self.fps_updated.emit(count / (now - prev))
                count = 0
                prev = now

            img = QImage(raw, self.width, self.height, self.width * 3,
                         QImage.Format.Format_RGB888)
            self.frame_ready.emit(img.copy())

        if self._proc:
            self._proc.terminate()
            try:
                self._proc.wait(timeout=2)
            except subprocess.TimeoutExpired:
                self._proc.kill()

    def stop(self):
        self._running = False
        if self._proc:
            self._proc.terminate()


class AudioWorker(QThread):
    levels_updated = pyqtSignal(float, float)  # L dBFS, R dBFS
    error = pyqtSignal(str)

    SAMPLE_RATE = 48000
    CHUNK_FRAMES = 1024

    def __init__(self):
        super().__init__()
        self.device = "plughw:Hagibis,0"
        self.mono_mix = False
        self.passthrough = False
        self.volume_db = 0
        self.volume_l_db = 0
        self.volume_r_db = 0
        self.output_device = "default"
        self._running = False
        self._proc = None

    @property
    def proc_pid(self) -> int | None:
        return self._proc.pid if self._proc else None

    def _build_cmd(self) -> list[str]:
        fmt = "alsa" if self.device.startswith(("hw:", "plughw:")) else "pulse"
        base = ["ffmpeg", "-loglevel", "quiet", "-f", fmt, "-i", self.device]

        if self.passthrough:
            mono_prefix = "pan=stereo|c0=0.5*c0+0.5*c1|c1=0.5*c0+0.5*c1," if self.mono_mix else ""
            fc = f"[0:a]{mono_prefix}asplit=2[vu][out]"
            return base + [
                "-filter_complex", fc,
                "-map", "[vu]",
                "-f", "s16le", "-ar", str(self.SAMPLE_RATE), "-ac", "2", "pipe:1",
                "-map", "[out]",
                "-f", "pulse", "-ar", str(self.SAMPLE_RATE), "-ac", "2", self.output_device,
            ]

        cmd = base[:]
        if self.mono_mix:
            cmd += ["-af", "pan=stereo|c0=0.5*c0+0.5*c1|c1=0.5*c0+0.5*c1"]
        cmd += ["-f", "s16le", "-ar", str(self.SAMPLE_RATE), "-ac", "2", "pipe:1"]
        return cmd

    def run(self):
        self._running = True
        try:
            self._proc = subprocess.Popen(
                self._build_cmd(),
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, bufsize=0
            )
        except FileNotFoundError:
            self.error.emit("ffmpeg not found")
            return

        chunk_size = self.CHUNK_FRAMES * 2 * 2  # 2 ch × 2 bytes (s16le)

        while self._running:
            raw = b""
            while len(raw) < chunk_size and self._running:
                chunk = self._proc.stdout.read(chunk_size - len(raw))
                if not chunk:
                    self._running = False
                    break
                raw += chunk
            if not self._running or len(raw) < chunk_size:
                break

            samples = np.frombuffer(raw, dtype=np.int16).reshape(-1, 2).astype(np.float32)
            l_gain = 10.0 ** ((self.volume_db + self.volume_l_db) / 20.0)
            r_gain = 10.0 ** ((self.volume_db + self.volume_r_db) / 20.0)
            samples[:, 0] *= l_gain
            samples[:, 1] *= r_gain
            l_rms = np.sqrt(np.mean(samples[:, 0] ** 2))
            r_rms = np.sqrt(np.mean(samples[:, 1] ** 2))
            l_db = 20.0 * np.log10(l_rms / 32768.0) if l_rms > 0 else -96.0
            r_db = 20.0 * np.log10(r_rms / 32768.0) if r_rms > 0 else -96.0
            self.levels_updated.emit(l_db, r_db)

        if self._proc:
            self._proc.terminate()
            try:
                self._proc.wait(timeout=2)
            except subprocess.TimeoutExpired:
                self._proc.kill()
            if self._running and self._proc.returncode not in (0, -15):
                stderr = self._proc.stderr.read().decode(errors="replace").strip()
                msg = stderr.splitlines()[-1] if stderr else f"ffmpeg exited ({self._proc.returncode})"
                self.error.emit(msg)

    def stop(self):
        self._running = False
        if self._proc:
            self._proc.terminate()
