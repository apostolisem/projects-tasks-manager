"""Small themed widgets shared across the main window: top bar, search field, kbd hints."""
from __future__ import annotations
import os
import sys
from typing import Optional

from PyQt6.QtCore import QRect, QSize, Qt, pyqtSignal
from PyQt6.QtGui import QColor, QIcon, QPainter, QPixmap
from PyQt6.QtWidgets import (QFrame, QHBoxLayout, QLabel, QLineEdit, QMenu, QPushButton, QSizePolicy,
                             QToolButton, QWidget, QWidgetAction)

from . import tokens as T
from .icons import icon, pixmap
from .painting import draw_corner_marks
from .theme import body_font, heading_font

IS_MAC = sys.platform == "darwin"
CTRL_LABEL = "⌘" if IS_MAC else "Ctrl"


def section_label(text: str, parent: Optional[QWidget] = None) -> QLabel:
    """Uppercase Barlow Condensed label used for VIEWS / PROJECTS / group headers."""
    label = QLabel(text.upper(), parent)
    label.setObjectName("SectionLabel")
    label.setFont(heading_font(12, 0.09))
    return label


class KbdLabel(QLabel):
    """Keyboard hint chip, e.g. "Ctrl K"."""

    def __init__(self, text: str, parent: Optional[QWidget] = None):
        super().__init__(text, parent)
        self.setObjectName("Kbd")
        self.setFont(body_font(10.5))
        self.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)


# The brand mark on an 18-unit grid: (left inset, width) of each "note" line; the last one is
# shorter, like the end of a paragraph. create_icon.py draws the app icon from the same numbers.
BRAND_LINES = ((4, 10), (4, 10), (4, 6))
BRAND_LINE_TOP, BRAND_LINE_HEIGHT, BRAND_LINE_STEP = 5, 2, 3


def paint_brand_mark(painter: QPainter, rect: QRect, *, marks: bool = True) -> None:
    """Accent square with three white note lines in rect (a square), and corner marks."""
    painter.save()
    painter.fillRect(rect, QColor(T.ACCENT))
    unit = rect.width() / 18.0
    white = QColor(T.BG)
    for i, (inset, width) in enumerate(BRAND_LINES):
        top = rect.top() + round((BRAND_LINE_TOP + i * BRAND_LINE_STEP) * unit)
        painter.fillRect(QRect(rect.left() + round(inset * unit), top,
                               max(1, round(width * unit)), max(1, round(BRAND_LINE_HEIGHT * unit))), white)
    if marks:
        draw_corner_marks(painter, rect, T.CORNER_MARK,
                          size=max(T.CORNER_MARK_SIZE, round(rect.width() * 0.22) | 1))
    painter.restore()


def brand_icon() -> QIcon:
    """The app icon drawn in code (used when app_icon.png is missing)."""
    result = QIcon()
    for size in (16, 24, 32, 48, 64, 128, 256):
        image = QPixmap(size, size)
        image.fill(Qt.GlobalColor.transparent)
        painter = QPainter(image)
        marks = size >= 48
        margin = round(size * (0.12 if marks else 0.04))
        paint_brand_mark(painter, QRect(margin, margin, size - 2 * margin, size - 2 * margin), marks=marks)
        painter.end()
        result.addPixmap(image)
    return result


