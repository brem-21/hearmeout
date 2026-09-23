"""Hear Me Out's look: colour tokens for light and dark (following the system), the
stylesheet, and a small set of line icons drawn in the current theme's colours."""

from __future__ import annotations

from PySide6.QtCore import QByteArray, QRectF, Qt
from PySide6.QtGui import QColor, QGuiApplication, QIcon, QPainter, QPalette, QPixmap
from PySide6.QtSvg import QSvgRenderer
from PySide6.QtWidgets import QApplication

from . import config

LIGHT = {
    "bg": "#f7f7f9", "surface": "#ffffff", "sidebar": "#f1f1f5", "raised": "#fbfbfc",
    "border": "#e6e6ec", "border_strong": "#d5d5de", "hover": "#ebebf1", "selected": "#e4e4f8",
    "text": "#1b1b22", "muted": "#696978", "faint": "#9d9dab",
    "accent": "#5b5bd6", "accent_hover": "#4c4cc4", "accent_text": "#ffffff", "accent_soft": "#ededfc",
    "red": "#dc3e42", "red_hover": "#c9353a", "red_soft": "#fdecec",
    "green": "#218358", "green_soft": "#e5f5ec", "amber": "#ab6400", "amber_soft": "#fdf1d9",
    "me": "#5b5bd6", "them": "#218358", "highlight": "#fff3c4", "rec": "#dc3e42", "rec_hover": "#c9353a",
}
DARK = {
    "bg": "#141418", "surface": "#1c1c21", "sidebar": "#111114", "raised": "#222228",
    "border": "#2a2a31", "border_strong": "#3a3a44", "hover": "#24242b", "selected": "#28284a",
    "text": "#ededf2", "muted": "#a3a3b0", "faint": "#6f6f7c",
    "accent": "#8b8aff", "accent_hover": "#9e9dff", "accent_text": "#101016", "accent_soft": "#23234a",
    "red": "#ff6b6f", "red_hover": "#ff8386", "red_soft": "#3b1c1f",
    "green": "#4cc38a", "green_soft": "#15291f", "amber": "#f1a10d", "amber_soft": "#33260b",
    "me": "#a3a2ff", "them": "#4cc38a", "highlight": "#4a3b00", "rec": "#d93d42", "rec_hover": "#e5484d",
}

T: dict[str, str] = dict(LIGHT)  # the tokens in use right now
MODE = "system"                  # "system", "light" or "dark", as chosen in Settings


def is_dark() -> bool:
    if MODE in ("light", "dark"):
        return MODE == "dark"
    hints = QGuiApplication.styleHints()
    try:
        scheme = hints.colorScheme()
        if scheme == Qt.ColorScheme.Dark:
            return True
        if scheme == Qt.ColorScheme.Light:
            return False
    except AttributeError:
        pass
    return QGuiApplication.palette().color(QPalette.Window).lightness() < 128


# --------------------------------------------------------------------------- icons

