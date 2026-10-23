"""Start Review: which projects the review covers (visible ones, plus none / some / all hidden)."""
from __future__ import annotations
from typing import Dict, List, Optional

from PyQt6.QtWidgets import (QButtonGroup, QDialog, QHBoxLayout, QLabel, QPushButton, QRadioButton,
                             QVBoxLayout, QWidget)

from . import tokens as T
from .theme import body_font

VISIBLE, HIDDEN_OPEN, ALL = 'visible', 'hidden_open', 'all'  # ProjectListModel.REVIEW_SCOPES


def _projects(count: int) -> str:
    return f"{count} project{'' if count == 1 else 's'}"


class ReviewScopeDialog(QDialog):
    """scopes: project ids per scope (ProjectListModel.review_scopes()); initial: the scope chosen
    last time. After exec(): scope().

    Without an earlier choice "Visible and hidden projects with open tasks" is chosen (hidden
    projects that still have work are worth a look; finished ones rarely are). An option that
    adds nothing to "Visible projects only" is disabled and never chosen.
    """

    def __init__(self, scopes: Dict[str, List[int]], initial: Optional[str] = None,
                 parent: Optional[QWidget] = None):
        super().__init__(parent)
        visible, with_open, everything = (len(scopes[key]) for key in (VISIBLE, HIDDEN_OPEN, ALL))
        hidden_open, hidden_rest = with_open - visible, everything - with_open
        self.setWindowTitle("Start Review")
        self.setModal(True)
        self.setMinimumWidth(440)

        col = QVBoxLayout(self)
        col.setContentsMargins(20, 18, 20, 18)
        col.setSpacing(10)
        heading = QLabel("Start review")
        heading.setObjectName("DialogTitle")
        col.addWidget(heading)
        hidden = hidden_open + hidden_rest
        intro = QLabel(f"{_projects(hidden)} {'is' if hidden == 1 else 'are'} hidden. "
                       "Which projects should this review cover?")
        intro.setFont(body_font(13))
        intro.setWordWrap(True)
        col.addWidget(intro)

        self._group = QButtonGroup(self)
        self._buttons: Dict[str, QRadioButton] = {}
        options = (
            (VISIBLE, "&Visible projects only", _projects(visible)),
            (HIDDEN_OPEN, "Visible and hidden projects with &open tasks",
             f"{_projects(with_open)} · also {hidden_open} hidden with open tasks"
             if hidden_open else "None of the hidden projects has open tasks"),
            (ALL, "Visible and &all hidden projects",
             f"{_projects(everything)} · also {hidden_rest} hidden without open tasks"
             if hidden_rest else _projects(everything)),
        )
        self._notes: Dict[str, QLabel] = {}
        for scope, label, detail in options:
            button = QRadioButton(label)
            button.setFont(body_font(14))
            button.setAccessibleDescription(detail)
            note = QLabel(detail)
            note.setObjectName("ScopeDetail")
            note.setFont(body_font(12))
            note.setContentsMargins(23, 0, 0, 0)  # under the label, past the indicator
            note.setWordWrap(True)
            note.setBuddy(button)
            option = QVBoxLayout()
            option.setSpacing(1)
            option.addWidget(button)
            option.addWidget(note)
            self._group.addButton(button)
            self._buttons[scope] = button
            self._notes[scope] = note
            col.addLayout(option)
        # Nothing to add: the option would be the same as Visible projects only
        self._buttons[HIDDEN_OPEN].setEnabled(hidden_open > 0)
        self._notes[HIDDEN_OPEN].setEnabled(hidden_open > 0)
        if initial not in self._buttons or not self._buttons[initial].isEnabled():
            initial = HIDDEN_OPEN if hidden_open else VISIBLE
        self._buttons[initial].setChecked(True)

        buttons = QHBoxLayout()
        buttons.setSpacing(8)
        buttons.addStretch(1)
        cancel = QPushButton("Cancel")
        cancel.clicked.connect(self.reject)
        buttons.addWidget(cancel)
        self.start_button = QPushButton("Start review")
        self.start_button.setProperty("primary", True)
        self.start_button.setDefault(True)
        self.start_button.clicked.connect(self.accept)
        buttons.addWidget(self.start_button)
        col.addSpacing(4)
        col.addLayout(buttons)
        # Round indicators (the theme draws them square, like check boxes) and greyed-out
        # text for an option that adds nothing
        self.setStyleSheet(
            f"QRadioButton::indicator{{border-radius:8px}}"
            f"QRadioButton:disabled{{color:{T.NEUTRAL_400}}}"
            f"QLabel#ScopeDetail{{color:{T.NEUTRAL_600}}}"
            f"QLabel#ScopeDetail:disabled{{color:{T.NEUTRAL_400}}}")
        self._buttons[self.scope()].setFocus()

    def scope(self) -> str:
        return next(scope for scope, button in self._buttons.items() if button.isChecked())

    def set_scope(self, scope: str):
        self._buttons[scope].setChecked(True)
