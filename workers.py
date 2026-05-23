import os
import queue
import subprocess
import time
import numpy as np
from PyQt6.QtCore import QThread, Qt, pyqtSignal
from PyQt6.QtGui import QColor, QImage, QPainter


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


class OutputWorker(QThread):
    """Renders input frames into a fixed-resolution canvas and pipes to a v4l2loopback device."""

    error = pyqtSignal(str)

    def __init__(self, device: str, width: int, height: int, fps: int, pixel_format: str):
        super().__init__()
        self._device = device
        self._width = width
        self._height = height
        self._fps = fps
        self._pixel_format = pixel_format
        self._q: queue.Queue = queue.Queue(maxsize=4)
        self._running = False

    def push_frame(self, img: QImage, pan_x: float, pan_y: float, zoom: float,
                   bg: QColor, scale_mode: str = "fit", crop_mode: str = "full"):
        item = (img, pan_x, pan_y, zoom, bg, scale_mode, crop_mode)
        if self._q.full():
            try:
                self._q.get_nowait()
            except queue.Empty:
                pass
        try:
            self._q.put_nowait(item)
        except queue.Full:
            pass

    def stop(self):
        self._running = False

    def run(self):
        self._running = True
        interval = 1.0 / self._fps
        cmd = [
            "ffmpeg", "-y", "-loglevel", "quiet",
            "-f", "rawvideo", "-pix_fmt", "rgb24",
            "-s", f"{self._width}x{self._height}",
            "-r", str(self._fps),
            "-i", "pipe:0",
            "-f", "v4l2",
            "-pix_fmt", self._pixel_format,
            self._device,
        ]
        try:
            proc = subprocess.Popen(
                cmd, stdin=subprocess.PIPE,
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
        except OSError as e:
            self.error.emit(str(e))
            return

        last_item = None
        next_pts = time.monotonic()

        while self._running and proc.poll() is None:
            # Drain queue, keeping only the most recent frame
            try:
                while True:
                    last_item = self._q.get_nowait()
            except queue.Empty:
                pass

            if last_item is None:
                # No frame yet — wait for the first one
                try:
                    last_item = self._q.get(timeout=0.1)
                    next_pts = time.monotonic()
                except queue.Empty:
                    continue

            # Sleep until the next frame is due
            sleep_for = next_pts - time.monotonic()
            if sleep_for > 0:
                time.sleep(sleep_for)

            src, pan_x, pan_y, zoom, bg, scale_mode, crop_mode = last_item
            data = self._render(src, pan_x, pan_y, zoom, bg, scale_mode, crop_mode)
            try:
                proc.stdin.write(data)
            except (BrokenPipeError, OSError):
                break

            next_pts += interval
            # Prevent spiral if rendering falls behind
            if time.monotonic() > next_pts + interval:
                next_pts = time.monotonic()

        self._running = False
        try:
            proc.stdin.close()
        except OSError:
            pass
        try:
            proc.wait(timeout=2)
        except subprocess.TimeoutExpired:
            proc.kill()

    @staticmethod
    def _crop(img: QImage, crop_mode: str) -> QImage:
        if crop_mode == "full":
            return img
        try:
            rw, rh = (int(p) for p in crop_mode.split(":"))
        except ValueError:
            return img
        sw, sh = img.width(), img.height()
        if sw == 0 or sh == 0:
            return img
        tgt = rw / rh
        src_ar = sw / sh
        if abs(src_ar - tgt) < 0.001:
            return img
        if src_ar > tgt:
            nw = int(sh * tgt)
            return img.copy((sw - nw) // 2, 0, nw, sh)
        else:
            nh = int(sw / tgt)
            return img.copy(0, (sh - nh) // 2, sw, nh)

    def _render(self, src: QImage, pan_x: float, pan_y: float, zoom: float,
                bg: QColor, scale_mode: str, crop_mode: str) -> bytes:
        out = QImage(self._width, self._height, QImage.Format.Format_RGB888)
        out.fill(bg)
        src = self._crop(src, crop_mode)
        src_w, src_h = src.width(), src.height()
        W, H = self._width, self._height
        if src_w > 0 and src_h > 0:
            smooth  = Qt.TransformationMode.SmoothTransformation
            ignore  = Qt.AspectRatioMode.IgnoreAspectRatio
            clip    = False

            if scale_mode == "stretch":
                dw, dh = max(1, int(W * zoom)), max(1, int(H * zoom))
            elif scale_mode == "fill":
                s = max(W / src_w, H / src_h) * zoom
                dw, dh = max(1, int(src_w * s)), max(1, int(src_h * s))
                clip = True
            elif scale_mode == "native":
                dw, dh = max(1, int(src_w * zoom)), max(1, int(src_h * zoom))
            elif scale_mode.startswith("area_"):
                rw, rh = (int(v) for v in scale_mode[5:].split("_"))
                as_ = min(W / rw, H / rh)
                s   = min(rw * as_ / src_w, rh * as_ / src_h) * zoom
                dw, dh = max(1, int(src_w * s)), max(1, int(src_h * s))
            elif scale_mode.startswith("stretch_"):
                rw, rh = (int(v) for v in scale_mode[8:].split("_"))
                as_ = min(W / rw, H / rh)
                dw, dh = max(1, int(rw * as_ * zoom)), max(1, int(rh * as_ * zoom))
            else:  # "fit"
                s = min(W / src_w, H / src_h) * zoom
                dw, dh = max(1, int(src_w * s)), max(1, int(src_h * s))

            dx = int((W - dw) / 2 + pan_x)
            dy = int((H - dh) / 2 + pan_y)
            scaled = src.scaled(dw, dh, ignore, smooth)
            p = QPainter(out)
            if clip:
                p.setClipRect(0, 0, W, H)
            p.drawImage(dx, dy, scaled)
            p.end()
        ptr = out.bits()
        ptr.setsize(out.sizeInBytes())
        return bytes(ptr)


class AudioWorker(QThread):
    levels_updated = pyqtSignal(float, float)  # L dBFS, R dBFS
    error = pyqtSignal(str)

    SAMPLE_RATE  = 48000
    CHUNK_FRAMES = 1024
    BUS_SINK     = "hagibis_bus"
    SOURCE_NAME  = "hagibis_virtual"

    def __init__(self):
        super().__init__()
        self.device         = "plughw:Hagibis,0"
        self.mono_mix       = False
        self.passthrough    = False
        self.virtual_output = False
        self.volume_db      = 0
        self.volume_l_db    = 0
        self.volume_r_db    = 0
        self.output_device  = "default"
        self._running    = False
        self._proc       = None
        self._pacat_proc = None
        self._bus_mod: int | None = None
        self._src_mod: int | None = None

    @property
    def proc_pid(self) -> int | None:
        return self._proc.pid if self._proc else None

    # ── virtual mic lifecycle ─────────────────────────────────────────────────

    def _cleanup_stale_source(self):
        try:
            out = subprocess.check_output(
                ["pactl", "list", "short", "modules"],
                stderr=subprocess.DEVNULL, timeout=3,
            ).decode(errors="replace")
            to_remove = []
            for line in out.splitlines():
                parts = line.split()
                if len(parts) < 2:
                    continue
                if parts[1] in ("module-virtual-source", "module-remap-source") and self.SOURCE_NAME in line:
                    to_remove.insert(0, parts[0])
                elif parts[1] == "module-null-sink" and self.BUS_SINK in line:
                    to_remove.append(parts[0])
            for idx in to_remove:
                subprocess.run(
                    ["pactl", "unload-module", idx],
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                    timeout=2,
                )
        except Exception:
            pass

    def _create_virtual_source(self) -> bool:
        self._cleanup_stale_source()
        try:
            out = subprocess.check_output(
                [
                    "pactl", "load-module", "module-null-sink",
                    f"sink_name={self.BUS_SINK}",
                    "sink_properties=device.description='Hagibis Internal Bus'",
                ],
                stderr=subprocess.DEVNULL, timeout=5,
            ).decode().strip()
            self._bus_mod = int(out)
        except Exception:
            return False
        # device.class=sound is what distinguishes a proper input device
        # (microphone) from a recording/monitor source in audio settings UIs.
        # Try module-virtual-source first; fall back to module-remap-source.
        src_props = (
            f"device.class=sound "
            f"device.description='Hagibis Virtual Microphone'"
        )
        for mod in ("module-virtual-source", "module-remap-source"):
            try:
                out = subprocess.check_output(
                    [
                        "pactl", "load-module", mod,
                        f"source_name={self.SOURCE_NAME}",
                        f"master={self.BUS_SINK}.monitor",
                        f"source_properties={src_props}",
                    ],
                    stderr=subprocess.DEVNULL, timeout=5,
                ).decode().strip()
                self._src_mod = int(out)
                return True
            except Exception:
                continue
        self._remove_virtual_source()
        return False

    def _remove_virtual_source(self):
        if self._pacat_proc is not None:
            self._pacat_proc.terminate()
            try:
                self._pacat_proc.wait(timeout=2)
            except subprocess.TimeoutExpired:
                self._pacat_proc.kill()
            self._pacat_proc = None
        # Unload remap-source before the bus sink it depends on
        for attr in ("_src_mod", "_bus_mod"):
            idx = getattr(self, attr)
            if idx is not None:
                subprocess.Popen(
                    ["pactl", "unload-module", str(idx)],
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                )
                setattr(self, attr, None)

    # ── ffmpeg command ────────────────────────────────────────────────────────

    def _build_cmd(self, virt_fd: int | None = None) -> list[str]:
        fmt  = "alsa" if self.device.startswith(("hw:", "plughw:")) else "pulse"
        base = ["ffmpeg", "-loglevel", "quiet", "-f", fmt, "-i", self.device]

        mono_filter = "pan=stereo|c0=0.5*c0+0.5*c1|c1=0.5*c0+0.5*c1" if self.mono_mix else ""

        master_lin = 10.0 ** (self.volume_db   / 20.0)
        l_lin      = 10.0 ** (self.volume_l_db / 20.0)
        r_lin      = 10.0 ** (self.volume_r_db / 20.0)
        vol_filter = f"pan=stereo|c0={master_lin * l_lin:.6f}*c0|c1={master_lin * r_lin:.6f}*c1"

        if not self.passthrough and not self.virtual_output:
            cmd = base[:]
            if mono_filter:
                cmd += ["-af", mono_filter]
            cmd += ["-f", "s16le", "-ar", str(self.SAMPLE_RATE), "-ac", "2", "pipe:1"]
            return cmd

        split_labels = ["[vu]"]
        if self.passthrough:
            split_labels.append("[pt]")
        if self.virtual_output:
            split_labels.append("[virt_raw]")

        n    = len(split_labels)
        mono = mono_filter + "," if mono_filter else ""
        fc   = f"[0:a]{mono}asplit={n}{''.join(split_labels)}"
        if self.virtual_output:
            fc += f";[virt_raw]{vol_filter}[virt]"

        cmd = base + ["-filter_complex", fc]
        cmd += ["-map", "[vu]", "-f", "s16le", "-ar", str(self.SAMPLE_RATE), "-ac", "2", "pipe:1"]
        if self.passthrough:
            cmd += ["-map", "[pt]", "-f", "pulse", "-ar", str(self.SAMPLE_RATE), "-ac", "2", self.output_device]
        if self.virtual_output:
            # Write raw PCM to the pipe connected to pacat rather than directly
            # to PulseAudio — pacat is more reliable for named-sink targeting.
            cmd += ["-map", "[virt]", "-f", "s16le", "-ar", str(self.SAMPLE_RATE), "-ac", "2",
                    f"pipe:{virt_fd}"]
        return cmd

    # ── thread entry ──────────────────────────────────────────────────────────

    def run(self):
        self._running = True

        if self.virtual_output and not self._create_virtual_source():
            self.error.emit("Failed to create virtual microphone — is PulseAudio/PipeWire running?")
            self.virtual_output = False

        virt_fd = None
        if self.virtual_output:
            virt_r, virt_w = os.pipe()
            try:
                self._pacat_proc = subprocess.Popen(
                    [
                        "pacat", "--playback", "--raw",
                        f"--device={self.BUS_SINK}",
                        "--rate=48000", "--channels=2", "--format=s16le",
                    ],
                    stdin=virt_r,
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                )
            except FileNotFoundError:
                self.error.emit("pacat not found — install pulseaudio-utils")
                self.virtual_output = False
            finally:
                os.close(virt_r)  # pacat (or nobody) owns the read end now
            if self.virtual_output:
                virt_fd = virt_w

        try:
            self._proc = subprocess.Popen(
                self._build_cmd(virt_fd=virt_fd),
                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                pass_fds=(virt_fd,) if virt_fd is not None else (),
                bufsize=0,
            )
        except FileNotFoundError:
            self.error.emit("ffmpeg not found")
            self._remove_virtual_source()
            return
        finally:
            if virt_fd is not None:
                os.close(virt_fd)  # ffmpeg owns the write end now

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

        self._remove_virtual_source()

    def stop(self):
        self._running = False
        if self._proc:
            self._proc.terminate()
        if self._pacat_proc:
            self._pacat_proc.terminate()
