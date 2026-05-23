import re
import subprocess


def _scan_audio_devices() -> list[tuple[str, str]]:
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
