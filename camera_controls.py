"""DirectShow image controls (brightness / contrast / saturation / hue) for Windows.

The Linux side of the app shells out to ``v4l2-ctl --set-ctrl`` from ui.py; this
module is the Windows counterpart. It talks to the capture device's DirectShow
``IAMVideoProcAmp`` interface via ``comtypes`` — pure Python, no native build
step, so it installs on any Windows Python (unlike duvc-ctl, which ships no wheel
for newer/pre-release interpreters and whose sdist does not build).

Everything fails soft: if ``comtypes`` is missing, no device matches, or a COM
call raises, the public functions return empty/False and ui.py disables the
image-control sliders. ui.py is the only importer; this stays a leaf module with
no project-local imports, matching video.py / audio.py / workers.py / power.py.

Public API (all safe to call on any platform):
    available()                          -> bool
    invalidate_cache()                   -> None
    supported_controls(device_id)        -> set[str]
    set_control_percent(device_id, c, p) -> bool
"""
import sys

_IS_WINDOWS = sys.platform == "win32"

# The four controls the UI exposes, mapped to tagVideoProcAmpProperty ordinals.
_PROP_MAP = {"brightness": 0, "contrast": 1, "hue": 2, "saturation": 3}
_VIDEOPROCAMP_FLAGS_MANUAL = 0x0002

# COM plumbing is imported lazily/guarded so importing this module is harmless
# everywhere (Linux, or a Windows box without comtypes).
_COM_OK = False
if _IS_WINDOWS:
    try:
        from ctypes import HRESULT, POINTER, byref, c_long, c_ulong
        import comtypes
        from comtypes import GUID, IUnknown, COMMETHOD, CoCreateInstance, CLSCTX_INPROC_SERVER
        from comtypes.automation import VARIANT
        from comtypes.persist import IPropertyBag

        # ── CLSIDs / IIDs ──────────────────────────────────────────────────────
        _CLSID_SystemDeviceEnum       = GUID("{62BE5D10-60EB-11d0-BD3B-00A0C911CE86}")
        _CLSID_VideoInputDeviceCat    = GUID("{860BB310-5D01-11d0-BD3B-00A0C911CE86}")
        _IID_IPropertyBag             = GUID("{55272A00-42CB-11CE-8135-00AA004BB851}")
        _IID_IAMVideoProcAmp          = GUID("{C6E13360-30AC-11d0-A18C-00A0C9118956}")

        class _IEnumMoniker(IUnknown):
            _iid_ = GUID("{00000102-0000-0000-C000-000000000046}")

        class _IMoniker(IUnknown):
            _iid_ = GUID("{0000000f-0000-0000-C000-000000000046}")

        class _IAMVideoProcAmp(IUnknown):
            _iid_ = _IID_IAMVideoProcAmp

        class _ICreateDevEnum(IUnknown):
            _iid_ = GUID("{29840822-5B84-11D0-BD3B-00A0C911CE86}")

        # IMoniker inherits IPersist(1) + IPersistStream(4) before its own
        # methods. Those seven leading slots are stubbed (never called) purely to
        # place BindToObject / BindToStorage at the correct vtable offsets.
        _stub = lambda name: COMMETHOD([], HRESULT, name)
        _IMoniker._methods_ = [
            _stub("GetClassID"), _stub("IsDirty"), _stub("Load"),
            _stub("Save"), _stub("GetSizeMax"),
            COMMETHOD([], HRESULT, "BindToObject",
                      (["in"], POINTER(IUnknown), "pbc"),
                      (["in"], POINTER(_IMoniker), "pmkToLeft"),
                      (["in"], POINTER(GUID), "riidResult"),
                      (["out"], POINTER(POINTER(IUnknown)), "ppvResult")),
            COMMETHOD([], HRESULT, "BindToStorage",
                      (["in"], POINTER(IUnknown), "pbc"),
                      (["in"], POINTER(_IMoniker), "pmkToLeft"),
                      (["in"], POINTER(GUID), "riid"),
                      (["out"], POINTER(POINTER(IUnknown)), "ppvObj")),
        ]

        _IEnumMoniker._methods_ = [
            COMMETHOD([], HRESULT, "Next",
                      (["in"], c_ulong, "celt"),
                      (["out"], POINTER(POINTER(_IMoniker)), "rgelt"),
                      (["out"], POINTER(c_ulong), "pceltFetched")),
            COMMETHOD([], HRESULT, "Skip", (["in"], c_ulong, "celt")),
            COMMETHOD([], HRESULT, "Reset"),
            COMMETHOD([], HRESULT, "Clone",
                      (["out"], POINTER(POINTER(_IEnumMoniker)), "ppenum")),
        ]

        _ICreateDevEnum._methods_ = [
            COMMETHOD([], HRESULT, "CreateClassEnumerator",
                      (["in"], POINTER(GUID), "clsidDeviceClass"),
                      (["out"], POINTER(POINTER(_IEnumMoniker)), "ppEnumMoniker"),
                      (["in"], c_ulong, "dwFlags")),
        ]

        _IAMVideoProcAmp._methods_ = [
            COMMETHOD([], HRESULT, "GetRange",
                      (["in"], c_long, "Property"),
                      (["out"], POINTER(c_long), "pMin"),
                      (["out"], POINTER(c_long), "pMax"),
                      (["out"], POINTER(c_long), "pSteppingDelta"),
                      (["out"], POINTER(c_long), "pDefault"),
                      (["out"], POINTER(c_long), "pCapsFlags")),
            COMMETHOD([], HRESULT, "Set",
                      (["in"], c_long, "Property"),
                      (["in"], c_long, "lValue"),
                      (["in"], c_long, "Flags")),
            COMMETHOD([], HRESULT, "Get",
                      (["in"], c_long, "Property"),
                      (["out"], POINTER(c_long), "lValue"),
                      (["out"], POINTER(c_long), "Flags")),
        ]

        _COM_OK = True
    except Exception:
        _COM_OK = False


