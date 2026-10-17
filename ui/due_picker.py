"""Due date picking: quick choices, an inline calendar, and a small dialog for the task menu.

The task detail panel embeds DueCalendar; the task list context menu opens DueDateDialog.
Both offer the same quick choices so picking a date feels the same everywhere.
"""
from __future__ import annotations
from typing import List, Optional, Tuple

from PyQt6.QtCore import QDate, Qt, pyqtSignal
from PyQt6.QtGui import QColor, QFont, QIcon, QTextCharFormat, QTransform
from PyQt6.QtWidgets import QCalendarWidget, QDialog, QHBoxLayout, QLabel, QPushButton, QToolButton, QVBoxLayout, QWidget

from . import tokens as T
from .icons import pixmap


def quick_due_dates(today: Optional[QDate] = None) -> List[Tuple[str, QDate]]:
    """(label, date) for the quick buttons: Today, Tomorrow, Next Monday, In 2 weeks."""
    today = today or QDate.currentDate()
    days_to_monday = (8 - today.dayOfWeek()) % 7 or 7
    return [
        ("Today", today),
        ("Tomorrow", today.addDays(1)),
        ("Next week", today.addDays(days_to_monday)),
        ("In 2 weeks", today.addDays(14)),
    ]


def relative_days(date: QDate) -> str:
    days = QDate.currentDate().daysTo(date)
    if days == 0:
        return "today"
    if days == 1:
        return "tomorrow"
    if days == -1:
        return "yesterday"
    return f"in {days} days" if days > 0 else f"{-days} days ago"


