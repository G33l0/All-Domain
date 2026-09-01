"""PySide6 desktop front-end. Used whenever PySide6 is installed; the Tk
interface remains as a fallback."""

from __future__ import annotations

import importlib
import sys
from typing import Optional

from ..config import DEFAULT_CONFIG_PATH, Config

__all__ = ["pyside_available", "run_qt_gui"]

#: Identifies the application to the Windows shell. Without it a source
#: checkout is grouped under python.exe and inherits the Python icon.
APP_USER_MODEL_ID = "DomainAtlas.DomainAtlas.Desktop.3"

_IMPORT_ERROR: Optional[BaseException] = None


def pyside_available() -> bool:
    """True when PySide6 can actually be imported (not just installed)."""
    global _IMPORT_ERROR
    try:
        importlib.import_module("PySide6.QtWidgets")
    except Exception as exc:  # missing package, missing system GL libs, ...
        _IMPORT_ERROR = exc
        return False
    return True


def import_error() -> Optional[BaseException]:
    return _IMPORT_ERROR


def claim_windows_taskbar_identity(app_id: str = APP_USER_MODEL_ID) -> bool:
    """Tell Windows this process is its own application.

    A source checkout runs inside python.exe, and the shell groups the window
    under that host process, taskbar icon included. Setting an explicit
    AppUserModelID gives the window its own button and its own icon. Frozen
    builds get the same treatment so pinning survives an upgrade.

    Returns True when the shell accepted the identifier.
    """
    if not sys.platform.startswith("win"):
        return False
    try:
        import ctypes

        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(app_id)
    except Exception:  # pragma: no cover - not Windows, or an old shell32
        return False
    return True


def run_qt_gui(config: Config, config_path: str = DEFAULT_CONFIG_PATH,
               theme: Optional[str] = None) -> int:
    """Open the Qt window.  Returns a process exit code."""
    if not pyside_available():
        raise RuntimeError(
            "PySide6 is not available.\n"
            "Install it with:  pip install PySide6\n"
            f"(import failed with: {_IMPORT_ERROR})\n"
            "You can also run the Tk interface with --ui tk, or headless with --headless."
        ) from _IMPORT_ERROR

    from PySide6.QtCore import QSettings
    from PySide6.QtWidgets import QApplication

    from .logo import logo_icon
    from .mainwindow import MainWindow
    from .theme import palette_for, theme_names

    claim_windows_taskbar_identity()

    app = QApplication.instance() or QApplication(sys.argv)
    app.setApplicationName("Domain Atlas")
    app.setOrganizationName("DomainAtlas")
    app.setApplicationDisplayName("Domain Atlas")
    app.setDesktopFileName("domain-atlas")  # taskbar identity on Linux
    app.setStyle("Fusion")  # renders identically on every platform
    app.setWindowIcon(logo_icon())

    if theme is None:
        theme = str(QSettings("DomainAtlas", "DomainAtlas").value("theme", "system"))
    if theme not in ("system", *theme_names()):
        theme = "system"

    window = MainWindow(config, config_path, theme=theme)

    # Follow the OS switching between light and dark at runtime (Qt 6.5+).
    hints = app.styleHints()
    signal = getattr(hints, "colorSchemeChanged", None)
    if signal is not None:
        signal.connect(lambda _scheme: window.theme_name == "system"
                       and window.apply_theme(palette_for("system")))

    window.show()
    return app.exec()
