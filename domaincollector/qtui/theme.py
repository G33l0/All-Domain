"""Fluent-flavoured light/dark theming for the Qt front-end.

Colours follow the Windows 11 (Fluent 2) palette so the app looks native on
Windows while still being perfectly at home on Linux and macOS.  The whole UI
is styled from one generated stylesheet, so switching theme is a single call.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor, QGuiApplication, QPalette


@dataclass(frozen=True)
class Palette:
    """Every colour the UI uses, named by role rather than by shade."""

    name: str
    window: str
    surface: str
    surface_alt: str
    surface_hover: str
    border: str
    border_strong: str
    text: str
    text_muted: str
    text_faint: str
    accent: str
    accent_hover: str
    accent_pressed: str
    accent_text: str
    success: str
    warning: str
    danger: str
    info: str
    shadow: str
    selection: str

    def as_dict(self) -> Dict[str, str]:
        return {field: getattr(self, field) for field in self.__dataclass_fields__}


LIGHT = Palette(
    name="light",
    window="#f3f3f3",
    surface="#ffffff",
    surface_alt="#fafafa",
    surface_hover="#f0f0f0",
    border="#e5e5e5",
    border_strong="#d0d0d0",
    text="#1a1a1a",
    text_muted="#5c5c5c",
    text_faint="#8a8a8a",
    accent="#0067c0",
    accent_hover="#0078d4",
    accent_pressed="#005ba1",
    accent_text="#ffffff",
    success="#0f7b0f",
    warning="#9d5d00",
    danger="#c42b1c",
    info="#005a9e",
    shadow="rgba(0, 0, 0, 0.08)",
    selection="#cce4f7",
)

DARK = Palette(
    name="dark",
    window="#202020",
    surface="#2b2b2b",
    surface_alt="#272727",
    surface_hover="#323232",
    border="#3d3d3d",
    border_strong="#4a4a4a",
    text="#ffffff",
    text_muted="#c8c8c8",
    text_faint="#9a9a9a",
    accent="#4cc2ff",
    accent_hover="#60cdff",
    accent_pressed="#3aa0d8",
    accent_text="#00243d",
    success="#6ccb5f",
    warning="#fce100",
    danger="#ff99a4",
    info="#60cdff",
    shadow="rgba(0, 0, 0, 0.35)",
    selection="#2d4d63",
)

#: Windows ships Segoe UI Variable (11) / Segoe UI (10); the rest are fallbacks.
UI_FONT_STACK = '"Segoe UI Variable Display", "Segoe UI", "Inter", "Noto Sans", sans-serif'
MONO_FONT_STACK = '"Cascadia Mono", "Consolas", "JetBrains Mono", "DejaVu Sans Mono", monospace'


def system_is_dark() -> bool:
    """True when the OS asks for dark mode (Qt 6.5+ reports this natively)."""
    hints = QGuiApplication.styleHints()
    scheme = getattr(hints, "colorScheme", None)
    if scheme is not None:
        try:
            return scheme() == Qt.ColorScheme.Dark
        except Exception:  # pragma: no cover - very old Qt
            pass
    # Fall back to "is the default window colour dark?"
    window = QGuiApplication.palette().color(QPalette.ColorRole.Window)
    return window.lightness() < 128


def palette_for(theme: str) -> Palette:
    """``"light"``, ``"dark"`` or ``"system"``."""
    if theme == "dark":
        return DARK
    if theme == "light":
        return LIGHT
    return DARK if system_is_dark() else LIGHT


def apply_qpalette(app, palette: Palette) -> None:
    """Keep native widgets (tooltips, menus, scrollbars) in step with the theme."""
    qpalette = QPalette()
    qpalette.setColor(QPalette.ColorRole.Window, QColor(palette.window))
    qpalette.setColor(QPalette.ColorRole.WindowText, QColor(palette.text))
    qpalette.setColor(QPalette.ColorRole.Base, QColor(palette.surface))
    qpalette.setColor(QPalette.ColorRole.AlternateBase, QColor(palette.surface_alt))
    qpalette.setColor(QPalette.ColorRole.Text, QColor(palette.text))
    qpalette.setColor(QPalette.ColorRole.Button, QColor(palette.surface))
    qpalette.setColor(QPalette.ColorRole.ButtonText, QColor(palette.text))
    qpalette.setColor(QPalette.ColorRole.Highlight, QColor(palette.accent))
    qpalette.setColor(QPalette.ColorRole.HighlightedText, QColor(palette.accent_text))
    qpalette.setColor(QPalette.ColorRole.ToolTipBase, QColor(palette.surface))
    qpalette.setColor(QPalette.ColorRole.ToolTipText, QColor(palette.text))
    qpalette.setColor(QPalette.ColorRole.PlaceholderText, QColor(palette.text_faint))
    app.setPalette(qpalette)


def stylesheet(palette: Palette) -> str:
    """The whole application stylesheet for one palette."""
    from .assets import write_assets

    colors = palette.as_dict()
    colors["font"] = UI_FONT_STACK
    colors["mono"] = MONO_FONT_STACK
    assets = write_assets(palette.name, palette.accent_text, palette.text_muted)
    # A missing asset degrades to "no glyph" rather than breaking the sheet.
    colors["check_icon"] = assets.get("check", "")
    colors["chevron_down"] = assets.get("chevron-down", "")
    colors["chevron_up"] = assets.get("chevron-up", "")
    return _TEMPLATE.format(**colors)


_TEMPLATE = """
* {{
    font-family: {font};
    font-size: 13px;
    color: {text};
}}
QWidget#root, QMainWindow {{
    background: {window};
}}
QWidget#sidebar {{
    background: {surface_alt};
    border-right: 1px solid {border};
}}
QLabel#appTitle {{
    font-size: 15px;
    font-weight: 600;
    color: {text};
}}
QLabel#appSubtitle {{
    font-size: 11px;
    color: {text_faint};
}}
QLabel#pageTitle {{
    font-size: 22px;
    font-weight: 600;
}}
QLabel#pageSubtitle {{
    font-size: 12px;
    color: {text_muted};
}}
QLabel#sectionTitle {{
    font-size: 13px;
    font-weight: 600;
    color: {text_muted};
}}

