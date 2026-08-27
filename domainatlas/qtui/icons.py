"""Inline SVG icons, tinted to match the current theme.

Icons are drawn from stroke-based SVG source so they stay crisp at any DPI and
can be recoloured per theme without shipping asset files.
"""

from __future__ import annotations

from typing import Dict

from PySide6.QtCore import QByteArray, QRectF, QSize, Qt
from PySide6.QtGui import QColor, QIcon, QPainter, QPixmap
from PySide6.QtSvg import QSvgRenderer

_SVG: Dict[str, str] = {
    "dashboard": '<rect x="3" y="3" width="7" height="9" rx="1.5"/>'
                 '<rect x="14" y="3" width="7" height="5" rx="1.5"/>'
                 '<rect x="14" y="12" width="7" height="9" rx="1.5"/>'
                 '<rect x="3" y="16" width="7" height="5" rx="1.5"/>',
    "domains": '<circle cx="12" cy="12" r="9"/><path d="M3 12h18"/>'
               '<path d="M12 3a15 15 0 0 1 0 18a15 15 0 0 1 0-18z"/>',
    "tech": '<path d="M12 3l7 4v10l-7 4-7-4V7z"/><path d="M12 12l7-4M12 12v9M12 12L5 8"/>',
    "log": '<path d="M4 5h16M4 10h16M4 15h10M4 20h7"/>',
    "settings": '<circle cx="12" cy="12" r="3"/>'
                '<path d="M19.4 15a1.6 1.6 0 0 0 .3 1.8l.1.1a2 2 0 1 1-2.8 2.8l-.1-.1a1.6 1.6 0 0 0'
                '-1.8-.3 1.6 1.6 0 0 0-1 1.5V21a2 2 0 1 1-4 0v-.1A1.6 1.6 0 0 0 9 19.4a1.6 1.6 0 0 0'
                '-1.8.3l-.1.1a2 2 0 1 1-2.8-2.8l.1-.1a1.6 1.6 0 0 0 .3-1.8 1.6 1.6 0 0 0-1.5-1H3a2 2'
                ' 0 1 1 0-4h.1A1.6 1.6 0 0 0 4.6 9a1.6 1.6 0 0 0-.3-1.8l-.1-.1a2 2 0 1 1 2.8-2.8l.1.1'
                'a1.6 1.6 0 0 0 1.8.3H9a1.6 1.6 0 0 0 1-1.5V3a2 2 0 1 1 4 0v.1a1.6 1.6 0 0 0 1 1.5'
                ' 1.6 1.6 0 0 0 1.8-.3l.1-.1a2 2 0 1 1 2.8 2.8l-.1.1a1.6 1.6 0 0 0-.3 1.8V9a1.6 1.6'
                ' 0 0 0 1.5 1H21a2 2 0 1 1 0 4h-.1a1.6 1.6 0 0 0-1.5 1z"/>',
    "play": '<path d="M6 4l14 8-14 8z" fill="currentColor" stroke="none"/>',
    "pause": '<rect x="6" y="4" width="4" height="16" rx="1" fill="currentColor" stroke="none"/>'
             '<rect x="14" y="4" width="4" height="16" rx="1" fill="currentColor" stroke="none"/>',
    "stop": '<rect x="5" y="5" width="14" height="14" rx="2" fill="currentColor" stroke="none"/>',
    "export": '<path d="M12 3v12"/><path d="M8 11l4 4 4-4"/><path d="M4 17v2a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2v-2"/>',
    "refresh": '<path d="M20 11a8 8 0 1 0-2.3 5.7"/><path d="M20 4v7h-7"/>',
    "search": '<circle cx="11" cy="11" r="7"/><path d="M20 20l-4.2-4.2"/>',
    "globe": '<circle cx="12" cy="12" r="9"/><path d="M3 12h18"/>'
             '<path d="M12 3a15 15 0 0 1 0 18a15 15 0 0 1 0-18z"/>',
    "check": '<path d="M4 12.5l5 5L20 6.5"/>',
    "cross": '<path d="M6 6l12 12M18 6L6 18"/>',
    "clock": '<circle cx="12" cy="12" r="9"/><path d="M12 7v5l3.5 2"/>',
}

_TEMPLATE = (
    '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none" '
    'stroke="{color}" stroke-width="{width}" stroke-linecap="round" '
    'stroke-linejoin="round" color="{color}">{body}</svg>'
)


def icon_pixmap(name: str, color: str, size: int = 18, stroke: float = 1.8,
                ratio: float = 1.0) -> QPixmap:
    """Render one icon to a device-pixel-ratio aware pixmap."""
    body = _SVG.get(name)
    if body is None:
        return QPixmap()
    svg = _TEMPLATE.format(color=color, width=stroke, body=body)
    renderer = QSvgRenderer(QByteArray(svg.encode("utf-8")))
    pixmap = QPixmap(QSize(int(size * ratio), int(size * ratio)))
    pixmap.setDevicePixelRatio(ratio)
    pixmap.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
    # The painter addresses a device-pixel-ratio aware pixmap in logical
    # units. Scaling the rect by the ratio as well would draw the glyph
    # several times too large, leaving only its top-left corner visible.
    renderer.render(painter, QRectF(0, 0, size, size))
    painter.end()
    return pixmap


#: Device pixel ratios to rasterise for. 2.0 alone leaves a 200% display
#: upscaling a half-resolution bitmap, which visibly deforms thin strokes.
ICON_RATIOS = (1.0, 1.5, 2.0, 2.5, 3.0, 4.0)


def make_icon(name: str, color: str, size: int = 18, stroke: float = 1.8) -> QIcon:
    """A QIcon for *name*, tinted with *color*, sharp at every scale factor."""
    icon = QIcon()
    for ratio in ICON_RATIOS:
        icon.addPixmap(icon_pixmap(name, color, size, stroke, ratio))
    return icon


def app_icon(accent: str, background: str = "#0067c0") -> QIcon:
    """Window / tray icon: a globe badge in the accent colour."""
    icon = QIcon()
    for size in (16, 24, 32, 48, 64, 256):
        pixmap = QPixmap(size, size)
        pixmap.fill(Qt.GlobalColor.transparent)
        painter = QPainter(pixmap)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        painter.setBrush(QColor(background))
        painter.setPen(Qt.PenStyle.NoPen)
        radius = size * 0.22
        painter.drawRoundedRect(0, 0, size, size, radius, radius)
        painter.end()
        glyph = icon_pixmap("globe", accent, int(size * 0.62), stroke=1.7)
        painter = QPainter(pixmap)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        offset = int(size * 0.19)
        painter.drawPixmap(offset, offset, glyph)
        painter.end()
        icon.addPixmap(pixmap)
    return icon
