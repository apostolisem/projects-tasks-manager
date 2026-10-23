"""Review panel: shown above the task list while the open project or view is a review step.

It says what needs attention in the project and offers the usual review actions, so a
project can be reviewed without hunting through the list.
"""
from __future__ import annotations
import datetime
from typing import Any, Dict, Optional

from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtWidgets import QFrame, QHBoxLayout, QLabel, QMenu, QPushButton, QSizePolicy, QToolButton, QVBoxLayout, QWidget

from . import tokens as T
from .icons import icon
from .theme import body_font, heading_font
from .widgets import CTRL_LABEL, KbdLabel

# Chip keys, in display order: (stats key, singular, plural)
CHIPS = (
    ('overdue', "overdue", "overdue"),
    ('waiting', "waiting", "waiting"),
    ('undated', "without due date", "without due date"),
    ('focus', "in Focus", "in Focus"),
)


def _days_since(stamp: Optional[str]) -> Optional[int]:
    """Whole days since a stored timestamp ('YYYY-MM-DD HH:MM:SS' or ISO, UTC or local)."""
    if not stamp:
        return None
    text = str(stamp).replace('T', ' ')[:19]
    try:
        then = datetime.datetime.strptime(text, '%Y-%m-%d %H:%M:%S')
    except ValueError:
        try:
            then = datetime.datetime.strptime(text[:10], '%Y-%m-%d')
        except ValueError:
            return None
    return max(0, (datetime.datetime.now() - then).days)


def _ago(days: Optional[int]) -> str:
    if days is None:
        return "—"
    if days == 0:
        return "today"
    if days == 1:
        return "yesterday"
    return f"{days} days ago"


def last_reviewed_text(stamp: Optional[str]) -> str:
    if not stamp:
        return "Never reviewed"
    try:
        when = datetime.datetime.fromisoformat(str(stamp)[:19])
        return f"Last reviewed {when.strftime('%a %d %b %Y')}"
    except ValueError:
        return "Last reviewed earlier"