class DueCalendar(QCalendarWidget):
    """Month calendar tuned for picking due dates: Monday first, no week numbers, today marked."""

    picked = pyqtSignal(QDate)  # a click or Enter on a day

    def __init__(self, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self.setFirstDayOfWeek(Qt.DayOfWeek.Monday)
        self.setVerticalHeaderFormat(QCalendarWidget.VerticalHeaderFormat.NoVerticalHeader)
        # Single letters always fit; short names get clipped to "..." in narrow panes
        self.setHorizontalHeaderFormat(QCalendarWidget.HorizontalHeaderFormat.SingleLetterDayNames)
        self.setMinimumSize(280, 230)
        self.setGridVisible(False)
        self.setStyleSheet(f"""
            QCalendarWidget {{ background: #ffffff; border: 1px solid {T.DIVIDER}; }}
            QCalendarWidget QAbstractItemView {{
                background: #ffffff; color: {T.TEXT}; outline: 0; border: 0; padding: 0 6px 6px 6px;
                selection-background-color: {T.ACCENT}; selection-color: #ffffff;
            }}
            QCalendarWidget QAbstractItemView:disabled {{ color: {T.NEUTRAL_500}; }}
            QCalendarWidget QWidget#qt_calendar_navigationbar {{
                background: #ffffff; border-bottom: 1px solid {T.DIVIDER}; padding: 2px 4px;
            }}
            QCalendarWidget QToolButton {{
                color: {T.TEXT}; background: transparent; border: 0; font-weight: 600;
                padding: 4px 8px; min-height: 22px;
            }}
            QCalendarWidget QToolButton:hover {{ background: {T.HOVER}; }}
            QCalendarWidget QToolButton::menu-indicator {{ image: none; width: 0; }}
            QCalendarWidget QSpinBox {{ background: #ffffff; color: {T.TEXT}; }}
        """)
        # Weekends in the normal text color (Qt paints them red by default)
        plain = QTextCharFormat()
        plain.setForeground(QColor(T.TEXT))
        for day in (Qt.DayOfWeek.Saturday, Qt.DayOfWeek.Sunday):
            self.setWeekdayTextFormat(day, plain)
        header = QTextCharFormat()
        header.setForeground(QColor(T.TEXT))  # weekend columns take this color too
        header.setBackground(QColor("#ffffff"))
        header.setFontWeight(QFont.Weight.DemiBold)
        self.setHeaderTextFormat(header)
        right = pixmap("chevr", 16, T.NEUTRAL_800)
        arrows = {"qt_calendar_prevmonth": right.transformed(QTransform().scale(-1, 1)),
                  "qt_calendar_nextmonth": right}
        for name, pm in arrows.items():
            button = self.findChild(QToolButton, name)
            if button is not None:
                button.setIcon(QIcon(pm))
        self.setMinimumDate(QDate(2000, 1, 2))
        self.setMaximumDate(QDate(2100, 12, 31))
        today_format = QTextCharFormat()
        today_format.setFontWeight(QFont.Weight.Bold)
        today_format.setForeground(QColor(T.ACCENT))
        today_format.setFontUnderline(True)
        self.setDateTextFormat(QDate.currentDate(), today_format)
        self.clicked.connect(self.picked.emit)
        self.activated.connect(self.picked.emit)

    def show_date(self, date: Optional[QDate]):
        """Select the given date (or today) and show its month."""
        target = date if date is not None and date.isValid() else QDate.currentDate()
        self.blockSignals(True)
        self.setSelectedDate(target)
        self.blockSignals(False)
        self.setCurrentPage(target.year(), target.month())


def quick_buttons(on_pick, parent: Optional[QWidget] = None) -> QWidget:
    """A row of quick-choice buttons; on_pick(QDate) is called with the chosen date."""
    row = QWidget(parent)
    layout = QHBoxLayout(row)
    layout.setContentsMargins(0, 0, 0, 0)
    layout.setSpacing(4)
    for label, date in quick_due_dates():
        button = QPushButton(label)
        button.setToolTip(date.toString("dddd d MMMM"))
        button.clicked.connect(lambda _checked=False, d=date: on_pick(d))
        layout.addWidget(button)
    layout.addStretch(1)
    return row


class DueDateDialog(QDialog):
    """Pick a due date for one or more tasks. result_date() is None when Remove was chosen."""

    def __init__(self, parent: Optional[QWidget] = None, current: Optional[QDate] = None,
                 task_count: int = 1, can_remove: bool = True):
        super().__init__(parent)
        self.setWindowTitle("Due date")
        self.setModal(True)
        self._date: Optional[QDate] = current if current is not None and current.isValid() else None

        col = QVBoxLayout(self)
        col.setContentsMargins(14, 12, 14, 12)
        col.setSpacing(10)
        if task_count > 1:
            heading = QLabel(f"Set the due date for {task_count} tasks")
            heading.setStyleSheet(f"color:{T.NEUTRAL_700}")
            col.addWidget(heading)

        col.addWidget(quick_buttons(self._pick, self))
        self.calendar = DueCalendar(self)
        self.calendar.show_date(self._date)
        self.calendar.picked.connect(self._pick)
        self.calendar.selectionChanged.connect(self._update_summary)
        col.addWidget(self.calendar)

        self.summary = QLabel()
        self.summary.setStyleSheet(f"color:{T.NEUTRAL_700}")
        col.addWidget(self.summary)

        buttons = QHBoxLayout()
        buttons.setSpacing(6)
        self.remove_button = QPushButton("Remove due date")
        self.remove_button.setVisible(can_remove)
        self.remove_button.clicked.connect(self._remove)
        buttons.addWidget(self.remove_button)
        buttons.addStretch(1)
        cancel = QPushButton("Cancel")
        cancel.clicked.connect(self.reject)
        ok = QPushButton("Set date")
        ok.setDefault(True)
        ok.clicked.connect(lambda: self._pick(self.calendar.selectedDate()))
        buttons.addWidget(cancel)
        buttons.addWidget(ok)
        col.addLayout(buttons)

        self._removed = False
        self._update_summary()
        self.calendar.setFocus()

    def _update_summary(self):
        date = self.calendar.selectedDate()
        self.summary.setText(f"{date.toString('dddd d MMMM yyyy')} · {relative_days(date)}")

    def _pick(self, date: QDate):
        self._date = date
        self._removed = False
        self.accept()

    def _remove(self):
        self._date = None
        self._removed = True
        self.accept()

    def result_date(self) -> Optional[QDate]:
        return None if self._removed else self._date
