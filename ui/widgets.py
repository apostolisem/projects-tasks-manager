"""Small themed widgets shared across the main window: top bar, search field, kbd hints."""
from __future__ import annotations
import os
import sys
from typing import Optional

from PyQt6.QtCore import QEvent, QObject, QPoint, QRect, QSize, Qt, QTimer, pyqtSignal
from PyQt6.QtGui import QColor, QIcon, QPainter, QPixmap, QStandardItem, QStandardItemModel
from PyQt6.QtWidgets import (QAbstractItemView, QApplication, QFrame, QHBoxLayout, QLabel, QLineEdit,
                             QListView, QMenu, QPushButton, QSizePolicy, QToolButton, QVBoxLayout,
                             QWidget, QWidgetAction)

from . import tokens as T
from .icons import icon, pixmap
from .painting import draw_corner_marks, draw_menu_shadow
from .search import fold
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


class RecentSearchPopup(QFrame):
    """Recent searches under the search field.

    A plain child of the main window, not a popup window: it never takes the keyboard or the
    mouse, so the field keeps the focus and typing works while it is open. It cannot take the
    focus itself. SearchField drives it: keys, filtering and when it closes. While it is open, an
    application filter closes it on a click anywhere else and keeps it under the field when the
    window is resized."""
    chosen = pyqtSignal(str)
    clearRequested = pyqtSignal()

    MARGIN = 8  # transparent ring for the shadow and the corner marks, as on menus
    TIP = 'Tip: "phrase"  -word  @name  in:project  is:done / is:open'

    def __init__(self, field: QLineEdit, parent: QWidget):
        super().__init__(parent)
        self._field = field
        self._filtering = False
        self.setObjectName("RecentSearchPopup")
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.setAccessibleName("Recent searches")
        self.hide()

        menu_qss = "font-size:13.5px;background:transparent"
        header = QLabel("RECENT SEARCHES", self)
        header.setFont(body_font(10.5))
        header.setStyleSheet(f"color:{T.NEUTRAL_600};padding:6px 9px 3px 9px;background:transparent")

        self.view = QListView(self)
        self.view.setObjectName("RecentSearchList")
        self.view.setAccessibleName("Recent searches")
        self.view.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.view.setMouseTracking(True)
        self.view.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.view.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.view.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.view.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.view.setTextElideMode(Qt.TextElideMode.ElideRight)
        self.view.setUniformItemSizes(True)
        self.view.setIconSize(QSize(14, 14))
        self.view.setStyleSheet(
            f"QListView{{{menu_qss};border:0;outline:0}}"
            f"QListView::item{{padding:7px 9px;color:{T.TEXT};border:0}}"
            f"QListView::item:selected{{background:{T.ACCENT_100};color:{T.ACCENT_900}}}")
        self.model = QStandardItemModel(self)
        self.view.setModel(self.model)
        self.view.entered.connect(lambda index: self.set_current(index.row()))
        self.view.clicked.connect(lambda index: self.chosen.emit(str(index.data())))

        self.clear_button = QPushButton("Clear recent searches", self)
        self.clear_button.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.clear_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.clear_button.setStyleSheet(
            # A menu entry, not a button: undo the app-wide condensed bold button font
            f"QPushButton{{{menu_qss};font-family:\"{T.FONT_BODY}\";font-weight:400;text-align:left;"
            f"padding:7px 9px;border:0;color:{T.TEXT};min-height:0}}"
            f"QPushButton:hover,QPushButton:pressed{{background:{T.ACCENT_100};color:{T.ACCENT_900}}}")
        self.clear_button.clicked.connect(self.clearRequested.emit)

        tip = QLabel(self.TIP, self)
        tip.setWordWrap(True)
        tip.setStyleSheet(f"{menu_qss};color:{T.NEUTRAL_500};padding:7px 9px")

        layout = QVBoxLayout(self)
        inner = self.MARGIN + 4  # the QMenu padding
        layout.setContentsMargins(inner, inner, inner, inner)
        layout.setSpacing(0)
        layout.addWidget(header)
        layout.addWidget(self.view)
        layout.addWidget(self._divider())
        layout.addWidget(self.clear_button)
        layout.addWidget(self._divider())
        layout.addWidget(tip)

    def _divider(self) -> QWidget:
        holder = QWidget(self)
        holder_layout = QVBoxLayout(holder)
        holder_layout.setContentsMargins(0, 4, 0, 4)
        line = QFrame(holder)
        line.setFixedHeight(1)
        line.setStyleSheet(f"background:{T.DIVIDER}")
        holder_layout.addWidget(line)
        return holder

    # ----- content and highlight -----
    def set_items(self, items, current: int = -1) -> None:
        self.model.clear()
        search_icon = icon("search", 14, T.NEUTRAL_600)
        for text in items:
            item = QStandardItem(search_icon, text)
            item.setToolTip(text)
            item.setEditable(False)
            self.model.appendRow(item)
        rows = self.model.rowCount()
        self.view.ensurePolished()  # row heights come from the style sheet padding
        self.view.setFixedHeight(self.view.sizeHintForRow(0) * rows + 2 * self.view.frameWidth() if rows else 0)
        self.set_current(min(current, rows - 1))
        if self.isVisible():
            self.place()

    def items(self):
        return [str(self.model.item(row).text()) for row in range(self.model.rowCount())]

    def current(self) -> int:
        index = self.view.currentIndex()
        return index.row() if index.isValid() and self.view.selectionModel().isSelected(index) else -1

    def current_text(self) -> str:
        row = self.current()
        return self.items()[row] if row >= 0 else ""

    def set_current(self, row: int) -> None:
        if row < 0 or row >= self.model.rowCount():
            self.view.selectionModel().clear()
            self._announce(-1)
            return
        index = self.model.index(row, 0)
        self.view.setCurrentIndex(index)
        self.view.scrollTo(index)
        self._announce(row)

    def step(self, delta: int) -> None:
        """Down from the field goes to the first row; Up from the first row back to the field."""
        row = self.current()
        if row < 0:
            target = 0 if delta > 0 else -1
        else:
            target = row + delta
            if target >= self.model.rowCount():
                target = row
        self.set_current(target)

    def _announce(self, row: int) -> None:
        """The keyboard focus stays in the field, so screen readers learn the highlighted entry
        from the field's description (Qt reports the change; PyQt has no QAccessible API)."""
        description = SearchField.ACCESSIBLE_HINT
        if 0 <= row < self.model.rowCount():
            description = f"Recent search {row + 1} of {self.model.rowCount()}: {self.model.item(row).text()}"
        if self._field.accessibleDescription() != description:
            self._field.setAccessibleDescription(description)

    # ----- showing and placement -----
    def open(self) -> None:
        if not self.isVisible():
            self.show()
        self.place()
        self.raise_()

    def place(self) -> None:
        parent = self.parentWidget()
        if parent is None:
            return
        m = self.MARGIN
        top_left = self._field.mapTo(parent, QPoint(0, self._field.height() + 2))
        width = self._field.width() + 2 * m
        height = self.heightForWidth(width) if self.hasHeightForWidth() else self.sizeHint().height()
        x = max(0, min(top_left.x() - m, parent.width() - width))
        self.setGeometry(x, top_left.y() - m, width, height)

    def showEvent(self, event):
        super().showEvent(event)
        app = QApplication.instance()
        if app is not None:
            app.installEventFilter(self)

    def hideEvent(self, event):
        self._announce(-1)
        app = QApplication.instance()
        if app is not None:
            app.removeEventFilter(self)
        super().hideEvent(event)

    def eventFilter(self, obj: QObject, event: QEvent) -> bool:
        # Only installed while the popup is visible
        kind = event.type()
        if kind in (QEvent.Type.MouseButtonPress, QEvent.Type.MouseButtonDblClick):
            # Mouse presses reach the window object first; only the widget delivery counts
            if isinstance(obj, QWidget) and obj is not self._field and obj is not self and not self.isAncestorOf(obj):
                self.hide()
        elif kind in (QEvent.Type.Resize, QEvent.Type.Move) and (obj is self.parentWidget() or obj is self._field):
            # After the layout has moved the field
            QTimer.singleShot(0, self._replace)
        return False

    def _replace(self):
        if self.isVisible():
            self.place()

    def mousePressEvent(self, event):
        # The transparent ring around the frame counts as outside
        m = self.MARGIN
        if not self.rect().adjusted(m, m, -m, -m).contains(event.position().toPoint()):
            self.hide()
        event.accept()

    def paintEvent(self, event):
        m = self.MARGIN
        frame = self.rect().adjusted(m, m, -m, -m)
        painter = QPainter(self)
        draw_menu_shadow(painter, frame)
        painter.fillRect(frame, QColor(T.BG))
        painter.setPen(QColor(T.DIVIDER))
        painter.drawRect(frame.adjusted(0, 0, -1, -1))
        draw_corner_marks(painter, frame, T.CORNER_MARK)
        painter.end()


