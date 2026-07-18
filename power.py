"""Keep the screen awake while the app is open, the way a video player does.

On Linux the primary mechanism is the freedesktop ScreenSaver D-Bus interface —
the same Inhibit/UnInhibit call VLC, mpv and browsers use, honoured by GNOME,
KDE and most other desktops on both X11 and Wayland. If that isn't reachable we
fall back to a `systemd-inhibit` idle/sleep block held for the session.

On Windows the equivalent is the Win32 ``SetThreadExecutionState`` API (the same
one media players use), which is invoked via ctypes. The module splits at the
top level so ``PyQt6.QtDBus`` is never imported on Windows — it isn't present in
every PyQt6 wheel there, and importing it would crash the app at startup.

Both implementations expose the same public API — ``inhibit()`` (idempotent),
``release()``, and the ``active`` property — and both fail soft (silently
no-op) if their mechanism is unavailable, so the app always runs.
"""
import sys

if sys.platform == "win32":
    import ctypes

    # Win32 EXECUTION_STATE flags (winbase.h).
    _ES_CONTINUOUS       = 0x80000000
    _ES_SYSTEM_REQUIRED  = 0x00000001
    _ES_DISPLAY_REQUIRED = 0x00000002

    class ScreenWakeInhibitor:
        """Keep the display and system awake via SetThreadExecutionState.

        Must be driven from a single, consistent thread — MainWindow only calls
        inhibit()/release() from the UI thread (_apply_screen_wake, closeEvent),
        which satisfies that. inhibit() is idempotent; release() clears the
        request. Fails soft: a NULL return leaves the lock un-held.
        """

        APP_NAME = "Hagibis Monitor"
        REASON   = "Live capture monitoring"

        def __init__(self):
            self._held = False

        def inhibit(self):
            if self._held:
                return
            prev = ctypes.windll.kernel32.SetThreadExecutionState(
                _ES_CONTINUOUS | _ES_SYSTEM_REQUIRED | _ES_DISPLAY_REQUIRED)
            self._held = bool(prev)  # NULL (0) means the call failed — stay soft

        def release(self):
            if not self._held:
                return
            # Clearing back to ES_CONTINUOUS alone drops the display/system
            # requirement while leaving the thread's continuous state defined.
            ctypes.windll.kernel32.SetThreadExecutionState(_ES_CONTINUOUS)
            self._held = False

        @property
        def active(self) -> bool:
            return self._held

else:
    import subprocess

    from PyQt6.QtDBus import QDBusConnection, QDBusInterface

    class ScreenWakeInhibitor:
        """Hold a screen-wake lock for the lifetime of the app.

        inhibit() is idempotent; release() tears the lock down. If nothing works
        the object silently no-ops so the app still runs.
        """

        APP_NAME = "Hagibis Monitor"
        REASON   = "Live capture monitoring"

        # (bus name, object path, interface). GNOME and KDE both expose
        # org.freedesktop.ScreenSaver; the two object paths cover both spellings
        # used in the wild.
        _SCREENSAVER_TARGETS = [
            ("org.freedesktop.ScreenSaver", "/org/freedesktop/ScreenSaver",
             "org.freedesktop.ScreenSaver"),
            ("org.freedesktop.ScreenSaver", "/ScreenSaver",
             "org.freedesktop.ScreenSaver"),
        ]

        def __init__(self):
            self._iface: QDBusInterface | None = None
            self._cookie: int | None = None
            self._proc: subprocess.Popen | None = None

        # ── public API ────────────────────────────────────────────────────────
        def inhibit(self):
            if self._cookie is not None or self._proc is not None:
                return  # already held
            if not self._dbus_inhibit():
                self._systemd_inhibit()

        def release(self):
            if self._cookie is not None and self._iface is not None:
                try:
                    self._iface.call("UnInhibit", self._cookie)
                except Exception:
                    pass
            self._iface = None
            self._cookie = None

            if self._proc is not None:
                self._proc.terminate()
                try:
                    self._proc.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    self._proc.kill()
                self._proc = None

        @property
        def active(self) -> bool:
            return self._cookie is not None or self._proc is not None

        # ── mechanisms ────────────────────────────────────────────────────────
        def _dbus_inhibit(self) -> bool:
            try:
                bus = QDBusConnection.sessionBus()
                if not bus.isConnected():
                    return False
                for service, path, interface in self._SCREENSAVER_TARGETS:
                    iface = QDBusInterface(service, path, interface, bus)
                    if not iface.isValid():
                        continue
                    reply = iface.call("Inhibit", self.APP_NAME, self.REASON)
                    args = reply.arguments()
                    # A successful reply carries the uint32 cookie; an error
                    # reply (bool for isinstance guards out) carries no usable
                    # argument.
                    if args and isinstance(args[0], int) and not isinstance(args[0], bool):
                        self._iface = iface
                        self._cookie = args[0]
                        return True
            except Exception:
                pass
            return False

        def _systemd_inhibit(self):
            try:
                self._proc = subprocess.Popen(
                    ["systemd-inhibit",
                     "--what=idle:sleep",
                     f"--who={self.APP_NAME}",
                     f"--why={self.REASON}",
                     "--mode=block",
                     "sleep", "infinity"],
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                )
            except (FileNotFoundError, OSError):
                self._proc = None
