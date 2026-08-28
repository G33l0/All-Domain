"""Where the application keeps its files.

Running from a checkout, relative paths resolve against the working directory,
which is what a developer expects. Installed, the program directory is
read-only for ordinary users, so the same relative defaults are anchored to a
per-user data directory instead.
"""

from __future__ import annotations

import os
import sys
from typing import Optional

APPLICATION_NAME = "Domain Atlas"


def is_frozen() -> bool:
    """True when running from a PyInstaller build rather than a checkout."""
    return bool(getattr(sys, "frozen", False))


def bundle_dir() -> str:
    """Directory holding bundled read-only resources."""
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        return str(meipass)
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def user_data_dir(application: str = APPLICATION_NAME) -> str:
    """Per-user, writable directory for the database, output and settings."""
    if sys.platform.startswith("win"):
        base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~\\AppData\\Local")
        return os.path.join(base, application)
    if sys.platform == "darwin":
        return os.path.join(os.path.expanduser("~/Library/Application Support"), application)
    base = os.environ.get("XDG_DATA_HOME") or os.path.expanduser("~/.local/share")
    return os.path.join(base, application.lower().replace(" ", "-"))


def default_base_dir() -> str:
    """Directory that relative paths are resolved against."""
    if is_frozen():
        return user_data_dir()
    return os.getcwd()


def resolve(path: str, base: Optional[str] = None) -> str:
    """Anchor a relative path to *base*; absolute paths are left alone."""
    if not path:
        return path
    if os.path.isabs(path):
        return path
    return os.path.join(base or default_base_dir(), path)


def ensure_base_dir(base: Optional[str] = None) -> str:
    """Create the base directory if it does not exist, and return it."""
    directory = base or default_base_dir()
    try:
        os.makedirs(directory, exist_ok=True)
    except OSError:
        # Fall back to the working directory rather than refusing to start.
        directory = os.getcwd()
    return directory


def ensure_streams() -> None:
    """Guarantee sys.stdout and sys.stderr exist.

    A windowed Windows build has no console, leaving both set to None, and any
    print() or logging call then raises AttributeError.
    """
    for name in ("stdout", "stderr"):
        if getattr(sys, name, None) is None:
            try:
                setattr(sys, name, open(os.devnull, "w", encoding="utf-8"))
            except OSError:  # pragma: no cover - defensive
                pass
