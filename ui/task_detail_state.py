"""Task metadata state, independent of visible or legacy widgets."""
from dataclasses import dataclass, field
from PyQt6.QtCore import QDate


@dataclass
class TaskDetailState:
    task_id: int | None = None
    due_date: QDate | None = None
    recurrence: dict | None = None
    people: list[str] = field(default_factory=list)
