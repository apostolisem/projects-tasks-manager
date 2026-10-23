"""Fonts, palette and stylesheet for the Project Notes theme."""
from __future__ import annotations
import glob
import os

from PyQt6.QtGui import QColor, QFont, QFontDatabase, QPalette
from PyQt6.QtWidgets import QApplication

from . import tokens as T

UI_DIR = os.path.abspath(os.path.dirname(__file__))
APP_DIR = os.path.dirname(UI_DIR)
FONTS_DIR = os.path.join(APP_DIR, "fonts")
QSS_PATH = os.path.join(UI_DIR, "theme.qss")

_fonts_loaded = False
_qss_cache = None


def load_fonts() -> list:
    """Register the bundled Barlow TTFs. Returns the font families that were added."""
    global _fonts_loaded
    families = []
    if _fonts_loaded:
        return families
    for path in sorted(glob.glob(os.path.join(FONTS_DIR, "*.ttf"))):
        font_id = QFontDatabase.addApplicationFont(path)
        if font_id < 0:
            print(f"Warning: could not load font {os.path.basename(path)}")
            continue
        families.extend(QFontDatabase.applicationFontFamilies(font_id))
    _fonts_loaded = True
    return families


def load_stylesheet() -> str:
    global _qss_cache
    if _qss_cache is None:
        try:
            with open(QSS_PATH, "r", encoding="utf-8") as fh:
                _qss_cache = fh.read()
        except OSError as exc:
            print(f"Warning: could not read theme.qss: {exc}")
            _qss_cache = ""
    return _qss_cache


def body_font(px: float = 14, weight: int = 400) -> QFont:
    font = QFont(T.FONT_BODY)
    font.setPixelSize(max(1, round(px)))
    font.setWeight(QFont.Weight(weight))
    return font


def heading_font(px: float = 14, letter_spacing_em: float = 0.0) -> QFont:
    """Barlow Condensed 600. letter_spacing_em follows the CSS em values in the handoff."""
    font = QFont(T.FONT_HEADING)
    font.setPixelSize(max(1, round(px)))
    font.setWeight(QFont.Weight.DemiBold)
    if letter_spacing_em:
        font.setLetterSpacing(QFont.SpacingType.AbsoluteSpacing, px * letter_spacing_em)
    return font


def build_palette() -> QPalette:
    pal = QPalette()
    for group in (QPalette.ColorGroup.Active, QPalette.ColorGroup.Inactive, QPalette.ColorGroup.Disabled):
        pal.setColor(group, QPalette.ColorRole.Window, QColor(T.BG))
        pal.setColor(group, QPalette.ColorRole.Base, QColor(T.BG))
        pal.setColor(group, QPalette.ColorRole.AlternateBase, QColor(T.SIDEBAR_BG))
        pal.setColor(group, QPalette.ColorRole.Button, QColor(T.BG))
        pal.setColor(group, QPalette.ColorRole.ToolTipBase, QColor(T.ACCENT_900))
        pal.setColor(group, QPalette.ColorRole.ToolTipText, QColor(T.BG))
        # Selection keeps its colour when the view loses focus
        pal.setColor(group, QPalette.ColorRole.Highlight, QColor(T.ACCENT_200))
        pal.setColor(group, QPalette.ColorRole.HighlightedText, QColor(T.ACCENT_900))
        pal.setColor(group, QPalette.ColorRole.Link, QColor(T.ACCENT_700))
        pal.setColor(group, QPalette.ColorRole.PlaceholderText, QColor(T.NEUTRAL_500))
    for group in (QPalette.ColorGroup.Active, QPalette.ColorGroup.Inactive):
        pal.setColor(group, QPalette.ColorRole.WindowText, QColor(T.TEXT))
        pal.setColor(group, QPalette.ColorRole.Text, QColor(T.TEXT))
        pal.setColor(group, QPalette.ColorRole.ButtonText, QColor(T.NEUTRAL_800))
    for role in (QPalette.ColorRole.WindowText, QPalette.ColorRole.Text, QPalette.ColorRole.ButtonText):
        pal.setColor(QPalette.ColorGroup.Disabled, role, QColor(T.NEUTRAL_500))
    return pal


def apply(app: QApplication = None, install_menus: bool = True) -> None:
    """Register fonts and apply the palette, base font and theme.qss to the application.

    install_menus adds the menu corner marks (an application event filter). The main window
    installs it and removes it again when it closes; start-up code passes False.
    """
    app = app or QApplication.instance()
    if app is None:
        return
    load_fonts()
    app.setStyle("Fusion")
    app.setPalette(build_palette())
    app.setFont(body_font(14))
    from . import menus
    qss = load_stylesheet()
    if menus.ENABLED:
        # Room for the registration marks and the shadow around every menu (see ui/menus.py)
        qss += f"\nQMenu{{margin:{menus.MARGIN}px}}\n"
    # Every menu also gets these rules as its own stylesheet, so a parent widget's stylesheet
    # (e.g. an unscoped "border: ...") can't add a second border or change its look
    menus.MENU_QSS = "\n".join(line for line in qss.splitlines() if line.startswith("QMenu") and not line.startswith("QMenuBar"))
    app.setStyleSheet(qss)
    if install_menus:
        menus.install_menu_chrome(app)
