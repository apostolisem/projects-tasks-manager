"""Move tasks to another project: search, pick (or create) the project, Enter."""
from __future__ import annotations
from typing import Iterable, List, Optional

from PyQt6.QtCore import QSize, Qt, pyqtSignal
from PyQt6.QtGui import QFontMetrics
from PyQt6.QtWidgets import (QAbstractItemView, QDialog, QFrame, QHBoxLayout, QLabel, QLineEdit, QListWidget,
                             QListWidgetItem, QPushButton, QStyle, QStyledItemDelegate, QVBoxLayout, QWidget)

from . import tokens as T
from .icons import pixmap
from .project_switcher import rank_projects
from .sidebar import ROW_HEIGHT, paint_sidebar_row
from .theme import body_font
from .widgets import CTRL_LABEL

ROLE_PROJECT = Qt.ItemDataRole.UserRole + 1   # project dict (with '_spans')
ROLE_CREATE = Qt.ItemDataRole.UserRole + 2    # title of the project to create
SHOWN_TASKS = 3


class _RowDelegate(QStyledItemDelegate):
    def paint(self, painter, option, index):
        selected = bool(option.state & QStyle.StateFlag.State_Selected)
        hover = bool(option.state & QStyle.StateFlag.State_MouseOver)
        create = index.data(ROLE_CREATE)
        if create:
            paint_sidebar_row(painter, option.rect, icon_name="plus", text=f"Create project “{create}” and move here",
                              active=selected, hover=hover, ghost=not selected)
            return
        data = index.data(ROLE_PROJECT) or {}
        open_count = int(data.get('open_count') or 0)
        paint_sidebar_row(painter, option.rect, icon_name="pin" if data.get('starred') else "folder",
                          text=data.get('title', ''), count=str(open_count) if open_count else None,
                          alert=bool(data.get('overdue_count')), active=selected, hover=hover,
                          ghost=bool(data.get('hidden_in_sidebar')), spans=data.get('_spans'))

    def sizeHint(self, option, index):
        return QSize(option.rect.width(), ROW_HEIGHT)


class _SearchEdit(QLineEdit):
    """The search field keeps the focus; the arrow keys move through the list."""
    moveRequested = pyqtSignal(int)
    createRequested = pyqtSignal()

    def keyPressEvent(self, event):
        key = event.key()
        ctrl = bool(event.modifiers() & Qt.KeyboardModifier.ControlModifier)
        if key in (Qt.Key.Key_Up, Qt.Key.Key_Down):
            self.moveRequested.emit(-1 if key == Qt.Key.Key_Up else 1)
        elif key in (Qt.Key.Key_PageUp, Qt.Key.Key_PageDown):
            self.moveRequested.emit(-8 if key == Qt.Key.Key_PageUp else 8)
        elif ctrl and key in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
            self.createRequested.emit()
        else:
            super().keyPressEvent(event)


