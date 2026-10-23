"""Application event filter: window-level key handling and the chrome of every QMenu.

Menu chrome is the registration marks at the corners of a menu plus a soft shadow. A QMenu is
its own window, so the marks (which sit half outside the frame) need a transparent margin
around it.

There is exactly one application-level filter, and it returns after a single type check for
everything except key presses and the Polish event of a menu: a Python filter on the
application runs for every Qt event, so it has to do as little as possible. Per-menu work
(show, hide, paint) is done by a second filter that is installed on each menu only.
"""
from __future__ import annotations
from typing import Callable, Optional

from PyQt6.QtCore import QCoreApplication, QEvent, QObject, QPoint, QRect, Qt
from PyQt6.QtGui import QPainter
from PyQt6.QtWidgets import QApplication, QMenu

from . import tokens as T
from .painting import draw_corner_marks, draw_menu_shadow

MARGIN = 8            # transparent ring around the menu frame
SHADOW_LAYERS = 5
ENABLED = True        # set False to fall back to plain QSS menus
MENU_QSS = ""         # the QMenu rules of the app stylesheet; set by theme.apply()

_KEY_PRESS = QEvent.Type.KeyPress
_POLISH = QEvent.Type.Polish
_SHOW = QEvent.Type.Show
_HIDE = QEvent.Type.Hide
_MOVE = QEvent.Type.Move
_PAINT = QEvent.Type.Paint

# Called as handler(obj, event) -> bool for every key press; True consumes the key.
_key_handler: Optional[Callable[[QObject, QEvent], bool]] = None


class _MenuFilter(QObject):
    """Installed on each QMenu: places it, and paints the shadow and the corner marks."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self._painting = False

    def eventFilter(self, obj, event):
        kind = event.type()
        if kind == _PAINT:
            if self._painting or not obj.property("chrome"):
                return False
            frame = obj.rect().adjusted(MARGIN, MARGIN, -MARGIN, -MARGIN)
            painter = QPainter(obj)
            draw_menu_shadow(painter, frame, SHADOW_LAYERS)
            painter.end()
            self._painting = True
            try:
                QCoreApplication.sendEvent(obj, event)
            finally:
                self._painting = False
            painter = QPainter(obj)
            draw_corner_marks(painter, QRect(frame), T.CORNER_MARK)
            painter.end()
            return True
        if kind == _MOVE or kind == _SHOW:
            # Keep the frame where Qt placed the menu; only the transparent ring extends outwards.
            # Done on the Move that places the menu while it is still hidden. Moving it from the
            # Show event instead (its native window already exists but still has the 100x30 size
            # it was created with) gave that stale size back to the menu on X11, so menus opened
            # as an empty box. Show is the fallback for a popup Qt did not move.
            if obj.property("chrome") and not obj.property("chromeMoved") and (kind == _SHOW or not obj.isVisible()):
                obj.setProperty("chromeMoved", True)
                obj.move(obj.pos() - QPoint(MARGIN, MARGIN))
        elif kind == _HIDE:
            obj.setProperty("chromeMoved", False)
        return False


class _AppFilter(QObject):
    """The only application-level filter. Everything but KeyPress and Polish leaves at once."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.menu_filter = _MenuFilter()

    def eventFilter(self, obj, event):
        kind = event.type()
        if kind == _KEY_PRESS:
            handler = _key_handler
            return bool(handler is not None and handler(obj, event))
        if kind == _POLISH and ENABLED and isinstance(obj, QMenu) and not obj.property("chrome"):
            self.add_chrome(obj)
        return False

    def add_chrome(self, menu: QMenu) -> None:
        menu.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        menu.setWindowFlag(Qt.WindowType.FramelessWindowHint, True)
        menu.setWindowFlag(Qt.WindowType.NoDropShadowWindowHint, True)
        menu.setProperty("chrome", True)
        if MENU_QSS and not menu.styleSheet():
            menu.setStyleSheet(MENU_QSS)  # same look whatever the parent's stylesheet says
        menu.installEventFilter(self.menu_filter)


_app_filter: Optional[_AppFilter] = None


def set_key_handler(handler: Optional[Callable[[QObject, QEvent], bool]]) -> None:
    """Route every key press in the application to handler(obj, event) first."""
    global _key_handler
    _key_handler = handler


def install_menu_chrome(app: QApplication) -> None:
    """Install the application filter once (menu chrome and the key handler)."""
    global _app_filter
    if _app_filter is not None:
        return
    # Kept alive by this module, not parented to the application: a Python QObject owned by
    # QApplication crashes PyQt's cleanup when the interpreter exits.
    _app_filter = _AppFilter()
    app.installEventFilter(_app_filter)
    # Menus polished before the filter existed (e.g. by an earlier app.setStyleSheet) would keep
    # the margin ring without the transparency, which shows as a second border
    if ENABLED:
        for widget in app.allWidgets():
            if isinstance(widget, QMenu) and not widget.property("chrome") and not widget.isVisible():
                _app_filter.add_chrome(widget)
    app.aboutToQuit.connect(uninstall_menu_chrome)


def uninstall_menu_chrome() -> None:
    """Remove the filter. Must happen before the QApplication object is released: a Python
    event filter that is still installed then crashes PyQt's shutdown cleanup."""
    global _app_filter, _key_handler
    _key_handler = None
    app = QApplication.instance()
    if _app_filter is not None and app is not None:
        app.removeEventFilter(_app_filter)
    _app_filter = None
