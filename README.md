# Hagibis Monitor

A PyQt6 desktop application for live monitoring and control of USB capture
cards on Linux. Originally built around the **Hagibis USB Capture Card**
(MS2130 / MacroSilicon chipset, USB ID `345f:2130`) but works with any
V4L2-compatible capture device.

---

<img width="1603" height="1240" alt="image" src="https://github.com/user-attachments/assets/0dd4d44a-c5e4-4395-b7e1-b8197bd61642" />

---

## Table of Contents

1. [Hardware context](#hardware-context)
2. [What the app does](#what-the-app-does)
3. [Project layout](#project-layout)
4. [Dependencies](#dependencies)
5. [Running the app](#running-the-app)
6. [UI walkthrough](#ui-walkthrough)
7. [Profiles](#profiles)
8. [Video display options](#video-display-options)
9. [Audio](#audio)
10. [Known quirks and gotchas](#known-quirks-and-gotchas)
11. [AI context — read this if you are a future assistant](#ai-context--read-this-if-you-are-a-future-assistant)

---

## Hardware context

| Item | Detail |
|---|---|
| Primary device | Hagibis USB Capture Card |
| USB ID | `345f:2130` (MacroSilicon MS2130 chipset) |
| Video node | `/dev/video0` (auto-detected at runtime) |
| V4L2 driver | `uvcvideo` |
| ALSA card | `card 2: Hagibis`, device 0 — `plughw:Hagibis,0` |
| PulseAudio/PipeWire source | auto-detected by name at runtime |
| Supported formats | MJPEG, YUYV 4:2:2 |
| Supported resolutions | 640×480 up to 2560×1440 (queried live from device) |
| Max frame rate | 60 fps at 1920×1080 and below; 30 fps at 2560×1440 |
| V4L2 controls | brightness, contrast, saturation, hue — all 0–100, default 50 |

---

## What the app does

### Video
- **Live video preview** — ffmpeg reads from the selected V4L2 device and
  pipes raw RGB24 frames into a custom `paintEvent`-based display widget.
- **Device selection** — scans all V4L2 devices via `v4l2-ctl --list-devices`;
  click ↻ to rescan without restarting.
- **Dynamic format / resolution / fps** — populated live from
  `v4l2-ctl --list-formats-ext` for the selected device. Resolutions are
  grouped into aspect-ratio tabs (16:9, 4:3, 5:4, 10:9, …).
- **Image controls** — brightness / contrast / saturation / hue sliders
  that call `v4l2-ctl --set-ctrl` in real time.
- **Scale modes** — twelve display modes covering fit, stretch, fill, native,
  and area-constrained variants (see [Video display options](#video-display-options)).
- **Crop modes** — pre-scale centre-crop to full / 10:9 / 5:4 / 4:3.
- **Background colour** — configurable letterbox / pillarbox colour (default `#1f1f1f`).
- **Collapsible panel** — the control panel can be hidden / shown via the ◀ ▶
  toggle strip on its left edge.

### Audio
- **Audio device selection** — scans ALSA capture devices via `arecord -l`;
  click ↻ to rescan.
- **VU meters** — stereo L/R segmented meters with peak hold and 0.4 dB/update
  decay, driven by per-chunk RMS of the raw s16le PCM.
- **Mono Mix** — sums L+R into a mono signal on both channels (useful for
  single-channel console audio).
- **Audio Passthrough** — routes captured audio to the default
  PulseAudio/PipeWire sink using ffmpeg's `asplit` filter. Volume is applied
  in real time via `pactl set-sink-input-volume`.
- **Master + per-channel volume** — master fader (−40 to +10 dB) and
  independent L/R trims (−20 to +20 dB). VU meters reflect volume changes
  instantly while dragging; speaker output follows via pactl.

### Profiles
- Named profiles stored as individual INI files under
  `~/.config/HagibisMonitor/profiles/`.
- Every UI value is captured in a flat `AppSettings` dataclass.
- Changes are tracked in memory; the **Save Profile** button flushes to disk.
- **Revert** reloads the active profile from disk, discarding in-memory changes.
- Switching profiles with unsaved changes prompts: Discard & Switch / Save &
  Switch / Save as New Profile / Cancel.
- Create (`+`) and delete (`✕`) profiles; Default cannot be deleted.

---

## Project layout

```
hagibis-monitor/
├── main.py       # Entry point, MainWindow, VideoDisplay, AppSettings, profiles
├── workers.py    # VideoWorker and AudioWorker (QThread subclasses)
├── vu_meter.py   # VuMeter, DbScale, StereoVuMeter custom widgets
├── .gitignore
└── .vscode/
    └── launch.json   # VS Code debugpy configuration
```

Settings are stored in:
```
~/.config/HagibisMonitor/
├── HagibisMonitor.ini        # window geometry + active profile name only
└── profiles/
    ├── Default.ini           # always present; created on first launch
    ├── GBC.ini               # example user profile
    └── N64.ini               # example user profile
```

### main.py

Key components:

**`AppSettings` (dataclass)** — flat struct holding every profile-able value:
panel visibility, scale/crop/bg_color, video device, fmt/res/fps/image
controls, audio device, audio enabled, mono mix, passthrough, and three
volume levels.

**`VideoDisplay(QLabel)`** — renders frames via `paintEvent` using a
pre-computed `(QPixmap, QPoint)` pair. Supports all twelve scale modes and
four crop modes independently.

**`MainWindow`** — builds the UI, owns the workers, manages profiles.
Key I/O methods:
- `_collect_settings() → AppSettings` — reads every widget into a struct.
- `_apply_settings(s)` — applies a struct to every widget + restarts streams.
- `_load_from_disk(name) → AppSettings` — reads a profile INI file.
- `_save_to_disk(s, name)` — writes a profile INI file atomically via one
  QSettings object (avoids the multi-object sync bug).

### workers.py

**`VideoWorker(QThread)`**:
- Spawns `ffmpeg -f v4l2 … -f rawvideo -pix_fmt rgb24 -`.
- Reads `width × height × 3` bytes per frame; uses an inner accumulation
  loop to handle partial `os.read()` returns (same pattern as `AudioWorker`).
- Emits `frame_ready(QImage)` and `fps_updated(float)`.
- Device, format, resolution and framerate are set via `configure()`.

**`AudioWorker(QThread)`**:
- Spawns ffmpeg reading from `self.device` (ALSA `plughw:` address).
- Without passthrough: pipes raw s16le PCM to stdout.
- With passthrough: `[0:a]<pan?>asplit=2[vu][out]` — `[vu]` to stdout,
  `[out]` to PulseAudio. Volume for the speaker output is controlled
  separately via `pactl` after the process starts.
- Python applies `10^((master_db + channel_trim) / 20)` gain to the stdout
  PCM before RMS computation, giving real-time VU response while dragging.
- Exposes `proc_pid` property so `MainWindow` can find the pactl sink input.

### vu_meter.py

`VuMeter(QWidget)`:
- 30 segments, colour-coded: green (< −24 dBFS), yellow-green (−24 to −12),
  orange (−12 to −6), red (−6 to 0).
- Peak hold for 90 updates (~1.4 s), then decays at 0.4 dB per update.

`DbScale(QWidget)` — fixed-width tick/label scale; ticks at 0, −3, −6, −12,
−20, −40, −60 dBFS.

`StereoVuMeter(QWidget)` — L meter / scale / R meter layout; `set_levels(l, r)`.

---

## Dependencies

| Dependency | Purpose | Check |
|---|---|---|
| Python ≥ 3.10 | `X \| Y` union hints, dataclasses | `python3 --version` |
| PyQt6 | GUI framework | `python3 -c "import PyQt6"` |
| numpy | Per-chunk RMS in AudioWorker | `python3 -c "import numpy"` |
| ffmpeg | Video + audio capture pipelines | `which ffmpeg` |
| v4l2-ctl | Device enumeration + image controls | `which v4l2-ctl` |
| arecord | ALSA capture device enumeration | `which arecord` |
| pactl | Real-time speaker volume control | `which pactl` |
| xdg-open | Open config folder button | `which xdg-open` |

Install missing Python packages:
```bash
pip install PyQt6 numpy
```

Install system tools (Debian/Ubuntu):
```bash
sudo apt install ffmpeg v4l2-utils alsa-utils
```

`pactl` and `xdg-open` are part of `pulseaudio-utils` and `xdg-utils`
respectively, both usually pre-installed on desktop systems.

---

## Running the app

```bash
cd ~/Development/projects/hagibis-monitor
python3 main.py
```

Or press **F5** in VS Code (uses `.vscode/launch.json`).

There is no install step.

---

## UI walkthrough

```
┌──────────────────────────────────────────────────◀┬──────────────────────┐
│                                                   │ Profile: [Default ▾] │
│                                                   │ [+] [✕]              │
│                                                   │ [Save Profile][Revert]│
│                                                   │ [⎆]  ● unsaved       │
│                                                   ├──────────────────────┤
│              Live video preview                   │  [Video]  [Audio]    │
│            (scale + crop applied)                 │ ┌──────────────────┐ │
│                                                   │ │ Capture Settings │ │
│                                                   │ │  Device  ▾  [↻]  │ │
│                                                   │ │  Format  ▾       │ │
│                                                   │ │  [16:9][4:3][5:4]│ │
│                                                   │ │  Resolution ▾    │ │
│                                                   │ │  Frame Rate ▾    │ │
│                                                   │ │  [Apply & Restart]│ │
│                                                   │ └──────────────────┘ │
│                                                   │ ┌──────────────────┐ │
│                                                   │ │ Display          │ │
│                                                   │ │  Scale Mode ▾    │ │
│                                                   │ │  Crop       ▾    │ │
│                                                   │ │  Background ████ │ │
│                                                   │ └──────────────────┘ │
│                                                   │ ┌──────────────────┐ │
│                                                   │ │ Image Controls   │ │
│                                                   │ │  Brightness ━●── │ │
│                                                   │ │  Contrast   ━●── │ │
│                                                   │ │  Saturation ━●── │ │
│                                                   │ │  Hue        ━●── │ │
│                                                   │ │  [Reset Defaults] │ │
│                                                   │ └──────────────────┘ │
├───────────────────────────────────────────────────┴──────────────────────┤
│ Capturing 1280×720 @ 30 fps [MJPEG]  /dev/video0              FPS: 30.0 │
└────────────────────────────────────────────────────────────────────────────┘
```

The ◀ strip on the right edge of the video area collapses/expands the panel.

**Audio tab:**
```
┌──────────────────────────────┐
│ ┌─ Audio Device ───────────┐ │
│ │ [Hagibis — USB ▾]  [↻]  │ │
│ └──────────────────────────┘ │
│ ☑ Enable Audio Monitor       │
│  ┌──┬──┬──┐                  │
│  │▓▓│  │▓▓│  ← L/R meters   │
│  │░░│-6│░░│                  │
│  │  │-20│  │                 │
│  │  │-40│  │                 │
│  │ L│  │R │                  │
│  └──┴──┴──┘                  │
│ ┌─ Audio Options ──────────┐ │
│ │ ☐ Force Mono Mix         │ │
│ │ ☐ Passthrough to System  │ │
│ └──────────────────────────┘ │
│ ┌─ Volume ─────────────────┐ │
│ │ Master: [━━━●━━] 0 dB   │ │
│ │ Left:   [━━━●━━] 0 dB   │ │
│ │ Right:  [━━━●━━] 0 dB   │ │
│ └──────────────────────────┘ │
└──────────────────────────────┘
```

---

## Profiles

Profiles let you save and recall complete configurations — useful when
switching between different sources (e.g. Game Boy Color vs N64).

| Control | Action |
|---|---|
| Profile combo | Switch active profile (prompts if unsaved changes) |
| `+` | Create a new profile from the current settings |
| `✕` | Delete the active profile (Default cannot be deleted) |
| **Save Profile** | Write current in-memory settings to the active profile INI |
| **Revert** | Reload the active profile from disk, discarding pending changes |
| `⎆` | Open the profiles folder in the file manager |
| `● unsaved` | Indicator — visible whenever there are unsaved changes |

When switching away from a profile with unsaved changes, a dialog offers:
- **Discard & Switch** — abandon changes, load the new profile
- **Save & Switch** — flush current changes to disk, then load the new profile
- **Save as New Profile…** — name a new profile, save there, then switch
- **Cancel** — stay on the current profile

Every setting in the UI is profile-aware. The profile is auto-saved when
the app closes, so the last session is always restored.

### What each profile stores

| Category | Settings |
|---|---|
| Display | Panel visible, Scale Mode, Crop, Background Color |
| Capture | Video device, Format, Resolution, Frame Rate |
| Image Controls | Brightness, Contrast, Saturation, Hue |
| Audio | Audio device, Enable monitoring, Mono Mix, Passthrough |
| Volume | Master, Left trim, Right trim |

Window size and position are global (not per-profile).

---

## Video display options

### Scale Mode

| Mode | Description |
|---|---|
| Fit (Keep Aspect) | Letterbox/pillarbox; nothing cropped |
| Stretch to Fill | Fills window, ignores aspect ratio |
| Zoom to Fill (Crop) | Fills window, keeps ratio, crops edges |
| Native (1:1 Pixels) | No scaling; centre-cropped by window edges |
| Fit to 16:9 / 10:9 / 5:4 / 4:3 Area | Constrain to that ratio's sub-rect; video keeps its own ratio inside it |
| Stretch to 16:9 / 10:9 / 5:4 / 4:3 Area | Same sub-rect, but video is stretched to fill it exactly |

### Crop

Applied **before** scaling. Centre-crops the incoming frame to the target ratio:

| Mode | Removes |
|---|---|
| Full Image | Nothing |
| Crop to 10:9 | Left/right if source is wider than 10:9 |
| Crop to 5:4 | Left/right or top/bottom as needed |
| Crop to 4:3 | Left/right if source is wider than 4:3 |

### Background Color

The colour that fills the letterbox/pillarbox areas and the NO SIGNAL
placeholder. Default: `#1f1f1f`. Click the colour swatch to change.

---

## Audio

### Mono Mix

Some HDMI sources output audio on only one channel. **Force Mono Mix**
applies the ffmpeg `pan` filter:
```
pan=stereo|c0=0.5*c0+0.5*c1|c1=0.5*c0+0.5*c1
```

| Input situation | Result with Mono Mix on |
|---|---|
| Audio on left channel only | Centred on both speakers |
| Audio on right channel only | Centred on both speakers |
| Truly mono (1-ch ALSA) | Duplicated to both speakers |
| Full stereo | Collapsed to centred mono |

### Passthrough & volume

When Passthrough is enabled, ffmpeg uses `asplit` to split the stream:
```
[0:a]<pan?>asplit=2[vu][out]
  → [vu]  s16le → stdout → Python RMS → VU meters
  → [out] pulse → default sink → speakers
```

Speaker volume is controlled in real time via `pactl set-sink-input-volume`
after the ffmpeg sink input is located by PID. This means dragging any
volume slider updates the speakers immediately without restarting ffmpeg.

VU meters always show the post-volume level (Python applies the same gain
to the PCM before computing RMS), so meters and speakers stay in sync.

### Volume controls

| Control | Range | Affects |
|---|---|---|
| Master | −40 to +10 dB | Both channels uniformly |
| Left trim | −20 to +20 dB | Left channel only (additive with master) |
| Right trim | −20 to +20 dB | Right channel only (additive with master) |

---

## Known quirks and gotchas

**ALSA device index changes** — `plughw:Hagibis,0` is resolved by card name
so it survives USB reconnects. If the name appears differently, run
`arecord -l` to confirm the short name and update the audio device selection.

**PipeWire blocking ALSA direct access** — `plughw:` uses ALSA directly. If
PipeWire holds exclusive access, ffmpeg will exit immediately and the audio
error label will show the last ffmpeg error line. Switch the audio device to
the PipeWire virtual device from `pactl list short sources` if this happens.

**YUYV at high resolutions is slow** — YUYV is uncompressed; at 1920×1080
the USB bus saturates quickly. Use MJPEG for anything above 720p.

**V4L2 controls apply to the hardware immediately** — slider values written
with `v4l2-ctl` persist in the driver until the device is replugged. The app
re-applies all image control values from the active profile each time a
profile is loaded or the video stream is restarted.

**pactl sink input lookup delay** — after audio starts, the app polls for the
ffmpeg sink input every 80 ms for up to ~3 s. During this window, the speaker
volume is at the hardware default (100%). Once found, all volume slider
positions are applied immediately.

**60 fps in MJPEG at 1920×1080** — confirmed supported by the Hagibis device.
If the pipeline drops frames, lower the resolution or frame rate.

---

## AI context — read this if you are a future assistant

> This section gives you the full picture so you can contribute without
> re-reading the whole conversation.

### What this project is

A PyQt6 GUI monitor for USB capture cards on a Linux desktop. Not a recording
tool — purely live monitoring, image control, and optional audio passthrough.
All settings are organised into named profiles.

### Architecture

```
MainWindow
  ├── VideoWorker (QThread) → ffmpeg -f v4l2 → raw RGB24 → QImage → VideoDisplay
  ├── AudioWorker (QThread) → ffmpeg -f alsa → s16le PCM → numpy RMS → VU meters
  │                                         └→ (passthrough) pulse sink
  └── pactl subprocess  ← real-time volume on AudioWorker's sink input
```

Two long-lived ffmpeg subprocesses; one per worker. Workers communicate back
to the main thread exclusively via Qt signals.

### Settings / profile system

All profile-able state lives in the `AppSettings` dataclass (flat, no nesting).
The three canonical operations are:

```python
settings = _collect_settings()         # UI → struct
_apply_settings(settings)              # struct → UI + restart streams
_save_to_disk(settings, profile_name)  # struct → INI file (single QSettings obj)
settings = _load_from_disk(name)       # INI file → struct
```

Profile INI files live in `~/.config/HagibisMonitor/profiles/`. The main
`HagibisMonitor.ini` stores only `window/geometry`, `window/state`, and
`profile/current`. **Never write profile data to the global QSettings** —
it breaks the profile separation.

The `_load_from_disk()` function supports the old per-device-keyed format
(`cap/{dev_key}/fmt`) as a migration fallback.

### Key design decisions

- **ffmpeg, not OpenCV** — OpenCV was not installed; ffmpeg handles MJPEG and
  YUYV without extra libraries.
- **ffmpeg, not sounddevice/pyaudio** — ffmpeg reads ALSA directly and outputs
  raw s16le PCM for numpy.
- **`asplit` for VU + passthrough** — avoids opening the ALSA device twice.
- **`plughw:Name,N` not `hw:N,N`** — name-based, survives USB re-enumeration;
  `plughw` allows format conversion via ALSA's plugin layer.
- **pactl for real-time speaker volume** — ffmpeg's filter graph is static;
  `pactl set-sink-input-volume` adjusts the stream volume without restarting.
- **Python-side gain for VU** — `AudioWorker` multiplies raw PCM by the linear
  gain each chunk, so VU meters respond instantly while dragging sliders.
- **`v4l2-ctl` subprocess for image controls** — fire-and-forget `Popen`;
  all values re-applied via `_apply_v4l2_all()` on profile load.
- **`paintEvent` rendering in `VideoDisplay`** — `setPixmap` can only fill
  the full label bounds; using `QPainter.drawPixmap` at a computed `QPoint`
  allows area-constrained scale modes with background colour filling the rest.
- **Single QSettings object per save** — previously, calling helper functions
  that each created their own QSettings object caused sync() to overwrite
  each other. All writes now go through one object before sync().
- **In-memory dirty tracking** — changes update `self._dirty` but do not write
  to disk. Explicit Save / close-event saves flush to disk. This prevents
  accidental profile corruption while experimenting.

### Things not yet implemented

- Auto-detecting the PulseAudio source name for audio passthrough (currently
  uses `plughw:` ALSA direct; switch to a PipeWire virtual device if blocked).
- Recording / snapshot functionality.
- Detection of whether the capture card is actually sending a valid signal.
- Appearance/theming tab.

### Hardware constants (defaults, all overridable via UI)

```python
AppSettings.video_device  = "/dev/video0"
AppSettings.audio_device  = "plughw:Hagibis,0"
AudioWorker.SAMPLE_RATE   = 48000
AudioWorker.CHUNK_FRAMES  = 1024
```

### How to restart streams from code

```python
win._restart_video()   # stops VideoWorker, starts fresh with current cap_params()
win._start_audio()     # stops AudioWorker, starts fresh with current UI state
win._apply_v4l2_all()  # re-applies all image control sliders to hardware
```

### How to load / save a profile from code

```python
s = win._load_from_disk("GBC")   # read GBC.ini → AppSettings
win._apply_settings(s)           # apply to UI + restart streams
win._save_to_disk(win._collect_settings(), "GBC")  # write current UI → GBC.ini
```