/* ---- sidebar navigation -------------------------------------------- */
QPushButton#navButton {{
    background: transparent;
    border: none;
    border-radius: 6px;
    padding: 9px 12px;
    text-align: left;
    font-size: 13px;
    color: {text_muted};
}}
QPushButton#navButton:hover {{
    background: {surface_hover};
    color: {text};
}}
QPushButton#navButton:checked {{
    background: {surface};
    color: {text};
    font-weight: 600;
}}

/* ---- cards ---------------------------------------------------------- */
QFrame#card {{
    background: {surface};
    border: 1px solid {border};
    border-radius: 8px;
}}
QFrame#statCard {{
    background: {surface};
    border: 1px solid {border};
    border-radius: 8px;
}}
QLabel#statValue {{
    font-size: 26px;
    font-weight: 600;
    color: {text};
}}
QLabel#statLabel {{
    font-size: 11px;
    font-weight: 600;
    color: {text_faint};
    letter-spacing: 0.6px;
}}
QLabel#statHint {{
    font-size: 11px;
    color: {text_muted};
}}

/* ---- buttons -------------------------------------------------------- */
QPushButton {{
    background: {surface};
    border: 1px solid {border_strong};
    border-radius: 6px;
    padding: 7px 16px;
    color: {text};
}}
QPushButton:hover {{ background: {surface_hover}; }}
QPushButton:pressed {{ background: {border}; }}
QPushButton:disabled {{ color: {text_faint}; border-color: {border}; }}
QPushButton#primary {{
    background: {accent};
    border: 1px solid {accent};
    color: {accent_text};
    font-weight: 600;
}}
QPushButton#primary:hover {{ background: {accent_hover}; border-color: {accent_hover}; }}
QPushButton#primary:pressed {{ background: {accent_pressed}; }}
QPushButton#primary:disabled {{
    background: {border};
    border-color: {border};
    color: {text_faint};
}}
QPushButton#danger:hover {{
    background: {danger};
    border-color: {danger};
    color: #ffffff;
}}

/* ---- status pill ---------------------------------------------------- */
QLabel#statusPill {{
    border-radius: 11px;
    padding: 3px 12px;
    font-size: 12px;
    font-weight: 600;
}}