_ICONS = {
    "mic": '<rect x="9" y="3" width="6" height="11" rx="3"/><path d="M5.5 11a6.5 6.5 0 0 0 13 0"/>'
           '<line x1="12" y1="17.5" x2="12" y2="21"/>',
    "record": '<circle cx="12" cy="12" r="6.5" fill="C" stroke="none"/>',
    "stop": '<rect x="6.5" y="6.5" width="11" height="11" rx="2.5" fill="C" stroke="none"/>',
    "play": '<path d="M8.5 5.8v12.4a.8.8 0 0 0 1.2.7l9.6-6.2a.8.8 0 0 0 0-1.4L9.7 5.1a.8.8 0 0 0-1.2.7z" '
            'fill="C" stroke="none"/>',
    "pause": '<rect x="6.5" y="5" width="4" height="14" rx="1.2" fill="C" stroke="none"/>'
             '<rect x="13.5" y="5" width="4" height="14" rx="1.2" fill="C" stroke="none"/>',
    "search": '<circle cx="11" cy="11" r="6.5"/><line x1="16" y1="16" x2="20.5" y2="20.5"/>',
    "settings": '<line x1="4" y1="7" x2="20" y2="7"/><line x1="4" y1="17" x2="20" y2="17"/>'
                '<circle cx="9" cy="7" r="2.4" fill="C"/><circle cx="15" cy="17" r="2.4" fill="C"/>',
    "trash": '<path d="M4.5 7h15"/><path d="M9.5 7V4.8h5V7"/><path d="M6.5 7l.9 12.2a1 1 0 0 0 1 .8h7.2'
             'a1 1 0 0 0 1-.8L17.5 7"/>',
    "folder": '<path d="M3.5 7.5a2 2 0 0 1 2-2h3.8l2 2h7.2a2 2 0 0 1 2 2v8a2 2 0 0 1-2 2h-13a2 2 0 0 1-2-2z"/>',
    "external": '<path d="M14 4h6v6"/><line x1="20" y1="4" x2="11" y2="13"/>'
                '<path d="M18 14v5a1 1 0 0 1-1 1H5a1 1 0 0 1-1-1V7a1 1 0 0 1 1-1h5"/>',
    "check": '<path d="M5 12.5l4.5 4.5L19 7.5"/>',
    "x": '<line x1="6.5" y1="6.5" x2="17.5" y2="17.5"/><line x1="17.5" y1="6.5" x2="6.5" y2="17.5"/>',
    "alert": '<path d="M10.3 4.6L2.9 17.5a2 2 0 0 0 1.7 3h14.8a2 2 0 0 0 1.7-3L13.7 4.6a2 2 0 0 0-3.4 0z"/>'
             '<line x1="12" y1="9.5" x2="12" y2="13.5"/><circle cx="12" cy="16.8" r=".6" fill="C"/>',
    "calendar": '<rect x="4" y="5.5" width="16" height="14.5" rx="2"/><line x1="4" y1="10" x2="20" y2="10"/>'
                '<line x1="9" y1="3.5" x2="9" y2="7"/><line x1="15" y1="3.5" x2="15" y2="7"/>',
    "clock": '<circle cx="12" cy="12" r="8.5"/><path d="M12 7.5V12l3 2"/>',
    "users": '<circle cx="9" cy="8.5" r="3.2"/><path d="M3.5 19.5a5.5 5.5 0 0 1 11 0"/>'
             '<path d="M16 5.6a3 3 0 0 1 0 5.8"/><path d="M17.5 14.3a5 5 0 0 1 3 5.2"/>',
    "sparkle": '<path d="M12 3.5l1.9 5.1 5.1 1.9-5.1 1.9L12 17.5l-1.9-5.1L5 10.5l5.1-1.9z"/>'
               '<path d="M18.5 16v4M16.5 18h4"/>',
    "tasks": '<path d="M4 7l1.6 1.6L8.5 5.7"/><line x1="11.5" y1="7" x2="20" y2="7"/>'
             '<path d="M4 16l1.6 1.6 2.9-2.9"/><line x1="11.5" y1="16" x2="20" y2="16"/>',
    "chat": '<path d="M5 5h14a1.5 1.5 0 0 1 1.5 1.5v9A1.5 1.5 0 0 1 19 17h-8.5L6 20.5V17H5a1.5 1.5 0 0 1'
            '-1.5-1.5v-9A1.5 1.5 0 0 1 5 5z"/>',
    "gem": '<path d="M7 4h10l3.5 5L12 20.5 3.5 9z"/><path d="M3.5 9h17"/><path d="M12 20.5L9 9l3-5 3 5z"/>',
    "headphones": '<path d="M4 17v-4a8 8 0 0 1 16 0v4"/><rect x="3.5" y="14" width="4" height="6" rx="1.5"/>'
                  '<rect x="16.5" y="14" width="4" height="6" rx="1.5"/>',
    "undo": '<path d="M9 6.5L5 10.5l4 4"/><path d="M5 10.5h9.5a5 5 0 0 1 0 10H12"/>',
    "refresh": '<path d="M20 11.5a8 8 0 1 0-2.3 5.6"/><path d="M20 5v6.5h-6.5"/>',
    "ear": '<path d="M7 9.5a5 5 0 0 1 10 0c0 3-2.5 4-3.2 5.8-.6 1.7-1.3 3.2-3.3 3.2a2.5 2.5 0 0 1-2.5-2.5"/>'
           '<path d="M10 9.5a2 2 0 0 1 4 0"/>',
    "key": '<circle cx="8" cy="15" r="4"/><path d="M11 12l8.5-8.5M16 7l2.5 2.5M14 9l2 2"/>',
    "eye": '<path d="M2.5 12S6 5.5 12 5.5 21.5 12 21.5 12 18 18.5 12 18.5 2.5 12 2.5 12z"/>'
           '<circle cx="12" cy="12" r="2.8"/>',
    "sun": '<circle cx="12" cy="12" r="4"/><path d="M12 2.5v2M12 19.5v2M4.6 4.6l1.4 1.4M18 18l1.4 1.4'
           'M2.5 12h2M19.5 12h2M4.6 19.4L6 18M18 6l1.4-1.4"/>',
    "moon": '<path d="M20 14.5A8 8 0 1 1 9.5 4a6.5 6.5 0 0 0 10.5 10.5z"/>',
    "monitor": '<rect x="3" y="4.5" width="18" height="12" rx="2"/><path d="M8.5 20h7M12 16.5V20"/>',
    "wave": '<line x1="4" y1="10" x2="4" y2="14"/><line x1="8" y1="7" x2="8" y2="17"/>'
            '<line x1="12" y1="4" x2="12" y2="20"/><line x1="16" y1="8" x2="16" y2="16"/>'
            '<line x1="20" y1="10.5" x2="20" y2="13.5"/>',
}


