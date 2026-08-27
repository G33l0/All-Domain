"""Fluent-flavoured light/dark theming for the Qt front-end.

Colours follow the Windows 11 (Fluent 2) palette so the app looks native on
Windows while still being perfectly at home on Linux and macOS.  The whole UI
is styled from one generated stylesheet, so switching theme is a single call.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List, Tuple

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor, QGuiApplication, QPalette


@dataclass(frozen=True)
class Palette:
    """Colour, typography and geometry for one theme."""

    name: str
    label: str
    is_dark: bool
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
    font: str = ""
    mono: str = ""
    radius: int = 6
    card_radius: int = 8

    def as_dict(self) -> Dict[str, str]:
        values = {field: getattr(self, field) for field in self.__dataclass_fields__}
        values["font"] = self.font or UI_FONT_STACK
        values["mono"] = self.mono or MONO_FONT_STACK
        return values


#: Windows ships Segoe UI Variable (11) / Segoe UI (10); the rest are fallbacks.
UI_FONT_STACK = '"Segoe UI Variable Display", "Segoe UI", "Inter", "Noto Sans", sans-serif'
MONO_FONT_STACK = '"Cascadia Mono", "Consolas", "JetBrains Mono", "DejaVu Sans Mono", monospace'

MONO_FONT_STACK_LITERAL = MONO_FONT_STACK


LIGHT = Palette(
    name="light",
    label="Light",
    is_dark=False,
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
    label="Dark",
    is_dark=True,
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

#: Deep navy with an azure accent.
MIDNIGHT = Palette(
    name="midnight",
    label="Midnight",
    is_dark=True,
    window="#0d1117",
    surface="#151b23",
    surface_alt="#11171f",
    surface_hover="#1c2530",
    border="#232c38",
    border_strong="#313d4d",
    text="#e6edf3",
    text_muted="#9aa8b8",
    text_faint="#6b7a8d",
    accent="#4b91f7",
    accent_hover="#66a3f8",
    accent_pressed="#3a7ad4",
    accent_text="#04122a",
    success="#3fb950",
    warning="#d29922",
    danger="#f85149",
    info="#58a6ff",
    shadow="rgba(0, 0, 0, 0.5)",
    selection="#1f3a5f",
)

#: Violet accent on near-black slate.
AURORA = Palette(
    name="aurora",
    label="Aurora",
    is_dark=True,
    window="#12101c",
    surface="#1b1827",
    surface_alt="#171422",
    surface_hover="#241f33",
    border="#2c2740",
    border_strong="#3b3455",
    text="#ede9fb",
    text_muted="#b3a9d4",
    text_faint="#8579ad",
    accent="#a78bfa",
    accent_hover="#bda4ff",
    accent_pressed="#8b6ce0",
    accent_text="#1a1030",
    success="#4ade80",
    warning="#fbbf24",
    danger="#fb7185",
    info="#c084fc",
    shadow="rgba(0, 0, 0, 0.5)",
    selection="#38305a",
)

#: Warm charcoal with an amber accent.
AMBER = Palette(
    name="amber",
    label="Amber",
    is_dark=True,
    window="#17130e",
    surface="#211b14",
    surface_alt="#1c1710",
    surface_hover="#2c241a",
    border="#342a1e",
    border_strong="#48392a",
    text="#f5e9d7",
    text_muted="#c7ae8c",
    text_faint="#9a8465",
    accent="#ffb020",
    accent_hover="#ffc14d",
    accent_pressed="#d99414",
    accent_text="#2a1a00",
    success="#9ecb5f",
    warning="#ffd166",
    danger="#ff6b57",
    info="#ffc14d",
    shadow="rgba(0, 0, 0, 0.5)",
    selection="#4a3616",
)

#: Monospaced phosphor-green terminal theme.
HACKER = Palette(
    name="hacker",
    label="Hacker",
    is_dark=True,
    window="#000000",
    surface="#050a06",
    surface_alt="#020602",
    surface_hover="#0c1a0e",
    border="#123d1a",
    border_strong="#1c5c28",
    text="#33ff66",
    text_muted="#20c24a",
    text_faint="#158a33",
    accent="#00ff66",
    accent_hover="#66ff99",
    accent_pressed="#00cc52",
    accent_text="#001a08",
    success="#00ff66",
    warning="#e8ff3a",
    danger="#ff4b4b",
    info="#00e5ff",
    shadow="rgba(0, 255, 102, 0.16)",
    selection="#0f3d1c",
    font=MONO_FONT_STACK_LITERAL,
    radius=2,
    card_radius=2,
)

#: Every selectable theme, in the order they appear in the picker.
THEMES: "Dict[str, Palette]" = {
    palette.name: palette
    for palette in (LIGHT, DARK, MIDNIGHT, AURORA, AMBER, HACKER)
}


def theme_names() -> List[str]:
    """Theme keys accepted by :func:`palette_for` (excluding ``"system"``)."""
    return list(THEMES)


def theme_labels() -> List[Tuple[str, str]]:
    """``[(key, label)]`` for building a picker."""
    return [(key, palette.label) for key, palette in THEMES.items()]




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
    """Look up a theme by key; ``"system"`` follows the OS light/dark setting."""
    palette = THEMES.get((theme or "").strip().lower())
    if palette is not None:
        return palette
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
    colors["check_radius"] = max(1, palette.radius - 2)
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
    border-radius: {radius}px;
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
    border-radius: {card_radius}px;
}}
QFrame#statCard {{
    background: {surface};
    border: 1px solid {border};
    border-radius: {card_radius}px;
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
    border-radius: {radius}px;
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
    border-radius: {radius}px;
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
    border-radius: {check_radius}px;
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
    border-top-right-radius: {radius}px;
    background: transparent;
}}
QSpinBox::down-button, QDoubleSpinBox::down-button {{
    subcontrol-origin: border;
    subcontrol-position: bottom right;
    width: 20px;
    border: none;
    border-bottom-right-radius: {radius}px;
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
    border-radius: {card_radius}px;
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
    border-radius: {card_radius}px;
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
    border-radius: {card_radius}px;
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
