"""Quick project switcher (Ctrl+P): type part of a name, Enter opens the project."""
from __future__ import annotations
from typing import Dict, Iterable, List, Optional

from PyQt6.QtCore import QPoint, QRect, QSize, Qt, pyqtSignal
from PyQt6.QtGui import QColor, QPainter
from PyQt6.QtWidgets import (QAbstractItemView, QFrame, QLineEdit, QListWidget, QListWidgetItem,
                             QStyle, QStyledItemDelegate, QVBoxLayout, QWidget)

from . import tokens as T
from .painting import draw_corner_marks
from .match import match
from .sidebar import ROW_HEIGHT, paint_sidebar_row

MARGIN = 8          # transparent ring for the corner marks, as on menus
WIDTH = 420
MAX_ROWS = 10
PROJECT_ID_ROLE = Qt.ItemDataRole.UserRole


def fuzzy_score(query: str, title: str) -> Optional[int]:
    """Score title for query typed as a subsequence (case-insensitive); None when it doesn't match.

    Consecutive letters and letters at the start of a word score higher, so "wr" ranks
    "Website relaunch" above "Hardware".
    """
    query = query.lower().strip()
    if not query:
        return 0
    text = title.lower()
    score = 0
    pos = -1
    previous = -2
    for ch in query:
        if ch == " ":
            continue
        pos = text.find(ch, pos + 1)
        if pos < 0:
            return None
        score += 10
        if pos == previous + 1:
            score += 15
        if pos == 0 or not text[pos - 1].isalnum():
            score += 20
        previous = pos
    return score - len(text) // 10


def rank_projects(query: str, projects: Iterable[dict], first_ids: Iterable[int] = ()) -> List[dict]:
    """Projects in picker order, each with '_spans' (matched letters, for bold).

    No query: first_ids (e.g. recent targets) in that order, then pinned, then by name.
    With a query: names whose words match the query's words (ui.match) first, best first;
    then names that only contain the typed letters in order ("wr" → "Website relaunch")."""
    projects = list(projects)
    if not query.strip():
        first = {pid: i for i, pid in enumerate(first_ids)}
        ordered = sorted(projects, key=lambda p: (p['id'] not in first, first.get(p['id'], 0),
                                                   not p.get('starred'), p['title'].lower()))
        return [dict(p, _spans=[]) for p in ordered]
    ranked = []
    for p in projects:
        found = match(query, p['title'])
        if found is not None:
            ranked.append(((0, -found[0], p['title'].lower()), dict(p, _spans=found[1])))
            continue
        loose = fuzzy_score(query, p['title'])
        if loose is not None:
            ranked.append(((1, -loose, p['title'].lower()), dict(p, _spans=[])))
    return [p for _key, p in sorted(ranked, key=lambda kp: kp[0])]


class _RowDelegate(QStyledItemDelegate):
    def paint(self, painter, option, index):
        data: Dict = index.data(Qt.ItemDataRole.UserRole + 1) or {}
        selected = bool(option.state & QStyle.StateFlag.State_Selected)
        open_count = int(data.get('open_count') or 0)
        paint_sidebar_row(painter, option.rect, icon_name="pin" if data.get('starred') else "folder",
                          text=data.get('title', ''), count=str(open_count) if open_count else None,
                          alert=bool(data.get('overdue_count')), active=selected,
                          hover=bool(option.state & QStyle.StateFlag.State_MouseOver),
                          ghost=bool(data.get('hidden_in_sidebar')), spans=data.get('_spans'))

    def sizeHint(self, option, index):
        return QSize(option.rect.width(), ROW_HEIGHT)


class _SwitcherEdit(QLineEdit):
    moveRequested = pyqtSignal(int)
    accepted = pyqtSignal()
    escaped = pyqtSignal()

    def keyPressEvent(self, event):
        key = event.key()
        if key in (Qt.Key.Key_Up, Qt.Key.Key_Down):
            self.moveRequested.emit(-1 if key == Qt.Key.Key_Up else 1)
        elif key in (Qt.Key.Key_PageUp, Qt.Key.Key_PageDown):
            self.moveRequested.emit(-MAX_ROWS if key == Qt.Key.Key_PageUp else MAX_ROWS)
        elif key in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
            self.accepted.emit()
        elif key == Qt.Key.Key_Escape:
            self.escaped.emit()
        else:
            super().keyPressEvent(event)