class BrandMark(QWidget):
    """The brand mark at a given square size, with room around it for the corner marks."""
    SQUARE = 18

    def __init__(self, parent: Optional[QWidget] = None, square: int = SQUARE):
        super().__init__(parent)
        self._square = square
        mark = max(T.CORNER_MARK_SIZE, round(square * 0.22) | 1)
        side = square + 2 * (mark // 2 + 1)
        self.setFixedSize(side, side)

    def paintEvent(self, event):
        painter = QPainter(self)
        offset = (self.width() - self._square) // 2
        paint_brand_mark(painter, QRect(offset, offset, self._square, self._square))
        painter.end()


class MenuButton(QPushButton):
    """Secondary button with a leading icon and a trailing chevron that opens a menu."""

    def __init__(self, icon_name: str, text: str = "", parent: Optional[QWidget] = None):
        super().__init__(text, parent)
        self.setIcon(icon(icon_name, 15, T.NEUTRAL_800))
        self.setIconSize(QSize(15, 15))
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFocusPolicy(Qt.FocusPolicy.TabFocus)
        # Room for the chevron; the stock menu indicator is hidden
        self.setStyleSheet("QPushButton{padding-right:28px;text-align:left} QPushButton::menu-indicator{image:none;width:0}")

    def paintEvent(self, event):
        super().paintEvent(event)
        painter = QPainter(self)
        chev = pixmap("chev", 15, T.NEUTRAL_700, scale=self.devicePixelRatioF())
        painter.drawPixmap(self.width() - 15 - 8, (self.height() - 15) // 2, chev)
        painter.end()


class DatabaseChip(QPushButton):
    """The open database in the top bar: icon, name and chevron, painted by hand so the chip
    is symmetric. Flat at rest; hover, press and an open menu show a framed fill."""
    HEIGHT = 30
    ICON = 14
    CHEV = 12
    EDGE = 10          # visible gap from the border to the icon, and from the arrow to the border
    GAP = 7            # icon to text, and text to arrow
    _insets: dict = {}  # (icon name, size) -> transparent margin at its (left, right)

    @classmethod
    def _inset(cls, name: str, size: int):
        """Transparent columns at the left and right of an icon, so gaps are measured from ink."""
        key = (name, size)
        if key not in cls._insets:
            image = pixmap(name, size, T.NEUTRAL_900, scale=1.0).toImage()
            ink = [x for x in range(image.width())
                   if any(image.pixelColor(x, y).alpha() > 30 for y in range(image.height()))]
            cls._insets[key] = (ink[0], image.width() - 1 - ink[-1]) if ink else (0, 0)
        return cls._insets[key]

    def __init__(self, parent: Optional[QWidget] = None):
        super().__init__("", parent)
        self.setFont(body_font(13, 500))
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFocusPolicy(Qt.FocusPolicy.TabFocus)
        self.setFixedHeight(self.HEIGHT)
        self.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)
        self.setAttribute(Qt.WidgetAttribute.WA_Hover, True)
        # The app style sets the condensed heading font on every button; this chip uses body text
        self.setStyleSheet("QPushButton{border:0;padding:0;background:transparent;"
                           "font-family:\"Barlow\";font-weight:500;font-size:13px}"
                           "QPushButton::menu-indicator{image:none;width:0}")

    def setText(self, text: str) -> None:
        super().setText(text)
        self.updateGeometry()
        self.update()

    def _layout(self):
        """x positions of the icon, the text and the chevron box, and the text width."""
        text_w = self.fontMetrics().horizontalAdvance(self.text())
        icon_left, icon_right = self._inset("db", self.ICON)
        chev_left, chev_right = self._inset("chev", self.CHEV)
        icon_x = self.EDGE - icon_left
        text_x = icon_x + self.ICON - icon_right + self.GAP
        chev_x = text_x + text_w + self.GAP - chev_left
        width = chev_x + self.CHEV - chev_right + self.EDGE
        return icon_x, text_x, chev_x, text_w, width

    def sizeHint(self):
        return QSize(self._layout()[-1], self.HEIGHT)

    def minimumSizeHint(self):
        return self.sizeHint()

    def paintEvent(self, event):
        painter = QPainter(self)
        menu = self.menu()
        active = self.isDown() or (menu is not None and menu.isVisible())
        if active or self.underMouse():
            frame = self.rect().adjusted(0, 0, -1, -1)
            painter.fillRect(frame, QColor(T.PRESSED if active else T.HOVER))
            painter.setPen(QColor(T.DIVIDER))
            painter.drawRect(frame)
        scale = self.devicePixelRatioF()
        icon_x, text_x, chev_x, text_w, _width = self._layout()
        mid = self.height() // 2
        painter.drawPixmap(icon_x, mid - self.ICON // 2, pixmap("db", self.ICON, T.NEUTRAL_600, scale=scale))
        painter.setFont(self.font())
        painter.setPen(QColor(T.NEUTRAL_800))
        painter.drawText(QRect(text_x, 0, text_w + 1, self.height()),
                         Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft, self.text())
        painter.drawPixmap(chev_x, mid - self.CHEV // 2, pixmap("chev", self.CHEV, T.NEUTRAL_600, scale=scale))
        if self.hasFocus():
            painter.setPen(QColor(T.ACCENT_400))
            painter.drawRect(self.rect().adjusted(0, 0, -1, -1))
        painter.end()


class SearchField(QLineEdit):
    """Search box with a leading icon, a shortcut hint when empty and a clear button otherwise.
    The "Completed" chip on the right decides whether completed tasks are part of the results."""
    clearRequested = pyqtSignal()
    escapePressed = pyqtSignal()
    includeDoneToggled = pyqtSignal(bool)
    recentRequested = pyqtSignal()  # Focus, Down arrow or a click while empty

    def __init__(self, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self.setObjectName("Search")
        self.setFixedHeight(36)
        self.setMaximumWidth(620)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)

        self._icon = QLabel(self)
        self._icon.setPixmap(pixmap("search", 16, T.NEUTRAL_600, scale=self.devicePixelRatioF()))
        self._icon.setFixedSize(16, 16)
        self._icon.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)

        self._hint = KbdLabel(f"{CTRL_LABEL} K", self)
        self._hint.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)

        self._clear = QToolButton(self)
        self._clear.setProperty("flat", True)
        self._clear.setIcon(icon("x", 15, T.NEUTRAL_700))
        self._clear.setIconSize(QSize(15, 15))
        self._clear.setFixedSize(24, 24)
        self._clear.setCursor(Qt.CursorShape.PointingHandCursor)
        self._clear.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self._clear.setToolTip("Clear search")
        self._clear.setStyleSheet("QToolButton{padding:0;border:0}")
        self._clear.clicked.connect(self.clearRequested.emit)

        self.done_toggle = QToolButton(self)
        self.done_toggle.setObjectName("SearchDoneToggle")
        self.done_toggle.setCheckable(True)
        self.done_toggle.setText("Completed")
        self.done_toggle.setFont(body_font(12))
        self.done_toggle.setIconSize(QSize(13, 13))
        self.done_toggle.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        self.done_toggle.setFixedHeight(24)
        self.done_toggle.setCursor(Qt.CursorShape.PointingHandCursor)
        self.done_toggle.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.done_toggle.toggled.connect(self._on_done_toggled)
        self._on_done_toggled(False, emit=False)

        self.setToolTip(
            "Every word must match a project name, task title or notes (accents and case ignored)\n"
            "\"exact phrase\"   -leave out   @person   in:project   is:done / is:open\n"
            "↓ on an empty box: recent searches")

        self.textChanged.connect(self._sync_adornments)
        self._sync_adornments()

    # Callers block signals while resetting the text, so keep the adornments in sync here too
    def setText(self, text: str) -> None:
        super().setText(text)
        self._sync_adornments()

    def clear(self) -> None:
        super().clear()
        self._sync_adornments()

    def include_done(self) -> bool:
        return self.done_toggle.isChecked()

    def set_include_done(self, checked: bool) -> None:
        """Set the chip without emitting includeDoneToggled."""
        self.done_toggle.blockSignals(True)
        self.done_toggle.setChecked(bool(checked))
        self.done_toggle.blockSignals(False)
        self._on_done_toggled(bool(checked), emit=False)

    def _on_done_toggled(self, checked: bool, emit: bool = True):
        self.done_toggle.setIcon(icon("check", 13, T.NEUTRAL_900 if checked else T.NEUTRAL_600))
        state = "included in" if checked else "left out of"
        self.done_toggle.setToolTip(f"Completed tasks are {state} the results (Alt+C)")
        if emit:
            self.includeDoneToggled.emit(checked)

    def _sync_adornments(self, *_):
        has_text = bool(self.text())
        self._hint.setVisible(not has_text)
        self._clear.setVisible(has_text)
        self._place_adornments()

    def _place_adornments(self):
        w, h = self.width(), self.height()
        self._hint.adjustSize()
        self._hint.move(w - self._hint.width() - 9, (h - self._hint.height()) // 2)
        self._clear.move(w - self._clear.width() - 6, (h - self._clear.height()) // 2)
        right = self._clear.width() + 6 if self.text() else self._hint.width() + 9
        self.done_toggle.adjustSize()
        toggle_x = w - right - 6 - self.done_toggle.width()
        self.done_toggle.move(toggle_x, (h - self.done_toggle.height()) // 2)
        # Keep typed text clear of the chip (the style sheet already pads 60px on the right)
        self.setTextMargins(0, 0, max(0, w - toggle_x + 4 - 60), 0)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._icon.move(11, (self.height() - 16) // 2)
        self._place_adornments()

    def focusInEvent(self, event):
        super().focusInEvent(event)
        if not self.text() and event.reason() in (
                Qt.FocusReason.TabFocusReason, Qt.FocusReason.BacktabFocusReason,
                Qt.FocusReason.ShortcutFocusReason, Qt.FocusReason.OtherFocusReason):
            self.recentRequested.emit()

    def mousePressEvent(self, event):
        super().mousePressEvent(event)
        if event.button() == Qt.MouseButton.LeftButton and not self.text():
            self.recentRequested.emit()

    def keyPressEvent(self, event):
        if event.key() == Qt.Key.Key_Down and not self.text():
            self.recentRequested.emit()
            event.accept()
            return
        if event.key() == Qt.Key.Key_Escape:
            self.escapePressed.emit()
            event.accept()
            return
        super().keyPressEvent(event)


class SavedIndicator(QWidget):
    """6x6 square dot plus "Saved" / "Saving…"."""

    def __init__(self, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self._saving = False
        self._error = ''
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(7)
        self._dot = QWidget(self)
        self._dot.setFixedSize(6, 6)
        self._label = QLabel("Saved", self)
        self._label.setFont(body_font(12.5))
        self._label.setStyleSheet(f"color:{T.NEUTRAL_700}")
        # Reserve the wider text so the bar does not shift between states
        self._label.setMinimumWidth(self._label.fontMetrics().horizontalAdvance("Saving…") + 2)
        layout.addWidget(self._dot, 0, Qt.AlignmentFlag.AlignVCenter)
        layout.addWidget(self._label, 0, Qt.AlignmentFlag.AlignVCenter)
        self._apply()

    def is_saving(self) -> bool:
        return self._saving

    def set_saving(self, saving: bool) -> None:
        saving = bool(saving)
        if saving == self._saving:
            return
        self._saving = saving
        self._apply()

    def set_error(self, error):
        self._error = error
        self.setToolTip(error)
        self._apply()

    def _apply(self):
        self._label.setText('Save failed' if self._error else ('Saving…' if self._saving else 'Saved'))
        self._dot.setStyleSheet(f"background:{T.ACCENT_800 if self._error else (T.NEUTRAL_400 if self._saving else T.ACCENT)}")


class TopBar(QWidget):
    """Flat bar under the menu bar: brand, database switcher, search and the saved indicator."""

    def __init__(self, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self.setObjectName("TopBar")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)

        self.brand_mark = BrandMark(self)
        self.brand_label = QLabel("Project Notes", self)
        self.brand_label.setObjectName("Brand")
        self.brand_label.setFont(heading_font(19, 0.02))

        # Hairline between the brand and the database switcher
        self.brand_divider = QFrame(self)
        self.brand_divider.setFixedSize(1, 20)
        self.brand_divider.setStyleSheet(f"background:{T.DIVIDER}")

        self.db_button = DatabaseChip(self)
        self.db_button.setObjectName("DbSwitcher")
        self.db_button.setToolTip("Switch database")
        self.db_menu = QMenu(self.db_button)
        self.db_button.setMenu(self.db_menu)

        self.search = SearchField(self)
        self.search.setPlaceholderText("Search projects, tasks and notes — @name for people")

        self.saved = SavedIndicator(self)

        left = QWidget(self)
        left_layout = QHBoxLayout(left)
        left_layout.setContentsMargins(0, 0, 0, 0)
        left_layout.setSpacing(7)
        left_layout.addWidget(self.brand_mark)
        left_layout.addWidget(self.brand_label)
        left_layout.addSpacing(7)
        left_layout.addWidget(self.brand_divider)
        left_layout.addSpacing(3)
        left_layout.addWidget(self.db_button)
        left_layout.addStretch(1)

        right = QWidget(self)
        right_layout = QHBoxLayout(right)
        right_layout.setContentsMargins(0, 0, 0, 0)
        right_layout.addStretch(1)
        right_layout.addWidget(self.saved)

        layout = QHBoxLayout(self)
        layout.setContentsMargins(14, 7, 14, 7)
        layout.setSpacing(14)
        layout.addWidget(left, 1)
        layout.addWidget(self.search, 3)
        layout.addWidget(right, 1)

    def set_database_name(self, name: str) -> None:
        """Shows the file name without its extension; the tooltip keeps the full name."""
        stem, ext = os.path.splitext(name)
        self.db_button.setText(stem if ext.lower() in (".sqlite", ".sqlite3", ".db") and stem else name)
        self.db_button.setToolTip(f"Switch database (current: {name})")


def kbd_hints(pairs, parent: Optional[QWidget] = None) -> QWidget:
    """Row of "key label" hints, e.g. [("N", "new"), ("Ctrl K", "search")]."""
    row = QWidget(parent)
    layout = QHBoxLayout(row)
    layout.setContentsMargins(0, 0, 0, 0)
    layout.setSpacing(5)
    for i, (key, text) in enumerate(pairs):
        if i:
            layout.addSpacing(5)
        layout.addWidget(KbdLabel(key, row))
        caption = QLabel(text, row)
        caption.setFont(body_font(12))
        caption.setStyleSheet(f"color:{T.NEUTRAL_700}")
        layout.addWidget(caption)
    return row


def menu_section_label(menu: QMenu, text: str) -> QWidgetAction:
    """Non-interactive uppercase label inside a menu (e.g. "MOVE TO")."""
    label = QLabel(text.upper(), menu)
    label.setFont(body_font(10.5))
    label.setStyleSheet(f"color:{T.NEUTRAL_600};padding:6px 9px 3px 9px;background:transparent")
    action = QWidgetAction(menu)
    action.setDefaultWidget(label)
    action.setEnabled(False)
    menu.addAction(action)
    return action
