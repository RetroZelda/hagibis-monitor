import datetime
import sys
import traceback
from pathlib import Path


def _error_log_path() -> Path:
    """Where to write crash logs — next to the app's settings, falling back to
    the user's home directory if that location can't be resolved."""
    try:
        from settings import global_qsettings
        return Path(global_qsettings().fileName()).parent / "hagibis-monitor-error.log"
    except Exception:
        return Path.home() / "hagibis-monitor-error.log"


def _install_crash_logger():
    """Append unhandled exceptions to a log file (and still echo them to stderr).

    PyQt routes exceptions raised inside Qt slots through ``sys.excepthook``, so
    this also captures failures like a bad frame in ``_on_frame`` — which would
    otherwise be lost when the app runs without a visible console (the windowed
    Windows build) or scroll away in a terminal.
    """
    def hook(exc_type, exc_value, exc_tb):
        text = "".join(traceback.format_exception(exc_type, exc_value, exc_tb))
        sys.stderr.write(text)
        sys.stderr.flush()
        try:
            path = _error_log_path()
            path.parent.mkdir(parents=True, exist_ok=True)
            stamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            with open(path, "a", encoding="utf-8") as fh:
                fh.write(f"\n===== {stamp} =====\n{text}")
            sys.stderr.write(f"[hagibis-monitor] traceback written to {path}\n")
            sys.stderr.flush()
        except Exception:
            pass  # logging must never mask the original error

    sys.excepthook = hook


# Install before importing the UI so import-time failures are captured too.
_install_crash_logger()

from PyQt6.QtWidgets import QApplication

from ui import MainWindow


def main():
    app = QApplication(sys.argv)
    app.setStyle("Fusion")
    win = MainWindow()
    win.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