class SearchField(QLineEdit):
    """Search box with a leading icon, a shortcut hint when empty and a clear button otherwise.
    The "Completed" chip on the right decides whether completed tasks are part of the results."""
    clearRequested = pyqtSignal()
    escapePressed = pyqtSignal()
    includeDoneToggled = pyqtSignal(bool)
    recentRequested = pyqtSignal()  # wants the recent list: show_recent() opens the popup
    recentChosen = pyqtSignal(str)  # a recent search was picked (Enter or a click)
    recentRemoveRequested = pyqtSignal(str)  # Shift+Delete on the highlighted entry
    recentClearRequested = pyqtSignal()
    committed = pyqtSignal()  # Enter, or the focus left the field: the query counts as used
    ACCESSIBLE_HINT = "Press Down for recent searches"

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
            "↓: recent searches (Shift+Delete removes the highlighted one)")
        self.setAccessibleDescription(self.ACCESSIBLE_HINT)

        self._recent: list = []
        self._popup: Optional[RecentSearchPopup] = None

        self.textChanged.connect(self._sync_adornments)
        self.textChanged.connect(self._refilter_recent)
        self._sync_adornments()

    # Callers block signals while resetting the text, so keep the adornments in sync here too.
    # Text set from code (a recent search, clearing, switching database) closes the popup.
    def setText(self, text: str) -> None:
        self.hide_recent()
        super().setText(text)
        self._sync_adornments()

    def clear(self) -> None:
        self.hide_recent()
        super().clear()
        self._sync_adornments()

    # ----- recent searches popup -----
    def show_recent(self, items, keep_highlight: bool = False, highlight_first: bool = False) -> None:
        """Open (or refresh) the popup with the entries that match the typed text."""
        self._recent = [str(item) for item in items]
        matches = self._matching_recent()
        if not matches:
            self.hide_recent()
            return
        popup = self._ensure_popup()
        if popup is None:
            return
        current = popup.current() if keep_highlight and popup.isVisible() else -1
        if highlight_first and current < 0:
            current = 0
        popup.set_items(matches, current)
        popup.open()

    def hide_recent(self) -> None:
        if self._popup is not None and self._popup.isVisible():
            self._popup.hide()

    def recent_popup(self) -> Optional[RecentSearchPopup]:
        return self._popup

    def recent_visible(self) -> bool:
        return self._popup is not None and self._popup.isVisible()

    def _ensure_popup(self) -> Optional[RecentSearchPopup]:
        window = self.window()
        if window is self:
            return None
        if self._popup is None or self._popup.parentWidget() is not window:
            self._popup = RecentSearchPopup(self, window)
            self._popup.chosen.connect(self.recentChosen.emit)
            self._popup.clearRequested.connect(self.recentClearRequested.emit)
        return self._popup

    def _matching_recent(self) -> list:
        """Everything while the box is empty; otherwise entries containing the typed text
        (accents and case ignored), without the one that is already typed out in full."""
        typed = fold(self.text().strip()) or ""
        if not typed:
            return list(self._recent)
        return [r for r in self._recent if typed in (fold(r) or "") and (fold(r) or "") != typed]

    def _refilter_recent(self, *_):
        # Typing while the popup is open narrows it down; it closes once nothing matches
        if self.recent_visible():
            self.show_recent(self._recent)

    # ----- events -----
    def focusOutEvent(self, event):
        super().focusOutEvent(event)
        self.hide_recent()
        if event.reason() != Qt.FocusReason.PopupFocusReason:  # e.g. the text context menu
            self.committed.emit()

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
        # Only when the person moved here (Tab, Ctrl+K), never when code sets the focus, e.g. at
        # startup. A mouse click opens it from mousePressEvent.
        if not self.text() and event.reason() in (
                Qt.FocusReason.TabFocusReason, Qt.FocusReason.BacktabFocusReason,
                Qt.FocusReason.ShortcutFocusReason):
            self.recentRequested.emit()

    def mousePressEvent(self, event):
        super().mousePressEvent(event)
        # A click while it is open only places the cursor
        if event.button() == Qt.MouseButton.LeftButton and not self.text() and not self.recent_visible():
            self.recentRequested.emit()

    def keyPressEvent(self, event):
        key = event.key()
        mods = event.modifiers() & ~Qt.KeyboardModifier.KeypadModifier
        popup = self._popup if self.recent_visible() else None
        if popup is not None and mods == Qt.KeyboardModifier.NoModifier and key in (Qt.Key.Key_Down, Qt.Key.Key_Up):
            popup.step(1 if key == Qt.Key.Key_Down else -1)
            event.accept()
            return
        if key == Qt.Key.Key_Down and mods == Qt.KeyboardModifier.NoModifier:
            self.recentRequested.emit()
            if self.recent_visible():
                self._popup.set_current(0)
                event.accept()
                return
        if key in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
            if popup is not None and popup.current() >= 0:
                self.recentChosen.emit(popup.current_text())
                event.accept()
                return
            self.hide_recent()
            super().keyPressEvent(event)
            self.committed.emit()
            return
        if key == Qt.Key.Key_Escape:
            if popup is not None:
                self.hide_recent()  # first Escape only closes the list
            else:
                self.escapePressed.emit()
            event.accept()
            return
        if (popup is not None and key == Qt.Key.Key_Delete and mods == Qt.KeyboardModifier.ShiftModifier
                and popup.current() >= 0):
            self.recentRemoveRequested.emit(popup.current_text())
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
