"""Merge tasks: name the new task that will hold the notes of the selected ones."""
from __future__ import annotations
from typing import List, Optional

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import QDialog, QHBoxLayout, QLabel, QLineEdit, QPushButton, QVBoxLayout, QWidget

from . import tokens as T
from .theme import body_font

SHOWN_TASKS = 8


class MergeTasksDialog(QDialog):
    """Asks for the merged task's title. After exec(): title()."""

    def __init__(self, task_titles: List[str], project_title: Optional[str], parent: Optional[QWidget] = None):
        super().__init__(parent)
        count = len(task_titles)
        self.setWindowTitle(f"Merge {count} tasks")
        self.setModal(True)
        self.setMinimumWidth(460)

        col = QVBoxLayout(self)
        col.setContentsMargins(20, 18, 20, 18)
        col.setSpacing(10)

        heading = QLabel(f"Merge {count} tasks" + (f" in {project_title}" if project_title else ""))
        heading.setObjectName("DialogTitle")
        heading.setWordWrap(True)
        col.addWidget(heading)

        explain = QLabel("A new task gets their notes, in this order. The merged tasks are completed "
                         "and their notes say which task they were merged into.")
        explain.setFont(body_font(13))
        explain.setStyleSheet(f"color:{T.NEUTRAL_700}")
        explain.setWordWrap(True)
        col.addWidget(explain)

        lines = [f"{i}. {title}" for i, title in enumerate(task_titles[:SHOWN_TASKS], 1)]
        if count > SHOWN_TASKS:
            lines.append(f"+{count - SHOWN_TASKS} more")
        tasks = QLabel("\n".join(lines))
        tasks.setFont(body_font(13))
        tasks.setWordWrap(True)
        tasks.setTextFormat(Qt.TextFormat.PlainText)
        col.addWidget(tasks)

        label = QLabel("Title of the new task")
        label.setFont(body_font(13))
        col.addWidget(label)
        self.title_edit = QLineEdit()
        self.title_edit.setFont(body_font(14))
        self.title_edit.setPlaceholderText("New task title")
        self.title_edit.textChanged.connect(self._sync_button)
        label.setBuddy(self.title_edit)
        col.addWidget(self.title_edit)

        buttons = QHBoxLayout()
        buttons.setSpacing(8)
        buttons.addStretch(1)
        cancel = QPushButton("Cancel")
        cancel.clicked.connect(self.reject)
        buttons.addWidget(cancel)
        self.merge_button = QPushButton("Merge")
        self.merge_button.setProperty("primary", True)
        self.merge_button.setDefault(True)
        self.merge_button.clicked.connect(self.accept)
        buttons.addWidget(self.merge_button)
        col.addLayout(buttons)

        self._sync_button()
        self.title_edit.setFocus()

    def title(self) -> str:
        return self.title_edit.text().strip()

    def _sync_button(self, *_):
        self.merge_button.setEnabled(bool(self.title()))

    def accept(self):
        if self.title():  # Enter in an empty field does nothing
            super().accept()
