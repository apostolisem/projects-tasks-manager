"""Undo toast: a small notice above the bottom edge with an optional Undo action."""
from __future__ import annotations
from typing import Callable, Optional

from PyQt6.QtCore import QSize, Qt, QTimer
from PyQt6.QtGui import QColor
from PyQt6.QtWidgets import QFrame, QGraphicsDropShadowEffect, QHBoxLayout, QLabel, QPushButton, QToolButton, QWidget

from . import tokens as T
from .icons import icon
from .theme import body_font, heading_font

BOTTOM_OFFSET = 44
AUTO_HIDE_MS = 5000


class Toast(QFrame):
    """Child overlay of the main window. show_message() replaces whatever is showing."""

    def __init__(self, parent: QWidget):
        super().__init__(parent)
        self.setObjectName("Toast")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self._undo: Optional[Callable[[], None]] = None

        self.label = QLabel("", self)
        self.label.setFont(body_font(14))

        self.undo_button = QPushButton("Undo", self)
        self.undo_button.setFont(heading_font(14))
        self.undo_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.undo_button.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.undo_button.clicked.connect(self._on_undo)

        self.close_button = QToolButton(self)
        self.close_button.setIcon(icon("x", 14, T.BG))
        self.close_button.setIconSize(QSize(14, 14))
        self.close_button.setFixedSize(24, 24)
        self.close_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.close_button.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.close_button.setToolTip("Dismiss")
        self.close_button.setStyleSheet(
            "QToolButton{border:0;padding:0;background:transparent} QToolButton:hover{background:rgba(242,242,243,31)}")
        self.close_button.clicked.connect(self.hide)

        row = QHBoxLayout(self)
        row.setContentsMargins(16, 9, 10, 9)
        row.setSpacing(10)
        row.addWidget(self.label)
        row.addWidget(self.undo_button)
        row.addWidget(self.close_button)

        shadow = QGraphicsDropShadowEffect(self)
        shadow.setBlurRadius(T.SHADOW_LG["blur"])
        shadow.setOffset(0, T.SHADOW_LG["dy"])
        shadow.setColor(QColor(*T.SHADOW_LG["color"]))
        self.setGraphicsEffect(shadow)

        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.setInterval(AUTO_HIDE_MS)
        self._timer.timeout.connect(self.hide)
        self.hide()

    def show_message(self, message: str, undo: Optional[Callable[[], None]] = None):
        self._undo = undo
        self.label.setText(message)
        self.undo_button.setVisible(undo is not None)
        self.adjustSize()
        self.reposition()
        self.show()
        self.raise_()
        self._timer.start()

    def message(self) -> str:
        return self.label.text()

    def can_undo(self) -> bool:
        return self.isVisible() and self._undo is not None

    def _on_undo(self):
        undo, self._undo = self._undo, None
        self._timer.stop()
        self.hide()
        if undo is not None:
            undo()

    def reposition(self):
        parent = self.parentWidget()
        if parent is None:
            return
        self.adjustSize()
        x = (parent.width() - self.width()) // 2
        y = parent.height() - self.height() - BOTTOM_OFFSET
        self.move(max(8, x), max(8, y))
