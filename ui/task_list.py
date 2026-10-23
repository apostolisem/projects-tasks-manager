"""Task list: single-column view, row delegate, list header, quick-add row and empty state.

The view shows TaskTableModel directly (one view row per model row). Group headers are painted
by the delegate above the first row of each group, so model row numbers stay usable everywhere.
The model has to provide:
    row_context(row) -> dict      title, done, pinned, waiting, mom, due_label, due_state,
                                  repeat_label, people, project_title, grip
    group_header(row) -> dict|None  key, label, count, collapsible, collapsed (first row of a group)
    is_row_collapsed(row) -> bool   row belongs to a collapsed group
    toggle_group(key), toggle_task_flag(row, flag), reorder_limit_row()
"""
from __future__ import annotations
import datetime
from typing import Dict, List, Optional, Tuple

from PyQt6.QtCore import QEvent, QItemSelectionModel, QModelIndex, QPoint, QPointF, QRect, QSize, Qt, pyqtSignal
from PyQt6.QtGui import QColor, QFont, QFontMetrics, QPainter, QPen, QTextCharFormat, QTextLayout, QTextOption
from PyQt6.QtWidgets import (QAbstractItemView, QFrame, QHBoxLayout, QLabel, QLineEdit, QListView, QMenu,
                             QPushButton, QSizePolicy, QStyle, QToolButton, QToolTip, QVBoxLayout, QWidget)

from . import tokens as T
from .delegates import ExpandingLineEditDelegate
from .icons import icon, pixmap
from .painting import draw_corner_marks
from .theme import body_font, heading_font
from .widgets import KbdLabel
from .search import utf16_offset

# ---- geometry (README "Task row" / "Grouping") ----
INSET_L = 14
INSET_R = 14
PAD_T = 8
PAD_B = 8
PAD_L = 10
PAD_R = 8
GAP = 10
CHECK = 18
FLAG = 26
FLAGS_W = FLAG * 2 + 2
GRIP = 14
TITLE_PX = 15
TITLE_LINE = 20
META_PX = 12
META_LINE = 16
META_ICON = 12
META_GAP_X = 12
META_GAP_Y = 4
MOM_TAG_H = 16
HEADER_PAD_T = 14
HEADER_LINE = 16
HEADER_H = HEADER_PAD_T + HEADER_LINE + 3 + 1 + 4
GROUP_GAP = 7
FLAG_HOVER_TINT = QColor(89, 128, 166, 31)   # accent @ 12%

WEEKDAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]


def parse_due_date(due: Optional[str]) -> Optional[datetime.date]:
    """Date part of a stored due date ("YYYY-MM-DD" or "YYYY-MM-DD HH:MM:SS")."""
    if not due:
        return None
    try:
        return datetime.date.fromisoformat(str(due).strip()[:10])
    except ValueError:
        return None


def due_info(due: Optional[str], today: Optional[datetime.date] = None) -> Tuple[str, str]:
    """(label, state) for a due date. state is 'overdue', 'today', 'future' or '' when unset."""
    date = parse_due_date(due)
    if date is None:
        return "", ""
    today = today or datetime.date.today()
    delta = (date - today).days
    if delta == 0:
        return "Today", "today"
    state = "overdue" if delta < 0 else "future"
    if delta == -1:
        return "Yesterday", state
    if delta == 1:
        return "Tomorrow", state
    if 1 < delta < 7:
        return f"{WEEKDAYS[date.weekday()]} {date.day}", state
    label = f"{MONTHS[date.month - 1]} {date.day}"
    if date.year != today.year:
        label += f", {date.year}"
    return label, state


class _RowLayout:
    __slots__ = ("header_h", "row_h", "title_lines", "title_h", "meta_items", "meta_h", "tag_w", "title_layout", "excerpt_layout", "excerpt_h")


