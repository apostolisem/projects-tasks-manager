"""Task detail panel: header actions, title, property grid (due / repeat / people) and notes."""
from __future__ import annotations
from typing import List, Optional

from PyQt6.QtCore import QDate, QEvent, QPoint, QRect, QSize, Qt, pyqtSignal
from PyQt6.QtGui import QAction, QFont, QFontMetrics, QKeySequence, QShortcut, QTextCursor, QTextOption, QTextBlockFormat, QPainter, QPen, QColor
from PyQt6.QtWidgets import (QProxyStyle, QStyle, QDateEdit, QFrame, QGridLayout, QHBoxLayout, QLabel,
                             QLayout, QLineEdit, QMenu, QPushButton, QScrollArea, QSizePolicy, QStackedLayout,
                             QTextEdit, QToolBar, QCompleter, QFileDialog, QToolButton, QSpinBox, QDoubleSpinBox, QVBoxLayout, QWidget)

from . import tokens as T
from .icons import icon, pixmap
from .task_list import EmptyState, due_info
from .due_picker import DueCalendar, quick_buttons, relative_days
from .theme import body_font, heading_font

MARK_MARGIN = T.CORNER_MARK_SIZE // 2 + 1
NO_DATE = QDate(2000, 1, 1)
NARROW_WIDTH = 460

REPEAT_OPTIONS = [
    ("none", "Doesn't repeat"),
    ("daily", "Daily"),
    ("weekly", "Weekly"),
    ("monthly", "Monthly"),
    ("completion", "After completion"),
]
REPEAT_CUSTOM = "custom"
REPEAT_EDIT = "edit"


class _FormattingToolbarStyle(QProxyStyle):
    def pixelMetric(self, metric, option=None, widget=None):
        if metric == QStyle.PixelMetric.PM_ToolBarExtensionExtent:
            return 28
        return super().pixelMetric(metric, option, widget)


class FlowLayout(QLayout):
    """Left-to-right layout that wraps its items onto new lines."""

    def __init__(self, parent=None, h_spacing: int = 4, v_spacing: int = 4):
        super().__init__(parent)
        self._items = []
        self._h = h_spacing
        self._v = v_spacing
        self.setContentsMargins(0, 0, 0, 0)

    def addItem(self, item):
        self._items.append(item)

    def count(self):
        return len(self._items)

    def itemAt(self, index):
        return self._items[index] if 0 <= index < len(self._items) else None

    def takeAt(self, index):
        return self._items.pop(index) if 0 <= index < len(self._items) else None

    def expandingDirections(self):
        return Qt.Orientation(0)

    def hasHeightForWidth(self):
        return True

    def heightForWidth(self, width):
        return self._do_layout(QRect(0, 0, width, 0), dry_run=True)

    def setGeometry(self, rect):
        super().setGeometry(rect)
        self._do_layout(rect, dry_run=False)

    def sizeHint(self):
        return self.minimumSize()

    def minimumSize(self):
        size = QSize()
        for item in self._items:
            size = size.expandedTo(item.minimumSize())
        return size

    def _do_layout(self, rect: QRect, dry_run: bool) -> int:
        x, y, line_h = rect.x(), rect.y(), 0
        for item in self._items:
            widget = item.widget()
            if widget is not None and widget.isHidden():
                continue
            hint = item.sizeHint()
            width = min(hint.width(), rect.width(), widget.maximumWidth() if widget else rect.width())
            # The trailing input takes the rest of the line
            if widget is not None and widget.property("flow-stretch"):
                remaining = rect.right() - x + 1
                width = remaining if remaining >= hint.width() else rect.width()
            if x > rect.x() and x + width > rect.right() + 1:
                x = rect.x()
                y += line_h + self._v
                line_h = 0
                if widget is not None and widget.property("flow-stretch"):
                    width = rect.width()
            if not dry_run:
                item.setGeometry(QRect(QPoint(x, y), QSize(width, hint.height())))
            x += width + self._h
            line_h = max(line_h, hint.height())
        return y + line_h - rect.y()


class PropertyButton(QPushButton):
    """A compact field with one consistently placed disclosure chevron."""
    def paintEvent(self, event):
        super().paintEvent(event)
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setPen(QPen(QColor(T.NEUTRAL_700), 1.3))
        x, y = self.width() - 13, self.height() // 2
        painter.drawLine(QPoint(x - 3, y - 1), QPoint(x, y + 2))
        painter.drawLine(QPoint(x, y + 2), QPoint(x + 3, y - 1))


class ProjectLink(QPushButton):
    """Keyboard-accessible navigation with an elided label and full tooltip."""
    def __init__(self, text='', parent=None):
        super().__init__(text, parent)
        self._full_text = text
        self.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Fixed)

    def setText(self, text):
        self._full_text = text
        super().setText(self.fontMetrics().elidedText(text, Qt.TextElideMode.ElideRight, max(20, self.width() - 12)))

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self.setText(self._full_text)