class ProjectSwitcher(QFrame):
    """Popup with a name field and the matching projects (listed and hidden)."""
    projectChosen = pyqtSignal(int)

    def __init__(self, parent: QWidget):
        super().__init__(parent, Qt.WindowType.Popup | Qt.WindowType.FramelessWindowHint
                         | Qt.WindowType.NoDropShadowWindowHint)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self._projects: List[dict] = []
        self._current_id: Optional[int] = None

        col = QVBoxLayout(self)
        col.setContentsMargins(MARGIN + 8, MARGIN + 8, MARGIN + 8, MARGIN + 8)
        col.setSpacing(6)
        self.edit = _SwitcherEdit()
        self.edit.setPlaceholderText("Go to project…")
        self.edit.textChanged.connect(self._refill)
        self.edit.moveRequested.connect(self._move)
        self.edit.accepted.connect(self._choose_current)
        self.edit.escaped.connect(self.close)
        col.addWidget(self.edit)

        self.list = QListWidget()
        self.list.setFrameShape(QFrame.Shape.NoFrame)
        self.list.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.list.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.list.setVerticalScrollMode(QAbstractItemView.ScrollMode.ScrollPerPixel)
        self.list.setMouseTracking(True)
        self.list.setUniformItemSizes(True)
        self.list.setItemDelegate(_RowDelegate(self.list))
        self.list.setStyleSheet(f"QListWidget{{background:{T.BG};border:0}}")
        self.list.itemClicked.connect(lambda item: self._choose(item))
        col.addWidget(self.list)
        self.setFixedWidth(WIDTH + 2 * MARGIN)

    # ----- data
    def open(self, projects: List[dict], *, placeholder: str = "Go to project…",
             anchor: Optional[QWidget] = None, current_id: Optional[int] = None) -> None:
        """projects: dicts with id, title, starred, open_count, overdue_count, hidden_in_sidebar.

        anchor places the popup under that widget (else centred under the top bar); current_id
        pre-selects that project while nothing is typed.
        """
        self._projects = projects
        self._current_id = current_id
        self.edit.setPlaceholderText(placeholder)
        self.edit.blockSignals(True)
        self.edit.clear()
        self.edit.blockSignals(False)
        self._refill()
        parent = self.parentWidget()
        if anchor is not None and anchor.isVisible():
            top_left = anchor.mapToGlobal(QPoint(-MARGIN - 8, anchor.height() + 2))
            screen = anchor.screen().availableGeometry() if anchor.screen() else None
            if screen is not None:
                top_left.setX(max(screen.left(), min(top_left.x(), screen.right() - self.width())))
                if top_left.y() + self.height() > screen.bottom():
                    top_left.setY(anchor.mapToGlobal(QPoint(0, 0)).y() - self.height() - 2)
        else:
            top_left = parent.mapToGlobal(QPoint((parent.width() - self.width()) // 2, 56))
        self.move(top_left)
        self.show()
        self.raise_()
        self.activateWindow()
        self.edit.setFocus()

    def _refill(self, *_):
        self.list.clear()
        for project in rank_projects(self.edit.text(), self._projects):
            item = QListWidgetItem(project['title'])
            item.setData(PROJECT_ID_ROLE, project['id'])
            item.setData(Qt.ItemDataRole.UserRole + 1, project)
            item.setToolTip("Hidden project" if project.get('hidden_in_sidebar') else "")
            self.list.addItem(item)
        if self.list.count():
            row = 0
            if self._current_id is not None and not self.edit.text().strip():
                for i in range(self.list.count()):
                    if self.list.item(i).data(PROJECT_ID_ROLE) == self._current_id:
                        row = i
                        break
            self.list.setCurrentRow(row)
            self.list.scrollToItem(self.list.currentItem())
        rows = max(1, min(self.list.count(), MAX_ROWS))
        self.list.setFixedHeight(rows * ROW_HEIGHT)
        self.adjustSize()

    # ----- keys
    def _move(self, step: int):
        if not self.list.count():
            return
        row = max(0, min(self.list.count() - 1, self.list.currentRow() + step))
        self.list.setCurrentRow(row)
        self.list.scrollToItem(self.list.currentItem())

    def _choose_current(self):
        item = self.list.currentItem()
        if item is not None:
            self._choose(item)

    def _choose(self, item: QListWidgetItem):
        project_id = item.data(PROJECT_ID_ROLE)
        self.close()
        if project_id is not None:
            self.projectChosen.emit(int(project_id))

    # ----- chrome: square frame, hairline border, corner marks
    def paintEvent(self, event):
        painter = QPainter(self)
        frame = self.rect().adjusted(MARGIN, MARGIN, -MARGIN - 1, -MARGIN - 1)
        for i in range(4, 0, -1):
            shadow = QColor(43, 43, 45, 10)
            painter.fillRect(frame.adjusted(-i + 2, -i + 4, i - 2, i + 1), shadow)
        painter.fillRect(frame, QColor(T.BG))
        painter.setPen(QColor(T.DIVIDER))
        painter.drawRect(frame)
        draw_corner_marks(painter, QRect(frame), T.CORNER_MARK)
        painter.end()