class TaskRowDelegate(ExpandingLineEditDelegate):
    """Paints a task row: complete box, title (+ MoM tag), meta line and the focus / wait flags."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self._title_font = body_font(TITLE_PX)
        self._title_font_done = body_font(TITLE_PX)
        self._title_font_done.setStrikeOut(True)
        self._meta_font = body_font(META_PX)
        self._meta_font_bold = body_font(META_PX, 700)
        self._tag_font = body_font(10, 500)
        self._tag_font.setLetterSpacing(QFont.SpacingType.AbsoluteSpacing, 0.6)
        self._header_font = heading_font(12, 0.09)
        self._count_font = body_font(12)
        self._cache: Dict[tuple, _RowLayout] = {}

    @staticmethod
    def _formats(text, spans):
        formats = []
        for start, end in spans:
            item = QTextLayout.FormatRange()
            item.start = utf16_offset(text, start)
            item.length = utf16_offset(text, end) - item.start
            item.format = QTextCharFormat()
            item.format.setFontWeight(QFont.Weight.Bold)
            formats.append(item)
        return formats

    def clear_cache(self):
        self._cache.clear()

    # ----- geometry -----
    def _view_width(self, option) -> int:
        view = option.widget
        if view is not None and hasattr(view, "viewport"):
            return view.viewport().width()
        return option.rect.width()

    def _text_left(self, ctx: dict) -> int:
        x = INSET_L + PAD_L
        if ctx.get("grip"):
            x += GRIP + GAP
        return x + CHECK + GAP

    def _text_width(self, ctx: dict, width: int) -> int:
        return max(60, width - INSET_R - PAD_R - FLAGS_W - GAP - self._text_left(ctx))

    def _meta_parts(self, ctx: dict) -> List[Tuple[Optional[str], str, str]]:
        """[(icon name, text, style)] for the meta line; style is '', 'overdue' or 'today'."""
        parts = []
        if ctx.get("project_title"):
            parts.append(("folder", ctx["project_title"], ""))
        if ctx.get("due_label") and not ctx.get("done"):
            state = ctx.get("due_state", "")
            parts.append(("alert" if state == "overdue" else "cal", ctx["due_label"], state if state in ("overdue", "today") else ""))
        if ctx.get("repeat_label"):
            parts.append(("repeat", ctx["repeat_label"], ""))
        if ctx.get("people"):
            parts.append((None, "@" + ", @".join(ctx["people"]), ""))
        if ctx.get("lingering"):
            parts.append(("arrow", "Leaves this view", ""))
        return parts

    def _layout(self, model, row: int, width: int) -> _RowLayout:
        ctx = model.row_context(row)
        header = model.group_header(row)
        collapsed = model.is_row_collapsed(row)
        key = (ctx["id"], width, row == 0, bool(header), collapsed, ctx["title"], ctx.get("mom"), ctx.get("grip"),
               ctx.get("done"), ctx.get("lingering"), ctx.get("project_title"), ctx.get("due_label"),
               ctx.get("repeat_label"), tuple(ctx.get("people") or ()), tuple(ctx.get("title_spans") or ()),
               repr(ctx.get("excerpt")))
        cached = self._cache.get(key)
        if cached is not None:
            return cached
        if len(self._cache) > 5000:
            self._cache.clear()

        layout = _RowLayout()
        layout.header_h = 0
        if header is not None:
            layout.header_h = HEADER_H + (0 if row == 0 else GROUP_GAP)
        elif row == 0:
            layout.header_h = GROUP_GAP
        layout.tag_w = 0
        layout.title_lines = []
        layout.title_h = 0
        layout.meta_items = []
        layout.meta_h = 0
        layout.row_h = 0
        layout.title_layout = None
        layout.excerpt_layout = None
        layout.excerpt_h = 0
        if collapsed:
            self._cache[key] = layout
            return layout

        text_w = self._text_width(ctx, width)
        if ctx.get("mom"):
            layout.tag_w = QFontMetrics(self._tag_font).horizontalAdvance("MoM") + 10 + 7

        # Title: word-wrapped, first line shortened by the MoM tag
        text_layout = QTextLayout(ctx["title"], self._title_font_done if ctx.get("done") else self._title_font)
        text_layout.setFormats(self._formats(ctx["title"], ctx.get("title_spans", [])))
        layout.title_layout = text_layout
        option = QTextOption()
        option.setWrapMode(QTextOption.WrapMode.WrapAtWordBoundaryOrAnywhere)
        text_layout.setTextOption(option)
        text_layout.beginLayout()
        while True:
            line = text_layout.createLine()
            if not line.isValid():
                break
            indent = layout.tag_w if not layout.title_lines else 0
            line.setLineWidth(max(20, text_w - indent))
            line.setPosition(QPointF(indent, len(layout.title_lines) * TITLE_LINE + (TITLE_LINE - line.height()) / 2))
            layout.title_lines.append((line.textStart(), line.textLength(), indent))
        text_layout.endLayout()
        if not layout.title_lines:
            layout.title_lines.append((0, 0, layout.tag_w))
        layout.title_h = len(layout.title_lines) * TITLE_LINE

        if ctx.get("excerpt"):
            text, spans = ctx["excerpt"]
            # Reserve enough width even where the matching letters are bold.
            text = QFontMetrics(self._meta_font_bold).elidedText(text, Qt.TextElideMode.ElideRight, text_w)
            spans = [(a, min(b, len(text.rstrip("…")))) for a, b in spans if a < len(text.rstrip("…"))]
            layout.excerpt_layout = QTextLayout(text, self._meta_font)
            layout.excerpt_layout.setFormats(self._formats(text, spans))
            layout.excerpt_layout.beginLayout()
            line = layout.excerpt_layout.createLine()
            if line.isValid():
                line.setLineWidth(text_w)
                line.setPosition(QPointF(0, (META_LINE - line.height()) / 2))
            layout.excerpt_layout.endLayout()
            layout.excerpt_h = META_LINE + 3

        # Meta line: flow layout with wrapping
        x = y = 0
        for icon_name, text, style in self._meta_parts(ctx):
            font = self._meta_font_bold if style else self._meta_font
            item_w = QFontMetrics(font).horizontalAdvance(text) + ((META_ICON + 4) if icon_name else 0)
            item_w = min(item_w, text_w)
            if x and x + item_w > text_w:
                x = 0
                y += META_LINE + META_GAP_Y
            layout.meta_items.append((x, y, item_w, icon_name, text, style))
            x += item_w + META_GAP_X
        if layout.meta_items:
            layout.meta_h = y + META_LINE

        content = layout.title_h + layout.excerpt_h + ((3 + layout.meta_h) if layout.meta_items else 0)
        layout.row_h = PAD_T + max(content, CHECK + 2) + PAD_B
        self._cache[key] = layout
        return layout

    def sizeHint(self, option, index):
        width = self._view_width(option)
        layout = self._layout(index.model(), index.row(), width)
        height = layout.header_h + layout.row_h
        if index.model().is_row_collapsed(index.row()):
            height = layout.header_h + 4
        return QSize(width, max(height, 1))

    def row_rect(self, item_rect: QRect, model, row: int) -> QRect:
        """The framed row area inside item_rect (below the group header, inside the list insets)."""
        layout = self._layout(model, row, item_rect.width())
        return QRect(item_rect.left() + INSET_L, item_rect.top() + layout.header_h,
                     item_rect.width() - INSET_L - INSET_R, layout.row_h)

    def _check_rect(self, row_rect: QRect, ctx: dict) -> QRect:
        x = row_rect.left() + PAD_L + ((GRIP + GAP) if ctx.get("grip") else 0)
        return QRect(x, row_rect.top() + PAD_T + 1, CHECK, CHECK)

    def _flag_rects(self, row_rect: QRect) -> Tuple[QRect, QRect]:
        x = row_rect.right() + 1 - PAD_R - FLAGS_W
        y = row_rect.top() + PAD_T - 3
        return QRect(x, y, FLAG, FLAG), QRect(x + FLAG + 2, y, FLAG, FLAG)

    def hit_test(self, item_rect: QRect, model, row: int, pos: QPoint) -> Optional[str]:
        """'header', 'check', 'pin', 'wait' or None for a position inside item_rect."""
        layout = self._layout(model, row, item_rect.width())
        if layout.header_h and pos.y() < item_rect.top() + layout.header_h:
            return "header"
        if model.is_row_collapsed(row):
            return "header"
        ctx = model.row_context(row)
        row_rect = self.row_rect(item_rect, model, row)
        if self._check_rect(row_rect, ctx).adjusted(-4, -4, 4, 4).contains(pos):
            return "check"
        pin_rect, wait_rect = self._flag_rects(row_rect)
        if pin_rect.contains(pos):
            return "pin"
        if wait_rect.contains(pos):
            return "wait"
        return None

    # ----- painting -----
    def paint(self, painter, option, index):
        model = index.model()
        row = index.row()
        view = option.widget
        item_rect = QRect(option.rect)
        ctx = model.row_context(row)
        layout = self._layout(model, row, item_rect.width())
        scale = painter.device().devicePixelRatioF() if painter.device() else 1.0

        painter.save()
        painter.setClipRect(item_rect)

        header = model.group_header(row)
        if header is not None:
            self._paint_header(painter, item_rect, layout, header, scale)
        if model.is_row_collapsed(row):
            painter.restore()
            return

        row_rect = self.row_rect(item_rect, model, row)
        selected = bool(option.state & QStyle.StateFlag.State_Selected)
        hover_pos = getattr(view, "hover_pos", None)
        hovered = bool(option.state & QStyle.StateFlag.State_MouseOver) and hover_pos is not None and row_rect.contains(hover_pos)
        hit = self.hit_test(item_rect, model, row, hover_pos) if hovered else None

        if selected:
            painter.fillRect(row_rect, QColor(T.ACCENT_100))
            painter.setPen(QPen(QColor(T.ACCENT_400), 1))
            painter.drawRect(row_rect.adjusted(0, 0, -1, -1))
        elif hovered:
            painter.fillRect(row_rect, QColor(T.HOVER))

        # grip (manual sort)
        if ctx.get("grip"):
            painter.drawPixmap(row_rect.left() + PAD_L, row_rect.top() + PAD_T + 3,
                               pixmap("grip", GRIP, T.NEUTRAL_500, scale=scale))

        # complete box
        check_rect = self._check_rect(row_rect, ctx)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, False)
        if ctx.get("done"):
            painter.fillRect(check_rect, QColor(T.ACCENT))
            painter.drawPixmap(check_rect.left() + 3, check_rect.top() + 3,
                               pixmap("check", 12, "#ffffff", scale=scale, stroke=2.5))
        else:
            check_hover = hit == "check"
            if check_hover:
                painter.fillRect(check_rect, QColor(T.ACCENT_100))
            pen = QPen(QColor(T.ACCENT if check_hover else T.NEUTRAL_500))
            pen.setWidthF(1.5)
            pen.setJoinStyle(Qt.PenJoinStyle.MiterJoin)
            painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
            painter.setPen(pen)
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.drawRect(check_rect.adjusted(0, 0, -1, -1).toRectF().adjusted(0.25, 0.25, 0.25, 0.25))
            painter.setRenderHint(QPainter.RenderHint.Antialiasing, False)

        # title (+ MoM tag)
        text_x = item_rect.left() + self._text_left(ctx)
        text_y = row_rect.top() + PAD_T
        title_font = self._title_font_done if ctx.get("done") else self._title_font
        title_color = QColor(T.NEUTRAL_600 if ctx.get("done") or ctx.get("lingering") else T.TEXT)
        if layout.tag_w:
            tag_w = layout.tag_w - 7
            tag_rect = QRect(text_x, text_y + (TITLE_LINE - MOM_TAG_H) // 2, tag_w, MOM_TAG_H)
            painter.setPen(QPen(QColor(T.ACCENT_700), 1))
            painter.drawRect(tag_rect.adjusted(0, 0, -1, -1))
            painter.setFont(self._tag_font)
            painter.drawText(tag_rect, Qt.AlignmentFlag.AlignCenter, "MoM")
        painter.setFont(title_font)
        painter.setPen(title_color)
        layout.title_layout.draw(painter, QPointF(text_x, text_y))
        if layout.excerpt_layout is not None:
            painter.setPen(QColor(T.NEUTRAL_700))
            layout.excerpt_layout.draw(painter, QPointF(text_x, text_y + layout.title_h + 3))

        # meta line
        meta_y = text_y + layout.title_h + layout.excerpt_h + 3
        for x, y, item_w, icon_name, text, style in layout.meta_items:
            if style == "overdue":
                color, font = T.ACCENT_800, self._meta_font_bold
            elif style == "today":
                color, font = T.ACCENT_700, self._meta_font_bold
            else:
                color, font = T.NEUTRAL_700, self._meta_font
            item_x = text_x + x
            if icon_name:
                painter.drawPixmap(item_x, meta_y + y + (META_LINE - META_ICON) // 2,
                                   pixmap(icon_name, META_ICON, color, scale=scale))
                item_x += META_ICON + 4
            painter.setFont(font)
            painter.setPen(QColor(color))
            avail = item_w - ((META_ICON + 4) if icon_name else 0)
            elided = QFontMetrics(font).elidedText(text, Qt.TextElideMode.ElideRight, avail + 1)
            painter.drawText(QRect(item_x, meta_y + y, avail + 2, META_LINE),
                             Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft, elided)

        # flags: shown on hover / selection, or while the flag is on
        pin_rect, wait_rect = self._flag_rects(row_rect)
        for name, icon_name, rect, on in (("pin", "target", pin_rect, ctx.get("pinned")), ("wait", "wait", wait_rect, ctx.get("waiting"))):
            if not (on or hovered or selected):
                continue
            flag_hover = hit == name
            if flag_hover:
                painter.fillRect(rect, FLAG_HOVER_TINT)
            if on:
                pm = pixmap(icon_name, 15, T.ACCENT_700, fill=T.ACCENT_300, scale=scale)
            else:
                pm = pixmap(icon_name, 15, T.ACCENT_700 if flag_hover else T.NEUTRAL_500, scale=scale)
            painter.drawPixmap(rect.left() + (FLAG - 15) // 2, rect.top() + (FLAG - 15) // 2, pm)

        painter.restore()

    def _paint_header(self, painter, item_rect: QRect, layout: _RowLayout, header: dict, scale: float):
        top = item_rect.top() + layout.header_h - HEADER_H
        line_rect = QRect(item_rect.left() + INSET_L + 2, top + HEADER_PAD_T, item_rect.width() - INSET_L - INSET_R - 4, HEADER_LINE)
        x = line_rect.left()
        if header.get("collapsible"):
            chevron = pixmap("chevr" if header.get("collapsed") else "chev", 13, T.NEUTRAL_700, scale=scale)
            painter.drawPixmap(x, line_rect.top() + (HEADER_LINE - 13) // 2, chevron)
            x += 13 + 6
        label = str(header.get("label", "")).upper()
        count = str(header.get("count", ""))
        count_w = QFontMetrics(self._count_font).horizontalAdvance(count)
        avail = line_rect.right() - x - 6 - count_w
        painter.setFont(self._header_font)
        painter.setPen(QColor(T.NEUTRAL_700))
        metrics = QFontMetrics(self._header_font)
        label = metrics.elidedText(label, Qt.TextElideMode.ElideRight, max(20, avail))
        painter.drawText(QRect(x, line_rect.top(), avail + 4, HEADER_LINE),
                         Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft, label)
        x += metrics.horizontalAdvance(label) + 6
        painter.setFont(self._count_font)
        painter.setPen(QColor(T.NEUTRAL_600))
        painter.drawText(QRect(x, line_rect.top(), count_w + 4, HEADER_LINE),
                         Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft, count)
        divider_y = line_rect.bottom() + 1 + 3
        painter.fillRect(QRect(item_rect.left() + INSET_L, divider_y, item_rect.width() - INSET_L - INSET_R, 1), QColor(T.DIVIDER))

    # ----- inline title editor -----
    def createEditor(self, parent, option, index):
        editor = super().createEditor(parent, option, index)
        editor.setFont(self._title_font)
        editor.setStyleSheet(f"QLineEdit{{padding:1px 4px;border:1px solid {T.ACCENT};background:{T.BG};}}")
        return editor

    def updateEditorGeometry(self, editor, option, index):
        model = index.model()
        ctx = model.row_context(index.row())
        row_rect = self.row_rect(QRect(option.rect), model, index.row())
        left = option.rect.left() + self._text_left(ctx) - 5
        editor.setGeometry(QRect(left, row_rect.top() + PAD_T - 4, row_rect.right() - PAD_R - left, TITLE_LINE + 8))

    def helpEvent(self, event, view, option, index):
        if event.type() == QEvent.Type.ToolTip and index.isValid():
            model = index.model()
            hit = self.hit_test(QRect(option.rect), model, index.row(), event.pos())
            ctx = model.row_context(index.row())
            text = {
                "check": "Mark as not done" if ctx.get("done") else "Mark as complete (Ctrl+Enter)",
                "pin": "Remove from focus" if ctx.get("pinned") else "Add to focus",
                "wait": "No longer waiting" if ctx.get("waiting") else "Mark as waiting",
            }.get(hit)
            if text:
                QToolTip.showText(event.globalPos(), text, view)
                return True
        return super().helpEvent(event, view, option, index)


class EmptyState(QWidget):
    """Centered title + body shown when a list or the detail pane has nothing to show."""

    def __init__(self, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self.title = QLabel("", self)
        self.title.setFont(heading_font(20))
        self.title.setAlignment(Qt.AlignmentFlag.AlignHCenter)
        self.body = QLabel("", self)
        self.body.setFont(body_font(14))
        self.body.setStyleSheet(f"color:{T.NEUTRAL_700}")
        self.body.setWordWrap(True)
        self.body.setAlignment(Qt.AlignmentFlag.AlignHCenter)
        self.body.setMaximumWidth(340)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(14, 27, 14, 27)
        layout.setSpacing(4)
        layout.addWidget(self.title, 0, Qt.AlignmentFlag.AlignHCenter)
        layout.addWidget(self.body, 0, Qt.AlignmentFlag.AlignHCenter)
        layout.addStretch(1)

    def set_text(self, title: str, body: str):
        self.title.setText(title)
        self.body.setText(body)


class TaskListView(QListView):
    """Single-column task list with group headers, collapsible groups and manual reordering."""
    selectionLost = pyqtSignal()

    def __init__(self, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self.setObjectName("TaskList")
        self.setFrameShape(QFrame.Shape.NoFrame)
        self.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.setVerticalScrollMode(QAbstractItemView.ScrollMode.ScrollPerPixel)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setResizeMode(QListView.ResizeMode.Adjust)
        self.setUniformItemSizes(False)
        self.setSpacing(0)
        self.setMouseTracking(True)
        self.viewport().setAttribute(Qt.WidgetAttribute.WA_Hover, True)
        self.setEditTriggers(QAbstractItemView.EditTrigger.DoubleClicked | QAbstractItemView.EditTrigger.EditKeyPressed)
        self.setDropIndicatorShown(False)
        self.setDragEnabled(True)
        self.setAcceptDrops(True)
        # Drags always start (a task can be dropped on a sidebar project); dropping back into the
        # list reorders only while manual_reorder is set
        self.setDragDropMode(QAbstractItemView.DragDropMode.DragDrop)
        self.setDefaultDropAction(Qt.DropAction.MoveAction)
        self.manual_reorder = False

        self.hover_pos: Optional[QPoint] = None
        self._pressed_hit: Optional[Tuple[int, str]] = None
        self._drop_row = -1
        self._saved_current: Optional[int] = None
        self._saved_selected: List[int] = []
        self._layout_width = -1

        self.empty_state = EmptyState(self.viewport())
        self.empty_state.hide()

    # ----- model wiring -----
    def setModel(self, model):
        super().setModel(model)
        if model is None:
            return
        model.modelAboutToBeReset.connect(self._remember_selection)
        model.modelReset.connect(self._after_reset)
        if hasattr(model, "groupsChanged"):
            model.groupsChanged.connect(self.sync_collapsed)
        self._after_reset()

    def _delegate(self) -> Optional[TaskRowDelegate]:
        delegate = self.itemDelegate()
        return delegate if isinstance(delegate, TaskRowDelegate) else None

    def current_task_id(self) -> Optional[int]:
        index = self.currentIndex()
        return self.model().task_id_at(index.row()) if index.isValid() and self.model() is not None else None

    def _remember_selection(self):
        model = self.model()
        self._saved_current = self.current_task_id()
        selection = self.selectionModel()
        rows = [idx.row() for idx in selection.selectedRows()] if selection is not None else []
        self._saved_selected = [tid for tid in (model.task_id_at(row) for row in rows) if tid is not None]

    def _after_reset(self):
        # The delegate's layout cache is keyed by row content, so it stays valid across reloads
        self.sync_collapsed()
        self._restore_selection()
        self._sync_empty_state()

    def _restore_selection(self):
        """Keep the same task selected across a model reset without re-firing selection handlers."""
        model = self.model()
        selection = self.selectionModel()
        saved_current, saved_selected = self._saved_current, self._saved_selected
        self._saved_current, self._saved_selected = None, []
        if model is None or selection is None or saved_current is None:
            return
        row_by_id = {model.task_id_at(row): row for row in range(model.rowCount())}
        if saved_current not in row_by_id:
            self.selectionLost.emit()
            return
        selection.blockSignals(True)
        try:
            for tid in saved_selected:
                if tid in row_by_id:
                    selection.select(model.index(row_by_id[tid], 0), QItemSelectionModel.SelectionFlag.Select)
            current = model.index(row_by_id[saved_current], 0)
            selection.select(current, QItemSelectionModel.SelectionFlag.Select)
            selection.setCurrentIndex(current, QItemSelectionModel.SelectionFlag.NoUpdate)
        finally:
            selection.blockSignals(False)
        self.viewport().update()

    def sync_collapsed(self, *_):
        """Hide the rows of collapsed groups; their first row stays as the header carrier."""
        model = self.model()
        if model is None:
            return
        for row in range(model.rowCount()):
            hide = model.is_row_collapsed(row) and model.group_header(row) is None
            if self.isRowHidden(row) != hide:
                self.setRowHidden(row, hide)
        self.scheduleDelayedItemsLayout()
        self.viewport().update()

    def _sync_empty_state(self):
        model = self.model()
        empty = model is None or model.rowCount() == 0
        self.empty_state.setVisible(empty)
        if empty:
            self.empty_state.setGeometry(self.viewport().rect())
            self.empty_state.raise_()

    def set_empty_text(self, title: str, body: str):
        self.empty_state.set_text(title, body)

    # ----- helpers used by the main window -----
    def selectRow(self, row: int):
        """QTableView-compatible helper: make row the only selected row."""
        model = self.model()
        if model is None or not (0 <= row < model.rowCount()):
            return
        self.selectionModel().setCurrentIndex(model.index(row, 0), QItemSelectionModel.SelectionFlag.ClearAndSelect)

    def is_row_selectable(self, row: int) -> bool:
        model = self.model()
        return model is not None and 0 <= row < model.rowCount() and not model.is_row_collapsed(row)

    def visible_rows(self) -> List[int]:
        model = self.model()
        if model is None:
            return []
        return [row for row in range(model.rowCount()) if not model.is_row_collapsed(row)]

    def neighbour_row(self, row: int, step: int) -> int:
        """Next selectable row from row in direction step (+1 / -1); -1 when there is none."""
        rows = self.visible_rows()
        if not rows:
            return -1
        if row not in rows:
            later = [r for r in rows if r > row]
            earlier = [r for r in rows if r < row]
            if step > 0:
                return later[0] if later else -1
            return earlier[-1] if earlier else -1
        i = rows.index(row) + step
        return rows[i] if 0 <= i < len(rows) else -1

    # ----- events -----
    def resizeEvent(self, event):
        super().resizeEvent(event)
        if self.empty_state.isVisible():
            self.empty_state.setGeometry(self.viewport().rect())
        # Row heights depend on the width (wrapping titles and meta lines)
        width = self.viewport().width()
        if width != self._layout_width:
            self._layout_width = width
            delegate = self._delegate()
            if delegate is not None:
                delegate.clear_cache()
            self.scheduleDelayedItemsLayout()

    def _hit(self, pos: QPoint) -> Tuple[QModelIndex, Optional[str]]:
        index = self.indexAt(pos)
        delegate = self._delegate()
        if not index.isValid() or delegate is None:
            return index, None
        return index, delegate.hit_test(self.visualRect(index), self.model(), index.row(), pos)

    def mouseMoveEvent(self, event):
        self.hover_pos = event.position().toPoint()
        self.viewport().update()
        if self._pressed_hit is not None:
            return
        super().mouseMoveEvent(event)

    def leaveEvent(self, event):
        self.hover_pos = None
        self.viewport().update()
        super().leaveEvent(event)

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            index, hit = self._hit(event.position().toPoint())
            if hit is not None:
                # Clicks on the header, the complete box and the flags never change the selection
                self._pressed_hit = (index.row(), hit)
                event.accept()
                return
        self._pressed_hit = None
        super().mousePressEvent(event)

    def mouseReleaseEvent(self, event):
        pressed, self._pressed_hit = self._pressed_hit, None
        if pressed is not None and event.button() == Qt.MouseButton.LeftButton:
            index, hit = self._hit(event.position().toPoint())
            if index.isValid() and (index.row(), hit) == pressed:
                self._activate_hit(index.row(), hit)
            event.accept()
            return
        super().mouseReleaseEvent(event)

    def mouseDoubleClickEvent(self, event):
        index, hit = self._hit(event.position().toPoint())
        if hit is not None:
            self.mousePressEvent(event)
            return
        super().mouseDoubleClickEvent(event)

    def _activate_hit(self, row: int, hit: str):
        model = self.model()
        if hit == "header":
            header = model.group_header(row)
            if header is not None and header.get("collapsible"):
                model.toggle_group(header["key"])
        elif hit == "check":
            model.toggle_task_flag(row, "done")
        elif hit == "pin":
            model.toggle_task_flag(row, "pinned")
        elif hit == "wait":
            model.toggle_task_flag(row, "waiting")

    def moveCursor(self, action, modifiers):
        index = super().moveCursor(action, modifiers)
        model = self.model()
        if index.isValid() and model is not None and model.is_row_collapsed(index.row()):
            return self.currentIndex()
        return index

    # ----- manual reordering -----
    def set_manual_reorder(self, enabled: bool) -> None:
        self.manual_reorder = bool(enabled)

    def _drop_row_at(self, pos: QPoint) -> int:
        model = self.model()
        limit = model.reorder_limit_row()
        index = self.indexAt(pos)
        if not index.isValid():
            return limit
        rect = self.visualRect(index)
        row = index.row() if pos.y() < rect.center().y() else index.row() + 1
        return max(0, min(row, limit))

    def _accepts(self, event) -> bool:
        return (self.manual_reorder
                and event.source() is self and event.mimeData().hasFormat("application/x-task-row"))

    def dragEnterEvent(self, event):
        if self._accepts(event):
            event.acceptProposedAction()
        else:
            event.ignore()

    def dragMoveEvent(self, event):
        if not self._accepts(event):
            event.ignore()
            return
        self._drop_row = self._drop_row_at(event.position().toPoint())
        self.viewport().update()
        event.acceptProposedAction()

    def dragLeaveEvent(self, event):
        self._drop_row = -1
        self.viewport().update()
        super().dragLeaveEvent(event)

    def dropEvent(self, event):
        drop_row, self._drop_row = self._drop_row, -1
        self.viewport().update()
        if not self._accepts(event) or drop_row < 0:
            event.ignore()
            return
        self.model().dropMimeData(event.mimeData(), Qt.DropAction.MoveAction, drop_row, 0, QModelIndex())
        # The model reorders and reloads itself; stop Qt from removing the source rows
        event.setDropAction(Qt.DropAction.IgnoreAction)
        event.accept()

    def paintEvent(self, event):
        super().paintEvent(event)
        model = self.model()
        delegate = self._delegate()
        if model is None or delegate is None:
            return
        painter = QPainter(self.viewport())
        # Registration marks on selected rows, drawn last so neighbours cannot cover them
        selection = self.selectionModel()
        if selection is not None:
            for index in selection.selectedRows():
                if self.isRowHidden(index.row()) or model.is_row_collapsed(index.row()):
                    continue
                rect = self.visualRect(index)
                if rect.isValid() and rect.intersects(self.viewport().rect().adjusted(0, -8, 0, 8)):
                    draw_corner_marks(painter, delegate.row_rect(rect, model, index.row()), T.ACCENT_700)
        # 2px drop indicator while reordering
        if self._drop_row >= 0:
            if self._drop_row < model.rowCount():
                rect = self.visualRect(model.index(self._drop_row, 0))
                y = delegate.row_rect(rect, model, self._drop_row).top()
            elif model.rowCount() > 0:
                last = model.rowCount() - 1
                y = delegate.row_rect(self.visualRect(model.index(last, 0)), model, last).bottom() + 1
            else:
                y = 0
            painter.fillRect(QRect(INSET_L, y - 1, self.viewport().width() - INSET_L - INSET_R, 2), QColor(T.ACCENT))
        painter.end()


class SegmentedControl(QWidget):
    """Square segmented control (Auto | Manual). API mirrors the QComboBox bits the app uses."""
    currentIndexChanged = pyqtSignal(int)

    def __init__(self, labels: List[str], parent: Optional[QWidget] = None):
        super().__init__(parent)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        self._buttons: List[QPushButton] = []
        for i, label in enumerate(labels):
            button = QPushButton(label, self)
            button.setProperty("seg", True)
            button.setCheckable(True)
            button.setCursor(Qt.CursorShape.PointingHandCursor)
            button.setFocusPolicy(Qt.FocusPolicy.TabFocus)
            button.setFont(body_font(12.5))
            if i:
                button.setStyleSheet("margin-left:-1px")
            button.clicked.connect(lambda _checked=False, idx=i: self._on_clicked(idx))
            layout.addWidget(button)
            self._buttons.append(button)
        self._index = 0
        self._apply()

    def _apply(self):
        for i, button in enumerate(self._buttons):
            button.setChecked(i == self._index)

    def _on_clicked(self, index: int):
        changed = index != self._index
        self._index = index
        self._apply()
        if changed and not self.signalsBlocked():
            self.currentIndexChanged.emit(index)

    def currentIndex(self) -> int:
        return self._index

    def setCurrentIndex(self, index: int):
        if index == self._index:
            return
        self._index = index
        self._apply()
        if not self.signalsBlocked():
            self.currentIndexChanged.emit(index)


class ElidedLabel(QLabel):
    """Single-line label that elides its text instead of forcing the pane wider."""

    def __init__(self, text: str = "", parent: Optional[QWidget] = None):
        super().__init__(parent)
        self._full_text = text
        self.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Fixed)
        self.setText(text)

    def setText(self, text: str):
        self._full_text = text or ""
        self.setToolTip(self._full_text)
        self._update_elision()

    def text(self) -> str:
        return self._full_text

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._update_elision()

    def _update_elision(self):
        elided = self.fontMetrics().elidedText(self._full_text, Qt.TextElideMode.ElideRight, max(10, self.width()))
        super().setText(elided)

    def sizeHint(self):
        hint = super().sizeHint()
        return QSize(self.fontMetrics().horizontalAdvance(self._full_text) + 2, hint.height())

    def minimumSizeHint(self):
        return QSize(20, super().minimumSizeHint().height())


class TaskListHeader(QWidget):
    """List title + subtitle, with the Auto | Manual control and the project menu on the right."""

    def __init__(self, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self.title_label = ElidedLabel("")
        self.title_label.setObjectName("ListTitle")
        self.title_label.setFont(heading_font(26, -0.015))
        self.subtitle_label = QLabel("")
        self.subtitle_label.setObjectName("Subtitle")
        self.subtitle_label.setFont(body_font(13))

        self._subtitle_row = QHBoxLayout()
        self._subtitle_row.setContentsMargins(0, 0, 0, 0)
        self._subtitle_row.setSpacing(10)
        self._subtitle_row.addWidget(self.subtitle_label)
        self._subtitle_row.addStretch(1)

        text_col = QVBoxLayout()
        text_col.setContentsMargins(0, 0, 0, 0)
        text_col.setSpacing(0)
        text_col.addWidget(self.title_label)
        text_col.addLayout(self._subtitle_row)

        self.sort_control = SegmentedControl(["Auto", "Manual"])
        self.sort_control.setToolTip("Auto groups tasks by status; Manual lets you drag tasks into your own order")
        self.menu_button = QToolButton()
        self.menu_button.setProperty("icon-only", True)
        self.menu_button.setIcon(icon("more", 15, T.NEUTRAL_800))
        self.menu_button.setIconSize(QSize(15, 15))
        self.menu_button.setFixedSize(32, 32)
        self.menu_button.setToolTip("Project actions")
        self.menu_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.menu_button.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        self.menu_button.setStyleSheet("QToolButton{padding:0} QToolButton::menu-indicator{image:none;width:0}")
        self.project_menu = QMenu(self.menu_button)
        self.menu_button.setMenu(self.project_menu)

        self.tools = QWidget()
        tools_row = QHBoxLayout(self.tools)
        tools_row.setContentsMargins(0, 0, 0, 0)
        tools_row.setSpacing(7)
        tools_row.addWidget(self.sort_control)
        tools_row.addWidget(self.menu_button)

        layout = QHBoxLayout(self)
        layout.setContentsMargins(14, 14, 14, 10)
        layout.setSpacing(10)
        layout.addLayout(text_col, 1)
        layout.addWidget(self.tools, 0, Qt.AlignmentFlag.AlignBottom)

    def add_subtitle_widget(self, widget: QWidget):
        self._subtitle_row.insertWidget(1, widget)

    def set_texts(self, title: str, subtitle: str):
        self.title_label.setText(title)
        self.subtitle_label.setText(subtitle)

    def set_project_tools_visible(self, visible: bool):
        self.tools.setVisible(visible)


class _QuickAddEdit(QLineEdit):
    submitted = pyqtSignal(str)
    escaped = pyqtSignal()
    focusChanged = pyqtSignal(bool)
    targetRequested = pyqtSignal()  # Ctrl+P while typing: choose the project new tasks go to

    @staticmethod
    def _is_target_key(event) -> bool:
        mods = event.modifiers() & ~Qt.KeyboardModifier.KeypadModifier
        return event.key() == Qt.Key.Key_P and mods == Qt.KeyboardModifier.ControlModifier

    def event(self, event):
        # Take Ctrl+P from the window's "Go to project" shortcut while this field has focus
        if event.type() == QEvent.Type.ShortcutOverride and self._is_target_key(event):
            event.accept()
            return True
        return super().event(event)

    def keyPressEvent(self, event):
        if self._is_target_key(event):
            self.targetRequested.emit()
            return
        if event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
            text = self.text().strip()
            if text:
                self.clear()
                self.submitted.emit(text)
            return
        if event.key() == Qt.Key.Key_Escape:
            self.clear()
            self.escaped.emit()
            return
        super().keyPressEvent(event)

    def focusInEvent(self, event):
        super().focusInEvent(event)
        self.focusChanged.emit(True)

    def focusOutEvent(self, event):
        super().focusOutEvent(event)
        self.focusChanged.emit(False)


class QuickAddRow(QWidget):
    """Always-visible "add a task" input above the list.

    In smart views a chip in front of the input names the project new tasks go to; clicking
    it (or Ctrl+P in the input) asks for another project (targetRequested).
    """
    submitted = pyqtSignal(str)
    escaped = pyqtSignal()
    targetRequested = pyqtSignal()
    targetResetRequested = pyqtSignal()  # chip menu: go back to the last opened project
    TARGET_MAX_WIDTH = 180

    def __init__(self, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self.frame = QFrame(self)
        self.frame.setObjectName("QuickAddFrame")
        self.frame.setProperty("focused", False)

        plus = QLabel(self.frame)
        plus.setPixmap(pixmap("plus", 16, T.ACCENT_700, scale=self.devicePixelRatioF()))
        plus.setFixedSize(16, 16)

        self.edit = _QuickAddEdit(self.frame)
        self.edit.setObjectName("QuickAdd")
        self.edit.setFont(body_font(14))
        self.edit.submitted.connect(self.submitted.emit)
        self.edit.escaped.connect(self.escaped.emit)
        self.edit.focusChanged.connect(self._on_focus_changed)
        self.edit.targetRequested.connect(self.targetRequested.emit)

        self.target_chip = QToolButton(self.frame)
        self.target_chip.setObjectName("QuickAddTarget")
        self.target_chip.setProperty("chosen", False)
        self.target_chip.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        self.target_chip.setIcon(icon("folder", 13, T.NEUTRAL_700))
        self.target_chip.setIconSize(QSize(13, 13))
        self.target_chip.setFont(body_font(12, 500))
        self.target_chip.setCursor(Qt.CursorShape.PointingHandCursor)
        self.target_chip.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.target_chip.clicked.connect(self.targetRequested.emit)
        self.target_chip.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.target_chip.customContextMenuRequested.connect(self._show_target_menu)
        self.target_chip.setVisible(False)
        self._target_name = ""

        self.hint = KbdLabel("N", self.frame)

        row = QHBoxLayout(self.frame)
        row.setContentsMargins(10, 0, 10, 0)
        row.setSpacing(10)
        row.addWidget(plus)
        row.addWidget(self.target_chip)
        row.addWidget(self.edit, 1)
        row.addWidget(self.hint)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(14, 0, 14, 3)
        outer.addWidget(self.frame)

    def _on_focus_changed(self, focused: bool):
        self.frame.setProperty("focused", focused)
        self.frame.style().unpolish(self.frame)
        self.frame.style().polish(self.frame)
        self.hint.setVisible(not focused)

    def set_target(self, name: str, *, chip: bool = False, chosen: bool = False):
        """Project views name the project in the placeholder; smart views show it in the chip.
        chosen marks a project picked by hand rather than the automatic one; it looks the same
        and only changes the tooltip and the chip menu."""
        if not chip:
            self.target_chip.setVisible(False)
            self.edit.setPlaceholderText(f"Add a task to {name} — Enter to save")
            return
        self.edit.setPlaceholderText("Add a task — Enter to save")
        self._target_name = name
        metrics = QFontMetrics(self.target_chip.font())
        self.target_chip.setText(metrics.elidedText(name, Qt.TextElideMode.ElideRight, self.TARGET_MAX_WIDTH))
        if chosen:
            self.target_chip.setToolTip(f"New tasks in the smart views go to {name} (kept until you close the app). "
                                        "Click to choose another project (Ctrl+P); right-click to go back to the last opened project")
        else:
            self.target_chip.setToolTip(f"New tasks in the smart views go to {name}, the last project you opened. "
                                        "Click to choose another project (Ctrl+P)")
        self.target_chip.setProperty("chosen", chosen)
        self.target_chip.setVisible(True)

    def _show_target_menu(self, pos: QPoint):
        menu = QMenu(self)
        menu.addAction("Choose project…", self.targetRequested.emit)
        reset = menu.addAction("Use the last opened project", self.targetResetRequested.emit)
        reset.setEnabled(bool(self.target_chip.property("chosen")))
        menu.exec(self.target_chip.mapToGlobal(pos))

    def target_name(self) -> str:
        return self._target_name if self.target_chip.isVisible() else ""

    def focus_input(self):
        self.edit.setFocus(Qt.FocusReason.ShortcutFocusReason)
