"""Domain Atlas brand mark.

Drawn with QPainter so it is sharp at any size and available in frozen builds.
``packaging/make_icons.py`` emits the same geometry as SVG and .ico. Detail is
reduced below 48px to keep the mark legible at 16px.
"""

from __future__ import annotations

import math

from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import (
    QBrush,
    QColor,
    QIcon,
    QLinearGradient,
    QPainter,
    QPainterPath,
    QPen,
    QPixmap,
)

#: Brand colours. Independent of the UI theme.
BRAND_START = "#1f6feb"
BRAND_END = "#00c2ff"
BRAND_INK = "#ffffff"
BRAND_NODE = "#c6f6ff"

ICON_SIZES = (16, 24, 32, 48, 64, 128, 256)

#: Sweep geometry, shared by the painter and the SVG so they cannot drift.
SWEEP_START_DEG = 52.0
SWEEP_SPAN_DEG = 88.0
BLIP_DEG = 150.0


def draw_logo(painter: QPainter, size: float, tile: bool = True) -> None:
    """Paint the mark into a *size* x *size* box at the painter's origin."""
    painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
    detail = "full" if size >= 48 else ("medium" if size >= 28 else "small")

    if tile:
        gradient = QLinearGradient(0, 0, size, size)
        gradient.setColorAt(0.0, QColor(BRAND_START))
        gradient.setColorAt(1.0, QColor(BRAND_END))
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QBrush(gradient))
        painter.drawRoundedRect(QRectF(0, 0, size, size), size * 0.22, size * 0.22)
        if detail != "small":
            sheen = QLinearGradient(0, 0, 0, size * 0.7)
            sheen.setColorAt(0.0, QColor(255, 255, 255, 40))
            sheen.setColorAt(1.0, QColor(255, 255, 255, 0))
            painter.setBrush(QBrush(sheen))
            painter.drawRoundedRect(QRectF(0, 0, size, size), size * 0.22, size * 0.22)

    center = QPointF(size / 2, size / 2)
    radius = size * (0.30 if detail == "small" else 0.315)
    stroke = size * (0.105 if detail == "small" else (0.078 if detail == "medium" else 0.058))
    scope = QRectF(center.x() - radius, center.y() - radius, radius * 2, radius * 2)

    # Sweep quadrant, offset from vertical so the mark does not read as a
    # power button.
    if detail != "small":
        painter.setPen(Qt.PenStyle.NoPen)
        wedge = QPainterPath()
        wedge.moveTo(center)
        wedge.arcTo(scope, SWEEP_START_DEG, -SWEEP_SPAN_DEG)
        wedge.closeSubpath()
        painter.setBrush(QColor(255, 255, 255, 66 if detail == "full" else 88))
        painter.drawPath(wedge)

    pen = QPen(QColor(BRAND_INK))
    pen.setWidthF(stroke)
    pen.setCapStyle(Qt.PenCapStyle.RoundCap)
    painter.setPen(pen)
    painter.setBrush(Qt.BrushStyle.NoBrush)
    painter.drawEllipse(scope)

    # Leading edge of the sweep.
    angle = math.radians(SWEEP_START_DEG)
    painter.drawLine(
        center,
        QPointF(center.x() + radius * math.cos(angle), center.y() - radius * math.sin(angle)),
    )

    if detail == "full":
        # Range ring.
        inner_pen = QPen(QColor(255, 255, 255, 190))
        inner_pen.setWidthF(stroke * 0.72)
        painter.setPen(inner_pen)
        inner = radius * 0.52
        painter.drawEllipse(QRectF(center.x() - inner, center.y() - inner, inner * 2, inner * 2))

    # Centre of the scope.
    painter.setPen(Qt.PenStyle.NoPen)
    painter.setBrush(QColor(BRAND_INK))
    painter.drawEllipse(center, size * 0.052, size * 0.052)

    if detail == "full":
        # A contact: one discovered host, opposite the sweep so nothing collides.
        blip = math.radians(BLIP_DEG)
        node = size * 0.05
        painter.setBrush(QColor(BRAND_NODE))
        painter.drawEllipse(
            QPointF(center.x() + radius * 0.62 * math.cos(blip),
                    center.y() - radius * 0.62 * math.sin(blip)),
            node, node,
        )


def logo_pixmap(size: int, ratio: float = 1.0, tile: bool = True) -> QPixmap:
    """The mark as a pixmap, honouring a device pixel ratio."""
    pixmap = QPixmap(int(size * ratio), int(size * ratio))
    pixmap.setDevicePixelRatio(ratio)
    pixmap.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pixmap)
    # Logical units: the ratio is carried by the pixmap, not the geometry.
    draw_logo(painter, float(size), tile=tile)
    painter.end()
    return pixmap


def logo_icon() -> QIcon:
    """Window / taskbar / tray icon at every size Windows asks for."""
    icon = QIcon()
    for size in ICON_SIZES:
        icon.addPixmap(logo_pixmap(size))
    return icon


def logo_svg(size: int = 256) -> str:
    """The same mark as standalone SVG (used for the README and packaging)."""
    s = float(size)
    c = s / 2
    r = s * 0.315
    stroke = s * 0.058
    start = math.radians(SWEEP_START_DEG)
    stop = math.radians(SWEEP_START_DEG - SWEEP_SPAN_DEG)
    blip = math.radians(BLIP_DEG)
    x1, y1 = c + r * math.cos(start), c - r * math.sin(start)
    x2, y2 = c + r * math.cos(stop), c - r * math.sin(stop)
    bx, by = c + r * 0.62 * math.cos(blip), c - r * 0.62 * math.sin(blip)
    return f"""<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {size} {size}" width="{size}" height="{size}">
  <defs>
    <linearGradient id="tile" x1="0" y1="0" x2="1" y2="1">
      <stop offset="0" stop-color="{BRAND_START}"/>
      <stop offset="1" stop-color="{BRAND_END}"/>
    </linearGradient>
    <linearGradient id="sheen" x1="0" y1="0" x2="0" y2="1">
      <stop offset="0" stop-color="#ffffff" stop-opacity="0.16"/>
      <stop offset="0.7" stop-color="#ffffff" stop-opacity="0"/>
    </linearGradient>
  </defs>
  <rect width="{size}" height="{size}" rx="{s * 0.22:.1f}" fill="url(#tile)"/>
  <rect width="{size}" height="{size}" rx="{s * 0.22:.1f}" fill="url(#sheen)"/>
  <path d="M {c:.1f} {c:.1f} L {x1:.1f} {y1:.1f} A {r:.1f} {r:.1f} 0 0 1 {x2:.1f} {y2:.1f} Z"
        fill="#ffffff" fill-opacity="0.26"/>
  <g fill="none" stroke="{BRAND_INK}" stroke-width="{stroke:.1f}" stroke-linecap="round">
    <circle cx="{c:.1f}" cy="{c:.1f}" r="{r:.1f}"/>
    <path d="M {c:.1f} {c:.1f} L {x1:.1f} {y1:.1f}"/>
  </g>
  <circle cx="{c:.1f}" cy="{c:.1f}" r="{r * 0.52:.1f}" fill="none"
          stroke="#ffffff" stroke-opacity="0.75" stroke-width="{stroke * 0.72:.1f}"/>
  <circle cx="{c:.1f}" cy="{c:.1f}" r="{s * 0.052:.1f}" fill="{BRAND_INK}"/>
  <circle cx="{bx:.1f}" cy="{by:.1f}" r="{s * 0.05:.1f}" fill="{BRAND_NODE}"/>
</svg>
"""
