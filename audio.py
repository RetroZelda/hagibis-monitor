import re
import subprocess
import sys
from collections import Counter

_IS_WINDOWS = sys.platform == "win32"
_NO_WINDOW = {"creationflags": subprocess.CREATE_NO_WINDOW} if _IS_WINDOWS else {}

# DirectShow device-list regexes. These intentionally duplicate the twin parser
# in video.py: audio.py and video.py are both leaf modules with no project-local
# imports (an AGENTS.md rule), so the ~2-regex walk is copied rather than shared.
# Keep the two in sync. The log-line prefix varies by ffmpeg version
# ("[in#0 @ …]" now, "[dshow @ …]" older) — anchor on a generic "[...]".
_DSHOW_DEV_RE = re.compile(r'^\[[^\]]+\]\s+"(?P<name>.+)"\s+\((?P<types>[^)]*)\)\s*$')
_DSHOW_ALT_RE = re.compile(r'^\[[^\]]+\]\s+Alternative name\s+"(?P<alt>.+)"\s*$')


def _scan_audio_devices() -> list[tuple[str, str]]:
    return _scan_audio_devices_windows() if _IS_WINDOWS else _scan_audio_devices_linux()


# ── Linux (PulseAudio → ALSA) ─────────────────────────────────────────────────
def _scan_audio_devices_linux() -> list[tuple[str, str]]:
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


# ── Windows (DirectShow via ffmpeg) ───────────────────────────────────────────
def _scan_audio_devices_windows() -> list[tuple[str, str]]:
    entries = _list_dshow_audio_devices()
    # Duplicate-name policy mirrors video.py: prefer the friendly name as the id
    # (readable, replug-stable); fall back to the @device_cm_ alternative name
    # only when a friendly name is ambiguous. No fake fallback row on Windows.
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


def _list_dshow_audio_devices() -> list[tuple[str, str]]:
    """Return [(friendly_name, alternative_name)] for dshow audio devices.

    ffmpeg prints the list to stderr and exits non-zero on the dummy open, so
    parse stderr regardless of return code; non-matching driver noise is skipped.
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
            out.append([m["name"], ""] if "audio" in types else None)
            continue
        m = _DSHOW_ALT_RE.match(line)
        if m and out and out[-1] is not None and out[-1][1] == "":
            out[-1][1] = m["alt"]
    return [(e[0], e[1]) for e in out if e is not None]
