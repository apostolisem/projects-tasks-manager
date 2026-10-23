"""Sidebar: smart views, the project list and the collapsible hidden-projects section."""
from __future__ import annotations
import json
from typing import Dict, Optional

from PyQt6.QtCore import QRect, QSize, Qt, pyqtSignal
from PyQt6.QtGui import QColor, QFont, QFontMetrics, QPainter
from PyQt6.QtWidgets import (QAbstractItemView, QFrame, QHBoxLayout, QLabel, QLineEdit, QListView,
                             QScrollArea, QSizePolicy, QToolButton, QVBoxLayout, QWidget)

from . import tokens as T
from .icons import icon, pixmap
from .theme import body_font, heading_font
from .widgets import section_label

TASK_MIME = "application/x-task-row"
ROW_HEIGHT = 32
ROW_PAD_X = 8
ROW_GAP = 9
ICON_SIZE = 15

# key, label, icon. Order is the display order in the VIEWS section.
SMART_VIEWS = [
    ("today", "Due & overdue", "sun"),
    ("pinned", "Focus", "target"),
    ("waiting", "Waiting", "wait"),
    ("mom", "Meeting notes", "mom"),
    ("triage", "No due date", "inbox"),
]
SMART_VIEW_TITLES = {key: label for key, label, _icon in SMART_VIEWS}

