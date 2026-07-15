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
10. [Output (virtual camera + virtual mic)](#output-virtual-camera--virtual-mic)
11. [Known quirks and gotchas](#known-quirks-and-gotchas)
12. [For AI agents](#for-ai-agents)

> **AI / coding agents:** before doing any work in this repo, read both
> this README and [AGENTS.md](AGENTS.md). AGENTS.md contains the rules
> you are expected to follow (versioning, diagrams, doc hygiene) and the
> deeper architectural context that humans don't usually need to re-read.

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
  single-channel console audio). Requires ffmpeg restart to apply.
- **Audio Passthrough** — routes captured audio to the default
  PulseAudio/PipeWire sink using ffmpeg's `asplit` filter. Volume is applied
  in real time via `pactl set-sink-input-volume`.
- **Virtual Microphone** — when Output is enabled, creates a persistent
  PulseAudio virtual source (`hagibis_virtual`) that other apps (e.g. OBS,
  Discord) can use as a microphone input. Volume is applied in real time via
  `pactl set-sink-volume hagibis_bus` without restarting ffmpeg.
- **Master + per-channel volume** — master fader (−40 to +10 dB) and
  independent L/R trims (−20 to +20 dB). VU meters reflect volume changes
  instantly while dragging.

### Output (virtual camera)
- **v4l2loopback virtual camera** — writes the processed video feed to a
  `/dev/videoN` loopback device. OBS and other apps can read from it.
- **Separate output scale / crop** — independent scale mode and crop for the
  loopback output, stored per-profile.
- **Pan / zoom** — adjustable pan and zoom applied to both the preview and the
  loopback output, stored per-profile.
- **Output resolution / format / fps** — configurable globally (not per-profile).
  All standard resolutions are advertised to readers.
- **Persistent virtual source** — the PulseAudio virtual mic modules are kept
  alive across audio worker restarts. OBS never loses the device when volume
  or mono settings change.

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
├── main.py       # Entry point — constructs QApplication + MainWindow
├── ui.py         # MainWindow + _StatusBar — all UI construction, profile mgmt, stream orchestration
├── video.py      # VideoDisplay widget + video device scanning / capability querying
├── audio.py      # Audio device scanning (PulseAudio sources → ALSA fallback)
├── output.py     # v4l2loopback discovery / load / unload + _ModprobeWorker
├── settings.py   # AppSettings + OutputSettings dataclasses + resolution/format/fps constant tables
├── utils.py      # Small shared helpers (_dev_key, _aspect_label, _sbin, _slider_row, _db_label)
├── workers.py    # VideoWorker, AudioWorker, OutputWorker (QThread subclasses)
├── vu_meter.py   # VuMeter, DbScale, StereoVuMeter custom widgets
├── .gitignore
└── .vscode/
    └── launch.json   # VS Code debugpy configuration
```

Settings are stored in:
```
~/.config/HagibisMonitor/
├── HagibisMonitor.ini        # window geometry + output settings (global)
└── profiles/
    ├── Default.ini           # always present; created on first launch
    ├── GBC.ini               # example user profile
    └── N64.ini               # example user profile
```

### main.py

Minimal entry point. Creates `QApplication`, sets the `"Fusion"` style,
instantiates `MainWindow` from `ui.py`, shows it, and enters the event loop.
No application logic lives here.

### ui.py

**`MainWindow(QMainWindow)`** — owns every widget and every worker. Builds
the three tabs (Video / Audio / Output), the profile bar, the dark theme,
and the status bar. Holds references to the running `VideoWorker`,
`AudioWorker`, `OutputWorker`, and `_ModprobeWorker`. Manages the dirty
flag and the unsaved-changes dialog.

Key I/O methods:
- `_collect_settings() → AppSettings` — reads every widget into a struct.
- `_apply_settings(s)` — applies a struct to every widget + restarts streams.
- `_load_from_disk(name) → AppSettings` — reads a profile INI file.
- `_save_to_disk(s, name)` — writes a profile INI file atomically via one
  QSettings object (avoids the multi-object sync bug).

**`_StatusBar(QWidget)`** — custom full-width status bar that centres the
`● Audio` / `● Video` output indicators over the video display area, with
the FPS readout pinned to the right.

### video.py

**`VideoDisplay(QLabel)`** — renders frames via `paintEvent` using a
pre-computed `(QPixmap, QPoint)` pair. Supports all twelve scale modes and
four crop modes independently for both display and loopback output. Owns
the pan/zoom state and emits `output_changed(pan_x, pan_y, zoom)` when the
user drags or wheel-scrolls inside the canvas.

**`_scan_video_devices()`** — runs `v4l2-ctl --list-devices`; falls back
to globbing `/dev/video*` if v4l2-ctl is missing.

**`_query_device_caps(dev)`** — runs `v4l2-ctl --list-formats-ext` for one
device and returns a `{format: {label, sizes: {(w, h): [fps, …]}}}` dict.

### audio.py

**`_scan_audio_devices()`** — prefers `pactl list sources` (works under
PipeWire); falls back to `arecord -l` for direct ALSA addressing. Returns
`[(display_label, ffmpeg_address), …]`.

### output.py

**`_find_loopback_devices()`** — walks `/sys/class/video4linux/` looking
for nodes that expose the `max_openers` attribute (or have `v4l2loopback`
in their device path / name). Returns `/dev/videoN` paths.

**`_v4l2loopback_installed()`** — checks for the `.ko` file under the
running kernel or `/sys/module/v4l2loopback`.

**`_load_v4l2loopback()` / `_unload_v4l2loopback()`** — modprobes the
module, falling back to `pkexec` if direct invocation fails. Loads
without `exclusive_caps` so OBS sees the full set of resolutions.

**`_ModprobeWorker(QThread)`** — runs `_load_v4l2loopback()` off the UI
thread; emits `done(device_path, loaded_by_us)`.

### settings.py

**`AppSettings` (dataclass)** — flat struct holding every profile-able value:
scale/crop/bg_color, video device, fmt/res/fps/image controls, audio device,
audio enabled, mono mix, passthrough, three volume levels, output scale/crop
modes, and pan/zoom.

**`OutputSettings` (dataclass)** — globally-stored output settings: enabled
flag, loopback device path, width, height, pixel format, fps. Always starts
disabled on launch regardless of saved state.

Also holds the constant tables used by the UI: `_DEFAULT_RESOLUTIONS`,
`_DEFAULT_FRAMERATES`, `_DEFAULT_FORMATS`, `_OUTPUT_RESOLUTIONS`,
`_OUTPUT_PIXEL_FORMATS`, `_OUTPUT_FPS`.

### utils.py

Tiny shared helpers used by `ui.py` and `output.py`:
- `_dev_key(path)` — sanitises a device path for use as an INI key (legacy
  migration support).
- `_aspect_label(w, h)` — `gcd`-reduced aspect ratio label (with `16:10`
  override for the `8:5` corner case).
- `_sbin(cmd)` — resolves a command that may live in `/usr/sbin` even
  when `PATH` is minimal.
- `_slider_row(lo, hi, val, on_change)` — builds a `(QSlider, QLabel)`
  pair with a value readout, returned as an `(HBoxLayout, slider, label)`
  tuple.
- `_db_label(v)` — formats an int dB value as `"0 dB"` or `"+3 dB"` /
  `"-12 dB"`.

### workers.py

**`VideoWorker(QThread)`**:
- Spawns `ffmpeg -f v4l2 … -f rawvideo -pix_fmt rgb24 -`.
- Reads `width × height × 3` bytes per frame.
- Emits `frame_ready(QImage)` and `fps_updated(float)`.

**`AudioWorker(QThread)`**:
- Spawns ffmpeg reading from `self.device` (ALSA `plughw:` address).
- Without passthrough/virtual: pipes raw s16le PCM to stdout for VU meters.
- With passthrough: `asplit` sends a copy to the default PulseAudio sink.
- With virtual output: `asplit` sends a copy via a pipe to `pacat`, which
  writes to the `hagibis_bus` null-sink. A virtual source (`hagibis_virtual`)
  remaps from `hagibis_bus.monitor` for use by OBS/Discord/etc.
- PA modules (`hagibis_bus` + `hagibis_virtual`) are kept alive across worker
  restarts via `_find_existing_modules()`. `teardown()` must be called
  explicitly to unload them (on output disable or app close).
- Python applies `10^((master_db + channel_trim) / 20)` gain to the stdout
  PCM before RMS computation, giving real-time VU response while dragging.

**`OutputWorker(QThread)`**:
- Spawns `ffmpeg -f rawvideo … -f v4l2 /dev/videoN`.
- Receives frames via `push_frame()` with a drop-newest queue (size 4).
- Uses a monotonic-clock pacing loop to write frames at exactly the target fps.
- Applies pan, zoom, scale mode, and crop mode per frame via QPainter.

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
| pactl | Real-time volume control + PA module management | `which pactl` |
| pacat | Pipe PCM into PulseAudio sink (virtual mic) | `which pacat` |
| xdg-open | Open config folder button | `which xdg-open` |
| v4l2loopback | Virtual camera device (optional) | `modinfo v4l2loopback` |

Install missing Python packages:
```bash
pip install PyQt6 numpy
```

Install system tools (Debian/Ubuntu):
```bash
sudo apt install ffmpeg v4l2-utils alsa-utils pulseaudio-utils
```

For the virtual camera output feature:
```bash
sudo apt install v4l2loopback-dkms
```

`xdg-open` is part of `xdg-utils`, usually pre-installed on desktop systems.

---

## Running the app

There are three ways to run the app, in increasing order of formality:

### 1. From source (development)

```bash
cd ~/Development/projects/hagibis-monitor
python3 main.py
```

Or press **F5** in VS Code (uses `.vscode/launch.json`). Requires the
Python deps from the [Dependencies](#dependencies) section installed on
your system (or in a venv) — there is no install step for this path.

### 2. Build a standalone binary — [`build.sh`](build.sh)

```bash
./build.sh
```

What it does:
- Creates (or reuses) an isolated build venv at `.venv-build/`.
- Installs `pyinstaller`, `PyQt6`, and `numpy` into that venv only — your
  system Python is not touched.
- Runs `pyinstaller hagibis-monitor.spec --clean --noconfirm`, producing
  a single-file executable at **`dist/hagibis-monitor`**.

The resulting binary bundles Python, PyQt6, and numpy, so it runs on any
glibc-compatible Linux without a Python install. It still needs the
runtime system tools (`ffmpeg`, `v4l2-ctl`, `pactl`, `pacat`, etc. — see
[Dependencies](#dependencies)).

Run it directly without installing:

```bash
./dist/hagibis-monitor
```

### 3. Install system-wide or per-user — [`install.sh`](install.sh)

After `./build.sh` has produced `dist/hagibis-monitor`:

```bash
./install.sh              # per-user (default): installs into ~/.local
./install.sh --system     # system-wide: installs into /usr/local (asks for sudo)
```

What it installs:

| Scope | Binary | Desktop entry |
|---|---|---|
| Per-user (default) | `~/.local/bin/hagibis-monitor` | `~/.local/share/applications/hagibis-monitor.desktop` |
| `--system` | `/usr/local/bin/hagibis-monitor` | `/usr/local/share/applications/hagibis-monitor.desktop` |

The desktop entry's `Exec=` line is rewritten to the absolute install
path, and `update-desktop-database` is run if available so the app shows
up in your application menu under **AudioVideo → Hagibis Monitor**
immediately (no re-login needed on most desktops).

If `~/.local/bin` is not on your `PATH`, the installer reminds you to add it:

```bash
export PATH="$HOME/.local/bin:$PATH"
```

### 4. Pre-built release tarball

If you'd rather not build locally, every push that bumps the version in
[`.github/workflows/release.yml`](.github/workflows/release.yml) (and
every manual workflow run) publishes a `hagibis-monitor-vX.Y.Z-linux-x86_64.tar.gz`
to the project's GitHub Releases page. Extract it and run `./install.sh`
from inside the extracted directory — same options as above.

> **Note:** `hagibis-monitor.sh` in the repo root is **not** the app —
> it's the original GStreamer one-liner that predated this project, kept
> only for reference. Don't use it.

---

## UI walkthrough

```
┌──────────────────────────────────────────────────◀┬──────────────────────┐
│                                                   │ Profile: [Default ▾] │
│                                                   │ [+] [✕]              │
│                                                   │ [Save Profile][Revert]│
│                                                   │ [⎆]  ● unsaved       │
│                                                   ├──────────────────────┤
│              Live video preview                   │ [Video][Audio][Output]│
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

**Output tab:**
```
┌──────────────────────────────┐
│ ☐ Enable Output              │
│ ┌─ Video Device ───────────┐ │
│ │ [/dev/video10 ▾]  [↻][⧉]│ │
│ └──────────────────────────┘ │
│ ┌─ Format ─────────────────┐ │
│ │  [16:9][4:3]…            │ │
│ │  Resolution ▾            │ │
│ │  Pixel Format ▾          │ │
│ │  Frame Rate ▾            │ │
│ └──────────────────────────┘ │
│ ┌─ Scale & Crop ───────────┐ │
│ │  Scale Mode ▾            │ │
│ │  Crop       ▾            │ │
│ └──────────────────────────┘ │
│ ┌─ Pan / Zoom ─────────────┐ │
│ │  X [━━━●━━]  Y [━━━●━━] │ │
│ │  Zoom [━━━●━━]  [Reset]  │ │
│ └──────────────────────────┘ │
│  ● Video  ● Audio            │
└──────────────────────────────┘
```

When output is enabled and v4l2loopback is loaded, the status line at the
bottom shows `● Video` and `● Audio` indicators.

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
| Output display | Output Scale Mode, Output Crop |
| Pan / Zoom | Pan X, Pan Y, Zoom (applies to both preview and loopback output) |

Output device, resolution, pixel format, and fps are **global** (not
per-profile). The output enabled state always starts as disabled on launch.

Window size and position are also global (not per-profile).

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

The Output tab has its own independent Scale Mode and Crop selection, so the
loopback output can be framed differently from the on-screen preview.

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

Changing Mono Mix restarts ffmpeg. Because the PulseAudio virtual source
modules are kept alive across restarts, OBS does not lose its microphone
device connection during this restart.

### Passthrough & volume

When Passthrough is enabled, ffmpeg uses `asplit` to split the stream:
```
[0:a]<pan?>asplit=2[vu][out]
  → [vu]  s16le → stdout → Python RMS → VU meters
  → [out] pulse → default sink → speakers
```

Speaker volume is controlled in real time via `pactl set-sink-input-volume`
after the ffmpeg sink input is located by PID.

### Virtual Microphone

When **Output** is enabled, a second split is added and `pacat` is used to
pipe PCM into a PulseAudio null-sink (`hagibis_bus`). A virtual source
(`hagibis_virtual`) remaps from its monitor:

```
ffmpeg → pipe → pacat → hagibis_bus (null-sink)
                              ↓ monitor
                       hagibis_virtual (remap-source) → OBS / Discord / …
```

Volume for the virtual mic is controlled in real time via
`pactl set-sink-volume hagibis_bus`, targeting the null-sink directly (more
reliable in PipeWire than setting volume on the virtual source).

The `hagibis_bus` and `hagibis_virtual` PA modules are **persistent** — they
are not unloaded when the audio worker restarts (e.g. when changing Mono Mix
or volume). They are only unloaded when Output is explicitly disabled or the
app closes.

### Volume controls

| Control | Range | Affects |
|---|---|---|
| Master | −40 to +10 dB | Both channels uniformly |
| Left trim | −20 to +20 dB | Left channel only (additive with master) |
| Right trim | −20 to +20 dB | Right channel only (additive with master) |

Dragging any volume slider immediately updates:
- The VU meter display (Python-side gain applied to PCM before RMS)
- The passthrough speaker level (via `pactl set-sink-input-volume`)
- The virtual mic level (via `pactl set-sink-volume hagibis_bus`)

---

## Output (virtual camera + virtual mic)

### Virtual camera

Enabling Output loads `v4l2loopback` (via `modprobe`, using `pkexec` for
privilege escalation if needed) and starts `OutputWorker`, which writes
processed frames to the loopback device at the selected resolution and fps.

The loopback device is loaded without `exclusive_caps`, so it advertises the
full standard set of V4L2 resolutions and formats. OBS and other readers see
all resolutions in their device settings.

When the output resolution is changed, the app:
1. Stops OutputWorker (closes the write side — triggers `V4L2_EVENT_SOURCE_CHANGE` to readers)
2. Waits 150 ms for readers to react
3. Starts a new OutputWorker at the new resolution

OBS handles `V4L2_EVENT_SOURCE_CHANGE` by automatically restarting its
capture pipeline with the updated format.

### Virtual microphone

See [Virtual Microphone](#virtual-microphone) in the Audio section above.

### Output status indicators

The status bar at the bottom of the window shows:

| Indicator | Meaning |
|---|---|
| `● Video` (green) | OutputWorker is running and writing to the loopback device |
| `● Audio` (green) | AudioWorker is running with virtual output enabled |
| Grey / absent | That output is not active |

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

**pactl sink input lookup delay** — after audio starts with passthrough
enabled, the app polls for the ffmpeg sink input every 80 ms for up to ~3 s.
During this window, the speaker volume is at the hardware default (100%).
Once found, all volume slider positions are applied immediately.

**Virtual mic volume applied after 500 ms** — when the audio worker starts
with virtual output enabled, the app waits 500 ms before calling
`pactl set-sink-volume hagibis_bus` to allow pacat time to connect to the
sink. During this window the virtual mic is at 100% volume.

**v4l2loopback requires a kernel module** — if the module is not installed,
enabling Output will show an error. Install `v4l2loopback-dkms` and grant
permission via the `pkexec` prompt. The module is loaded once and left loaded
for the session; the app never unloads it.

**v4l2loopback module loaded with old exclusive_caps** — if the module was
previously loaded with `exclusive_caps=1` (e.g. from a prior session), OBS
will only see one resolution. Reload the module:
```bash
sudo modprobe -r v4l2loopback
# then re-enable Output in the app
```

**60 fps in MJPEG at 1920×1080** — confirmed supported by the Hagibis device.
If the pipeline drops frames, lower the resolution or frame rate.

**Frames are dropped, not queued, under load** — the capture worker holds at
most one un-rendered frame in flight. If the GUI can't keep up (heavy zoom,
a slow machine, a busy compositor), newer frames are dropped at the source
rather than buffered. This keeps memory flat; the trade-off is that the
preview/output framerate follows what the GUI can actually paint. Earlier
versions buffered every frame and could exhaust system memory.

**Output re-enables itself on launch (only if the device survived)** — if you
quit with Output on and the v4l2loopback device is still present next launch,
Output turns back on automatically without a `pkexec` prompt. If the device is
gone, Output stays off so launching never triggers a module-load prompt.

**Missing saved device** — if a profile's saved video or audio device isn't
present at load, the app falls back to an available device and shows a warning
in the status bar instead of silently capturing the wrong one.

---

## For AI agents

If you are an AI coding agent (Claude Code, Cursor, Codex, Aider, Copilot
agents, etc.), the rules and architectural context you need to do work in
this repo live in **[AGENTS.md](AGENTS.md)**. That file covers:

- The expectation that you read this README first.
- How and when to bump the version in
  [`.github/workflows/release.yml`](.github/workflows/release.yml) (semver
  rules, the "inherit pending bumps" rule, push-trigger interaction).
- The requirement to keep this README — and AGENTS.md — up to date in the
  same commit as any change that makes either of them stale.
- The requirement to use Mermaid (never ASCII boxes or external images)
  for any diagram.
- The full module dependency graph, runtime architecture graph, profile
  system, `AppSettings` field list, design-decision log, and "how to do X
  from code" snippets.

Humans can skip AGENTS.md; the sections above this one cover everything
needed to install, run, and use the app.