/* ---- inputs --------------------------------------------------------- */
QLineEdit, QSpinBox, QDoubleSpinBox, QComboBox {{
    background: {surface};
    border: 1px solid {border_strong};
    border-radius: 6px;
    padding: 6px 10px;
    selection-background-color: {accent};
    selection-color: {accent_text};
}}
QLineEdit:focus, QSpinBox:focus, QDoubleSpinBox:focus, QComboBox:focus {{
    border: 1px solid {accent};
}}
QLineEdit#search {{
    padding-left: 12px;
}}
QCheckBox {{ spacing: 8px; padding: 3px 0; }}
QCheckBox::indicator {{
    width: 17px; height: 17px;
    border: 1px solid {border_strong};
    border-radius: 4px;
    background: {surface};
}}
QCheckBox::indicator:checked {{
    background: {accent};
    border-color: {accent};
    image: url("{check_icon}");
}}
QCheckBox::indicator:hover {{ border-color: {accent}; }}
QComboBox::drop-down {{ border: none; width: 22px; }}
QComboBox::down-arrow {{ image: url("{chevron_down}"); width: 13px; height: 13px; }}
QSpinBox::up-button, QDoubleSpinBox::up-button {{
    subcontrol-origin: border;
    subcontrol-position: top right;
    width: 20px;
    border: none;
    border-top-right-radius: 6px;
    background: transparent;
}}
QSpinBox::down-button, QDoubleSpinBox::down-button {{
    subcontrol-origin: border;
    subcontrol-position: bottom right;
    width: 20px;
    border: none;
    border-bottom-right-radius: 6px;
    background: transparent;
}}
QSpinBox::up-button:hover, QDoubleSpinBox::up-button:hover,
QSpinBox::down-button:hover, QDoubleSpinBox::down-button:hover {{
    background: {surface_hover};
}}
QSpinBox::up-arrow, QDoubleSpinBox::up-arrow {{
    image: url("{chevron_up}"); width: 11px; height: 11px;
}}
QSpinBox::down-arrow, QDoubleSpinBox::down-arrow {{
    image: url("{chevron_down}"); width: 11px; height: 11px;
}}
QComboBox QAbstractItemView {{
    background: {surface};
    border: 1px solid {border};
    selection-background-color: {selection};
    outline: none;
}}

/* ---- tables --------------------------------------------------------- */
QTableView {{
    background: {surface};
    alternate-background-color: {surface_alt};
    border: 1px solid {border};
    border-radius: 8px;
    gridline-color: transparent;
    selection-background-color: {selection};
    selection-color: {text};
    outline: none;
}}
QTableView::item {{ padding: 5px 8px; border: none; }}
QHeaderView::section {{
    background: {surface_alt};
    color: {text_muted};
    border: none;
    border-bottom: 1px solid {border};
    padding: 8px;
    font-weight: 600;
    font-size: 11px;
}}
QTableCornerButton::section {{ background: {surface_alt}; border: none; }}

/* ---- log ------------------------------------------------------------ */
QPlainTextEdit#log {{
    background: {surface_alt};
    border: 1px solid {border};
    border-radius: 8px;
    font-family: {mono};
    font-size: 12px;
    padding: 8px;
    selection-background-color: {accent};
    selection-color: {accent_text};
}}

/* ---- scrollbars ----------------------------------------------------- */
QScrollBar:vertical {{ background: transparent; width: 10px; margin: 2px; }}
QScrollBar::handle:vertical {{
    background: {border_strong};
    border-radius: 5px;
    min-height: 28px;
}}
QScrollBar::handle:vertical:hover {{ background: {text_faint}; }}
QScrollBar:horizontal {{ background: transparent; height: 10px; margin: 2px; }}
QScrollBar::handle:horizontal {{
    background: {border_strong};
    border-radius: 5px;
    min-width: 28px;
}}
QScrollBar::add-line, QScrollBar::sub-line {{ height: 0; width: 0; }}
QScrollBar::add-page, QScrollBar::sub-page {{ background: transparent; }}

/* ---- misc ----------------------------------------------------------- */
QProgressBar {{
    background: {surface_hover};
    border: none;
    border-radius: 3px;
    height: 6px;
    text-align: center;
}}
QProgressBar::chunk {{ background: {accent}; border-radius: 3px; }}
QToolTip {{
    background: {surface};
    color: {text};
    border: 1px solid {border};
    border-radius: 4px;
    padding: 5px 8px;
}}
QStatusBar {{ background: {surface_alt}; border-top: 1px solid {border}; color: {text_muted}; }}
QStatusBar::item {{ border: none; }}
QGroupBox {{
    border: 1px solid {border};
    border-radius: 8px;
    background: {surface};
    margin-top: 14px;
    padding: 14px;
    font-weight: 600;
}}
QGroupBox::title {{
    subcontrol-origin: margin;
    left: 12px;
    padding: 0 4px;
    color: {text_muted};
}}
QMenu {{
    background: {surface};
    border: 1px solid {border};
    border-radius: 6px;
    padding: 4px;
}}
QMenu::item {{ padding: 6px 24px 6px 12px; border-radius: 4px; }}
QMenu::item:selected {{ background: {surface_hover}; }}
QSplitter::handle {{ background: transparent; }}
"""