def paint_sidebar_row(painter: QPainter, rect: QRect, *, icon_name: Optional[str], text: str,
                      count: Optional[str] = None, badge: bool = False, active: bool = False,
                      hover: bool = False, pressed: bool = False, dim: bool = False,
                      bold: bool = False, ghost: bool = False, faded: bool = False,
                      focused: bool = False, dot: bool = False, alert: bool = False, spans=None) -> None:
    """Paint one sidebar row (smart view, project or ghost item) into rect.

    The count is always the plain number of tasks. alert (something is overdue) and dot
    (something is due today) add the same small square left of it, so the number never reads
    as "this many are overdue"; the row tooltip gives the details."""
    painter.save()
    if dim:
        painter.setOpacity(0.45)

    if active:
        painter.fillRect(rect, QColor(T.ACCENT_200))
    elif pressed:
        painter.fillRect(rect, QColor(T.PRESSED))
    elif hover or focused:
        painter.fillRect(rect, QColor(T.HOVER))
    if focused:
        painter.setPen(QColor(T.ACCENT_400))
        painter.drawRect(rect.adjusted(0, 0, -1, -1))

    if active:
        text_color, icon_color = T.ACCENT_900, T.ACCENT_700
    elif ghost or faded:
        text_color, icon_color = T.NEUTRAL_600, T.NEUTRAL_600
    else:
        text_color, icon_color = T.NEUTRAL_900, T.NEUTRAL_600

    x = rect.left() + ROW_PAD_X
    if icon_name:
        scale = painter.device().devicePixelRatioF() if painter.device() else 1.0
        pm = pixmap(icon_name, ICON_SIZE, icon_color, scale=scale)
        painter.drawPixmap(x, rect.top() + (rect.height() - ICON_SIZE) // 2, pm)
        x += ICON_SIZE + ROW_GAP

    right = rect.right() - ROW_PAD_X
    if count:
        count_font = body_font(12, 500 if badge else 400)
        metrics = QFontMetrics(count_font)
        text_w = metrics.horizontalAdvance(count)
        painter.setFont(count_font)
        if badge:
            badge_rect = QRect(right - text_w - 12 + 1, rect.top() + (rect.height() - 18) // 2, text_w + 12, 18)
            painter.fillRect(badge_rect, QColor(T.ACCENT_700))
            painter.setPen(QColor(T.BG))
            painter.drawText(badge_rect, Qt.AlignmentFlag.AlignCenter, count)
            right = badge_rect.left() - ROW_GAP
        else:
            painter.setPen(QColor(T.ACCENT_800 if active else T.NEUTRAL_600))
            count_rect = QRect(right - text_w + 1, rect.top(), text_w, rect.height())
            painter.drawText(count_rect, Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignRight, count)
            right = count_rect.left() - ROW_GAP
        if alert or dot:
            # Small square left of the count: something is overdue or due today
            size = 5
            painter.fillRect(QRect(right + ROW_GAP - size - 4, rect.top() + (rect.height() - size) // 2, size, size),
                             QColor(T.ACCENT_700))
            right -= size

    font = body_font(14, 700 if bold else (500 if active else 400))
    painter.setFont(font)
    painter.setPen(QColor(text_color))
    text_rect = QRect(x, rect.top(), max(0, right - x), rect.height())
    if spans and _draw_with_spans(painter, text_rect, text, font, spans):
        painter.restore()
        return
    elided = QFontMetrics(font).elidedText(text, Qt.TextElideMode.ElideRight, text_rect.width())
    painter.drawText(text_rect, Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft, elided)
    painter.restore()


def _draw_with_spans(painter: QPainter, rect: QRect, text: str, font, spans) -> bool:
    """Draw text with the (start, end) spans in bold; False when it would not fit (caller elides)."""
    bold = QFont(font)
    bold.setWeight(QFont.Weight.Bold)
    pieces = []
    at = 0
    for start, end in spans:
        if start > at:
            pieces.append((text[at:start], font))
        pieces.append((text[start:end], bold))
        at = end
    if at < len(text):
        pieces.append((text[at:], font))
    if sum(QFontMetrics(f).horizontalAdvance(piece) for piece, f in pieces) > rect.width():
        return False
    x = rect.left()
    for piece, f in pieces:
        painter.setFont(f)
        width = QFontMetrics(f).horizontalAdvance(piece)
        painter.drawText(QRect(x, rect.top(), width + 1, rect.height()),
                         Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft, piece)
        x += width
    return True


class SidebarItem(QWidget):
    """Clickable sidebar row painted with paint_sidebar_row. Reachable with the arrow keys."""
    clicked = pyqtSignal()
    stepRequested = pyqtSignal(int)  # Up / Down: move keyboard focus to the previous / next sidebar row
    enterRequested = pyqtSignal()    # Right: open the row and move to the task list

    def __init__(self, text: str, icon_name: Optional[str] = None, *, ghost: bool = False,
                 parent: Optional[QWidget] = None):
        super().__init__(parent)
        self._text = text
        self._icon_name = icon_name
        self._ghost = ghost
        self._count: Optional[str] = None
        self._badge = False
        self._active = False
        self._hover = False
        self._pressed = False
        self.setFixedHeight(ROW_HEIGHT)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setAttribute(Qt.WidgetAttribute.WA_Hover, True)
        # Tab or the arrow keys only, so a mouse click doesn't take focus from the task list
        self.setFocusPolicy(Qt.FocusPolicy.TabFocus)

    def keyPressEvent(self, event):
        key = event.key()
        if key in (Qt.Key.Key_Up, Qt.Key.Key_Down):
            self.stepRequested.emit(-1 if key == Qt.Key.Key_Up else 1)
        elif key in (Qt.Key.Key_Return, Qt.Key.Key_Enter, Qt.Key.Key_Space):
            self.clicked.emit()
        elif key == Qt.Key.Key_Right:
            self.clicked.emit()
            self.enterRequested.emit()
        else:
            super().keyPressEvent(event)

    def focusInEvent(self, event):
        super().focusInEvent(event)
        self.update()

    def focusOutEvent(self, event):
        super().focusOutEvent(event)
        self.update()

    def set_text(self, text: str) -> None:
        if text != self._text:
            self._text = text
            self.update()

    def set_count(self, count: Optional[int], badge: bool = False) -> None:
        text = str(count) if count else None
        if text != self._count or badge != self._badge:
            self._count, self._badge = text, bool(badge and count)  # drawn as the overdue marker
            self.update()

    def set_active(self, active: bool) -> None:
        if active != self._active:
            self._active = active
            self.update()

    def is_active(self) -> bool:
        return self._active

    def count_text(self) -> Optional[str]:
        return self._count

    def has_badge(self) -> bool:
        return self._badge

    def enterEvent(self, event):
        self._hover = True
        self.update()

    def leaveEvent(self, event):
        self._hover = False
        self._pressed = False
        self.update()

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self._pressed = True
            self.update()

    def mouseReleaseEvent(self, event):
        was_pressed = self._pressed
        self._pressed = False
        self.update()
        if was_pressed and event.button() == Qt.MouseButton.LeftButton and self.rect().contains(event.position().toPoint()):
            self.clicked.emit()

    def paintEvent(self, event):
        painter = QPainter(self)
        paint_sidebar_row(painter, self.rect(), icon_name=self._icon_name, text=self._text, count=self._count,
                          alert=self._badge, active=self._active, hover=self._hover, pressed=self._pressed,
                          ghost=self._ghost, focused=self.hasFocus())
        painter.end()


class SectionToggle(QWidget):
    """Collapsible section label with a chevron, e.g. "HIDDEN · 3"."""
    toggled = pyqtSignal(bool)

    def __init__(self, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self._expanded = False
        self._text = ""
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFixedHeight(34)

    def set_text(self, text: str) -> None:
        self._text = text.upper()
        self.update()

    def is_expanded(self) -> bool:
        return self._expanded

    def set_expanded(self, expanded: bool) -> None:
        if expanded != self._expanded:
            self._expanded = expanded
            self.update()

    def mouseReleaseEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self._expanded = not self._expanded
            self.update()
            self.toggled.emit(self._expanded)

    def paintEvent(self, event):
        painter = QPainter(self)
        scale = self.devicePixelRatioF()
        chevron = pixmap("chev" if self._expanded else "chevr", 12, T.NEUTRAL_700, scale=scale)
        baseline_top = 14
        row_h = self.height() - baseline_top - 3
        painter.drawPixmap(ROW_PAD_X, baseline_top + (row_h - 12) // 2, chevron)
        painter.setFont(heading_font(12, 0.09))
        painter.setPen(QColor(T.NEUTRAL_700))
        painter.drawText(QRect(ROW_PAD_X + 12 + 5, baseline_top, self.width(), row_h),
                         Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft, self._text)
        painter.end()


class FlatListView(QListView):
    """List view without its own scrolling that shows one section of the project model.

    section "visible" shows rows before model.hidden_start, "hidden" shows the rest.
    The view is as tall as its visible rows so the surrounding scroll area does the scrolling.
    Tasks dragged from the task list can be dropped on a row (tasksDropped).
    """
    tasksDropped = pyqtSignal(int, list)  # model row, task ids
    edgeReached = pyqtSignal(int)          # Up on the first row (-1) / Down on the last row (+1)
    enterRequested = pyqtSignal()          # Enter / Right: move to the task list

    def __init__(self, section: str = "visible", parent: Optional[QWidget] = None):
        super().__init__(parent)
        self._section = section
        self.drop_row = -1  # row under a task drag, painted as the drop target
        self.context_row = -1  # row whose context menu is open, painted as hovered
        self.outer_scroll: Optional[QScrollArea] = None  # the sidebar's scroll area (set by Sidebar)
        self.setDragDropMode(QAbstractItemView.DragDropMode.DropOnly)
        self.setDropIndicatorShown(False)
        self.setFrameShape(QFrame.Shape.NoFrame)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setVerticalScrollMode(QAbstractItemView.ScrollMode.ScrollPerPixel)
        self.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.setMouseTracking(True)
        self.setSpacing(0)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.setFixedHeight(0)

    def setModel(self, model):
        super().setModel(model)
        if model is not None:
            for signal in (model.modelReset, model.rowsInserted, model.rowsRemoved, model.layoutChanged):
                signal.connect(self.sync_rows)
        self.sync_rows()

    def sync_rows(self, *_):
        model = self.model()
        if model is None:
            self.setFixedHeight(0)
            return
        total = model.rowCount()
        split = min(getattr(model, "hidden_start", total), total)
        shown = 0
        for row in range(total):
            in_section = row < split if self._section == "visible" else row >= split
            self.setRowHidden(row, not in_section)
            shown += 1 if in_section else 0
        self.setFixedHeight(shown * ROW_HEIGHT)

    def wheelEvent(self, event):
        # Let the surrounding scroll area handle the wheel
        event.ignore()

    def scrollTo(self, index, hint=QAbstractItemView.ScrollHint.EnsureVisible):
        """This view never scrolls itself: bring the row into view in the sidebar's scroll area."""
        if self.outer_scroll is None or not index.isValid() or self.isRowHidden(index.row()):
            return
        content = self.outer_scroll.widget()
        rect = self.visualRect(index)
        if content is None or not rect.isValid():
            return
        center = self.viewport().mapTo(content, rect.center())
        # Margin of a row and a bit, so the neighbour above/below shows too
        self.outer_scroll.ensureVisible(center.x(), center.y(), 0, ROW_HEIGHT + ROW_HEIGHT // 2)

    def currentChanged(self, current, previous):
        super().currentChanged(current, previous)
        if current.isValid() and self.hasFocus():
            self.scrollTo(current)

    def section_rows(self) -> list:
        """Model rows this view shows, top to bottom."""
        model = self.model()
        if model is None:
            return []
        return [row for row in range(model.rowCount()) if not self.isRowHidden(row)]

    def keyPressEvent(self, event):
        if self.state() == QAbstractItemView.State.EditingState:
            super().keyPressEvent(event)
            return
        key = event.key()
        if key in (Qt.Key.Key_Return, Qt.Key.Key_Enter, Qt.Key.Key_Right):
            self.enterRequested.emit()
            return
        if key in (Qt.Key.Key_Up, Qt.Key.Key_Down) and event.modifiers() == Qt.KeyboardModifier.NoModifier:
            rows = self.section_rows()
            current = self.currentIndex().row() if self.currentIndex().isValid() else -1
            step = -1 if key == Qt.Key.Key_Up else 1
            if not rows or current not in rows:
                self.edgeReached.emit(step)
                return
            if (step < 0 and current == rows[0]) or (step > 0 and current == rows[-1]):
                self.edgeReached.emit(step)
                return
        super().keyPressEvent(event)

    def focus_row(self, first: bool) -> bool:
        """Take keyboard focus on the first or last row of this section and select it."""
        rows = self.section_rows()
        if not rows or not self.isVisible():
            return False
        self.setFocus(Qt.FocusReason.OtherFocusReason)
        self.setCurrentIndex(self.model().index(rows[0] if first else rows[-1], 0))
        return True

    def focusInEvent(self, event):
        super().focusInEvent(event)
        self.viewport().update()

    def focusOutEvent(self, event):
        super().focusOutEvent(event)
        self.viewport().update()

    # ----- dropping tasks on a project
    def _set_drop_row(self, row: int) -> None:
        if row != self.drop_row:
            self.drop_row = row
            self.viewport().update()

    def dragEnterEvent(self, event):
        if event.mimeData().hasFormat(TASK_MIME) and event.source() is not self:
            event.acceptProposedAction()
        else:
            event.ignore()

    def dragMoveEvent(self, event):
        index = self.indexAt(event.position().toPoint())
        row = index.row() if index.isValid() and not self.isRowHidden(index.row()) else -1
        self._set_drop_row(row)
        if row >= 0 and event.mimeData().hasFormat(TASK_MIME):
            event.acceptProposedAction()
        else:
            event.ignore()

    def dragLeaveEvent(self, event):
        self._set_drop_row(-1)
        event.accept()

    def dropEvent(self, event):
        row = self.drop_row
        self._set_drop_row(-1)
        try:
            payload = json.loads(bytes(event.mimeData().data(TASK_MIME)).decode("utf-8"))
            task_ids = [int(task_id) for task_id in payload.get("task_ids", [])]
        except (ValueError, TypeError, AttributeError):
            task_ids = []
        if row < 0 or not task_ids:
            event.ignore()
            return
        # The move happens here; stop the source view from removing its rows itself
        event.setDropAction(Qt.DropAction.IgnoreAction)
        event.accept()
        self.tasksDropped.emit(row, task_ids)


class Sidebar(QWidget):
    """Projects pane. The project list views are created by the main window and inserted here."""
    smartViewRequested = pyqtSignal(str)
    newProjectSubmitted = pyqtSignal(str)
    hiddenToggled = pyqtSignal(bool)
    filterAccepted = pyqtSignal()  # Enter in the filter field: open the first match
    taskListRequested = pyqtSignal()  # Right / Enter from the sidebar: move focus to the task list
    reviewNextRequested = pyqtSignal()
    reviewEndRequested = pyqtSignal()

    def __init__(self, project_view: QListView, hidden_view: QListView, project_filter_edit: QLineEdit,
                 parent: Optional[QWidget] = None):
        super().__init__(parent)
        self.setObjectName("Sidebar")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setMinimumWidth(180)

        self.project_view = project_view
        self.hidden_view = hidden_view
        self.project_filter_edit = project_filter_edit

        content = QWidget()
        content.setObjectName("SidebarContent")
        col = QVBoxLayout(content)
        col.setContentsMargins(10, 7, 10, 20)
        col.setSpacing(0)

        # --- VIEWS
        col.addWidget(section_label("Views"))
        self.view_items: Dict[str, SidebarItem] = {}
        for key, label, icon_name in SMART_VIEWS:
            item = SidebarItem(label, icon_name)
            item.clicked.connect(lambda k=key: self.smartViewRequested.emit(k))
            item.stepRequested.connect(lambda step, w=item: self.focus_step(w, step))
            item.enterRequested.connect(self.taskListRequested)
            self.view_items[key] = item
            col.addWidget(item)

        # --- PROJECTS header: label + filter toggle + add button
        self.projects_header = QWidget()
        header_row = QHBoxLayout(self.projects_header)
        header_row.setContentsMargins(0, 0, 4, 0)
        header_row.setSpacing(2)
        self.projects_label = section_label("Projects")
        header_row.addWidget(self.projects_label, 1)

        self.filter_button = self._header_button("search", "Filter projects by name")
        self.filter_button.setCheckable(True)
        self.filter_button.toggled.connect(self._on_filter_toggled)
        header_row.addWidget(self.filter_button, 0, Qt.AlignmentFlag.AlignBottom)

        self.add_button = self._header_button("plus", "New project")
        self.add_button.clicked.connect(self.begin_new_project)
        header_row.addWidget(self.add_button, 0, Qt.AlignmentFlag.AlignBottom)
        col.addWidget(self.projects_header)

        # --- REVIEW · n left (only while a periodic review runs)
        self.review_strip = QWidget()
        review_row = QHBoxLayout(self.review_strip)
        review_row.setContentsMargins(0, 0, 4, 2)
        review_row.setSpacing(2)
        self.review_label = QLabel()
        self.review_label.setObjectName("ReviewLabel")
        self.review_label.setFont(heading_font(12, 0.09))
        review_row.addWidget(self.review_label, 1)
        self.review_next_button = self._header_button("chevr", "Reviewed → next: mark this project reviewed and open the next one (Ctrl+Shift+R)")
        self.review_next_button.clicked.connect(self.reviewNextRequested)
        review_row.addWidget(self.review_next_button)
        self.review_end_button = self._header_button("x", "End the review now (shows a summary; can be undone)")
        self.review_end_button.clicked.connect(self.reviewEndRequested)
        review_row.addWidget(self.review_end_button)
        self.review_strip.setVisible(False)
        col.addWidget(self.review_strip)

        self.project_filter_edit.setPlaceholderText("Filter projects…")
        if isinstance(self.project_filter_edit, FilterEdit):
            self.project_filter_edit.escaped.connect(lambda: self.filter_button.setChecked(False))
            self.project_filter_edit.accepted.connect(self.filterAccepted)
            self.project_filter_edit.downPressed.connect(self.focus_first_project)
        self.project_filter_edit.setVisible(False)
        self.project_filter_edit.setStyleSheet("QLineEdit{padding:4px 8px;margin:2px 0 4px 0;font-size:13px}")
        col.addWidget(self.project_filter_edit)

        col.addWidget(self.project_view)
        for view in (self.project_view, self.hidden_view):
            if isinstance(view, FlatListView):
                view.edgeReached.connect(lambda step, v=view: self.focus_step(v, step))
                view.enterRequested.connect(self.taskListRequested)

        # --- inline "new project" editor + ghost item
        self.new_project_edit = _NewProjectEdit()
        self.new_project_edit.setPlaceholderText("Project name")
        self.new_project_edit.setVisible(False)
        self.new_project_edit.setStyleSheet(
            f"QLineEdit{{padding:5px 8px;margin:1px 0;background:{T.BG};border:1px solid {T.ACCENT}}}")
        self.new_project_edit.submitted.connect(self._submit_new_project)
        self.new_project_edit.cancelled.connect(self.cancel_new_project)
        col.addWidget(self.new_project_edit)

        self.new_project_item = SidebarItem("New project", "plus", ghost=True)
        self.new_project_item.clicked.connect(self.begin_new_project)
        col.addWidget(self.new_project_item)

        # --- HIDDEN · n
        self.hidden_toggle = SectionToggle()
        self.hidden_toggle.setVisible(False)
        self.hidden_toggle.toggled.connect(self._on_hidden_toggled)
        col.addWidget(self.hidden_toggle)
        self.hidden_view.setVisible(False)
        col.addWidget(self.hidden_view)

        col.addStretch(1)

        self.scroll = QScrollArea(self)
        self.scroll.setObjectName("SidebarScroll")
        self.scroll.setFrameShape(QFrame.Shape.NoFrame)
        self.scroll.setWidgetResizable(True)
        self.scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.scroll.setWidget(content)
        for view in (self.project_view, self.hidden_view):
            if isinstance(view, FlatListView):
                view.outer_scroll = self.scroll

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.addWidget(self.scroll)

    def _header_button(self, icon_name: str, tooltip: str) -> QToolButton:
        button = QToolButton()
        button.setProperty("flat", True)
        button.setIcon(icon(icon_name, 15, T.NEUTRAL_700, on_color=T.ACCENT_700))
        button.setIconSize(QSize(15, 15))
        button.setFixedSize(22, 22)
        button.setToolTip(tooltip)
        button.setCursor(Qt.CursorShape.PointingHandCursor)
        button.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        button.setStyleSheet("QToolButton{padding:0;border:0;margin-bottom:2px} "
                             f"QToolButton:checked{{background:{T.ACCENT_200}}}")
        return button

    # ----- smart views
    def set_view_counts(self, counts: Dict[str, int]) -> None:
        overdue = int(counts.get("overdue", 0) or 0)
        for key, item in self.view_items.items():
            item.set_count(int(counts.get(key, 0) or 0), badge=(key == "today" and overdue > 0))
        today = self.view_items.get("today")
        if today is not None:
            due = int(counts.get("today", 0) or 0)
            today.setToolTip(f"{due} due · {overdue} overdue" if overdue else "")

    def set_active_view(self, key: Optional[str]) -> None:
        for view_key, item in self.view_items.items():
            item.set_active(view_key == key)

    # ----- keyboard path: views -> projects -> hidden projects
    def _focus_stops(self) -> list:
        stops = [item for item in self.view_items.values() if item.isVisible()]
        stops += [view for view in (self.project_view, self.hidden_view)
                  if isinstance(view, FlatListView) and view.isVisible() and view.section_rows()]
        return stops

    def focus_step(self, origin: QWidget, step: int) -> None:
        """Move keyboard focus from origin to the previous / next sidebar stop (no wrap-around)."""
        stops = self._focus_stops()
        if origin not in stops:
            return
        index = stops.index(origin) + step
        if not 0 <= index < len(stops):
            return
        target = stops[index]
        if isinstance(target, FlatListView):
            target.focus_row(first=step > 0)
        else:
            target.setFocus(Qt.FocusReason.OtherFocusReason)
            self.scroll.ensureWidgetVisible(target)

    def set_review_status(self, text: Optional[str], tooltip: str = "") -> None:
        """Show the review strip ("REVIEW · 5", "REVIEW · Waiting") while a review runs; None hides it."""
        self.review_strip.setVisible(text is not None)
        if text is not None:
            self.review_label.setText(text)
            self.review_strip.setToolTip(tooltip)

    def set_project_count(self, count: int) -> None:
        self.projects_label.setText(f"PROJECTS · {count}" if count else "PROJECTS")

    # ----- project name filter
    def focus_first_project(self) -> None:
        """Move the keyboard into the project list (from the filter field), selecting the first row."""
        for view in (self.project_view, self.hidden_view):
            model = view.model()
            if view.isVisible() and view.height() > 0 and model is not None:
                for row in range(model.rowCount()):
                    if not view.isRowHidden(row):
                        view.setFocus()
                        view.setCurrentIndex(model.index(row, 0))
                        return

    def _on_filter_toggled(self, checked: bool) -> None:
        self.project_filter_edit.setVisible(checked)
        if checked:
            self.project_filter_edit.setFocus()
        elif self.project_filter_edit.text():
            self.project_filter_edit.clear()

    def sync_filter_visibility(self) -> None:
        """Hide the filter field again when something else cleared its text."""
        has_text = bool(self.project_filter_edit.text())
        if not has_text and not self.project_filter_edit.hasFocus() and self.filter_button.isChecked():
            self.filter_button.blockSignals(True)
            self.filter_button.setChecked(False)
            self.filter_button.blockSignals(False)
            self.project_filter_edit.setVisible(False)

    # ----- new project
    def begin_new_project(self) -> None:
        self.new_project_item.setVisible(False)
        self.new_project_edit.clear()
        self.new_project_edit.setVisible(True)
        self.new_project_edit.setFocus()
        self.scroll.ensureWidgetVisible(self.new_project_edit)

    def cancel_new_project(self) -> None:
        self.new_project_edit.setVisible(False)
        self.new_project_item.setVisible(True)

    def _submit_new_project(self) -> None:
        title = self.new_project_edit.text().strip()
        self.cancel_new_project()
        if title:
            self.newProjectSubmitted.emit(title)

    # ----- hidden section
    def set_hidden_count(self, count: int) -> None:
        self.hidden_toggle.set_text(f"Hidden · {count}")
        self.hidden_toggle.setVisible(count > 0)
        self.hidden_view.setVisible(count > 0 and self.hidden_toggle.is_expanded())

    def set_hidden_expanded(self, expanded: bool) -> None:
        self.hidden_toggle.set_expanded(expanded)
        self.hidden_view.setVisible(expanded and self.hidden_toggle.isVisible())

    def _on_hidden_toggled(self, expanded: bool) -> None:
        self.hidden_view.setVisible(expanded)
        self.hiddenToggled.emit(expanded)


class FilterEdit(QLineEdit):
    """Project name filter: Esc closes it, Enter opens the first match, Down moves into the list."""
    escaped = pyqtSignal()
    accepted = pyqtSignal()
    downPressed = pyqtSignal()

    def keyPressEvent(self, event):
        key = event.key()
        if key == Qt.Key.Key_Escape:
            self.escaped.emit()
            return
        if key in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
            self.accepted.emit()
            return
        if key == Qt.Key.Key_Down:
            self.downPressed.emit()
            return
        super().keyPressEvent(event)


class _NewProjectEdit(QLineEdit):
    submitted = pyqtSignal()
    cancelled = pyqtSignal()

    def keyPressEvent(self, event):
        if event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
            self.submitted.emit()
            return
        if event.key() == Qt.Key.Key_Escape:
            self.cancelled.emit()
            return
        super().keyPressEvent(event)

    def focusOutEvent(self, event):
        super().focusOutEvent(event)
        # Clicking elsewhere keeps what was typed, the same as pressing Enter
        if self.isVisible():
            self.submitted.emit()
