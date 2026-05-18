# Hagibis Monitor

A PyQt6 desktop application for live monitoring and control of the
**Hagibis USB capture card** (MS2130 / MacroSilicon chipset, USB ID `345f:2130`).

Replaces an earlier GStreamer shell script (`hagibis-monitor.sh`) with a
full windowed GUI that lets you tweak every setting live without restarting
the pipeline.

---

## Table of Contents

1. [Hardware context](#hardware-context)
2. [What the app does](#what-the-app-does)
3. [Project layout](#project-layout)
4. [Dependencies](#dependencies)
5. [Running the app](#running-the-app)
6. [UI walkthrough](#ui-walkthrough)
7. [Audio: Mono Mix explained](#audio-mono-mix-explained)
8. [The original shell script](#the-original-shell-script)
9. [Known quirks and gotchas](#known-quirks-and-gotchas)
10. [AI context — read this if you are a future assistant](#ai-context--read-this-if-you-are-a-future-assistant)

---

## Hardware context

| Item | Detail |
|---|---|
| Device | Hagibis USB Capture Card |
| USB ID | `345f:2130` (MacroSilicon MS2130 chipset) |
| Video node | `/dev/video0` (detected via udevadm) |
| V4L2 driver | `uvcvideo` |
| ALSA card | `card 2: Hagibis`, device 0 — `plughw:Hagibis,0` |
| PulseAudio/PipeWire source | auto-detected by name at runtime |
| Supported formats | MJPEG, YUYV 4:2:2 |
| Supported resolutions | 640×480 up to 2560×1440 |
| Max frame rate | 60 fps at 1920×1080 and below; 30 fps at 2560×1440 |
| V4L2 controls | brightness, contrast, saturation, hue — all 0–100, default 50 |

---

## What the app does

- **Live video preview** — ffmpeg reads from the V4L2 device and pipes raw
  RGB24 frames into PyQt6 for display, scaled to fill the window while
  preserving aspect ratio.
- **Image controls** — brightness / contrast / saturation / hue sliders
  that call `v4l2-ctl --set-ctrl` in real time without restarting capture.
- **Capture settings** — change format (MJPEG / YUYV), resolution, and
  frame rate; hit *Apply & Restart* to restart the ffmpeg pipeline.
- **Audio VU meters** — stereo L/R segmented meters with peak hold and
  decay, fed by a separate ffmpeg process reading from the ALSA device.
- **Mono Mix** — optional filter that sums L+R into a single mono signal
  and sends it to both output channels. See [below](#audio-mono-mix-explained).
- **Audio Passthrough** — routes captured audio to the default
  PulseAudio/PipeWire sink using ffmpeg's `asplit` filter so the same
  process feeds both the VU meters and the speakers simultaneously.
- **Persistent settings** — all values (resolution, fps, format, V4L2
  control positions, audio toggles) are saved via `QSettings` and restored
  on next launch.

---

## Project layout

```
hagibis-monitor/
├── main.py       # Entry point + MainWindow + VideoDisplay widget
├── workers.py    # VideoWorker and AudioWorker (QThread subclasses)
└── vu_meter.py   # VuMeter, DbScale, StereoVuMeter custom widgets
```

The companion script that preceded this app lives at
`~/hagibis-monitor.sh` (GStreamer pipeline, no GUI).

### main.py

Contains:
- `VideoDisplay` — a `QLabel` subclass that scales frames to fill the
  widget while painting a "NO SIGNAL" placeholder when no frames are
  arriving.
- `MainWindow` — builds the UI (left: video; right: tabbed control panel),
  wires everything together, manages worker lifecycle, and handles
  `QSettings` persistence.
- `main()` entry point with Fusion style and dark palette.

### workers.py

`VideoWorker(QThread)`:
- Spawns `ffmpeg -f v4l2 … -f rawvideo -pix_fmt rgb24 -` and reads
  `width × height × 3` bytes per frame from stdout.
- Emits `frame_ready(QImage)` and `fps_updated(float)`.
- `stop()` terminates the subprocess and the thread exits cleanly.

`AudioWorker(QThread)`:
- Spawns an ffmpeg command whose shape depends on `mono_mix` and
  `passthrough` flags (see [below](#audio-mono-mix-explained)).
- Reads chunks of 1024 frames × 2 channels × 2 bytes (s16le at 48 kHz)
  from stdout, computes per-channel RMS dBFS with numpy, and emits
  `levels_updated(float, float)`.

### vu_meter.py

`VuMeter(QWidget)`:
- 30 segments, colour-coded: green (below −24 dBFS), yellow-green
  (−24 to −12), orange (−12 to −6), red (−6 to 0).
- Peak hold for 90 updates (~1.4 s at the audio chunk rate), then decays
  at 0.4 dB per update.
- `DbScale(QWidget)` — fixed-width tick/label scale placed between the two
  meters; ticks at 0, −3, −6, −12, −20, −40, −60 dBFS.
- `StereoVuMeter(QWidget)` — horizontal layout of L meter, scale, R meter;
  exposes `set_levels(l_db, r_db)`.

---

## Dependencies

All must be present on the system:

| Dependency | Purpose | Check |
|---|---|---|
| Python ≥ 3.10 | type-union syntax (`X \| Y`) used in type hints | `python3 --version` |
| PyQt6 | GUI framework | `python3 -c "import PyQt6"` |
| numpy | RMS calculation in AudioWorker | `python3 -c "import numpy"` |
| ffmpeg | Video + audio capture pipelines | `which ffmpeg` |
| v4l2-ctl | Applying V4L2 control changes live | `which v4l2-ctl` |
| PulseAudio or PipeWire (PulseAudio compat) | Audio passthrough output | `pactl info` |

Install missing Python packages:
```bash
pip install PyQt6 numpy
```

Install system tools (Debian/Ubuntu):
```bash
sudo apt install ffmpeg v4l2-utils
```

---

## Running the app

```bash
python3 main.py
```

There is no install step. Settings are stored in
`~/.config/HagibisMonitor/HagibisMonitor.conf` (via `QSettings`).

---

## UI walkthrough

```
┌─────────────────────────────────────────────┬──────────────────────┐
│                                             │  [Video] [Audio]     │
│                                             │ ┌──────────────────┐ │
│           Live video preview                │ │ Capture Settings │ │
│         (scales with window)                │ │  Format  ▾       │ │
│                                             │ │  Resolution ▾    │ │
│                                             │ │  Frame Rate ▾    │ │
│                                             │ │  [Apply & Restart]│ │
│                                             │ └──────────────────┘ │
│                                             │ ┌──────────────────┐ │
│                                             │ │ Image Controls   │ │
│                                             │ │  Brightness ━●── │ │
│                                             │ │  Contrast   ━●── │ │
│                                             │ │  Saturation ━●── │ │
│                                             │ │  Hue        ━●── │ │
│                                             │ │  [Reset Defaults] │ │
│                                             │ └──────────────────┘ │
├─────────────────────────────────────────────┴──────────────────────┤
│ Capturing 1280×720 @ 30 fps [MJPEG]                     FPS: 30.0 │
└────────────────────────────────────────────────────────────────────┘
```

**Audio tab:**

```
┌──────────────────────┐
│ ☑ Enable Audio Monitor│
│  ┌──┬──┬──┐          │
│  │▓▓│  │▓▓│ ← meters │
│  │▓▓│-6│▓▓│          │
│  │░░│  │░░│          │
│  │  │-20│  │         │
│  │  │  │  │          │
│  │  │-40│  │         │
│  │ L│  │R │          │
│  └──┴──┴──┘          │
│ ☐ Force Mono Mix     │
│ ☐ Passthrough to     │
│   System Audio       │
└──────────────────────┘
```

---

## Audio: Mono Mix explained

Some HDMI sources (consoles, older cameras, certain games) output audio on
only one channel. Without correction, you hear sound from only one ear.

**Force Mono Mix** applies the ffmpeg `pan` filter:

```
pan=stereo|c0=0.5*c0+0.5*c1|c1=0.5*c0+0.5*c1
```

This mixes left and right input channels to a single mono signal and sends
it to both output channels. It handles all cases:

| Input situation | Result with Mono Mix on |
|---|---|
| Audio on left channel only | Centred on both speakers |
| Audio on right channel only | Centred on both speakers |
| Truly mono (1-ch ALSA) | Duplicated to both speakers |
| Full stereo | Collapsed to centred mono (tradeoff) |

When **Passthrough** is also enabled, ffmpeg uses `asplit` to feed the VU
meter stdout pipe and the PulseAudio sink from a single decoding pass —
the ALSA device is opened only once:

```
[0:a]<pan_filter,>asplit=2[vu][out]
  → [vu]  s16le → stdout → Python RMS → VU meters
  → [out] pulse → default sink → speakers
```

When Passthrough is off, the `asplit` is dropped and all PCM goes to
stdout only.

---

## The original shell script

`~/hagibis-monitor.sh` is a GStreamer-based predecessor with no GUI:

```bash
gst-launch-1.0 -e \
  v4l2src device=/dev/video0 ! \
  videoconvert ! \
  videobalance brightness=0.05 contrast=1.1 saturation=1.15 hue=0.0 ! \
  videoscale add-borders=true ! \
  video/x-raw,width=1440,height=1080 ! \
  glimagesink sync=false force-aspect-ratio=true \
  pulsesrc device="<hagibis source>" ! audioconvert ! audioresample ! autoaudiosink
```

Useful notes from it:
- The card often works best at **1440×1080** (native, no scaling artefacts)
  or **2560×1440** for high-detail capture.
- `add-borders=true` on `videoscale` adds black letterbox/pillarbox to
  enforce the target resolution rather than stretching. The PyQt6 app
  does the same with Qt's `KeepAspectRatio` mode.
- The script's colour defaults (brightness +0.05, contrast 1.1,
  saturation 1.15) are a good starting point for the sliders in the GUI.
  In V4L2 integer terms (0–100 scale) that maps roughly to:
  brightness ≈ 53, contrast ≈ 55, saturation ≈ 57.

---

## Known quirks and gotchas

**ALSA device index changes** — `plughw:Hagibis,0` is resolved by name
so it survives USB reconnects, but if the card name ever appears differently
run `arecord -l` to confirm. The numeric fallback is `plughw:2,0`.

**Two ffmpeg instances vs. ALSA direct** — `plughw:` uses ALSA directly.
If another app holds the ALSA capture device exclusively the AudioWorker
will fail silently (ffmpeg exits immediately). PipeWire normally prevents
this by presenting a virtual device; if problems arise, switch the
`AudioWorker.DEVICE` constant to the PulseAudio source name from
`pactl list short sources`.

**YUYV at high resolutions is slow** — YUYV is uncompressed; at 1920×1080
the USB bus is saturated quickly. Stick to **MJPEG** for anything above
720p to get stable frame rates.

**V4L2 controls apply to the hardware** — slider values written with
`v4l2-ctl` persist in the driver until the device is replugged or the
driver resets them. The app saves the last-used values in QSettings and
reloads them on next launch, but does not re-apply them to the hardware
automatically on startup (the driver already holds the last state).

**60 fps in MJPEG at 1920×1080** — confirmed supported by the device.
ffmpeg's `-framerate` must be set before `-i`; the worker command does
this correctly. If the pipeline drops frames, lower the rate or resolution.

**PyQt6 vs PySide6** — the code imports `PyQt6` specifically. Swapping to
PySide6 requires changing import paths (`PyQt6.QtCore` → `PySide6.QtCore`,
`pyqtSignal` → `Signal`, etc.) but the logic is identical.

---

## AI context — read this if you are a future assistant

> This section gives you the full picture so you can contribute without
> re-reading the whole conversation.

**What this project is:** A PyQt6 GUI monitor for a Hagibis USB capture
card on a Linux desktop. Not a recording tool — purely live monitoring,
image control, and optional audio passthrough.

**Architecture in one sentence:** Two QThreads each own one long-lived
ffmpeg subprocess; they emit PyQt signals carrying frame data or dB levels
back to the main thread.

**Key design decisions and why:**

- **ffmpeg, not OpenCV** — OpenCV (`cv2`) was not installed; ffmpeg is
  always present and handles both MJPEG and YUYV correctly without extra
  libraries.
- **ffmpeg, not sounddevice/pyaudio** — neither was installed; ffmpeg reads
  ALSA directly and outputs raw s16le PCM which numpy can process.
- **`asplit` for simultaneous VU + passthrough** — avoids opening the ALSA
  capture device twice (which would fail on raw hardware). One ffmpeg
  process splits the stream with the filter graph.
- **`plughw:Hagibis,0` not `hw:2,0`** — name-based so it survives USB
  re-enumeration; `plughw` allows format conversion by ALSA's plugin layer.
- **`v4l2-ctl` subprocess for image controls** — the V4L2 Python bindings
  (`v4l2py`) were not installed; calling `v4l2-ctl` is one line and has
  no dependencies. The call is fire-and-forget (`Popen`, no wait).
- **QSettings for persistence** — org `HagibisMonitor`, app
  `HagibisMonitor`; stored at `~/.config/HagibisMonitor/HagibisMonitor.conf`.

**Things that are NOT done yet that a future session might add:**
- Re-applying saved V4L2 values to hardware on app startup.
- Auto-detecting the PulseAudio source name for `AudioWorker.DEVICE`
  (currently hardcoded to `plughw:Hagibis,0`; for passthrough this works
  because ffmpeg reads ALSA and writes to pulse, but if PipeWire ever
  blocks ALSA direct access, switch to the pulse source).
- An Appearance tab for theming (placeholder was planned but skipped).
- Recording / snapshot functionality.
- Detection of whether the capture card is actually sending a signal
  (currently the VU meter just stays at −96 dBFS and the video shows
  NO SIGNAL only if ffmpeg exits).

**Hardware constants to know:**
```python
VideoWorker.DEVICE  = "/dev/video0"
AudioWorker.DEVICE  = "plughw:Hagibis,0"
AudioWorker.SAMPLE_RATE = 48000
```

**How to restart capture from code:** Call `MainWindow._restart_video()`.
It stops the current `VideoWorker` (terminates the ffmpeg process, waits
up to 3 s), then starts a fresh one with whatever the combo boxes currently
say.

**How to restart audio from code:** Call `MainWindow._start_audio()`. It
stops any running `AudioWorker` first, then builds a new one using the
current `mono_mix` and `passthrough` checkbox states.