# device_id -> {"procamp": _IAMVideoProcAmp, "ranges": {ctrl: (min, max, step, default)}}
_cache: dict[str, dict] = {}


def available() -> bool:
    """True only when this platform + comtypes can drive image controls."""
    return _IS_WINDOWS and _COM_OK


def invalidate_cache() -> None:
    """Drop cached device bindings — call on a device rescan or replug."""
    _cache.clear()


def supported_controls(device_id: str) -> set[str]:
    entry = _resolve(device_id)
    return set(entry["ranges"]) if entry else set()


def set_control_percent(device_id: str, ctrl: str, pct: int) -> bool:
    """Map a 0-100 UI value onto the device's control range and apply it.

    Returns False (and leaves the slider effectively inert) if the control is
    unsupported or the COM call fails — e.g. the device was unplugged.
    """
    entry = _resolve(device_id)
    if not entry or ctrl not in entry["ranges"]:
        return False
    lo, hi, step, _default = entry["ranges"][ctrl]
    step = step or 1
    span = hi - lo
    raw = lo + round((pct / 100.0) * span / step) * step
    raw = max(lo, min(hi, raw))
    try:
        entry["procamp"].Set(_PROP_MAP[ctrl], int(raw), _VIDEOPROCAMP_FLAGS_MANUAL)
        return True
    except Exception:
        _cache.pop(device_id, None)  # binding likely stale (device removed)
        return False


# ── internals ─────────────────────────────────────────────────────────────────
def _resolve(device_id: str):
    """Match an ffmpeg dshow id (friendly name or @device_pnp_ alt name) to the
    device's IAMVideoProcAmp, caching the binding and its control ranges."""
    if not available() or not device_id:
        return None
    if device_id in _cache:
        return _cache[device_id]
    try:
        comtypes.CoInitialize()
    except Exception:
        pass
    try:
        devices = _enumerate_video_devices()  # [(friendly, device_path, moniker)]
    except Exception:
        return None
    moniker = _match_device(device_id, devices)
    if moniker is None:
        return None
    try:
        unk = moniker.BindToObject(None, None, byref(_IID_IAMVideoProcAmp))
        procamp = unk.QueryInterface(_IAMVideoProcAmp)
    except Exception:
        return None
    ranges = {}
    for ctrl, prop in _PROP_MAP.items():
        try:
            mn, mx, step, default, _caps = procamp.GetRange(prop)
            ranges[ctrl] = (mn, mx, step, default)
        except Exception:
            pass  # control not supported on this device
    if not ranges:
        return None
    _cache[device_id] = {"procamp": procamp, "ranges": ranges}
    return _cache[device_id]


def _enumerate_video_devices():
    """Yield (friendly_name, device_path, moniker) for each video input device,
    read from each moniker's IPropertyBag — the same set ffmpeg enumerates."""
    dev_enum = CoCreateInstance(
        _CLSID_SystemDeviceEnum, _ICreateDevEnum, CLSCTX_INPROC_SERVER)
    enum_mon = dev_enum.CreateClassEnumerator(byref(_CLSID_VideoInputDeviceCat), 0)
    out = []
    if not enum_mon:  # NULL when the category has no devices
        return out
    while True:
        try:
            moniker, fetched = enum_mon.Next(1)
        except Exception:
            break
        if not moniker or not fetched:
            break
        friendly = _read_prop(moniker, "FriendlyName")
        path = _read_prop(moniker, "DevicePath")
        out.append((friendly or "", path or "", moniker))
    return out


def _read_prop(moniker, name: str) -> str:
    try:
        unk = moniker.BindToStorage(None, None, byref(_IID_IPropertyBag))
        bag = unk.QueryInterface(IPropertyBag)
        # comtypes' IPropertyBag.Read marks the VARIANT param in|out and returns
        # its value directly; pass a fresh VARIANT and use the return value.
        value = bag.Read(name, VARIANT(), None)
        return str(value) if value is not None else ""
    except Exception:
        return ""


def _norm(s: str) -> str:
    return "".join(ch for ch in s.lower() if ch.isalnum())


def _match_device(device_id: str, devices):
    """Correlate the stored dshow id to an enumerated moniker."""
    if device_id.startswith("@device_"):
        core = _norm(device_id)  # the alt name embeds the DevicePath's usb#... core
        for _friendly, path, moniker in devices:
            if path and (_norm(path) in core or core in _norm(path)):
                return moniker
    else:
        want = device_id.strip().lower()
        for friendly, _path, moniker in devices:
            if friendly.strip().lower() == want:
                return moniker
    if len(devices) == 1:  # unambiguous single camera
        return devices[0][2]
    return None