class ReviewPanel(QFrame):
    nextRequested = pyqtSignal()
    skipRequested = pyqtSignal()
    hideRequested = pyqtSignal()
    clearFocusRequested = pyqtSignal()
    clearDueRequested = pyqtSignal()
    chipClicked = pyqtSignal(str)  # 'overdue' | 'waiting' | 'undated' | 'focus'

    def __init__(self, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self.setObjectName("ReviewPanel")
        # Minimum, not Fixed: the age line wraps in a narrow list and the panel must grow with it
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Minimum)

        col = QVBoxLayout(self)
        col.setContentsMargins(12, 9, 12, 9)
        col.setSpacing(7)

        # Row 1: REVIEWING · last reviewed ........ n left
        top = QHBoxLayout()
        top.setSpacing(8)
        self.kicker = QLabel("REVIEWING")
        self.kicker.setObjectName("ReviewKicker")
        self.kicker.setFont(heading_font(12, 0.09))
        self.last_label = QLabel()
        self.last_label.setObjectName("ReviewMuted")
        self.last_label.setFont(body_font(12))
        self.left_label = QLabel()
        self.left_label.setObjectName("ReviewMuted")
        self.left_label.setFont(body_font(12))
        top.addWidget(self.kicker)
        top.addWidget(self.last_label)
        top.addStretch(1)
        top.addWidget(self.left_label)
        col.addLayout(top)

        # Row 2: counts; the ones that need attention are clickable and select those tasks
        chips = QHBoxLayout()
        chips.setSpacing(6)
        self.open_label = QLabel()
        self.open_label.setObjectName("ReviewCount")
        self.open_label.setFont(body_font(13, 500))
        chips.addWidget(self.open_label)
        self.chips: Dict[str, QToolButton] = {}
        for key, _one, _many in CHIPS:
            chip = QToolButton()
            chip.setObjectName("ReviewChip")
            chip.setFont(body_font(12, 500))
            chip.setCursor(Qt.CursorShape.PointingHandCursor)
            chip.setFocusPolicy(Qt.FocusPolicy.NoFocus)
            chip.clicked.connect(lambda _checked=False, k=key: self.chipClicked.emit(k))
            chips.addWidget(chip)
            self.chips[key] = chip
        chips.addStretch(1)
        col.addLayout(chips)

        self.age_label = QLabel()
        self.age_label.setObjectName("ReviewMuted")
        self.age_label.setFont(body_font(12))
        self.age_label.setWordWrap(True)
        col.addWidget(self.age_label)

        # Row 3: actions
        actions = QHBoxLayout()
        actions.setSpacing(6)
        self.next_button = self._button("check", "Reviewed → next", primary=True,
                                        tip="Mark this project reviewed and open the next one (Ctrl+Shift+R)")
        self.next_button.clicked.connect(self.nextRequested.emit)
        self.skip_button = self._button(None, "Skip", tip="Review this project later in this review")
        self.skip_button.clicked.connect(self.skipRequested.emit)
        self.hide_button = self._button("eyeoff", "Hide project", tip="Nothing is open here: mark it reviewed and hide it")
        self.hide_button.clicked.connect(self.hideRequested.emit)
        # Clean-up actions are less frequent: behind a "⋯" menu (each can be undone)
        self.more_button = QToolButton()
        self.more_button.setProperty("flat", True)
        self.more_button.setIcon(icon("more", 15, T.NEUTRAL_700))
        self.more_button.setToolTip("Clean up this project")
        self.more_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.more_button.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.more_button.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        self.more_button.setStyleSheet("QToolButton::menu-indicator{image:none;width:0}")
        self.more_menu = QMenu(self.more_button)
        self.clear_focus_action = self.more_menu.addAction(icon("target", 15, T.NEUTRAL_700), "")
        self.clear_focus_action.triggered.connect(self.clearFocusRequested.emit)
        self.clear_due_action = self.more_menu.addAction(icon("cal", 15, T.NEUTRAL_700), "")
        self.clear_due_action.triggered.connect(self.clearDueRequested.emit)
        self.more_button.setMenu(self.more_menu)
        for button in (self.next_button, self.skip_button, self.hide_button):
            actions.addWidget(button)
        self.shortcut_hint = KbdLabel(f"{CTRL_LABEL} ⇧ R", self)
        self.shortcut_hint.setToolTip("Reviewed → next")
        actions.addWidget(self.shortcut_hint)
        actions.addStretch(1)
        actions.addWidget(self.more_button)
        col.addLayout(actions)

    def _button(self, icon_name: Optional[str], text: str, *, primary: bool = False, tip: str = "") -> QPushButton:
        button = QPushButton(text)
        if icon_name:
            button.setIcon(icon(icon_name, 14, T.BG if primary else T.NEUTRAL_700))
        if primary:
            button.setProperty("primary", True)
        button.setToolTip(tip)
        button.setCursor(Qt.CursorShape.PointingHandCursor)
        button.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        return button

    def set_view_step(self, title: str, step: int, total: int, remaining: int) -> None:
        """A review view step (Due & overdue, Waiting, No due date) instead of a project."""
        self.last_label.setText(f"· {title} · step {step} of {total}")
        self.left_label.setText(f"then {remaining} project{'s' if remaining != 1 else ''}")
        self.open_label.setText("Go through the tasks here, then move on.")
        for chip in self.chips.values():
            chip.setVisible(False)
        self.age_label.setVisible(False)
        self.next_button.setText("Done → next")
        self.next_button.setToolTip("Finish this view and open the next step (Ctrl+Shift+R)")
        self.shortcut_hint.setToolTip("Done → next")
        for widget in (self.skip_button, self.hide_button, self.more_button):
            widget.setVisible(False)

    def set_stats(self, stats: Dict[str, Any], remaining: int) -> None:
        """stats: DB.project_review_stats(); remaining: projects left including this one."""
        self.age_label.setVisible(True)
        self.skip_button.setVisible(True)
        self.next_button.setText("Reviewed → next")
        self.next_button.setToolTip("Mark this project reviewed and open the next one (Ctrl+Shift+R)")
        self.shortcut_hint.setToolTip("Reviewed → next")
        open_count = int(stats.get('open_count') or 0)
        self.last_label.setText("· " + last_reviewed_text(stats.get('last_reviewed_at')))
        self.left_label.setText(f"{remaining} left")
        self.open_label.setText(f"{open_count} open" if open_count else "Nothing open")
        for key, one, many in CHIPS:
            count = int(stats.get(key) or 0)
            chip = self.chips[key]
            chip.setText(f"{count} {one if count == 1 else many}")
            chip.setToolTip(f"Select the {count} task{'s' if count != 1 else ''}")
            chip.setVisible(count > 0)
        parts = []
        if open_count:
            parts.append(f"Oldest open task: {_ago(_days_since(stats.get('oldest_open')))}")
        parts.append(f"Last change: {_ago(_days_since(stats.get('last_activity')))}")
        self.age_label.setText(" · ".join(parts))
        self.age_label.setToolTip("Oldest open task: how long since the open task changed least recently was "
                                  "last edited. Last change: the most recent edit to any task in the project.")
        self.hide_button.setVisible(open_count == 0)
        focus = int(stats.get('focus') or 0)
        dated = int(stats.get('dated') or 0)
        self.clear_focus_action.setText(f"Take {focus} open task{'s' if focus != 1 else ''} out of Focus")
        self.clear_focus_action.setVisible(focus > 0)
        self.clear_due_action.setText(f"Remove {dated} due date{'s' if dated != 1 else ''}")
        self.clear_due_action.setVisible(dated > 0)
        self.more_button.setVisible(focus > 0 or dated > 0)