class TitleEdit(QTextEdit):
    """Borderless multi-line title that grows with its content. Enter commits."""
    committed = pyqtSignal(str)

    def __init__(self, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self.setObjectName("TaskTitle")
        self.setAcceptRichText(False)
        self.setFrameShape(QFrame.Shape.NoFrame)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setWordWrapMode(QTextOption.WrapMode.WrapAtWordBoundaryOrAnywhere)
        self.setTabChangesFocus(True)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.document().setDocumentMargin(0)
        self._font = body_font(25, 600)
        self.setAccessibleName("Task title")
        self.setToolTip("Edit title · Enter saves · Esc cancels")
        self._done = False
        self._original = ""
        self._apply_font()
        self.document().contentsChanged.connect(self._fit_height)
        self._fit_height()

    def _apply_font(self):
        font = QFont(self._font)
        font.setStrikeOut(self._done)
        self.setFont(font)
        self.document().setDefaultFont(font)
        self.setStyleSheet(f"QTextEdit#TaskTitle{{color:{T.NEUTRAL_600 if self._done else T.TEXT}}}")

    def set_title(self, text: str, done: bool = False):
        self._original = text
        if done != self._done:
            self._done = done
            self._apply_font()
        if self.toPlainText() != text:
            self.blockSignals(True)
            self.setPlainText(text)
            cursor = QTextCursor(self.document())
            cursor.select(QTextCursor.SelectionType.Document)
            spacing = QTextBlockFormat()
            spacing.setLineHeight(112, QTextBlockFormat.LineHeightTypes.ProportionalHeight.value)
            cursor.mergeBlockFormat(spacing)
            self.blockSignals(False)
        self._fit_height()

    def _fit_height(self):
        width = max(50, self.viewport().width())
        self.document().setTextWidth(width)
        height = int(self.document().size().height()) + 6
        self.setFixedHeight(max(height, 40))

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._fit_height()

    def _commit(self):
        text = " ".join(self.toPlainText().split())
        if not text:
            self.set_title(self._original, self._done)
            return
        if text != self._original:
            self._original = text
            self.committed.emit(text)
        if self.toPlainText() != text:
            self.blockSignals(True)
            self.setPlainText(text)
            self.blockSignals(False)

    def keyPressEvent(self, event):
        if event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
            self._commit()
            self.clearFocus()
            return
        if event.key() == Qt.Key.Key_Escape:
            self.set_title(self._original, self._done)
            self.clearFocus()
            return
        super().keyPressEvent(event)

    def focusOutEvent(self, event):
        super().focusOutEvent(event)
        self._commit()
        cursor = self.textCursor()
        cursor.clearSelection()
        self.setTextCursor(cursor)

    def insertFromMimeData(self, source):
        # Titles are single paragraphs: flatten pasted line breaks
        self.insertPlainText(" ".join(source.text().split()))


class Chip(QFrame):
    removed = pyqtSignal(str)

    def __init__(self, name: str, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self.setObjectName("Chip")
        self.name = name
        row = QHBoxLayout(self)
        row.setContentsMargins(8, 2, 4, 2)
        row.setSpacing(3)
        label = QLabel(name, self)
        label.setFont(body_font(12.5))
        label.setStyleSheet(f"color:{T.ACCENT_800};background:transparent")
        close = QToolButton(self)
        close.setIcon(icon("x", 11, T.ACCENT_800))
        close.setIconSize(QSize(11, 11))
        close.setFixedSize(16, 16)
        close.setCursor(Qt.CursorShape.PointingHandCursor)
        close.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        close.setAccessibleName(f"Remove {name}")
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setAccessibleName(f"{name}; Delete to remove")
        close.setToolTip(f"Remove {name}")
        close.clicked.connect(lambda: self.removed.emit(self.name))
        row.addWidget(label)
        row.addWidget(close)

    def keyPressEvent(self, event):
        if event.key() in (Qt.Key.Key_Backspace, Qt.Key.Key_Delete):
            self.removed.emit(self.name)
            event.accept()
            return
        super().keyPressEvent(event)


class _ChipInput(QLineEdit):
    commitRequested = pyqtSignal()
    backspaceOnEmpty = pyqtSignal()

    def keyPressEvent(self, event):
        key = event.key()
        if key in (Qt.Key.Key_Return, Qt.Key.Key_Enter, Qt.Key.Key_Comma, Qt.Key.Key_Semicolon):
            self.commitRequested.emit()
            return
        if key == Qt.Key.Key_Backspace and not self.text():
            self.backspaceOnEmpty.emit()
            return
        super().keyPressEvent(event)

    def focusOutEvent(self, event):
        super().focusOutEvent(event)
        self.commitRequested.emit()


class ChipsField(QWidget):
    """Stakeholders as removable chips followed by an inline input."""
    changed = pyqtSignal(list)

    def __init__(self, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self._names: List[str] = []
        self._chips: List[Chip] = []
        self._flow = FlowLayout(self, 4, 4)
        self.input = _ChipInput(self)
        self.input.setProperty("flow-stretch", True)
        self.input.setFont(body_font(14))
        self.input.setAccessibleName("Add people; Enter or comma confirms")
        self.input.setToolTip("Full names are supported. Use Enter, comma, or semicolon to add a person.")
        self.input.setMinimumWidth(70)
        self.input.setMinimumHeight(26)
        self.input.commitRequested.connect(self._commit_input)
        self.input.backspaceOnEmpty.connect(self._remove_last)
        self._flow.addWidget(self.input)
        self._sync_placeholder()

    def set_suggestions(self, names):
        completer = QCompleter(names, self.input)
        completer.setCaseSensitivity(Qt.CaseSensitivity.CaseInsensitive)
        completer.setFilterMode(Qt.MatchFlag.MatchContains)
        completer.activated.connect(self._accept_suggestion)
        self.input.setCompleter(completer)

    def _accept_suggestion(self, name):
        self.input.setText(name)
        self._commit_input()

    def names(self) -> List[str]:
        return list(self._names)

    def set_names(self, names: List[str]):
        names = [n for n in names if n]
        if names == self._names:
            return
        self._names = list(names)
        self._rebuild()

    def _rebuild(self):
        for chip in self._chips:
            self._flow.removeWidget(chip)
            chip.setParent(None)
            chip.deleteLater()
        self._chips = []
        self._flow.removeWidget(self.input)
        for name in self._names:
            chip = Chip(name, self)
            chip.removed.connect(self._remove)
            chip.show()
            self._flow.addWidget(chip)
            self._chips.append(chip)
        self._flow.addWidget(self.input)
        self._sync_placeholder()
        self._flow.invalidate()
        self.updateGeometry()

    def _sync_placeholder(self):
        self.input.setPlaceholderText("Add…" if self._names else "Add people")

    def _commit_input(self):
        raw = self.input.text()
        if not raw.strip():
            return
        self.input.clear()
        added = False
        for part in raw.replace(";", ",").split(","):
            name = part.strip().lstrip("@").strip()
            if name and name.casefold() not in (n.casefold() for n in self._names):
                self._names.append(name)
                added = True
        if added:
            self._rebuild()
            self.changed.emit(self.names())

    def _remove(self, name: str):
        if name in self._names:
            self._names.remove(name)
            self._rebuild()
            self.changed.emit(self.names())

    def _remove_last(self):
        if self._names:
            self._chips[-1].setFocus(Qt.FocusReason.BacktabFocusReason)

    def hasHeightForWidth(self):
        return True

    def heightForWidth(self, width):
        return self._flow.heightForWidth(width)

    def sizeHint(self):
        return QSize(200, max(26, self._flow.heightForWidth(max(120, self.width()))))


class _FindEdit(QLineEdit):
    stepRequested = pyqtSignal(int)
    closeRequested = pyqtSignal()

    def keyPressEvent(self, event):
        if event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
            self.stepRequested.emit(-1 if event.modifiers() & Qt.KeyboardModifier.ShiftModifier else 1)
            return
        if event.key() == Qt.Key.Key_Escape:
            self.closeRequested.emit()
            return
        super().keyPressEvent(event)


class FindBar(QWidget):
    """Slim find-in-note bar shown under the notes toolbar."""
    textChanged = pyqtSignal(str)
    stepRequested = pyqtSignal(int)
    closeRequested = pyqtSignal()

    def __init__(self, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self.setObjectName("NotesToolbar")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.edit = _FindEdit(self)
        self.edit.setPlaceholderText("Find in note")
        self.edit.setFont(body_font(13))
        self.edit.setStyleSheet(f"QLineEdit{{padding:3px 8px;background:{T.BG}}}")
        self.edit.textChanged.connect(self.textChanged.emit)
        self.edit.stepRequested.connect(self.stepRequested.emit)
        self.edit.closeRequested.connect(self.closeRequested.emit)
        self.count = QLabel("", self)
        self.count.setFont(body_font(12))
        self.count.setStyleSheet(f"color:{T.NEUTRAL_700}")
        self.count.setMinimumWidth(70)
        row = QHBoxLayout(self)
        row.setContentsMargins(6, 4, 6, 4)
        row.setSpacing(4)
        row.addWidget(self.edit, 1)
        row.addWidget(self.count)
        for icon_name, tip, handler in (("chev", "Next match (Enter)", lambda: self.stepRequested.emit(1)),
                                        ("x", "Close (Esc)", self.closeRequested.emit)):
            button = QToolButton(self)
            button.setProperty("flat", True)
            button.setIcon(icon(icon_name, 14, T.NEUTRAL_800))
            button.setIconSize(QSize(14, 14))
            button.setFixedSize(26, 26)
            button.setToolTip(tip)
            button.setFocusPolicy(Qt.FocusPolicy.NoFocus)
            button.setStyleSheet("QToolButton{padding:0;border:0}")
            button.clicked.connect(handler)
            row.addWidget(button)

    def set_position(self, index: int, count: int):
        if not self.edit.text():
            self.count.setText("")
        elif count == 0:
            self.count.setText("No matches")
        else:
            self.count.setText(f"{index} of {count}" + ("+" if count >= 500 else ""))


class TaskDetailPanel(QWidget):
    """Right pane. The notes editor is created by the main window and placed in the notes frame."""
    completeClicked = pyqtSignal()
    pinToggled = pyqtSignal(bool)
    waitingToggled = pyqtSignal(bool)
    momToggled = pyqtSignal(bool)
    focusClicked = pyqtSignal()
    titleCommitted = pyqtSignal(str)
    projectClicked = pyqtSignal()
    dueDateChanged = pyqtSignal(object)      # QDate or None
    repeatChosen = pyqtSignal(str)           # key from REPEAT_OPTIONS or REPEAT_EDIT
    stakeholdersChanged = pyqtSignal(list)

    def __init__(self, notes_editor: QTextEdit, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self.notes = notes_editor
        self._loading = False
        self._done = False
        self._compact = None

        # ---- top row
        self.crumb_icon = QLabel()
        self.crumb_icon.setPixmap(pixmap("folder", 13, T.NEUTRAL_700, scale=self.devicePixelRatioF()))
        self.crumb_icon.setFixedSize(13, 13)
        self.crumb = ProjectLink("")
        self.crumb.setObjectName("ProjectLink")
        self.crumb.setFlat(True)
        self.crumb.setCursor(Qt.CursorShape.PointingHandCursor)
        self.crumb.clicked.connect(self.projectClicked.emit)
        self.crumb.setFont(body_font(12.5))
        self.crumb.setMinimumWidth(30)

        self.complete_button = QPushButton("Complete")
        self.complete_button.setProperty("primary", True)
        self.complete_button.setObjectName("TaskComplete")
        self.complete_button.setFixedHeight(32)
        self.complete_button.setFont(body_font(13, 600))
        self.complete_button.setIconSize(QSize(15, 15))
        self.complete_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.complete_button.clicked.connect(self.completeClicked.emit)
        self.complete_wrap = self.complete_button

        self.pin_button = self._toggle_button("Focus", "target", self.pinToggled)
        self.pin_button.setToolTip("Add to or remove from Focus")
        self.waiting_button = self._toggle_button("Waiting", "wait", self.waitingToggled)
        self.mom_button = self._toggle_button("MoM", "mom", self.momToggled)
        self.mom_button.setToolTip("Minutes of meeting")

        self.more_button = self._square_button("more", "More actions")
        self.more_button.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        self.more_button.setStyleSheet("QToolButton{padding:0} QToolButton::menu-indicator{image:none;width:0}")
        self.more_menu = QMenu(self.more_button)
        self.more_button.setMenu(self.more_menu)

        self.focus_button = self._square_button("max", "Full view")
        self.focus_button.clicked.connect(self.focusClicked.emit)

        self.header = QWidget()
        self._header_grid = QGridLayout(self.header)
        self._header_grid.setContentsMargins(0, 0, 0, 0)
        self._header_grid.setSpacing(6)
        self.project_link_row = QWidget()
        top = QHBoxLayout(self.project_link_row)
        top.setContentsMargins(0, 0, 0, 0)
        top.setSpacing(7)
        top.addWidget(self.crumb_icon)
        top.addWidget(self.crumb, 1)
        self.header_actions = QWidget()
        action_row = FlowLayout(self.header_actions, 6, 4)
        action_row.addWidget(self.complete_wrap)
        action_row.addWidget(self.pin_button)
        action_row.addWidget(self.waiting_button)
        action_row.addWidget(self.mom_button)
        self.header_utilities = QWidget()
        utilities = QHBoxLayout(self.header_utilities)
        utilities.setContentsMargins(0, 0, 0, 0)
        utilities.setSpacing(6)
        utilities.addWidget(self.more_button)
        utilities.addWidget(self.focus_button)
        self._header_inline = None
        self._header_grid.addWidget(self.project_link_row, 0, 0)
        self._header_grid.addWidget(self.header_actions, 0, 1)
        self._header_grid.addWidget(self.header_utilities, 0, 2)
        self._header_grid.setColumnStretch(0, 1)

        # ---- title
        self.title_edit = TitleEdit()
        self.title_edit.committed.connect(self.titleCommitted.emit)

        # ---- property grid
        self.due_edit = QDateEdit()
        self.due_edit.setCalendarPopup(True)
        self.due_edit.setDisplayFormat("ddd d MMM yyyy")
        self.due_edit.setMinimumDate(NO_DATE)
        self.due_edit.setMaximumDate(QDate(2100, 12, 31))
        self.due_edit.setSpecialValueText("No date")
        self.due_edit.setDate(NO_DATE)
        self.due_edit.setButtonSymbols(QDateEdit.ButtonSymbols.NoButtons)
        # Wide enough for the longest date text, so the year is never clipped
        self.due_edit.setMinimumWidth(QFontMetrics(body_font(14)).horizontalAdvance("Wed 30 Sep 2026") + 30)
        self.due_edit.dateChanged.connect(self._on_due_changed)
        self.due_edit.installEventFilter(self)
        self.due_relative = QLabel("")
        self.due_relative.setFont(body_font(12.5, 700))
        self.due_clear = QToolButton()
        self.due_clear.setProperty("flat", True)
        self.due_clear.setIcon(icon("x", 12, T.NEUTRAL_600))
        self.due_clear.setIconSize(QSize(12, 12))
        self.due_clear.setFixedSize(20, 20)
        self.due_clear.setToolTip("Clear due date")
        self.due_clear.setCursor(Qt.CursorShape.PointingHandCursor)
        self.due_clear.setStyleSheet("QToolButton{padding:0;border:0}")
        self.due_clear.clicked.connect(lambda: self._pick_due(NO_DATE))
        self.due_clear.hide()
        # The QDateEdit only holds the value; picking happens in the calendar below
        self.due_edit.hide()
        self.due_text = QLabel("No due date")
        self.due_text.setFont(body_font(13, 500))
        due_field = QWidget()
        due_row = QHBoxLayout(due_field)
        due_row.setContentsMargins(0, 0, 0, 0)
        due_row.setSpacing(6)
        due_row.addWidget(self.due_edit)
        due_row.addWidget(self.due_text)
        due_row.addWidget(self.due_relative)
        due_row.addWidget(self.due_clear)
        due_row.addStretch(1)
        # Picking a date closes the editor; Done is for leaving it unchanged
        self.due_done = QPushButton('Done')
        self.due_done.clicked.connect(self._close_property_editor)
        due_row.addWidget(self.due_done)

        self.chips = ChipsField()
        self.chips.changed.connect(self.stakeholdersChanged.emit)

        self.prop_wrap = QWidget()
        properties = QVBoxLayout(self.prop_wrap)
        properties.setContentsMargins(0, 0, 0, 0)
        properties.setSpacing(5)
        summaries = QWidget()
        summary_row = FlowLayout(summaries, 6, 4)
        self.due_summary = PropertyButton('Due: Add date')
        self.repeat_summary = PropertyButton('Repeat: None')
        self.people_summary = PropertyButton('People: Add')
        for button in (self.due_summary, self.repeat_summary, self.people_summary):
            button.setObjectName('PropertySummary')
            button.setFixedHeight(30)
            button.setCursor(Qt.CursorShape.PointingHandCursor)
            summary_row.addWidget(button)
        self.due_summary.clicked.connect(lambda: self.show_property_editor('due'))
        self.people_summary.clicked.connect(lambda: self.show_property_editor('people'))
        self.repeat_menu = QMenu(self.repeat_summary)
        self.repeat_summary.setMenu(self.repeat_menu)
        self.repeat_menu.aboutToShow.connect(self._populate_repeat_menu)
        properties.addWidget(summaries)
        self.property_editor = QFrame()
        self.property_editor.setObjectName('PropertyEditor')
        editor_layout = QVBoxLayout(self.property_editor)
        editor_layout.setContentsMargins(8, 6, 8, 6)
        self.due_panel = QWidget()
        dates = QVBoxLayout(self.due_panel)
        dates.setContentsMargins(0, 0, 0, 0)
        dates.setSpacing(8)
        self.due_quick_row = quick_buttons(self._pick_due)
        dates.addWidget(self.due_quick_row)
        self.due_calendar = DueCalendar()
        self.due_calendar.setFixedWidth(300)
        self.due_calendar.picked.connect(self._pick_due)
        dates.addWidget(self.due_calendar, 0, Qt.AlignmentFlag.AlignLeft)
        dates.addWidget(due_field)
        editor_layout.addWidget(self.due_panel)
        editor_layout.addWidget(self.chips)
        self.people_done = QPushButton('Done')
        self.people_done.clicked.connect(self._close_property_editor)
        editor_layout.addWidget(self.people_done, 0, Qt.AlignmentFlag.AlignRight)
        properties.addWidget(self.property_editor)
        self.property_editor.hide()
        self._property_kind = None

        # ---- notes
        self.notes_frame = QFrame()
        self.notes_frame.setObjectName("NotesFrame")
        self.notes_frame.setProperty("focused", False)
        self.notes_toolbar = QToolBar('Note formatting')
        self.notes_toolbar.setObjectName('NotesToolbar')
        self._formatting_style = _FormattingToolbarStyle()
        self._formatting_style.setParent(self.notes_toolbar)
        self.notes_toolbar.setStyle(self._formatting_style)
        self.notes_toolbar.setMovable(False)
        self.notes_toolbar.setFloatable(False)
        self.notes_toolbar.setIconSize(QSize(16, 16))
        self.notes_toolbar.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Fixed)
        self.notes_toolbar.setMinimumWidth(0)
        self.notes_toolbar.installEventFilter(self)
        self.template_button: Optional[QToolButton] = None
        self.find_bar = FindBar()
        self.find_bar.hide()
        self.find_bar.textChanged.connect(self._on_find_text)
        self.find_bar.stepRequested.connect(self._on_find_step)
        self.find_bar.closeRequested.connect(self.close_find)
        self.notes.findStateChanged.connect(self.find_bar.set_position)
        self._find_shortcut = QShortcut(QKeySequence(QKeySequence.StandardKey.Find), self)
        self._find_shortcut.setContext(Qt.ShortcutContext.WidgetWithChildrenShortcut)
        self._find_shortcut.activated.connect(self.show_find)
        self.notes_hint = QLabel("Paste or drop images")
        self.notes_hint.setFont(body_font(11.5))
        self.notes_hint.setStyleSheet(f"color:{T.NEUTRAL_600}")
        self.notes_hint.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        self.notes_hint.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)

        self.notes.setObjectName("NotesEditor")
        self.notes.setFrameShape(QFrame.Shape.NoFrame)
        self.notes.setPlaceholderText("Write notes…")
        self.notes.setMinimumHeight(110)
        self.notes.installEventFilter(self)

        notes_col = QVBoxLayout(self.notes_frame)
        notes_col.setContentsMargins(1, 1, 1, 1)
        notes_col.setSpacing(0)
        notes_col.addWidget(self.notes_toolbar)
        notes_col.addWidget(self.find_bar)
        self.save_error_bar = QWidget()
        errors = QHBoxLayout(self.save_error_bar)
        errors.setContentsMargins(8, 4, 8, 4)
        self.save_error_label = QLabel('Not saved')
        self.save_error_label.setWordWrap(True)
        errors.addWidget(self.save_error_label, 1)
        for title, handler in (('Retry', self.notes.save_all_now), ('Export', self.notes.export_unsaved), ('Discard', self.notes.discard_unsaved)):
            button = QPushButton(title)
            button.setFocusPolicy(Qt.FocusPolicy.NoFocus)
            button.clicked.connect(handler)
            errors.addWidget(button)
        self.save_error_bar.hide()
        self.notes.saveStateChanged.connect(self._note_save_state)
        notes_col.addWidget(self.save_error_bar)

        self.search_matches_bar = QWidget()
        matches = QHBoxLayout(self.search_matches_bar)
        matches.setContentsMargins(8, 3, 8, 3)
        self.search_matches_label = QLabel()
        matches.addWidget(self.search_matches_label, 1)
        for title, step in (('Previous', -1), ('Next', 1)):
            button = QPushButton(title)
            button.setFocusPolicy(Qt.FocusPolicy.NoFocus)
            button.clicked.connect(lambda _checked=False, direction=step: self.notes.search_step(direction))
            matches.addWidget(button)
        self.search_matches_bar.hide()
        self.notes.searchStateChanged.connect(self._search_match_state)
        notes_col.addWidget(self.search_matches_bar)

        self.image_tools = QWidget()
        image_rows = QVBoxLayout(self.image_tools)
        image_rows.setContentsMargins(6, 4, 6, 4)
        image_rows.setSpacing(4)
        image_controls = QHBoxLayout()
        image_controls.setSpacing(2)
        self.image_buttons = []
        for title, symbol, handler in (
                ('Copy image', 'copy', self.notes.copy_image_at_cursor),
                ('Cut image', 'cut', self.notes.cut_image_at_cursor),
                ('Remove image', 'trash', self.notes.remove_image_at_cursor),
                ('Crop image', 'crop', self.notes.crop_image_visual),
                ('Fit width (shrink only)', 'expand', self.notes.fit_image_width),
                ('Actual size', 'image', self.notes.actual_image_size),
                ('Restore original proportions', 'rotate-ccw', self.notes.restore_image_proportions),
                ('Drag to resize', 'max', self.notes.resize_image_at_cursor)):
            button = QToolButton()
            button.setIcon(icon(symbol, 16, T.NEUTRAL_800))
            button.setToolTip(title)
            button.setAccessibleName(title)
            button.setFixedSize(28, 28)
            button.setStyleSheet('QToolButton{padding:0;border:0}')
            button.setFocusPolicy(Qt.FocusPolicy.NoFocus)
            button.clicked.connect(handler)
            image_controls.addWidget(button)
            self.image_buttons.append(button)
        self.image_percent = QDoubleSpinBox()
        self.image_percent.setDecimals(2)
        self.image_percent.setRange(0.01, 3276700)
        self.image_percent.setSuffix('%')
        self.image_percent.setFixedWidth(100)
        self.image_percent.setToolTip('Width as a percentage of the original; preserves current proportions')
        self.image_percent.setKeyboardTracking(False)
        self.image_percent.valueChanged.connect(self.notes.set_image_scale)
        image_controls.addWidget(self.image_percent)
        image_controls.addStretch(1)
        image_rows.addLayout(image_controls)
        self.image_resize_row = QWidget()
        resize_controls = QHBoxLayout(self.image_resize_row)
        resize_controls.setContentsMargins(0, 0, 0, 0)
        hint = QLabel('Drag corner · Shift unlocks proportions · In note: Enter applies, Esc cancels')
        hint.setWordWrap(True)
        hint.setFont(body_font(11))
        resize_controls.addWidget(hint, 1)
        self.image_apply = QPushButton('Apply')
        self.image_apply.clicked.connect(self._apply_image_resize)
        self.image_cancel = QPushButton('Cancel')
        self.image_cancel.clicked.connect(self.notes.cancel_image_resize)
        for button in (self.image_apply, self.image_cancel):
            button.setFocusPolicy(Qt.FocusPolicy.NoFocus)
            resize_controls.addWidget(button)
        self.image_size_row = QWidget()
        size_controls = QHBoxLayout(self.image_size_row)
        size_controls.setContentsMargins(0, 0, 0, 0)
        self.image_width = QSpinBox()
        self.image_height = QSpinBox()
        for label, spin in (('Width', self.image_width), ('Height', self.image_height)):
            spin.setRange(1, 32767)
            spin.setPrefix(label[0] + ': ')
            spin.setSuffix(' px')
            spin.setAccessibleName('Image ' + label.lower())
            spin.setKeyboardTracking(False)
            size_controls.addWidget(spin)
        self.image_lock = QPushButton('Lock ratio')
        self.image_lock.setCheckable(True)
        self.image_lock.setChecked(True)
        self.image_lock.setToolTip('Preserve current proportions; Shift temporarily unlocks while dragging')
        self.image_lock.toggled.connect(self.notes.set_image_resize_locked)
        size_controls.addWidget(self.image_lock)
        fit = QPushButton('Fit width')
        fit.clicked.connect(self.notes.fit_image_width)
        size_controls.addWidget(fit)
        self.image_width.valueChanged.connect(lambda value: self.notes.preview_image_size(
            value, self.image_height.value(), dimension='width'))
        self.image_height.valueChanged.connect(lambda value: self.notes.preview_image_size(
            self.image_width.value(), value, dimension='height'))
        self.notes.imageResizeChanged.connect(self._image_resize_dimensions)
        image_rows.addWidget(self.image_size_row)
        image_rows.addWidget(self.image_resize_row)
        self.image_tools.hide()
        self.notes.imageSelectionChanged.connect(self._image_selected)
        notes_col.addWidget(self.image_tools)
        notes_col.addWidget(self.notes, 1)
        metadata = QHBoxLayout()
        metadata.setContentsMargins(8, 3, 8, 3)
        self.note_save_label = QLabel('Saved')
        self.note_save_label.setAccessibleName('Note save status')
        metadata.addWidget(self.note_save_label)
        metadata.addWidget(self.notes_hint, 1)
        notes_col.addLayout(metadata)
        # Small minimum: the editor scrolls its own content, so the panel rarely needs to scroll
        self.notes_frame.setMinimumHeight(160)

        # ---- content column (max 860px, centered)
        self.content = QWidget()
        self.content.setMaximumWidth(860)
        self._content_col = QVBoxLayout(self.content)
        self._content_col.setContentsMargins(20, 14 - MARK_MARGIN, 20, 20)
        self._content_col.setSpacing(0)
        self._content_col.addWidget(self.header)
        self._content_col.addSpacing(16)
        self._content_col.addWidget(self.title_edit)
        self._content_col.addSpacing(12)
        self._content_col.addWidget(self.prop_wrap)
        self._content_col.addSpacing(16)
        self._content_col.addWidget(self.notes_frame, 1)

        holder = QWidget()
        holder_row = QHBoxLayout(holder)
        holder_row.setContentsMargins(0, 0, 0, 0)
        holder_row.addStretch(1)
        holder_row.addWidget(self.content, 100)
        holder_row.addStretch(1)

        self.scroll = QScrollArea()
        self.scroll.setFrameShape(QFrame.Shape.NoFrame)
        self.scroll.setWidgetResizable(True)
        self.scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.scroll.setWidget(holder)

        # ---- empty state (keeps the focus button reachable)
        self.empty = EmptyState()
        self.empty.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, False)
        self.empty.set_text("No task selected", "Pick a task to see its notes, dates and people. Press N to add one.")
        empty_page = QWidget()
        empty_col = QVBoxLayout(empty_page)
        empty_col.setContentsMargins(0, 0, 0, 0)
        empty_col.addStretch(1)
        empty_col.addWidget(self.empty)
        empty_col.addStretch(1)

        self._stack = QStackedLayout(self)
        self._stack.setContentsMargins(0, 0, 0, 0)
        self._stack.addWidget(empty_page)
        self._stack.addWidget(self.scroll)
        self._stack.setCurrentIndex(0)

    # ----- builders
    def _toggle_button(self, text: str, icon_name: str, signal) -> QPushButton:
        button = QPushButton(text)
        button.setCheckable(True)
        button.setIcon(icon(icon_name, 15, T.NEUTRAL_800, on_color=T.ACCENT_700, on_fill=T.ACCENT_300))
        button.setIconSize(QSize(15, 15))
        button.setCursor(Qt.CursorShape.PointingHandCursor)
        button.setFont(body_font(13))
        button.setFixedHeight(32)
        button.setProperty("label", text)
        button.setAccessibleName(text)
        button.setObjectName("TaskFlag")
        button.setToolTip(text)
        button.clicked.connect(lambda checked: signal.emit(bool(checked)))
        return button

    def _square_button(self, icon_name: str, tooltip: str) -> QToolButton:
        button = QToolButton()
        button.setProperty("icon-only", True)
        button.setIcon(icon(icon_name, 18, T.NEUTRAL_800))
        button.setIconSize(QSize(18, 18))
        button.setObjectName("HeaderUtility")
        button.setFixedSize(32, 32)
        button.setToolTip(tooltip)
        button.setAccessibleName(tooltip)
        button.setCursor(Qt.CursorShape.PointingHandCursor)
        button.setStyleSheet("QToolButton{padding:0}")
        return button

    def add_toolbar_action(self, action, icon_name=None):
        if icon_name:
            action.setIcon(icon(icon_name, 16, T.NEUTRAL_800, on_color=T.ACCENT_900))
        self.notes_toolbar.addAction(action)
        button = self.notes_toolbar.widgetForAction(action)
        button.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        button.setAccessibleName(action.text().replace('&', ''))
        return button

    def add_toolbar_menu(self, title, menu):
        action = QAction(title, self)
        action.setMenu(menu)
        button = self.add_toolbar_action(action)
        button.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextOnly)
        button.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        button.setObjectName("FormattingMenu")
        button.setFixedHeight(32)
        return button

    def add_template_button(self, populate):
        # Templates and insertion actions are available even with no existing templates.
        menu = QMenu(self)
        menu.addAction('Image…', self._insert_image)
        menu.addAction(self.notes.action_link)
        templates = menu.addMenu('Template')
        templates.aboutToShow.connect(lambda: (templates.clear(), populate(templates)))
        self.template_button = self.add_toolbar_menu('Insert', menu)

    def _insert_image(self):
        path, _ = QFileDialog.getOpenFileName(self, 'Insert image', '', 'Images (*.png *.jpg *.jpeg *.webp *.gif *.bmp)')
        if path:
            from PyQt6.QtCore import QMimeData, QUrl
            mime = QMimeData()
            mime.setUrls([QUrl.fromLocalFile(path)])
            self.notes.insertFromMimeData(mime)

    def set_template_button_visible(self, visible):
        pass  # Insert remains available; template contents refresh when opened.

    def add_find_button(self):
        action = QAction('Find in note (Ctrl+F)', self)
        action.triggered.connect(self.show_find)
        self.add_toolbar_action(action, 'search')

    def _note_save_state(self, state, error):
        self.save_error_bar.setVisible(state == 'error')
        if hasattr(self, 'note_save_label'):
            self.note_save_label.setText({'dirty': 'Unsaved changes', 'saving': 'Saving…', 'saved': 'Saved', 'error': 'Not saved'}.get(state, state))
        if error:
            self.save_error_label.setText('Not saved — retry or export this note.')
            self.save_error_label.setToolTip(error)

    def _search_match_state(self, index, count):
        self.search_matches_bar.setVisible(bool(self.notes._search_terms))
        suffix = '+' if count >= 200 else ''
        self.search_matches_label.setText(
            f'Search in note: {index} of {count}{suffix}' if index else f'Search in note: {count}{suffix} matches')

    def _apply_image_resize(self):
        # Apply has NoFocus to preserve the image selection; commit pending spin text explicitly.
        self.image_width.interpretText()
        self.image_height.interpretText()
        self.notes.apply_image_resize()

    def _image_resize_dimensions(self, width, height):
        for spin, value in ((self.image_width, width), (self.image_height, height)):
            spin.blockSignals(True)
            spin.setValue(value)
            spin.blockSignals(False)

    def _image_selected(self, selected):
        self.image_tools.setVisible(selected or self.notes.is_resizing_image)
        resizing = self.notes.is_resizing_image
        self.image_resize_row.setVisible(resizing)
        self.image_size_row.setVisible(resizing)
        self.image_percent.setEnabled(not resizing)
        for button in self.image_buttons:
            button.setEnabled(not resizing)
        if not selected:
            return
        cursor = self.notes._image_cursor()
        fmt = cursor.charFormat().toImageFormat()
        image = self.notes.assets.image(fmt.name())
        if not image.isNull():
            self.image_percent.blockSignals(True)
            self.image_percent.setValue(100 * (fmt.width() or image.width()) / image.width())
            self.image_percent.blockSignals(False)

    def set_note_meta(self, text: str, tooltip: str = ""):
        self.notes_hint.setText(text)
        self.notes_hint.setToolTip(tooltip)

    # ----- find in note
    def show_find(self):
        if not self.has_task():
            return
        selected = self.notes.textCursor().selectedText()
        self.find_bar.show()
        if selected and "\u2029" not in selected:
            self.find_bar.edit.setText(selected)
        self.find_bar.edit.setFocus()
        self.find_bar.edit.selectAll()
        self._on_find_text(self.find_bar.edit.text())

    def close_find(self):
        if not self.find_bar.isVisible():
            return
        had_focus = self.find_bar.edit.hasFocus()
        self.find_bar.hide()
        self.notes.clear_find()
        if had_focus:
            self.notes.setFocus()

    def _on_find_text(self, text: str):
        count = self.notes.set_find_text(text)
        index, _count = self.notes.find_position()
        self.find_bar.set_position(index, count)

    def _on_find_step(self, step: int):
        self.find_bar.set_position(*self.notes.find_step(step))

    def add_toolbar_separator(self):
        self.notes_toolbar.addSeparator()

    def has_task(self) -> bool:
        return self._stack.currentIndex() == 1

    def set_has_task(self, has_task: bool):
        self._stack.setCurrentIndex(1 if has_task else 0)

    def set_task_header(self, *, title: str, project: str, done: bool, pinned: bool, waiting: bool, mom: bool):
        self._done = done
        self.crumb.setText(project)
        self.crumb.setToolTip(f"Open project: {project}")
        self.crumb.setAccessibleName(f"Open project {project}")
        if not self.title_edit.hasFocus():
            self.title_edit.set_title(title, done)
        self.complete_button.setProperty("primary", not done)
        self.complete_button.setText("Reopen" if done else "Complete")
        self.complete_button.setIcon(icon("check", 15, T.NEUTRAL_800 if done else T.BG))
        self.complete_button.setToolTip("Mark as not done" if done else "Mark as complete (Ctrl+Enter)")
        self.complete_button.style().unpolish(self.complete_button)
        self.complete_button.style().polish(self.complete_button)
        self.mom_button.setVisible(mom)
        self._layout_header()
        for button, checked in ((self.pin_button, pinned), (self.waiting_button, waiting), (self.mom_button, mom)):
            button.blockSignals(True)
            button.setChecked(checked)
            button.blockSignals(False)

    def set_due_date(self, date: Optional[QDate]):
        self._loading = True
        try:
            self.due_edit.setDate(date if date is not None and date.isValid() else NO_DATE)
        finally:
            self._loading = False
        self._update_due_relative()

    def set_repeat(self, key: str, custom_label=None, tooltip=''):
        self._repeat_key = key
        label = custom_label or ('Custom schedule' if key == REPEAT_CUSTOM else dict(REPEAT_OPTIONS).get(key, 'None'))
        if key == 'none':
            label = 'None'
        display = self.repeat_summary.fontMetrics().elidedText(label, Qt.TextElideMode.ElideRight, 200)
        self.repeat_summary.setText('Repeat: ' + display)
        self.repeat_summary.setToolTip(tooltip or custom_label or label)

    def set_stakeholders(self, names: List[str]):
        self.chips.set_names(names)
        self.people_summary.setText(f"People: {len(names)}" if names else "People: Add")
        self.people_summary.setToolTip(", ".join(names) or "Add people")

    def set_focus_mode(self, active: bool):
        self.focus_button.setIcon(icon("min" if active else "max", 15, T.NEUTRAL_800))
        self.focus_button.setToolTip("Leave full view (Esc)" if active else "Full view")

    # ----- field handlers
    def _update_due_relative(self):
        date = self.due_edit.date()
        has_date = date != NO_DATE
        self.due_text.setText(date.toString("ddd d MMM yyyy") if has_date else "No due date")
        self.due_summary.setText("Due: " + (date.toString("d MMM") if has_date else "Add date"))
        self.due_summary.setToolTip(date.toString("dddd d MMMM yyyy") if has_date else "Set a due date")
        self.due_clear.setVisible(has_date)
        if not has_date:
            self.due_relative.setText("")
            return
        label, state = due_info(date.toString("yyyy-MM-dd"))
        color = {"overdue": T.ACCENT_800, "today": T.ACCENT_700}.get(state, T.NEUTRAL_700)
        self.due_relative.setFont(body_font(12.5, 700 if state in ("overdue", "today") else 400))
        if state not in ("overdue", "today"):
            label = relative_days(date)
        self.due_relative.setText("Overdue · " + label if state == "overdue" else label)
        self.due_relative.setStyleSheet(f"color:{color}")

    def _pick_due(self, date: QDate):
        """A quick button, a calendar day or Clear: set the date and close the editor."""
        self.due_edit.setDate(date)
        self._close_property_editor()

    def _on_due_changed(self, date: QDate):
        self._update_due_relative()
        if not self._loading:
            self.dueDateChanged.emit(None if date == NO_DATE else date)

    def eventFilter(self, obj, event):
        if obj is getattr(self, 'notes_toolbar', None) and event.type() == QEvent.Type.Resize:
            extension = self.notes_toolbar.findChild(QToolButton, 'qt_toolbar_ext_button')
            if extension:
                extension.setIcon(icon('more', 16, T.NEUTRAL_800))
                extension.setStyleSheet('QToolButton{padding:0;border:1px solid transparent} QToolButton:focus,QToolButton:hover{border-color:#5980a6;background:#e5eff8}')
                extension.setToolTip('More formatting actions')
                extension.setAccessibleName('More formatting actions')
                extension.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        if obj is self.notes and event.type() in (QEvent.Type.FocusIn, QEvent.Type.FocusOut):
            self.notes_frame.setProperty("focused", event.type() == QEvent.Type.FocusIn)
            self.notes_frame.style().unpolish(self.notes_frame)
            self.notes_frame.style().polish(self.notes_frame)
        elif obj is self.due_edit and event.type() == QEvent.Type.MouseButtonPress and self.due_edit.date() == NO_DATE:
            # Open the calendar on today instead of the placeholder date
            self._loading = True
            self.due_edit.calendarWidget().setSelectedDate(QDate.currentDate())
            self._loading = False
        elif obj is self.due_edit and event.type() == QEvent.Type.Wheel and not self.due_edit.hasFocus():
            event.ignore()
            return True
        return super().eventFilter(obj, event)

    def show_property_editor(self, kind):
        if self.property_editor.isVisible() and self._property_kind == kind:
            self._close_property_editor()
            return
        self._property_kind = kind
        self.due_panel.setVisible(kind == 'due')
        self.chips.setVisible(kind == 'people')
        self.people_done.setVisible(kind == 'people')
        self.property_editor.show()
        if kind == 'due':
            date = self.due_edit.date()
            self.due_calendar.show_date(None if date == NO_DATE else date)
            self.due_calendar.setFocus()
        else:
            self.chips.input.setFocus()

    def _close_property_editor(self):
        self.chips._commit_input()
        self.property_editor.hide()
        self._property_kind = None

    def _populate_repeat_menu(self):
        self.repeat_menu.clear()
        for key, title in REPEAT_OPTIONS + [(REPEAT_EDIT, 'Custom…')]:
            action = self.repeat_menu.addAction(title)
            action.setCheckable(key != REPEAT_EDIT)
            action.setChecked(key == getattr(self, '_repeat_key', 'none'))
            action.triggered.connect(lambda _checked=False, value=key: self.repeatChosen.emit(value))

    def _layout_header(self):
        buttons = (self.complete_button, self.pin_button, self.waiting_button, self.mom_button)
        visible = [button for button in buttons if not button.isHidden()]
        actions_width = sum(button.sizeHint().width() for button in visible) + 6 * max(0, len(visible) - 1)
        available = min(860, self.width() - (28 if self.width() < NARROW_WIDTH else 40))
        inline = available >= actions_width + 70 + 130
        self.header_actions.setMinimumWidth(actions_width if inline else 0)
        self.header_actions.setMaximumWidth(actions_width if inline else 16777215)
        if inline == self._header_inline:
            return
        self._header_inline = inline
        for widget in (self.project_link_row, self.header_actions, self.header_utilities):
            self._header_grid.removeWidget(widget)
        self._header_grid.addWidget(self.project_link_row, 0, 0, 1, 1 if inline else 2)
        self._header_grid.addWidget(self.header_utilities, 0, 2)
        self._header_grid.addWidget(self.header_actions, 0 if inline else 1, 1 if inline else 0, 1, 1 if inline else 3)
        self._header_grid.setColumnStretch(0, 1)
        self._header_grid.setColumnStretch(1, 0)
        self.header_actions.setMaximumWidth(actions_width if inline else 16777215)
        self.header.updateGeometry()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        compact = self.width() < NARROW_WIDTH
        side = 14 if compact else 20
        self._content_col.setContentsMargins(side, 10, side, 14)
        self.notes_hint.setVisible(True)
        self._layout_header()