def icon(name: str, color: str | None = None, size: int = 18) -> QIcon:
    return QIcon(pixmap(name, color, size))


def pixmap(name: str, color: str | None = None, size: int = 18) -> QPixmap:
    c = color or T["text"]
    body = _ICONS[name].replace('"C"', f'"{c}"')
    svg = (f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none" stroke="{c}" '
           f'stroke-width="1.9" stroke-linecap="round" stroke-linejoin="round">{body}</svg>')
    ratio = 2.0
    pix = QPixmap(int(size * ratio), int(size * ratio))
    pix.fill(Qt.transparent)
    p = QPainter(pix)
    QSvgRenderer(QByteArray(svg.encode())).render(p, QRectF(0, 0, size * ratio, size * ratio))
    p.end()
    pix.setDevicePixelRatio(ratio)
    return pix


def _icon_file(name: str, color: str, size: int = 14) -> str:
    """An icon saved as a file, for stylesheet rules that need a url()."""
    folder = config.DATA_DIR / "theme"
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"{name}-{color.lstrip('#')}.png"
    if not path.exists():
        pixmap(name, color, size).toImage().save(str(path))
    return path.as_posix()


# --------------------------------------------------------------------------- stylesheet


def stylesheet(t: dict[str, str]) -> str:
    check = _icon_file("check", t["accent_text"])
    return f"""
* {{ outline: none; }}
QWidget {{ color: {t['text']}; }}
QMainWindow, QDialog {{ background: {t['bg']}; }}
QWidget#Content, QWidget#Page {{ background: {t['bg']}; }}
QWidget#Sidebar {{ background: {t['sidebar']}; border-right: 1px solid {t['border']}; }}
QScrollArea {{ background: transparent; border: none; }}
QScrollArea > QWidget > QWidget {{ background: transparent; }}

QLabel[role="brand"] {{ font-size: 15px; font-weight: 700; }}
QLabel[role="h1"] {{ font-size: 22px; font-weight: 700; }}
QLabel[role="h2"] {{ font-size: 15px; font-weight: 650; }}
QLabel[role="section"] {{ color: {t['faint']}; font-size: 11px; font-weight: 700; letter-spacing: 0.6px; }}
QLabel[role="muted"] {{ color: {t['muted']}; }}
QLabel[role="faint"] {{ color: {t['faint']}; }}
QLabel[role="mono"] {{ font-family: monospace; color: {t['muted']}; }}
QLabel[role="bigtime"] {{ font-size: 44px; font-weight: 300; font-family: monospace; }}
QLabel[role="quote"] {{ color: {t['muted']}; font-style: italic; }}

QPushButton {{ background: {t['surface']}; border: 1px solid {t['border_strong']}; border-radius: 8px;
               padding: 6px 12px; font-weight: 550; }}
QPushButton:hover {{ background: {t['hover']}; }}
QPushButton:pressed {{ background: {t['border']}; }}
QPushButton:disabled {{ color: {t['faint']}; border-color: {t['border']}; }}
QPushButton[variant="primary"] {{ background: {t['accent']}; color: {t['accent_text']}; border: 1px solid {t['accent']}; }}
QPushButton[variant="primary"]:hover {{ background: {t['accent_hover']}; border-color: {t['accent_hover']}; }}
QPushButton[variant="primary"]:disabled {{ background: {t['border']}; border-color: {t['border']}; color: {t['faint']}; }}
QPushButton[variant="record"] {{ background: {t['rec']}; color: white; border: 1px solid {t['rec']};
                                 padding: 8px 14px; font-weight: 650; }}
QPushButton[variant="record"]:hover {{ background: {t['rec_hover']}; border-color: {t['rec_hover']}; }}
QPushButton[variant="ghost"] {{ background: transparent; border: 1px solid transparent; }}
QPushButton[variant="ghost"]:hover {{ background: {t['hover']}; }}
QPushButton[variant="danger"] {{ color: {t['red']}; }}
QPushButton[variant="link"] {{ background: transparent; border: none; color: {t['accent']}; padding: 0; font-weight: 550; }}
QPushButton[variant="link"]:hover {{ text-decoration: underline; }}
QPushButton[variant="tab"] {{ background: transparent; border: none; border-bottom: 2px solid transparent;
                              border-radius: 0; padding: 8px 4px; margin-right: 16px; color: {t['muted']}; }}
QPushButton[variant="tab"]:hover {{ color: {t['text']}; }}
QPushButton[variant="tab"]:checked {{ color: {t['text']}; border-bottom: 2px solid {t['accent']}; }}
QPushButton[variant="chip"] {{ border-radius: 13px; padding: 5px 12px; background: {t['surface']};
                               color: {t['muted']}; font-weight: 550; }}
QPushButton[variant="chip"]:checked {{ background: {t['accent_soft']}; border-color: {t['accent']}; color: {t['accent']}; }}
QPushButton[variant="round"] {{ background: {t['accent']}; border: none; border-radius: 17px;
                                min-width: 34px; max-width: 34px; min-height: 34px; max-height: 34px; padding: 0; }}
QPushButton[variant="round"]:hover {{ background: {t['accent_hover']}; }}

QLineEdit, QSpinBox, QComboBox, QPlainTextEdit {{ background: {t['surface']}; border: 1px solid {t['border_strong']};
    border-radius: 8px; padding: 6px 10px; selection-background-color: {t['accent']};
    selection-color: {t['accent_text']}; }}
QLineEdit:focus, QSpinBox:focus, QComboBox:focus {{ border: 1px solid {t['accent']}; }}
QLineEdit[role="title"] {{ font-size: 22px; font-weight: 700; border: 1px solid transparent;
                           background: transparent; padding: 2px 6px; margin-left: -7px; }}
QLineEdit[role="title"]:hover {{ border: 1px solid {t['border_strong']}; }}
QLineEdit[role="title"]:focus {{ border: 1px solid {t['accent']}; background: {t['surface']}; }}
QLineEdit[role="search"] {{ padding-left: 30px; background: {t['surface']}; }}
QComboBox::drop-down {{ border: none; width: 22px; }}
QComboBox QAbstractItemView {{ background: {t['surface']}; border: 1px solid {t['border']};
    selection-background-color: {t['selected']}; selection-color: {t['text']}; }}

QCheckBox, QRadioButton {{ spacing: 8px; }}
QCheckBox::indicator {{ width: 16px; height: 16px; border-radius: 5px; border: 1.5px solid {t['border_strong']};
                        background: {t['surface']}; }}
QCheckBox::indicator:hover {{ border-color: {t['accent']}; }}
QCheckBox::indicator:checked {{ background: {t['accent']}; border-color: {t['accent']}; image: url({check}); }}
QRadioButton::indicator {{ width: 14px; height: 14px; border-radius: 8px; border: 2px solid {t['border_strong']};
                           background: {t['surface']}; }}
QRadioButton::indicator:hover {{ border-color: {t['accent']}; }}
QRadioButton::indicator:checked {{ width: 8px; height: 8px; border: 5px solid {t['accent']}; border-radius: 9px;
                                   background: {t['surface']}; }}

QListWidget#Meetings {{ background: transparent; border: none; }}
QListWidget#Meetings::item {{ border-radius: 8px; margin: 1px 8px; border: none; color: {t['text']}; }}
QListWidget#Meetings::item:hover {{ background: {t['hover']}; }}
QListWidget#Meetings::item:selected {{ background: {t['selected']}; }}

QFrame[card="true"] {{ background: {t['surface']}; border: 1px solid {t['border']}; border-radius: 12px; }}
QFrame[card="true"][dim="true"] {{ background: {t['raised']}; }}
QFrame[tone="info"] {{ background: {t['accent_soft']}; border-radius: 10px; }}
QFrame[tone="ok"] {{ background: {t['green_soft']}; border-radius: 10px; }}
QFrame[tone="warn"] {{ background: {t['amber_soft']}; border-radius: 10px; }}
QFrame[tone="bad"] {{ background: {t['red_soft']}; border-radius: 10px; }}
QFrame#RecBanner {{ background: {t['red_soft']}; border-bottom: 1px solid {t['border']}; }}
QFrame[role="divider"] {{ background: {t['border']}; max-height: 1px; min-height: 1px; border: none; }}

QLabel[badge] {{ border-radius: 9px; padding: 1px 8px; font-size: 11px; font-weight: 650; }}
QLabel[badge="rec"] {{ background: {t['red_soft']}; color: {t['red']}; }}
QLabel[badge="busy"] {{ background: {t['amber_soft']}; color: {t['amber']}; }}
QLabel[badge="ready"] {{ background: {t['accent_soft']}; color: {t['accent']}; }}
QLabel[badge="bad"] {{ background: {t['red_soft']}; color: {t['red']}; }}
QLabel[badge="ok"] {{ background: {t['green_soft']}; color: {t['green']}; }}
QLabel[badge="plain"] {{ background: {t['hover']}; color: {t['muted']}; }}
QFrame[chipbox] {{ border-radius: 10px; background: {t['hover']}; }}
QFrame[chipbox="bad"] {{ background: {t['red_soft']}; }}
QFrame[chipbox="warn"] {{ background: {t['amber_soft']}; }}

QProgressBar {{ background: {t['hover']}; border: none; border-radius: 4px; max-height: 8px; min-height: 8px; }}
QProgressBar::chunk {{ background: {t['accent']}; border-radius: 4px; }}
QProgressBar[role="meter"] {{ max-height: 6px; min-height: 6px; border-radius: 3px; }}
QProgressBar[role="meter"]::chunk {{ background: {t['green']}; border-radius: 3px; }}

QSlider::groove:horizontal {{ height: 4px; background: {t['border']}; border-radius: 2px; }}
QSlider::sub-page:horizontal {{ background: {t['accent']}; border-radius: 2px; }}
QSlider::handle:horizontal {{ background: {t['surface']}; border: 2px solid {t['accent']}; width: 10px; height: 10px;
                              margin: -5px 0; border-radius: 7px; }}

QTextBrowser {{ background: transparent; border: none; }}
QScrollBar:vertical {{ background: transparent; width: 10px; margin: 2px; }}
QScrollBar::handle:vertical {{ background: {t['border_strong']}; border-radius: 3px; min-height: 30px; }}
QScrollBar::handle:vertical:hover {{ background: {t['faint']}; }}
QScrollBar::add-line, QScrollBar::sub-line {{ height: 0; width: 0; }}
QScrollBar::add-page, QScrollBar::sub-page {{ background: transparent; }}
QScrollBar:horizontal {{ height: 0; }}

QMenu {{ background: {t['surface']}; border: 1px solid {t['border']}; border-radius: 8px; padding: 4px; }}
QMenu::item {{ padding: 6px 22px 6px 12px; border-radius: 6px; }}
QMenu::item:selected {{ background: {t['selected']}; }}
QMenu::item:disabled {{ color: {t['faint']}; }}
QMenu::separator {{ height: 1px; background: {t['border']}; margin: 4px 6px; }}
QToolTip {{ background: {t['text']}; color: {t['bg']}; border: none; border-radius: 6px; padding: 5px 8px; }}
QSplitter::handle {{ background: {t['border']}; width: 1px; }}
"""