class MoveTasksDialog(QDialog):
    """Pick the project to move tasks to. After exec(): selected_project_id, or create_title
    for a project to create first."""

    def __init__(self, projects: List[dict], task_titles: List[str], source_title: Optional[str],
                 recent_ids: Iterable[int] = (), parent: Optional[QWidget] = None):
        super().__init__(parent)
        self._projects = projects
        self._recent_ids = list(recent_ids)
        self._count = len(task_titles)
        self.selected_project_id: Optional[int] = None
        self.create_title: Optional[str] = None

        noun = "task" if self._count == 1 else "tasks"
        self.setWindowTitle(f"Move {self._count} {noun}")
        self.setModal(True)
        self.resize(520, 560)
        self.setMinimumSize(380, 360)

        col = QVBoxLayout(self)
        col.setContentsMargins(20, 18, 20, 18)
        col.setSpacing(10)

        heading = QLabel(f"Move {self._count} {noun}" + (f" from {source_title}" if source_title else ""))
        heading.setObjectName("DialogTitle")
        heading.setWordWrap(True)
        col.addWidget(heading)

        shown = task_titles[:SHOWN_TASKS]
        lines = [f"• {title}" for title in shown]
        if self._count > SHOWN_TASKS:
            lines.append(f"+{self._count - SHOWN_TASKS} more")
        tasks = QLabel("\n".join(lines))
        tasks.setFont(body_font(13))
        tasks.setStyleSheet(f"color:{T.NEUTRAL_700}")
        tasks.setWordWrap(True)
        tasks.setTextInteractionFlags(Qt.TextInteractionFlag.NoTextInteraction)
        col.addWidget(tasks)

        # Search field with an icon, like the top bar
        search_row = QFrame()
        search_row.setObjectName("MoveSearch")
        search_layout = QHBoxLayout(search_row)
        search_layout.setContentsMargins(10, 0, 10, 0)
        search_layout.setSpacing(8)
        glass = QLabel()
        glass.setPixmap(pixmap("search", 16, T.NEUTRAL_600, scale=self.devicePixelRatioF()))
        self.search = _SearchEdit()
        self.search.setObjectName("MoveSearchEdit")
        self.search.setFont(body_font(14))
        self.search.setPlaceholderText("Search projects — words in any order, e.g. “ba gent”")
        self.search.textChanged.connect(self._refill)
        self.search.moveRequested.connect(self._move)
        self.search.createRequested.connect(self._create_from_query)
        search_layout.addWidget(glass)
        search_layout.addWidget(self.search, 1)
        col.addWidget(search_row)

        self.list = QListWidget()
        self.list.setObjectName("MoveList")
        self.list.setFrameShape(QFrame.Shape.NoFrame)
        self.list.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.list.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.list.setVerticalScrollMode(QAbstractItemView.ScrollMode.ScrollPerPixel)
        self.list.setUniformItemSizes(True)
        self.list.setMouseTracking(True)
        self.list.setItemDelegate(_RowDelegate(self.list))
        self.list.currentRowChanged.connect(self._sync_button)
        self.list.itemDoubleClicked.connect(lambda _item: self.accept())
        col.addWidget(self.list, 1)

        self.empty = QLabel()
        self.empty.setFont(body_font(13))
        self.empty.setStyleSheet(f"color:{T.NEUTRAL_600}")
        self.empty.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.empty.setVisible(False)
        col.addWidget(self.empty)

        buttons = QHBoxLayout()
        buttons.setSpacing(8)
        hint = QLabel("↑↓ choose · Enter move · Esc cancel")
        hint.setFont(body_font(12))
        hint.setStyleSheet(f"color:{T.NEUTRAL_600}")
        buttons.addWidget(hint)
        buttons.addStretch(1)
        cancel = QPushButton("Cancel")
        cancel.clicked.connect(self.reject)
        buttons.addWidget(cancel)
        self.move_button = QPushButton()
        self.move_button.setProperty("primary", True)
        self.move_button.setDefault(True)
        self.move_button.clicked.connect(self.accept)
        buttons.addWidget(self.move_button)
        col.addLayout(buttons)

        self.setStyleSheet(
            f"#MoveSearch{{border:1px solid {T.DIVIDER};background:{T.BG}}}"
            f"QLineEdit#MoveSearchEdit{{border:0;background:transparent;padding:9px 0}}"
            f"#MoveList{{background:{T.BG};border:1px solid {T.DIVIDER}}}")
        self._refill()
        self.search.setFocus()

    # ----- list
    def _refill(self, *_):
        query = self.search.text().strip()
        self.list.clear()
        for project in rank_projects(query, self._projects, self._recent_ids):
            item = QListWidgetItem(project['title'])
            item.setData(ROLE_PROJECT, project)
            if project.get('hidden_in_sidebar'):
                item.setToolTip("Hidden project")
            self.list.addItem(item)
        exact = any(p['title'].strip().lower() == query.lower() for p in self._projects)
        if query and not exact:
            item = QListWidgetItem(query)
            item.setData(ROLE_CREATE, query)
            item.setToolTip(f"{CTRL_LABEL}+Enter")
            self.list.addItem(item)
        matches = self.list.count() - (1 if query and not exact else 0)
        self.empty.setText(f"No project matches “{query}”." if query and matches == 0 else "")
        self.empty.setVisible(bool(query) and matches == 0)
        if self.list.count():
            self.list.setCurrentRow(0)
        self._sync_button()

    def _move(self, step: int):
        if self.list.count():
            row = max(0, min(self.list.count() - 1, self.list.currentRow() + step))
            self.list.setCurrentRow(row)
            self.list.scrollToItem(self.list.currentItem())

    def _sync_button(self, *_):
        item = self.list.currentItem()
        metrics = QFontMetrics(self.move_button.font())
        if item is None:
            self.move_button.setText("Move")
            self.move_button.setEnabled(False)
            return
        create = item.data(ROLE_CREATE)
        target = create if create else item.data(ROLE_PROJECT)['title']
        label = metrics.elidedText(target, Qt.TextElideMode.ElideRight, 220)
        self.move_button.setText(f"Create and move to {label}" if create else f"Move to {label}")
        self.move_button.setToolTip(target)
        self.move_button.setEnabled(True)

    def _create_from_query(self):
        query = self.search.text().strip()
        if not query:
            return
        existing = next((p for p in self._projects if p['title'].strip().lower() == query.lower()), None)
        if existing is not None:
            # Already there: move to it instead of creating "Name 2"
            self.selected_project_id, self.create_title = existing['id'], None
        else:
            self.selected_project_id, self.create_title = None, query
        super().accept()

    def accept(self):
        item = self.list.currentItem()
        if item is None:
            return
        create = item.data(ROLE_CREATE)
        if create:
            self.selected_project_id, self.create_title = None, create
        else:
            self.selected_project_id, self.create_title = item.data(ROLE_PROJECT)['id'], None
        super().accept()
