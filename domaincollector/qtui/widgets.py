"""Reusable widgets for the Qt front-end."""

from __future__ import annotations

from typing import Optional

from PySide6.QtCore import QRectF, QSize, Qt
from PySide6.QtGui import QColor, QFont, QPainter, QPen
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QSizePolicy,
    QStyledItemDelegate,
    QVBoxLayout,
    QWidget,
)

from .icons import make_icon
from .theme import Palette


class StatCard(QFrame):
    """A large number with a caption - the dashboard's headline metric."""

    def __init__(self, label: str, hint: str = "", accent: Optional[str] = None, parent=None) -> None:
        super().__init__(parent)
        self.setObjectName("statCard")
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self._accent = accent

        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 14, 16, 14)
        layout.setSpacing(2)

        self.label = QLabel(label.upper())
        self.label.setObjectName("statLabel")
        self.value = QLabel("0")
        self.value.setObjectName("statValue")
        self.hint = QLabel(hint)
        self.hint.setObjectName("statHint")

        layout.addWidget(self.label)
        layout.addWidget(self.value)
        layout.addWidget(self.hint)

    def set_value(self, value: object) -> None:
        text = f"{value:,}" if isinstance(value, int) else str(value)
        if self.value.text() != text:
            self.value.setText(text)

    def set_hint(self, text: str) -> None:
        if self.hint.text() != text:
            self.hint.setText(text)

    def apply_accent(self, color: Optional[str]) -> None:
        self._accent = color
        if color:
            self.value.setStyleSheet(f"color: {color};")


class StatusPill(QLabel):
    """Coloured state badge: Stopped / Running / Paused / Stopping."""

    STATES = {
        "stopped": ("Stopped", "text_faint"),
        "running": ("Running", "success"),
        "paused": ("Paused", "warning"),
        "stopping": ("Stopping", "warning"),
        "error": ("Error", "danger"),
    }

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setObjectName("statusPill")
        self.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._state = "stopped"
        self._palette: Optional[Palette] = None
        self.set_state("stopped")

    def set_palette_colors(self, palette: Palette) -> None:
        self._palette = palette
        self.set_state(self._state)

    def set_state(self, state: str) -> None:
        self._state = state if state in self.STATES else "stopped"
        text, role = self.STATES[self._state]
        self.setText(f"  ●  {text}  ")
        if self._palette is None:
            return
        color = getattr(self._palette, role)
        tint = QColor(color)
        tint.setAlpha(38)
        self.setStyleSheet(
            f"color: {color}; background: rgba({tint.red()},{tint.green()},{tint.blue()},0.15);"
        )


class SearchBox(QLineEdit):
    """Line edit with a leading magnifier icon."""

    def __init__(self, placeholder: str, color: str, parent=None) -> None:
        super().__init__(parent)
        self.setObjectName("search")
        self.setPlaceholderText(placeholder)
        self.setClearButtonEnabled(True)
        self.addAction(make_icon("search", color, 16), QLineEdit.ActionPosition.LeadingPosition)


class Card(QFrame):
    """A titled surface panel."""

    def __init__(self, title: str = "", parent=None) -> None:
        super().__init__(parent)
        self.setObjectName("card")
        self.layout_ = QVBoxLayout(self)
        self.layout_.setContentsMargins(16, 14, 16, 16)
        self.layout_.setSpacing(10)
        self.header = QHBoxLayout()
        self.header.setSpacing(8)
        if title:
            label = QLabel(title)
            label.setObjectName("sectionTitle")
            self.header.addWidget(label)
        self.header.addStretch(1)
        self.layout_.addLayout(self.header)

    def add_widget(self, widget: QWidget, stretch: int = 0) -> None:
        self.layout_.addWidget(widget, stretch)

    def add_header_widget(self, widget: QWidget) -> None:
        self.header.addWidget(widget)


class NavButton(QPushButton):
    """Sidebar entry."""

    def __init__(self, text: str, icon_name: str, parent=None) -> None:
        super().__init__(text, parent)
        self.setObjectName("navButton")
        self.setCheckable(True)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.icon_name = icon_name
        self.setIconSize(QSize(17, 17))

    def retint(self, color: str) -> None:
        self.setIcon(make_icon(self.icon_name, color, 17))


class ShareBarDelegate(QStyledItemDelegate):
    """Draws the technology share column as a slim accent bar."""

    def __init__(self, share_role: int, color: str, track: str, parent=None) -> None:
        super().__init__(parent)
        self.share_role = share_role
        self.color = color
        self.track = track

    def set_colors(self, color: str, track: str) -> None:
        self.color = color
        self.track = track

    def paint(self, painter: QPainter, option, index) -> None:
        share = index.data(self.share_role)
        if share is None:
            super().paint(painter, option, index)
            return
        rect = option.rect.adjusted(8, 0, -8, 0)
        height = 6
        bar = QRectF(rect.left(), rect.center().y() - height / 2 + 1, rect.width(), height)
        painter.save()
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor(self.track))
        painter.drawRoundedRect(bar, 3, 3)
        filled = QRectF(bar)
        filled.setWidth(max(4.0, bar.width() * float(share)))
        painter.setBrush(QColor(self.color))
        painter.drawRoundedRect(filled, 3, 3)
        painter.restore()

    def sizeHint(self, option, index) -> QSize:
        size = super().sizeHint(option, index)
        return QSize(max(size.width(), 90), size.height())


class StatusDotDelegate(QStyledItemDelegate):
    """Live/Down column: a coloured dot plus the label."""

    def __init__(self, responsive_role: int, good: str, bad: str, parent=None) -> None:
        super().__init__(parent)
        self.responsive_role = responsive_role
        self.good = good
        self.bad = bad

    def set_colors(self, good: str, bad: str) -> None:
        self.good = good
        self.bad = bad

    def paint(self, painter: QPainter, option, index) -> None:
        responsive = index.data(self.responsive_role)
        if responsive is None:
            super().paint(painter, option, index)
            return
        painter.save()
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        color = QColor(self.good if responsive else self.bad)
        rect = option.rect
        dot_x = rect.left() + 12
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(color)
        painter.drawEllipse(QRectF(dot_x, rect.center().y() - 3, 6, 6))
        painter.setPen(QPen(color))
        font = QFont(painter.font())
        font.setPointSizeF(max(8.0, font.pointSizeF() - 0.5))
        painter.setFont(font)
        text_rect = rect.adjusted(int(dot_x - rect.left()) + 12, 0, -6, 0)
        painter.drawText(text_rect, int(Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft),
                         index.data(Qt.ItemDataRole.DisplayRole) or "")
        painter.restore()
