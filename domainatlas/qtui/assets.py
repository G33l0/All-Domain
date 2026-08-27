"""Small SVG assets materialised on disk for the stylesheet to reference.

Qt's stylesheet ``image:`` property needs a file path (or a compiled Qt
resource).  Rather than shipping binary resources, the handful of glyphs the
theme needs - the checkbox tick, the combo/spin chevrons - are written into the
user's cache directory the first time a theme is applied, tinted for that
theme.
"""

from __future__ import annotations

import os
import tempfile
from typing import Dict

from PySide6.QtCore import QStandardPaths

_CHECK = (
    '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none" stroke="{color}" '
    'stroke-width="3.2" stroke-linecap="round" stroke-linejoin="round">'
    '<path d="M5 12.5l4.5 4.5L19 7"/></svg>'
)
_CHEVRON_DOWN = (
    '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none" stroke="{color}" '
    'stroke-width="2.4" stroke-linecap="round" stroke-linejoin="round">'
    '<path d="M6 9l6 6 6-6"/></svg>'
)
_CHEVRON_UP = (
    '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none" stroke="{color}" '
    'stroke-width="2.4" stroke-linecap="round" stroke-linejoin="round">'
    '<path d="M18 15l-6-6-6 6"/></svg>'
)

_SHAPES = {"check": _CHECK, "chevron-down": _CHEVRON_DOWN, "chevron-up": _CHEVRON_UP}


def _asset_dir() -> str:
    base = QStandardPaths.writableLocation(QStandardPaths.StandardLocation.CacheLocation)
    directory = os.path.join(base or tempfile.gettempdir(), "domain-atlas-assets")
    try:
        os.makedirs(directory, exist_ok=True)
    except OSError:  # pragma: no cover - read-only home
        directory = os.path.join(tempfile.gettempdir(), "domain-atlas-assets")
        os.makedirs(directory, exist_ok=True)
    return directory


def write_assets(theme_name: str, tick_color: str, arrow_color: str) -> Dict[str, str]:
    """Write this theme's glyphs and return ``{name: qss-safe path}``."""
    directory = _asset_dir()
    colors = {"check": tick_color, "chevron-down": arrow_color, "chevron-up": arrow_color}
    paths: Dict[str, str] = {}
    for name, template in _SHAPES.items():
        path = os.path.join(directory, f"{name}-{theme_name}.svg")
        content = template.format(color=colors[name])
        try:
            if not os.path.exists(path) or open(path, "r", encoding="utf-8").read() != content:
                with open(path, "w", encoding="utf-8") as handle:
                    handle.write(content)
        except OSError:  # pragma: no cover - defensive
            continue
        # Qt stylesheets want forward slashes, on Windows too.
        paths[name] = path.replace("\\", "/")
    return paths