def apply(app: QApplication, mode: str | None = None) -> None:
    """Use Hear Me Out's theme: light, dark, or whatever the system uses."""
    global MODE
    if mode is not None:
        MODE = mode if mode in ("system", "light", "dark") else "system"
    T.clear()
    T.update(DARK if is_dark() else LIGHT)
    app.setStyle("Fusion")
    pal = QPalette()
    for role, key in ((QPalette.Window, "bg"), (QPalette.Base, "surface"), (QPalette.AlternateBase, "raised"),
                      (QPalette.Text, "text"), (QPalette.WindowText, "text"), (QPalette.ButtonText, "text"),
                      (QPalette.Button, "surface"), (QPalette.Highlight, "accent"),
                      (QPalette.HighlightedText, "accent_text"), (QPalette.PlaceholderText, "faint"),
                      (QPalette.ToolTipBase, "text"), (QPalette.ToolTipText, "bg"), (QPalette.Link, "accent")):
        pal.setColor(role, QColor(T[key]))
    app.setPalette(pal)
    app.setStyleSheet(stylesheet(T))


def follow_system(app: QApplication, on_change) -> None:
    """When set to "system", switch along with the desktop's light/dark setting."""
    def changed(*_):
        if MODE == "system":
            apply(app)
            on_change()
    try:
        QGuiApplication.styleHints().colorSchemeChanged.connect(changed)
    except AttributeError:
        pass


def repolish(widget) -> None:
    """Re-apply the stylesheet after changing a dynamic property."""
    widget.style().unpolish(widget)
    widget.style().polish(widget)
