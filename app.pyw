#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Project Notes - PyQt6 Task Management App

Implements:
- Three-pane layout: Projects | Tasks | Notes
- SQLite persistence with auto-save
- Inline editing (projects/tasks), checkboxes for action/done
- Notes as rich text (HTML) with image paste/drag
- Attachments folder for images; <img src="..."> in notes HTML
- Image tools: visual crop (rubber band), numeric resize, cut
- Search: AND across general fields, OR across stakeholders; case-insensitive partial
- Sorting: action=True tasks first, then title A→Z; dynamic updates
- Hide/Show completed tasks; faded style when shown

Deps: PyQt6, Pillow
Run:  pip install -r requirements.txt
      python app.pyw
"""
from __future__ import annotations
import os, sys, sqlite3, uuid, re, datetime, hashlib, tempfile, shutil, glob, json, subprocess, calendar, mimetypes
import threading, queue, gzip, time
from typing import Optional, List, Tuple, Callable, Dict, Any
from PyQt6.QtCore import (Qt, QAbstractTableModel, QMimeData, QTimer, QSize, QRect, QModelIndex, QItemSelectionModel, QUrl, pyqtSignal, QPoint, QDate, QTime)
from PyQt6.QtCore import QSettings, QEvent
from PyQt6.QtGui import (QAction, QKeySequence, QUndoStack, QUndoCommand, QPixmap, QImage, QPalette, QFont, QPainter, QPen, QColor, QIcon, QTextCursor, QTextDocument, QDesktopServices, QTextCharFormat, QTextBlockFormat, QTextListFormat, QScreen)
from PyQt6.QtWidgets import (QApplication, QMainWindow, QWidget, QSplitter, QVBoxLayout, QHBoxLayout, QListView, QTableView, QTextEdit, QLineEdit, QToolBar, QFileDialog, QMessageBox, QStyle, QLabel, QInputDialog, QHeaderView, QDialog, QDialogButtonBox, QRubberBand, QPushButton, QCheckBox, QMenu, QComboBox, QScrollArea, QDateEdit, QTimeEdit, QGroupBox, QSpinBox, QPlainTextEdit, QAbstractSpinBox, QAbstractItemView)
from PyQt6.QtWidgets import QStyledItemDelegate, QFrame
from PIL import Image

if os.path.dirname(os.path.abspath(__file__)) not in sys.path:
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from ui import theme as ui_theme, tokens as T, menus as ui_menus
from ui.icons import icon as lucide_icon, pixmap as lucide_pixmap
from ui.painting import draw_corner_marks
from ui.widgets import TopBar, kbd_hints, CTRL_LABEL, menu_section_label, brand_icon
from ui.delegates import ExpandingLineEditDelegate
from ui.detail import TaskDetailPanel, REPEAT_CUSTOM, REPEAT_EDIT
from ui.task_detail_state import TaskDetailState
from ui.toast import Toast
from ui import search as search_query
import settings_store
from ui.project_switcher import ProjectSwitcher
from ui.review_panel import ReviewPanel
from ui.match import match as name_match
from ui.move_dialog import MoveTasksDialog
from ui.due_picker import DueDateDialog, DueCalendar
from ui.about_dialog import AboutDialog
from ui import notes_html
from ui.note_assets import NoteAssets, backup_notes
from ui.image_resize import ResizeSession, ImageResizePreview, constrained_size
from ui.notes_features import NotesFeaturesMixin, INTERNAL_MIME
from ui.task_list import (TaskListView, TaskRowDelegate, TaskListHeader, QuickAddRow, due_info,
                          parse_due_date)
from ui.sidebar import Sidebar, FlatListView, FilterEdit, paint_sidebar_row, SMART_VIEWS, SMART_VIEW_TITLES, ROW_HEIGHT as SIDEBAR_ROW_HEIGHT

# Precompile search split regex early
IMG_SRC_RE = re.compile(r'<img[^>]+src=["\']([^"\']+)["\']', re.IGNORECASE)
CSV_METADATA_UNSET = object()

# Columns written by the CSV export. Import accepts any subset that has the two title columns.
CSV_EXPORT_FIELDS = [
    'project_title', 'task_title', 'pinned', 'waiting', 'marked_mom', 'done', 'due_date',
    'stakeholders', 'notes_html', 'notes_plain', 'created_at', 'updated_at',
    'recurring', 'recurrence_rule', 'recurrence_start_at', 'recurrence_next_at', 'recurrence_last_at',
]
CSV_DELIMITERS = '|,;\t'
COMPLETION_STAMP_HTML_RE = re.compile(r'<p>---<br><b>Completed:</b> [^<]*</p>')
COMPLETION_STAMP_PLAIN_RE = re.compile(r'\n---\nCompleted: [^\n]*')


def detect_csv_delimiter(header_line: str) -> str:
    """Delimiter of a task CSV, judged from its header line only.

    csv.Sniffer looks at the first kilobyte, which is mostly note HTML full of commas and
    semicolons, and can also guess the quoting rules wrong. The header has no such noise.
    """
    header = (header_line or '').lstrip('\ufeff')
    best, best_count = '|', 0
    for candidate in CSV_DELIMITERS:
        count = header.count(candidate)
        if count > best_count:
            best, best_count = candidate, count
    return best


def _py_lower(value):
    """SQL function PYLOWER: Unicode-aware lower(). SQLite's LOWER() only folds ASCII."""
    return value.lower() if isinstance(value, str) else value

# Helper: derive plain text from HTML for backward compatibility (do not store in DB)
def html_to_text(html: str) -> str:
    if not html:
        return ""
    try:
        from html import unescape
        # Strip tags, collapse whitespace, unescape entities
        text = re.sub(r"<[^>]+>", " ", html)
        text = re.sub(r"\s+", " ", text)
        return unescape(text).strip()
    except Exception:
        return re.sub(r"<[^>]+>", " ", html)


def _parse_flexible_datetime(value: Any, *, allow_date_only: bool = False) -> Optional[datetime.datetime]:
    """Parse common task timestamp formats used by the app and CSV exports."""
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None

    try:
        normalized = text.replace("Z", "+00:00") if text.endswith("Z") else text
        return datetime.datetime.fromisoformat(normalized)
    except Exception:
        pass

    for fmt in ("%Y-%m-%d %H:%M:%S.%f", "%Y-%m-%d %H:%M:%S"):
        try:
            return datetime.datetime.strptime(text, fmt)
        except ValueError:
            continue

    if allow_date_only:
        try:
            return datetime.datetime.strptime(text, "%Y-%m-%d")
        except ValueError:
            return None
    return None

SNOOZE_OPTION_ORDER = ["today", "later_today", "tomorrow", "next_week", "weekend"]


def _settings_bool(settings: QSettings, key: str, default: bool) -> bool:
    """Read a boolean preference with sensible defaults."""
    try:
        value = settings.value(key, 'true' if default else 'false')
    except Exception:
        value = default
    if isinstance(value, str):
        return value.lower() in ('true', '1', 'yes')
    if isinstance(value, (bool, int)):
        return bool(value)
    return default


def _settings_int(settings: QSettings, key: str, default: int, minimum: int, maximum: int) -> int:
    """Read an integer preference clamped to the provided bounds."""
    try:
        value = int(settings.value(key, default))
    except Exception:
        value = default
    return max(minimum, min(maximum, value))


def load_snooze_preferences(settings: QSettings) -> Dict[str, Any]:
    """Return snooze preference flags and parameters from settings."""
    return {
        'today_enabled': _settings_bool(settings, 'Preferences/SnoozeTodayEnabled', True),
        'later_today_enabled': _settings_bool(settings, 'Preferences/SnoozeLaterTodayEnabled', True),
        'later_today_hours': _settings_int(settings, 'Preferences/SnoozeLaterTodayHours', 2, 1, 12),
        'tomorrow_enabled': _settings_bool(settings, 'Preferences/SnoozeTomorrowEnabled', True),
        'next_week_enabled': _settings_bool(settings, 'Preferences/SnoozeNextWeekEnabled', True),
        'weekend_enabled': _settings_bool(settings, 'Preferences/SnoozeWeekendEnabled', False)
    }


def format_later_today_label(hours: int) -> str:
    suffix = 'hr' if hours == 1 else 'hrs'
    return f"Later Today (+{hours}{suffix})"


def get_weekend_option_label() -> str:
    today = datetime.date.today()
    # Monday=0 ... Sunday=6
    return "Next Weekend" if today.weekday() >= 5 else "This Weekend"


def compute_snooze_due_datetime(snooze_type: str, later_today_hours: int) -> Optional[str]:
    """Compute the ISO timestamp for the requested snooze type."""
    now = datetime.datetime.now()
    today = now.date()
    if snooze_type == 'later_today':
        target = now + datetime.timedelta(hours=max(1, later_today_hours))
    elif snooze_type == 'today':
        target = datetime.datetime.combine(today, datetime.time.min)
    elif snooze_type == 'tomorrow':
        target_date = today + datetime.timedelta(days=1)
        target = datetime.datetime.combine(target_date, datetime.time.min)
    elif snooze_type == 'next_week':
        weekday = today.weekday()  # Monday=0
        days_until_next_monday = (7 - weekday) % 7
        if days_until_next_monday == 0:
            days_until_next_monday = 7
        target_date = today + datetime.timedelta(days=days_until_next_monday)
        target = datetime.datetime.combine(target_date, datetime.time.min)
    elif snooze_type == 'weekend':
        weekday = today.weekday()
        if weekday <= 4:  # Mon-Fri -> this weekend
            days_until_saturday = 5 - weekday
        else:  # Sat/Sun -> next weekend
            days_until_saturday = 12 - weekday  # 7 when Sat, 6 when Sun
        target_date = today + datetime.timedelta(days=days_until_saturday)
        target = datetime.datetime.combine(target_date, datetime.time.min)
    elif snooze_type == 'clear':
        return None
    else:
        return None
    return target.strftime("%Y-%m-%d %H:%M:%S")


RECURRENCE_FREQ_EVERY_N_DAYS = "every_n_days"
RECURRENCE_FREQ_DAILY = "daily"
RECURRENCE_FREQ_WEEKLY = "weekly"
RECURRENCE_FREQ_MONTHLY = "monthly"

RECURRENCE_MODE_FIXED = "fixed"
RECURRENCE_MODE_COMPLETION = "completion"

RECURRENCE_DEFAULT_TIME = "09:00"
RECURRENCE_DEFAULT_TIME_KEY = 'Preferences/RecurrenceDefaultTime'


def recurrence_default_time(settings: Optional[QSettings]) -> str:
    """Start time ("HH:MM") for new fixed-schedule repeats: Preferences → Planning, else 09:00."""
    value = str(settings.value(RECURRENCE_DEFAULT_TIME_KEY, RECURRENCE_DEFAULT_TIME) or '') if settings else ''
    parsed = QTime.fromString(value.strip(), "HH:mm")
    return parsed.toString("HH:mm") if parsed.isValid() else RECURRENCE_DEFAULT_TIME
RECURRENCE_WEEKDAY_LABELS = {
    1: "Mon",
    2: "Tue",
    3: "Wed",
    4: "Thu",
    5: "Fri",
    6: "Sat",
    7: "Sun",
}

def _parse_recurrence_rule(raw: Optional[str]) -> Optional[Dict[str, Any]]:
    if not raw:
        return None
    try:
        rule = json.loads(raw)
    except Exception:
        return None
    return rule if isinstance(rule, dict) else None


def _format_recurrence_datetime(dt: datetime.datetime) -> str:
    return dt.strftime("%Y-%m-%d %H:%M:%S")


def _parse_recurrence_datetime(value: Optional[str]) -> Optional[datetime.datetime]:
    if not value:
        return None
    try:
        return datetime.datetime.strptime(value, "%Y-%m-%d %H:%M:%S")
    except ValueError:
        pass
    try:
        if "T" in value:
            compact = value.replace("Z", "").split(".")[0].replace("T", " ")
            return datetime.datetime.strptime(compact, "%Y-%m-%d %H:%M:%S")
    except Exception:
        pass
    try:
        date_part = value.split()[0]
        return datetime.datetime.strptime(date_part, "%Y-%m-%d")
    except Exception:
        return None


def _parse_recurrence_time(value: Optional[str], fallback: datetime.time) -> datetime.time:
    if value:
        try:
            parts = value.split(":")
            hour = int(parts[0])
            minute = int(parts[1]) if len(parts) > 1 else 0
            return datetime.time(hour, minute)
        except Exception:
            pass
    return fallback


def _normalize_recurrence_weekdays(weekdays: Any, fallback_day: Optional[int]) -> List[int]:
    result: List[int] = []
    if isinstance(weekdays, (list, tuple)):
        for day in weekdays:
            try:
                day_int = int(day)
            except Exception:
                continue
            if 1 <= day_int <= 7:
                result.append(day_int)
    if not result and fallback_day is not None:
        result = [fallback_day]
    return sorted(set(result))


def _compute_next_recurrence(rule: Dict[str, Any], start_dt: datetime.datetime, after_dt: datetime.datetime) -> Optional[datetime.datetime]:
    if not rule or not start_dt or not after_dt:
        return None
    freq = rule.get("freq")
    interval = max(1, int(rule.get("interval", 1) or 1))
    time_of_day = _parse_recurrence_time(rule.get("time"), start_dt.time())
    anchor_dt = start_dt.replace(hour=time_of_day.hour, minute=time_of_day.minute, second=0, microsecond=0)
    after_dt = after_dt.replace(microsecond=0)

    if freq == RECURRENCE_FREQ_EVERY_N_DAYS:
        interval_days = max(1, int(rule.get("interval_days", interval) or 1))
        return _compute_next_daily(anchor_dt, after_dt, interval_days)
    if freq == RECURRENCE_FREQ_DAILY:
        return _compute_next_daily(anchor_dt, after_dt, interval)
    if freq == RECURRENCE_FREQ_WEEKLY:
        weekdays = _normalize_recurrence_weekdays(rule.get("weekdays"), anchor_dt.isoweekday())
        return _compute_next_weekly(anchor_dt, after_dt, interval, weekdays)
    if freq == RECURRENCE_FREQ_MONTHLY:
        day_of_month = int(rule.get("day_of_month", anchor_dt.day) or anchor_dt.day)
        day_of_month = max(1, min(31, day_of_month))
        return _compute_next_monthly(anchor_dt, after_dt, interval, day_of_month)
    return None


def _compute_next_after_completion(rule: Dict[str, Any], done_dt: datetime.datetime) -> Optional[datetime.datetime]:
    """'After completion' repeats: the task comes back N days/weeks/months after it was
    completed, at the rule's time of day. Weekdays and day of month do not apply."""
    if not rule or not done_dt:
        return None
    freq = rule.get("freq")
    interval = max(1, int(rule.get("interval_days", rule.get("interval", 1)) or 1)) \
        if freq == RECURRENCE_FREQ_EVERY_N_DAYS else max(1, int(rule.get("interval", 1) or 1))
    time_of_day = _parse_recurrence_time(rule.get("time"), done_dt.time())
    day = done_dt.date()
    if freq in (RECURRENCE_FREQ_EVERY_N_DAYS, RECURRENCE_FREQ_DAILY):
        day += datetime.timedelta(days=interval)
    elif freq == RECURRENCE_FREQ_WEEKLY:
        day += datetime.timedelta(weeks=interval)
    elif freq == RECURRENCE_FREQ_MONTHLY:
        index = day.year * 12 + day.month - 1 + interval
        year, month = index // 12, index % 12 + 1
        day = datetime.date(year, month, min(day.day, calendar.monthrange(year, month)[1]))
    else:
        return None
    return datetime.datetime.combine(day, time_of_day)


def _format_recurrence_when(dt: Optional[datetime.datetime]) -> str:
    return dt.strftime("%a %d %b %Y, %H:%M") if dt else "unavailable"


def _compute_next_daily(anchor_dt: datetime.datetime, after_dt: datetime.datetime, interval_days: int) -> datetime.datetime:
    interval_days = max(1, interval_days)
    if after_dt < anchor_dt:
        return anchor_dt
    elapsed = after_dt - anchor_dt
    interval_seconds = interval_days * 86400
    intervals = int(elapsed.total_seconds() // interval_seconds)
    candidate = anchor_dt + datetime.timedelta(days=interval_days * intervals)
    if candidate <= after_dt:
        candidate += datetime.timedelta(days=interval_days)
    return candidate


def _compute_next_weekly(anchor_dt: datetime.datetime, after_dt: datetime.datetime, interval_weeks: int, weekdays: List[int]) -> Optional[datetime.datetime]:
    interval_weeks = max(1, interval_weeks)
    if not weekdays:
        weekdays = [anchor_dt.isoweekday()]
    anchor_week_start = anchor_dt.date() - datetime.timedelta(days=anchor_dt.isoweekday() - 1)
    search_start = max(after_dt, anchor_dt).date()
    for offset in range(0, 400):
        candidate_date = search_start + datetime.timedelta(days=offset)
        if candidate_date.isoweekday() not in weekdays:
            continue
        week_start = candidate_date - datetime.timedelta(days=candidate_date.isoweekday() - 1)
        weeks_since_anchor = (week_start - anchor_week_start).days // 7
        if weeks_since_anchor % interval_weeks != 0:
            continue
        candidate = datetime.datetime.combine(candidate_date, anchor_dt.time())
        if candidate <= after_dt or candidate < anchor_dt:
            continue
        return candidate
    return None


def _compute_next_monthly(anchor_dt: datetime.datetime, after_dt: datetime.datetime, interval_months: int, day_of_month: int) -> Optional[datetime.datetime]:
    interval_months = max(1, interval_months)
    day_of_month = max(1, min(31, day_of_month))
    anchor_index = anchor_dt.year * 12 + (anchor_dt.month - 1)
    start_dt = max(after_dt, anchor_dt)
    candidate_index = start_dt.year * 12 + (start_dt.month - 1)
    months_since_anchor = candidate_index - anchor_index
    if months_since_anchor < 0:
        candidate_index = anchor_index
    else:
        remainder = months_since_anchor % interval_months
        if remainder:
            candidate_index += interval_months - remainder
    for _ in range(0, 240):
        year = candidate_index // 12
        month = candidate_index % 12 + 1
        last_day = calendar.monthrange(year, month)[1]
        day = min(day_of_month, last_day)
        candidate = datetime.datetime(year, month, day, anchor_dt.hour, anchor_dt.minute, 0)
        if candidate <= after_dt or candidate < anchor_dt:
            candidate_index += interval_months
            continue
        return candidate
    return None


def _format_recurrence_summary(rule: Optional[Dict[str, Any]]) -> str:
    if not rule:
        return "Recurrence"
    mode = rule.get("mode", RECURRENCE_MODE_FIXED)
    freq = rule.get("freq", RECURRENCE_FREQ_DAILY)
    interval = max(1, int(rule.get("interval", 1) or 1))
    time_str = rule.get("time")

    if mode == RECURRENCE_MODE_COMPLETION:
        if freq == RECURRENCE_FREQ_EVERY_N_DAYS:
            interval = max(1, int(rule.get("interval_days", interval) or 1))
        unit = {RECURRENCE_FREQ_WEEKLY: "week", RECURRENCE_FREQ_MONTHLY: "month"}.get(freq, "day")
        summary = f"{interval} {unit}{'s' if interval != 1 else ''} after you complete it"
        return summary + (f", at {time_str}" if time_str else "")

    if freq == RECURRENCE_FREQ_EVERY_N_DAYS:
        interval_days = max(1, int(rule.get("interval_days", interval) or 1))
        summary = "Daily" if interval_days == 1 else f"Every {interval_days} days"
    elif freq == RECURRENCE_FREQ_DAILY:
        summary = "Daily" if interval == 1 else f"Every {interval} days"
    elif freq == RECURRENCE_FREQ_WEEKLY:
        summary = "Weekly" if interval == 1 else f"Every {interval} weeks"
        weekdays = _normalize_recurrence_weekdays(rule.get("weekdays"), None)
        if weekdays:
            labels = [RECURRENCE_WEEKDAY_LABELS.get(day, str(day)) for day in weekdays]
            summary += f" on {', '.join(labels)}"
    elif freq == RECURRENCE_FREQ_MONTHLY:
        summary = "Monthly" if interval == 1 else f"Every {interval} months"
        day_of_month = int(rule.get("day_of_month", 1) or 1)
        summary += f" on day {day_of_month}"
    else:
        summary = "Recurring"

    if time_str:
        summary += f" at {time_str}"
    return summary


def _recurrence_short_label(rule: Optional[Dict[str, Any]]) -> str:
    """Compact repeat label for the task list: Daily / Weekly / Monthly / After done."""
    if not rule:
        return "Repeats"
    if rule.get("mode") == RECURRENCE_MODE_COMPLETION:
        return "After done"
    freq = rule.get("freq")
    try:
        interval = int(rule.get("interval") or rule.get("interval_days") or 1)
    except (TypeError, ValueError):
        interval = 1
    if freq in (RECURRENCE_FREQ_DAILY, RECURRENCE_FREQ_EVERY_N_DAYS):
        return "Daily" if interval <= 1 else f"Every {interval} days"
    if freq == RECURRENCE_FREQ_WEEKLY:
        return "Weekly" if interval <= 1 else f"Every {interval} weeks"
    if freq == RECURRENCE_FREQ_MONTHLY:
        return "Monthly" if interval <= 1 else f"Every {interval} months"
    return "Repeats"


def _format_recurrence_tooltip(rule: Optional[Dict[str, Any]], next_at: Optional[str]) -> Optional[str]:
    if not rule:
        return None
    summary = _format_recurrence_summary(rule)
    if next_at:
        summary += f"\nNext: {next_at}"
    return summary


# -------------------------- Activity Logger --------------------------

class ActivityLogger:
    """Thread-safe activity logger with automatic rotation."""
    
    def __init__(self, log_dir: str):
        self.log_dir = log_dir
        self.log_path = os.path.join(log_dir, 'activity.log')
        self.backup_dir = os.path.join(log_dir, 'backups')
        self.max_size = 100 * 1024 * 1024  # 100 MiB
        self.batch_delay = 0.2  # 200ms batch delay
        
        # Ensure directories exist
        os.makedirs(self.backup_dir, exist_ok=True)
        
        # Thread-safe logging queue
        self.log_queue = queue.Queue()
        self.shutdown_event = threading.Event()
        
        # Start worker thread
        self.worker_thread = threading.Thread(target=self._worker_loop, daemon=True)
        self.worker_thread.start()
    
    def _worker_loop(self):
        """Background thread worker for log writing."""
        batch = []
        last_flush = datetime.datetime.now()

        while not self.shutdown_event.is_set():
            try:
                # Get log entries with timeout
                try:
                    entry = self.log_queue.get(timeout=0.1)
                    batch.append(entry)
                    self.log_queue.task_done()
                except queue.Empty:
                    pass

                # Flush batch if we have entries and enough time has passed
                now = datetime.datetime.now()
                if batch and (now - last_flush).total_seconds() >= self.batch_delay:
                    self._flush_batch(batch)
                    batch = []
                    last_flush = now

            except Exception as e:
                print(f"Activity logger error: {e}")

        # Drain any entries still in the queue that the loop didn't pick up
        while True:
            try:
                entry = self.log_queue.get_nowait()
                batch.append(entry)
                self.log_queue.task_done()
            except queue.Empty:
                break

        # Flush all remaining entries before exiting
        if batch:
            self._flush_batch(batch)
    
    def _flush_batch(self, batch: List[str]):
        """Write batch of log entries to file."""
        if not batch:
            return
            
        try:
            # Check if rotation is needed
            if os.path.exists(self.log_path) and os.path.getsize(self.log_path) >= self.max_size:
                self._rotate_log()
            
            # Write batch to log file
            with open(self.log_path, 'a', encoding='utf-8') as f:
                for entry in batch:
                    f.write(entry + '\n')
                f.flush()
                os.fsync(f.fileno())  # Force write to disk
                
        except (OSError, IOError) as e:
            # Handle read-only filesystem or other IO errors
            print(f"Warning: Could not write to activity log: {e}")
    
    def _rotate_log(self):
        """Rotate the current log file to backup directory."""
        if not os.path.exists(self.log_path):
            return
            
        try:
            # Generate timestamp with milliseconds
            now = datetime.datetime.now()
            timestamp = now.strftime('%Y%m%d-%H%M%S-') + f"{now.microsecond // 1000:03d}"
            backup_name = f"activity-{timestamp}.log"
            backup_path = os.path.join(self.backup_dir, backup_name)
            
            # Atomic move to backup directory
            shutil.move(self.log_path, backup_path)
            print(f"Activity log rotated to: {backup_name}")
            
            # Optional: gzip the backup if gzip is available
            try:
                with open(backup_path, 'rb') as f_in:
                    with gzip.open(backup_path + '.gz', 'wb') as f_out:
                        shutil.copyfileobj(f_in, f_out)
                os.remove(backup_path)  # Remove uncompressed version
                print(f"Activity log backup compressed: {backup_name}.gz")
            except Exception:
                # Keep uncompressed if gzip fails
                pass
                
        except Exception as e:
            print(f"Warning: Could not rotate activity log: {e}")
    
    def _escape_content(self, content: str) -> str:
        """Escape content to prevent log line corruption."""
        if not content:
            return ""
        
        # Truncate if too long
        if len(content) > 4096:
            content = content[:4093] + "…"
        
        # Replace problematic characters
        content = content.replace('\n', '\\n').replace('\r', '\\r').replace('\t', '\\t')
        return content
    
    def _log_entry(self, level: str, action: str, **kwargs):
        """Queue a log entry for writing."""
        try:
            # Create timestamp with milliseconds
            now = datetime.datetime.now(datetime.UTC)
            timestamp = now.strftime('%Y-%m-%dT%H:%M:%S.') + f"{now.microsecond // 1000:03d}Z"
            
            # Build log entry
            parts = [timestamp, level, action]
            
            # Add key-value pairs
            for key, value in kwargs.items():
                if value is not None:
                    escaped_value = self._escape_content(str(value))
                    parts.append(f'{key}="{escaped_value}"')
            
            log_line = " ".join(parts)
            self.log_queue.put(log_line)
            
        except Exception as e:
            print(f"Error creating log entry: {e}")
    
    # Public API methods
    def log_task_created(self, task_id: int, title: str):
        """Log task creation."""
        self._log_entry("INFO", "task_created", id=task_id, title=title)
    
    def log_task_updated(self, task_id: int, field: str, old_value, new_value):
        """Log task field update."""
        self._log_entry("INFO", "task_updated", id=task_id, field=field, old_value=old_value, new_value=new_value)
    
    def log_task_deleted(self, task_id: int, title: str):
        """Log task deletion."""
        self._log_entry("INFO", "task_deleted", id=task_id, title=title)
    
    def log_project_created(self, project_id: int, title: str):
        """Log project creation."""
        self._log_entry("INFO", "project_created", id=project_id, title=title)
    
    def log_project_renamed(self, project_id: int, old_title: str, new_title: str):
        """Log project rename."""
        self._log_entry("INFO", "project_renamed", id=project_id, title_from=old_title, title_to=new_title)
    
    def log_project_deleted(self, project_id: int, title: str):
        """Log project deletion."""
        self._log_entry("INFO", "project_deleted", id=project_id, title=title)
    
    def shutdown(self):
        """Gracefully shutdown the logger."""
        self.shutdown_event.set()

        # Wait for the worker thread to drain the queue and flush pending entries
        if self.worker_thread.is_alive():
            self.worker_thread.join(timeout=2.0)

        if self.worker_thread.is_alive():
            print("Warning: Activity logger thread did not shutdown cleanly")
    
    def __enter__(self):
        """Context manager entry."""
        return self
    
    def __exit__(self, exc_type, exc_val, exc_tb):
        """Context manager exit with automatic cleanup."""
        self.shutdown()

APP_DIR = os.path.abspath(os.path.dirname(__file__))
DB_PATH = os.path.join(APP_DIR, 'database.sqlite')
# Per-user config folder; main() moves an old settings.ini from APP_DIR there (settings_store)
SETTINGS_PATH = settings_store.default_settings_path()

# Helper functions to get database-specific directories
def migrate_legacy_folders(db_path: str):
    """
    Migrate legacy 'attachments' and 'backups' folders to database-specific naming.
    Only runs if ALL conditions are met:
    - Database-specific folders don't exist
    - Legacy 'attachments' and 'backups' folders exist
    
    Also updates all image paths in the database from 'attachments/' to '<databasename>_attachments/'.
    """
    db_dir = os.path.dirname(os.path.abspath(db_path))
    db_name = os.path.splitext(os.path.basename(db_path))[0]
    
    # Define paths
    new_attach_dir = os.path.join(db_dir, f"{db_name}_attachments")
    new_backup_dir = os.path.join(db_dir, f"{db_name}_backups")
    old_attach_dir = os.path.join(db_dir, "attachments")
    old_backup_dir = os.path.join(db_dir, "backups")
    
    # Check if all conditions are met
    new_folders_dont_exist = not os.path.exists(new_attach_dir) and not os.path.exists(new_backup_dir)
    old_folders_exist = os.path.exists(old_attach_dir) and os.path.exists(old_backup_dir)
    
    if new_folders_dont_exist and old_folders_exist:
        try:
            # Rename attachments first, then backups.  If the second rename fails,
            # roll back the first so both folders stay in their original state and
            # the migration guard remains valid for the next startup attempt.
            os.rename(old_attach_dir, new_attach_dir)
            try:
                os.rename(old_backup_dir, new_backup_dir)
            except Exception:
                try:
                    os.rename(new_attach_dir, old_attach_dir)
                except Exception:
                    pass
                raise
            print(f"Migrated legacy folders to {db_name}_attachments and {db_name}_backups")
            
            # Update image paths in the database
            import sqlite3
            try:
                conn = sqlite3.connect(db_path)
                cursor = conn.cursor()
                
                # Update all notes that contain 'attachments/' to use new path
                cursor.execute("""
                    UPDATE notes 
                    SET content_html = REPLACE(content_html, 'attachments/', ?)
                    WHERE content_html LIKE '%attachments/%'
                """, (f"{db_name}_attachments/",))
                
                updated_count = cursor.rowcount
                conn.commit()
                conn.close()
                
                if updated_count > 0:
                    print(f"Updated {updated_count} notes with new attachment paths")
                    
            except Exception as db_error:
                print(f"Failed to update database paths: {db_error}")
                
        except Exception as e:
            print(f"Failed to migrate legacy folders: {e}")

def get_attach_dir(db_path: str) -> str:
    """Get attachments directory for a specific database."""
    db_dir = os.path.dirname(os.path.abspath(db_path))
    db_name = os.path.splitext(os.path.basename(db_path))[0]
    attach_dir = os.path.join(db_dir, f"{db_name}_attachments")
    os.makedirs(attach_dir, exist_ok=True)
    return attach_dir

def get_backup_dir(db_path: str) -> str:
    """Get backups directory for a specific database."""
    db_dir = os.path.dirname(os.path.abspath(db_path))
    db_name = os.path.splitext(os.path.basename(db_path))[0]
    backup_dir = os.path.join(db_dir, f"{db_name}_backups")
    os.makedirs(backup_dir, exist_ok=True)
    return backup_dir

# Initialize global activity logger
activity_logger = ActivityLogger(APP_DIR)

# -------------------------- Database Layer --------------------------

SCHEMA_SQL = r"""
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS projects (
  id INTEGER PRIMARY KEY,
  title TEXT NOT NULL UNIQUE COLLATE NOCASE,
  force_visibility INTEGER NOT NULL DEFAULT 0,
  hidden INTEGER NOT NULL DEFAULT 0,
  created_at DATETIME NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
  updated_at DATETIME
);

CREATE TABLE IF NOT EXISTS tasks (
  id INTEGER PRIMARY KEY,
  project_id INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
  title TEXT NOT NULL COLLATE NOCASE,
  pinned INTEGER NOT NULL DEFAULT 0,
  waiting INTEGER NOT NULL DEFAULT 0, -- False = actionable; True = waiting on something
  done INTEGER NOT NULL DEFAULT 0,
  force_visibility INTEGER NOT NULL DEFAULT 0,
  due_date DATETIME,
  recurring INTEGER NOT NULL DEFAULT 0,
  recurrence_rule TEXT,
  recurrence_start_at DATETIME,
  recurrence_next_at DATETIME,
  recurrence_last_at DATETIME,
  created_at DATETIME NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
  updated_at DATETIME
);
CREATE INDEX IF NOT EXISTS idx_tasks_project ON tasks(project_id);
-- waiting-specific composite index created after migration (see init_schema)
CREATE INDEX IF NOT EXISTS idx_tasks_done ON tasks(done);
CREATE INDEX IF NOT EXISTS idx_tasks_title_ci ON tasks(title COLLATE NOCASE);

CREATE TABLE IF NOT EXISTS notes (
  task_id INTEGER PRIMARY KEY REFERENCES tasks(id) ON DELETE CASCADE,
  content_html TEXT NOT NULL,
  plain_text TEXT,
  updated_at DATETIME
);
CREATE INDEX IF NOT EXISTS idx_notes_plain_ci ON notes(plain_text COLLATE NOCASE);

CREATE TABLE IF NOT EXISTS note_templates (
  id INTEGER PRIMARY KEY,
  title TEXT NOT NULL UNIQUE COLLATE NOCASE,
  is_default INTEGER NOT NULL DEFAULT 0,
  content_html TEXT NOT NULL,
  created_at DATETIME NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
  updated_at DATETIME NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);

CREATE TABLE IF NOT EXISTS attachments (
  id INTEGER PRIMARY KEY,
  task_id INTEGER NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
  filename TEXT NOT NULL,
  mime_type TEXT NOT NULL,
  width INTEGER,
  height INTEGER,
  created_at DATETIME NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);
CREATE INDEX IF NOT EXISTS idx_attachments_task ON attachments(task_id);

CREATE TABLE IF NOT EXISTS stakeholders (
  id INTEGER PRIMARY KEY,
  name TEXT NOT NULL UNIQUE COLLATE NOCASE
);

CREATE TABLE IF NOT EXISTS task_stakeholders (
  task_id INTEGER NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
  stakeholder_id INTEGER NOT NULL REFERENCES stakeholders(id) ON DELETE CASCADE,
  PRIMARY KEY (task_id, stakeholder_id)
);
CREATE INDEX IF NOT EXISTS idx_ts_stakeholder ON task_stakeholders(stakeholder_id);
"""

class DatabaseLockException(Exception):
    """Exception raised when database is locked by another process."""
    pass

class DatabaseLockManager:
    """Manages database locking to prevent multiple instances from accessing the same database."""
    
    def __init__(self, db_path: str):
        self.db_path = db_path
        self.lock_path = db_path + '.lock'
        self.lock_acquired = False
        self.pid = os.getpid()
    
    def acquire_lock(self):
        """Acquire lock for the database. Raises DatabaseLockException if already locked."""
        if os.path.exists(self.lock_path):
            try:
                with open(self.lock_path, 'r') as f:
                    content = f.read().strip()
                    if content:
                        lines = content.split('\n')
                        if len(lines) >= 2:
                            locked_pid = int(lines[0])
                            timestamp = lines[1]
                            
                            # Check if the process is still running
                            if self._is_process_running(locked_pid):
                                raise DatabaseLockException(
                                    f"Database is already open in another instance (PID: {locked_pid})\n"
                                    f"Locked at: {timestamp}\n"
                                    f"Please close the other instance or select a different database."
                                )
                            else:
                                # Process is dead, remove stale lock file
                                try:
                                    os.remove(self.lock_path)
                                except OSError:
                                    pass
            except (ValueError, OSError) as e:
                # Invalid lock file, remove it
                try:
                    os.remove(self.lock_path)
                except OSError:
                    pass
        
        # Create new lock file
        try:
            timestamp = datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')
            with open(self.lock_path, 'w') as f:
                f.write(f"{self.pid}\n{timestamp}\n")
            self.lock_acquired = True
        except OSError as e:
            raise DatabaseLockException(f"Failed to create lock file: {e}")
    
    def release_lock(self):
        """Release the database lock."""
        if self.lock_acquired and os.path.exists(self.lock_path):
            try:
                # Verify this is our lock file before removing it
                with open(self.lock_path, 'r') as f:
                    content = f.read().strip()
                    if content:
                        lines = content.split('\n')
                        if len(lines) >= 1 and int(lines[0]) == self.pid:
                            os.remove(self.lock_path)
            except (OSError, ValueError):
                # Best effort cleanup
                try:
                    os.remove(self.lock_path)
                except OSError:
                    pass
            finally:
                self.lock_acquired = False
    
    def _is_process_running(self, pid: int) -> bool:
        """Check if a process with the given PID is still running."""
        try:
            if sys.platform.startswith('win'):
                # On Windows, use tasklist command or import psutil if available
                try:
                    import psutil
                    return psutil.pid_exists(pid)
                except ImportError:
                    # Fallback: use tasklist command on Windows
                    import subprocess
                    try:
                        result = subprocess.run(
                            ['tasklist', '/FI', f'PID eq {pid}'],
                            capture_output=True,
                            text=True,
                            timeout=5
                        )
                        return str(pid) in result.stdout
                    except (subprocess.SubprocessError, subprocess.TimeoutExpired):
                        # If we can't check, assume it's running to be safe
                        return True
            else:
                # On Unix-like systems, sending signal 0 checks if process exists
                os.kill(pid, 0)
                return True
        except OSError:
            return False
    
    def __enter__(self):
        self.acquire_lock()
        return self
    
    def __exit__(self, exc_type, exc_val, exc_tb):
        self.release_lock()

class DB:
    def __init__(self, path: str):
        self.path = path
        self.lock_manager = DatabaseLockManager(path)
        
        # Acquire database lock before opening connection
        self.lock_manager.acquire_lock()
        
        try:
            self.conn = sqlite3.connect(self.path)
            self.conn.row_factory = sqlite3.Row
            self.conn.create_function("PYLOWER", 1, _py_lower, deterministic=True)
            self.conn.create_function("FOLD", 1, search_query.fold, deterministic=True)
            # Performance pragmas
            try:
                self.conn.execute("PRAGMA journal_mode=WAL")
                self.conn.execute("PRAGMA synchronous=NORMAL")
                self.conn.execute("PRAGMA cache_size=-8000")  # ~8MB cache
                self.conn.execute("PRAGMA temp_store=MEMORY")
                self.conn.commit()
            except Exception:
                pass
            self.init_schema()
        except Exception:
            # Release lock if database initialization fails
            self.lock_manager.release_lock()
            raise
    
    def close(self):
        """Properly close database connection and release lock."""
        if hasattr(self, 'conn') and self.conn:
            try:
                self.conn.close()
            except Exception as e:
                print(f"Error closing database connection: {e}")
            finally:
                self.conn = None

        # Release database lock
        if hasattr(self, 'lock_manager'):
            self.lock_manager.release_lock()
    
    def __enter__(self):
        """Context manager entry."""
        return self
    
    def __exit__(self, exc_type, exc_val, exc_tb):
        """Context manager exit with automatic cleanup."""
        self.close()

    def init_schema(self):
        self.conn.executescript(SCHEMA_SQL)
        # Migration + index (action -> waiting)
        try:
            cols = [r[1] for r in self.conn.execute("PRAGMA table_info(tasks)")]
            if 'action' in cols and 'waiting' not in cols:
                # Legacy column present: rename first
                self.conn.execute("ALTER TABLE tasks RENAME COLUMN action TO waiting")
                # Drop old index if any
                self.conn.execute("DROP INDEX IF EXISTS idx_tasks_project_action_title")
            # Add pinned column if missing
            if 'pinned' not in cols:
                self.conn.execute("ALTER TABLE tasks ADD COLUMN pinned INTEGER NOT NULL DEFAULT 0")
            # Add force_visibility column if missing for tasks
            if 'force_visibility' not in cols:
                self.conn.execute("ALTER TABLE tasks ADD COLUMN force_visibility INTEGER NOT NULL DEFAULT 0")
            
            # Check and add force_visibility for projects
            project_cols = [r[1] for r in self.conn.execute("PRAGMA table_info(projects)")]
            if 'force_visibility' not in project_cols:
                self.conn.execute("ALTER TABLE projects ADD COLUMN force_visibility INTEGER NOT NULL DEFAULT 0")
            
            # Add hidden column if missing for projects
            if 'hidden' not in project_cols:
                self.conn.execute("ALTER TABLE projects ADD COLUMN hidden INTEGER NOT NULL DEFAULT 0")

            # Add template default flag if missing
            template_cols = [r[1] for r in self.conn.execute("PRAGMA table_info(note_templates)")]
            if 'is_default' not in template_cols:
                self.conn.execute("ALTER TABLE note_templates ADD COLUMN is_default INTEGER NOT NULL DEFAULT 0")
            self.conn.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS idx_note_templates_single_default "
                "ON note_templates(is_default) WHERE is_default=1"
            )

            # Heal historical bad states where multiple templates are marked default
            default_template_rows = list(self.conn.execute(
                "SELECT id FROM note_templates WHERE is_default=1 ORDER BY updated_at DESC, id DESC"
            ))
            if len(default_template_rows) > 1:
                keep_id = default_template_rows[0]['id']
                self.conn.execute(
                    "UPDATE note_templates SET is_default=0 WHERE is_default=1 AND id<>?",
                    (keep_id,)
                )
            
            # Add marked_mom column if missing
            if 'marked_mom' not in cols:
                self.conn.execute("ALTER TABLE tasks ADD COLUMN marked_mom INTEGER NOT NULL DEFAULT 0")
            
            # Add manual_order column if missing
            if 'manual_order' not in cols:
                self.conn.execute("ALTER TABLE tasks ADD COLUMN manual_order INTEGER")
            
            # Add due_date column if missing
            if 'due_date' not in cols:
                self.conn.execute("ALTER TABLE tasks ADD COLUMN due_date DATETIME")

            # Add recurrence columns if missing
            if 'recurring' not in cols:
                self.conn.execute("ALTER TABLE tasks ADD COLUMN recurring INTEGER NOT NULL DEFAULT 0")
            if 'recurrence_rule' not in cols:
                self.conn.execute("ALTER TABLE tasks ADD COLUMN recurrence_rule TEXT")
            if 'recurrence_start_at' not in cols:
                self.conn.execute("ALTER TABLE tasks ADD COLUMN recurrence_start_at DATETIME")
            if 'recurrence_next_at' not in cols:
                self.conn.execute("ALTER TABLE tasks ADD COLUMN recurrence_next_at DATETIME")
            if 'recurrence_last_at' not in cols:
                self.conn.execute("ALTER TABLE tasks ADD COLUMN recurrence_last_at DATETIME")
            
            # Add effort column if missing
            if 'effort' not in cols:
                self.conn.execute("ALTER TABLE tasks ADD COLUMN effort REAL DEFAULT 0.0")
                # Set all existing tasks to have 0 effort
                self.conn.execute("UPDATE tasks SET effort = 0.0 WHERE effort IS NULL OR effort = 1.0")
            
            # Add manual_sort_enabled column if missing for projects
            if 'manual_sort_enabled' not in project_cols:
                self.conn.execute("ALTER TABLE projects ADD COLUMN manual_sort_enabled INTEGER NOT NULL DEFAULT 0")

            # Starred projects are listed first in the sidebar
            if 'starred' not in project_cols:
                self.conn.execute("ALTER TABLE projects ADD COLUMN starred INTEGER NOT NULL DEFAULT 0")

            # Periodic review: projects still to review in the running review, and when each was last reviewed
            if 'review_pending' not in project_cols:
                self.conn.execute("ALTER TABLE projects ADD COLUMN review_pending INTEGER NOT NULL DEFAULT 0")
            if 'last_reviewed_at' not in project_cols:
                self.conn.execute("ALTER TABLE projects ADD COLUMN last_reviewed_at TEXT")
            # Small key/value store for state that belongs to the database (the running review)
            self.conn.execute("CREATE TABLE IF NOT EXISTS app_meta (key TEXT PRIMARY KEY, value TEXT)")
            
            # Ensure only the correct composite index exists
            self.conn.execute("DROP INDEX IF EXISTS idx_tasks_project_action_title")
            if 'waiting' in [r[1] for r in self.conn.execute("PRAGMA table_info(tasks)")]:
                self.conn.execute("CREATE INDEX IF NOT EXISTS idx_tasks_project_waiting_title ON tasks(project_id, waiting, title)")
                # Create pinned index for efficient filtering
                self.conn.execute("CREATE INDEX IF NOT EXISTS idx_tasks_pinned ON tasks(pinned)")
                self.conn.execute("CREATE INDEX IF NOT EXISTS idx_tasks_recurrence_next ON tasks(recurrence_next_at)")
        except Exception as e:
            print(f"Warning: Database migration step failed: {e}")
        self.conn.commit()

    # Project ops
    def list_projects(self) -> List[sqlite3.Row]:
        return list(self.conn.execute("SELECT * FROM projects ORDER BY title COLLATE NOCASE"))

    def add_project(self, title: str) -> int:
        """Add a new project with a unique title, auto-incrementing if necessary."""
        base_title = title.strip() or 'Untitled'
        unique_title = base_title
        counter = 1
        
        # Keep trying with incremented numbers until we find a unique title
        while True:
            try:
                cur = self.conn.execute("INSERT INTO projects(title) VALUES (?)", (unique_title,))
                self.conn.commit()
                project_id = cur.lastrowid
                # Log project creation
                activity_logger.log_project_created(project_id, unique_title)
                return project_id
            except sqlite3.IntegrityError as e:
                if "UNIQUE constraint failed" in str(e):
                    # Generate next candidate title
                    counter += 1
                    unique_title = f"{base_title} {counter}"
                    # Safety check to prevent infinite loop
                    if counter > 1000:
                        unique_title = f"{base_title} {uuid.uuid4().hex[:8]}"
                        break
                else:
                    # Re-raise if it's a different integrity error
                    raise
        
        # Final attempt with UUID suffix if counter approach failed
        try:
            cur = self.conn.execute("INSERT INTO projects(title) VALUES (?)", (unique_title,))
            self.conn.commit()
            project_id = cur.lastrowid
            activity_logger.log_project_created(project_id, unique_title)
            return project_id
        except sqlite3.IntegrityError:
            # If even UUID suffix fails, something is seriously wrong
            raise RuntimeError("Could not create project with unique title after multiple attempts")

    def update_project_title(self, project_id: int, title: str):
        # Get old title for logging
        old_row = self.conn.execute("SELECT title FROM projects WHERE id=?", (project_id,)).fetchone()
        old_title = old_row['title'] if old_row else None
        
        self.conn.execute(
            "UPDATE projects SET title=?, updated_at=CURRENT_TIMESTAMP WHERE id=?",
            (title, project_id)
        )
        self.conn.commit()
        
        # Log project rename if title actually changed
        if old_title and old_title != title:
            activity_logger.log_project_renamed(project_id, old_title, title)

    def remove_project(self, project_id: int):
        # Get project title for logging
        old_row = self.conn.execute("SELECT title FROM projects WHERE id=?", (project_id,)).fetchone()
        old_title = old_row['title'] if old_row else None
        
        self.conn.execute("DELETE FROM projects WHERE id=?", (project_id,))
        self.conn.commit()
        
        # Log project deletion
        if old_title:
            activity_logger.log_project_deleted(project_id, old_title)
    
    def is_manual_sort_enabled(self, project_id: int) -> bool:
        """Check if manual sort is enabled for a project."""
        row = self.conn.execute(
            "SELECT manual_sort_enabled FROM projects WHERE id=?", 
            (project_id,)
        ).fetchone()
        return bool(row['manual_sort_enabled']) if row else False

    def _list_tasks_for_sort_mode(
        self,
        project_id: int,
        *,
        include_done: bool,
        pinned_only: bool = False,
        manual_sort: bool
    ) -> List[sqlite3.Row]:
        if manual_sort:
            if include_done:
                if pinned_only:
                    sql = "SELECT * FROM tasks WHERE project_id=? AND (pinned=1 OR force_visibility=1) ORDER BY COALESCE(manual_order, 999999), id"
                else:
                    sql = "SELECT * FROM tasks WHERE project_id=? AND (force_visibility=1 OR 1=1) ORDER BY COALESCE(manual_order, 999999), id"
            else:
                if pinned_only:
                    sql = "SELECT * FROM tasks WHERE project_id=? AND done=0 AND (pinned=1 OR force_visibility=1) ORDER BY COALESCE(manual_order, 999999), id"
                else:
                    sql = "SELECT * FROM tasks WHERE project_id=? AND (done=0 OR force_visibility=1) ORDER BY COALESCE(manual_order, 999999), id"
        else:
            if include_done:
                if pinned_only:
                    sql = "SELECT * FROM tasks WHERE project_id=? AND (pinned=1 OR force_visibility=1) ORDER BY done ASC, COALESCE(marked_mom, 0) DESC, pinned DESC, title COLLATE NOCASE ASC"
                else:
                    sql = """SELECT * FROM tasks WHERE project_id=? AND (force_visibility=1 OR 1=1)
                             ORDER BY
                                 done ASC,
                                 COALESCE(marked_mom, 0) DESC,
                                 pinned DESC,
                                 title COLLATE NOCASE ASC"""
            else:
                if pinned_only:
                    sql = "SELECT * FROM tasks WHERE project_id=? AND done=0 AND (pinned=1 OR force_visibility=1) ORDER BY COALESCE(marked_mom, 0) DESC, pinned DESC, title COLLATE NOCASE ASC"
                else:
                    sql = """SELECT * FROM tasks WHERE project_id=? AND (done=0 OR force_visibility=1)
                             ORDER BY
                                 COALESCE(marked_mom, 0) DESC,
                                 pinned DESC,
                                 title COLLATE NOCASE ASC"""
        return list(self.conn.execute(sql, (project_id,)))

    def _load_manual_order_rows(self, project_id: int) -> List[sqlite3.Row]:
        return list(self.conn.execute(
            """
            SELECT id, manual_order
            FROM tasks
            WHERE project_id=?
            ORDER BY COALESCE(manual_order, 999999), id
            """,
            (project_id,)
        ))

    def _has_valid_manual_order(self, rows: List[sqlite3.Row]) -> bool:
        if not rows:
            return True
        try:
            orders = [int(row['manual_order']) for row in rows]
        except (TypeError, ValueError):
            return False
        return len(set(orders)) == len(orders) and sorted(orders) == list(range(len(rows)))

    def ensure_manual_task_order(self, project_id: int, fallback_to_auto: bool = False) -> List[int]:
        rows = self._load_manual_order_rows(project_id)
        if self._has_valid_manual_order(rows):
            return [row['id'] for row in rows]
        if not rows:
            return []

        has_any_manual_order = any(row['manual_order'] is not None for row in rows)
        if fallback_to_auto and not has_any_manual_order:
            ordered_ids = [
                row['id']
                for row in self._list_tasks_for_sort_mode(
                    project_id,
                    include_done=True,
                    pinned_only=False,
                    manual_sort=False
                )
            ]
        else:
            ordered_ids = [row['id'] for row in rows]

        self.reorder_tasks(project_id, ordered_ids)
        return ordered_ids

    def insert_task_ids_in_manual_order(
        self,
        project_id: int,
        task_ids: List[int],
        *,
        before_task_id: Optional[int] = None
    ) -> List[int]:
        if not task_ids:
            return self.ensure_manual_task_order(project_id, fallback_to_auto=True)

        current_order = self.ensure_manual_task_order(project_id, fallback_to_auto=True)
        current_set = set(current_order)
        ordered_block = [task_id for task_id in task_ids if task_id in current_set]
        if not ordered_block:
            return current_order

        moving_set = set(ordered_block)
        remaining_ids = [task_id for task_id in current_order if task_id not in moving_set]
        insert_at = len(remaining_ids)
        if before_task_id in moving_set:
            before_task_id = None
        if before_task_id is not None and before_task_id in remaining_ids:
            insert_at = remaining_ids.index(before_task_id)

        new_order = remaining_ids[:insert_at] + ordered_block + remaining_ids[insert_at:]
        self.reorder_tasks(project_id, new_order)
        return new_order

    def order_task_rows_for_project(self, project_id: int, task_rows: List[sqlite3.Row]) -> List[sqlite3.Row]:
        if not task_rows:
            return []

        manual_sort = self.is_manual_sort_enabled(project_id)
        if manual_sort:
            ordered_ids = self.ensure_manual_task_order(project_id, fallback_to_auto=True)
        else:
            ordered_ids = [
                row['id']
                for row in self._list_tasks_for_sort_mode(
                    project_id,
                    include_done=True,
                    pinned_only=False,
                    manual_sort=False
                )
            ]

        rows_by_id = {row['id']: row for row in task_rows}
        ordered_rows = [rows_by_id[task_id] for task_id in ordered_ids if task_id in rows_by_id]
        if len(ordered_rows) < len(task_rows):
            ordered_row_ids = {row['id'] for row in ordered_rows}
            ordered_rows.extend(row for row in task_rows if row['id'] not in ordered_row_ids)
        return ordered_rows
    
    def set_manual_sort_enabled(self, project_id: int, enabled: bool):
        """Enable or disable manual sort for a project."""
        self.conn.execute(
            "UPDATE projects SET manual_sort_enabled=? WHERE id=?",
            (1 if enabled else 0, project_id)
        )
        self.conn.commit()
        if enabled:
            self.ensure_manual_task_order(project_id, fallback_to_auto=True)
    
    def set_all_projects_sort_mode(self, enabled: bool):
        """Set the sort mode for all projects."""
        project_ids = [row['id'] for row in self.conn.execute("SELECT id FROM projects ORDER BY id")]
        self.conn.execute(
            "UPDATE projects SET manual_sort_enabled=?",
            (1 if enabled else 0,)
        )
        self.conn.commit()
        if enabled:
            for project_id in project_ids:
                self.ensure_manual_task_order(project_id, fallback_to_auto=True)
    
    def toggle_project_hidden(self, project_id: int) -> bool:
        """Toggle the hidden flag for a project. Returns the new hidden state."""
        row = self.conn.execute(
            "SELECT hidden FROM projects WHERE id=?", 
            (project_id,)
        ).fetchone()
        
        if row is None:
            return False
        
        current_hidden = bool(row['hidden'])
        new_hidden = not current_hidden
        
        self.conn.execute("UPDATE projects SET hidden=? WHERE id=?", (1 if new_hidden else 0, project_id))
        self.conn.commit()
        
        return new_hidden

    def set_project_starred(self, project_id: int, starred: bool):
        self.conn.execute("UPDATE projects SET starred=? WHERE id=?", (1 if starred else 0, project_id))
        self.conn.commit()

    # ----- key/value state kept in the database
    def get_meta(self, key: str, default: Optional[str] = None) -> Optional[str]:
        row = self.conn.execute("SELECT value FROM app_meta WHERE key=?", (key,)).fetchone()
        return row['value'] if row else default

    def set_meta(self, key: str, value: Optional[str], *, commit: bool = True):
        if value is None:
            self.conn.execute("DELETE FROM app_meta WHERE key=?", (key,))
        else:
            self.conn.execute("INSERT OR REPLACE INTO app_meta(key, value) VALUES (?, ?)", (key, value))
        if commit:
            self.conn.commit()

    # ----- periodic review (state lives in the database, so it survives restarts)
    REVIEW_META_KEYS = ('review_started_at', 'review_include_hidden', 'review_open_task_ids',
                        'review_total', 'review_focus_cleared', 'review_due_cleared', 'review_views_pending')

    def review_active(self) -> bool:
        return self.get_meta('review_started_at') is not None

    def review_pending_ids(self) -> set:
        return {r['id'] for r in self.conn.execute("SELECT id FROM projects WHERE review_pending=1")}

    def start_review(self, project_ids: List[int], include_hidden: bool, views: List[str]):
        """Mark project_ids as still to review and remember what the summary needs."""
        open_ids = [r['id'] for r in self.conn.execute("SELECT id FROM tasks WHERE done=0")]
        with self.conn:
            self.conn.execute("UPDATE projects SET review_pending=0")
            self.conn.executemany("UPDATE projects SET review_pending=1 WHERE id=?", [(pid,) for pid in project_ids])
            for key, value in (('review_started_at', datetime.datetime.now().isoformat(timespec='seconds')),
                               ('review_include_hidden', '1' if include_hidden else '0'),
                               ('review_open_task_ids', json.dumps(open_ids)),
                               ('review_total', str(len(project_ids))),
                               ('review_focus_cleared', '0'), ('review_due_cleared', '0'),
                               ('review_views_pending', ','.join(views) if views else None)):
                self.set_meta(key, value, commit=False)

    def mark_project_reviewed(self, project_id: int):
        self.conn.execute("UPDATE projects SET review_pending=0, last_reviewed_at=? WHERE id=?",
                          (datetime.datetime.now().isoformat(timespec='seconds'), project_id))
        self.conn.commit()

    def add_review_count(self, key: str, amount: int):
        if self.review_active() and amount:
            self.set_meta(key, str(int(self.get_meta(key, '0') or 0) + amount))

    def review_summary(self) -> Dict[str, int]:
        """Numbers for the end-of-review notice."""
        total = int(self.get_meta('review_total', '0') or 0)
        pending = len(self.review_pending_ids())
        try:
            open_ids = json.loads(self.get_meta('review_open_task_ids', '[]') or '[]')
        except ValueError:
            open_ids = []
        completed = 0
        for start in range(0, len(open_ids), 500):
            chunk = open_ids[start:start + 500]
            completed += self.conn.execute(
                f"SELECT COUNT(*) FROM tasks WHERE done=1 AND id IN ({','.join('?' * len(chunk))})", chunk).fetchone()[0]
        return {'total': total, 'reviewed': max(0, total - pending), 'completed': completed,
                'focus_cleared': int(self.get_meta('review_focus_cleared', '0') or 0),
                'due_cleared': int(self.get_meta('review_due_cleared', '0') or 0)}

    def end_review(self) -> Dict[str, Any]:
        """Stop the review; returns what restore_review() needs to undo that."""
        state = {'pending': sorted(self.review_pending_ids()),
                 'meta': {key: self.get_meta(key) for key in self.REVIEW_META_KEYS}}
        with self.conn:
            self.conn.execute("UPDATE projects SET review_pending=0 WHERE review_pending=1")
            for key in self.REVIEW_META_KEYS:
                self.set_meta(key, None, commit=False)
        return state

    def restore_review(self, state: Dict[str, Any]):
        with self.conn:
            self.conn.executemany("UPDATE projects SET review_pending=1 WHERE id=?", [(pid,) for pid in state['pending']])
            for key, value in state['meta'].items():
                self.set_meta(key, value, commit=False)

    def review_views_pending(self) -> List[str]:
        raw = self.get_meta('review_views_pending') or ''
        return [key for key in raw.split(',') if key]

    def pop_review_view(self):
        views = self.review_views_pending()[1:]
        self.set_meta('review_views_pending', ','.join(views) if views else None)

    def project_review_stats(self, project_id: int) -> Dict[str, Any]:
        """What the review panel shows about a project."""
        today = datetime.date.today().isoformat()
        row = self.conn.execute(
            """SELECT COUNT(CASE WHEN done=0 THEN 1 END) AS open_count,
                      COUNT(CASE WHEN done=0 AND COALESCE(TRIM(due_date), '') != '' AND substr(due_date, 1, 10) < :today THEN 1 END) AS overdue,
                      COUNT(CASE WHEN done=0 AND waiting=1 THEN 1 END) AS waiting,
                      COUNT(CASE WHEN done=0 AND COALESCE(TRIM(due_date), '') = '' THEN 1 END) AS undated,
                      COUNT(CASE WHEN done=0 AND COALESCE(TRIM(due_date), '') != '' THEN 1 END) AS dated,
                      COUNT(CASE WHEN done=0 AND pinned=1 THEN 1 END) AS focus,
                      MIN(CASE WHEN done=0 THEN substr(replace(COALESCE(updated_at, created_at), 'T', ' '), 1, 19) END) AS oldest_open,
                      MAX(substr(replace(COALESCE(updated_at, created_at), 'T', ' '), 1, 19)) AS last_activity
               FROM tasks WHERE project_id=:pid""", {'pid': project_id, 'today': today}).fetchone()
        stats = dict(row)
        last = self.conn.execute("SELECT last_reviewed_at FROM projects WHERE id=?", (project_id,)).fetchone()
        stats['last_reviewed_at'] = last['last_reviewed_at'] if last else None
        return stats

    def is_project_hidden(self, project_id: int) -> Optional[bool]:
        """Return the current hidden flag for a project."""
        row = self.conn.execute(
            "SELECT hidden FROM projects WHERE id=?",
            (project_id,)
        ).fetchone()

        if row is None:
            return None

        return bool(row['hidden'])
    
    def update_task_manual_order(self, task_id: int, new_order: int):
        """Update the manual_order for a task."""
        self.conn.execute(
            "UPDATE tasks SET manual_order=? WHERE id=?",
            (new_order, task_id)
        )
        self.conn.commit()
    
    def reorder_tasks(self, project_id: int, task_ids: List[int]):
        """Reorder tasks by assigning sequential manual_order values."""
        actual_ids = [
            row['id']
            for row in self.conn.execute(
                "SELECT id FROM tasks WHERE project_id=? ORDER BY id",
                (project_id,)
            )
        ]
        if len(task_ids) != len(actual_ids) or len(set(task_ids)) != len(task_ids) or set(task_ids) != set(actual_ids):
            raise ValueError(f"Expected a complete task order for project {project_id}.")

        with self.conn:
            self.conn.executemany(
                "UPDATE tasks SET manual_order=? WHERE id=?",
                [(order, task_id) for order, task_id in enumerate(task_ids)]
            )

    # Task ops
    def list_tasks(self, project_id: int, include_done: bool, pinned_only: bool = False) -> List[sqlite3.Row]:
        manual_sort = self.is_manual_sort_enabled(project_id)
        if manual_sort:
            self.ensure_manual_task_order(project_id, fallback_to_auto=True)
        return self._list_tasks_for_sort_mode(
            project_id,
            include_done=include_done,
            pinned_only=pinned_only,
            manual_sort=manual_sort
        )

    def _default_task_template_html(self) -> str:
        default_template = self.get_default_note_template()
        return default_template['content_html'] if default_template else ""

    def _insert_task_with_template(self, project_id: int, title: str, template_html: str, manual_order: Optional[int] = None) -> Tuple[int, str]:
        clean_title = title.strip() or 'Untitled task'

        if manual_order is None:
            cur = self.conn.execute(
                "INSERT INTO tasks(project_id, title, force_visibility) VALUES (?, ?, ?)",
                (project_id, clean_title, 1)
            )
        else:
            cur = self.conn.execute(
                "INSERT INTO tasks(project_id, title, force_visibility, manual_order) VALUES (?, ?, ?, ?)",
                (project_id, clean_title, 1, manual_order)
            )

        task_id = cur.lastrowid
        self.conn.execute(
            "INSERT OR IGNORE INTO notes(task_id, content_html, updated_at) VALUES (?, ?, CURRENT_TIMESTAMP)",
            (task_id, template_html or "")
        )
        return task_id, clean_title

    def add_task(self, project_id: int, title: str) -> int:
        default_html = self._default_task_template_html()
        manual_order = None
        if self.is_manual_sort_enabled(project_id):
            manual_order = len(self.ensure_manual_task_order(project_id, fallback_to_auto=True))
        task_id, clean_title = self._insert_task_with_template(project_id, title, default_html, manual_order=manual_order)
        self.conn.commit()
        activity_logger.log_task_created(task_id, clean_title)
        return task_id

    def add_tasks_bulk(self, project_id: int, titles: List[str]) -> List[int]:
        clean_titles = [(title or "").strip() or 'Untitled task' for title in titles if title is not None]
        if not clean_titles:
            return []

        default_html = self._default_task_template_html()
        created_task_ids: List[int] = []
        created_titles: List[str] = []

        manual_order_start: Optional[int] = None
        if self.is_manual_sort_enabled(project_id):
            manual_order_start = len(self.ensure_manual_task_order(project_id, fallback_to_auto=True))

        with self.conn:
            for idx, clean_title in enumerate(clean_titles):
                manual_order = manual_order_start + idx if manual_order_start is not None else None
                task_id, created_title = self._insert_task_with_template(
                    project_id,
                    clean_title,
                    default_html,
                    manual_order=manual_order
                )
                created_task_ids.append(task_id)
                created_titles.append(created_title)

        for task_id, created_title in zip(created_task_ids, created_titles):
            activity_logger.log_task_created(task_id, created_title)

        return created_task_ids

    def get_task_due_date(self, task_id: int) -> Optional[str]:
        """Get the due date for a task. Returns date string or None."""
        row = self.conn.execute("SELECT due_date FROM tasks WHERE id=?", (task_id,)).fetchone()
        return row['due_date'] if row and row['due_date'] else None
    
    def set_task_due_date(self, task_id: int, due_date: Optional[str]):
        """Set the due date for a task. Pass None to clear."""
        self.conn.execute(
            "UPDATE tasks SET due_date=?, updated_at=CURRENT_TIMESTAMP WHERE id=?",
            (due_date, task_id)
        )
        self.conn.commit()
    
    def set_imported_task_state(
        self,
        task_id: int,
        *,
        pinned: Optional[bool] = None,
        done: Optional[bool] = None,
        waiting: Optional[bool] = None,
        marked_mom: Optional[bool] = None
    ):
        """Apply imported task state without side effects like completion note stamping."""
        sets = []
        params: List[Any] = []
        if pinned is not None:
            sets.append("pinned=?")
            params.append(1 if pinned else 0)
        if waiting is not None:
            sets.append("waiting=?")
            params.append(1 if waiting else 0)
        if marked_mom is not None:
            sets.append("marked_mom=?")
            params.append(1 if marked_mom else 0)
        if done is not None:
            sets.append("done=?")
            params.append(1 if done else 0)
            if done:
                sets.append("force_visibility=?")
                params.append(0)
        if not sets:
            return
        sets.append("updated_at=CURRENT_TIMESTAMP")
        params.append(task_id)
        self.conn.execute(f"UPDATE tasks SET {', '.join(sets)} WHERE id=?", params)
        self.conn.commit()

    def restore_imported_task_metadata(
        self,
        task_id: int,
        *,
        due_date: Any = CSV_METADATA_UNSET,
        created_at: Any = CSV_METADATA_UNSET,
        updated_at: Any = CSV_METADATA_UNSET
    ):
        """Restore exported task metadata after import-side mutations complete."""
        sets = []
        params: List[Any] = []
        if due_date is not CSV_METADATA_UNSET:
            sets.append("due_date=?")
            params.append(due_date)
        if created_at is not CSV_METADATA_UNSET and created_at is not None:
            sets.append("created_at=?")
            params.append(created_at)
        if updated_at is not CSV_METADATA_UNSET:
            sets.append("updated_at=?")
            params.append(updated_at)
        if not sets:
            return
        params.append(task_id)
        self.conn.execute(f"UPDATE tasks SET {', '.join(sets)} WHERE id=?", params)
        self.conn.commit()

    def get_task_recurrence(self, task_id: int) -> Dict[str, Any]:
        row = self.conn.execute(
            """
            SELECT recurring, recurrence_rule, recurrence_start_at,
                   recurrence_next_at, recurrence_last_at, done
            FROM tasks WHERE id=?
            """,
            (task_id,)
        ).fetchone()
        if not row:
            return {}
        return {
            'recurring': bool(row['recurring']),
            'recurrence_rule': row['recurrence_rule'],
            'recurrence_start_at': row['recurrence_start_at'],
            'recurrence_next_at': row['recurrence_next_at'],
            'recurrence_last_at': row['recurrence_last_at'],
            'done': bool(row['done'])
        }

    def set_task_recurrence(
        self,
        task_id: int,
        *,
        recurring: bool,
        rule_text: Optional[str],
        start_at: Optional[str],
        next_at: Optional[str],
        last_at: Optional[str]
    ):
        self.conn.execute(
            """
            UPDATE tasks
            SET recurring=?,
                recurrence_rule=?,
                recurrence_start_at=?,
                recurrence_next_at=?,
                recurrence_last_at=?,
                updated_at=CURRENT_TIMESTAMP
            WHERE id=?
            """,
            (1 if recurring else 0, rule_text, start_at, next_at, last_at, task_id)
        )
        self.conn.commit()

    def list_due_recurring_tasks(self, now_str: str) -> List[sqlite3.Row]:
        return list(self.conn.execute(
            """
            SELECT * FROM tasks
            WHERE recurring=1
              AND recurrence_next_at IS NOT NULL
              AND TRIM(recurrence_next_at) <> ''
              AND recurrence_next_at <= ?
            """,
            (now_str,)
        ))

    def apply_recurrence_occurrence(
        self,
        task_id: int,
        *,
        next_at: Optional[str],
        last_at: Optional[str],
        due_date: Optional[str],
    ):
        self.conn.execute(
            """
            UPDATE tasks
            SET pinned=1,
                done=0,
                recurrence_next_at=?,
                recurrence_last_at=?,
                due_date=?,
                updated_at=CURRENT_TIMESTAMP
            WHERE id=?
            """,
            (next_at, last_at, due_date, task_id)
        )
        self.conn.commit()

    def update_task(self, task_id: int, *, title: Optional[str]=None, pinned: Optional[bool]=None, waiting: Optional[bool]=None, done: Optional[bool]=None, marked_mom: Optional[bool]=None):
        # Get current values for logging
        old_row = self.conn.execute("SELECT title, pinned, waiting, done, marked_mom FROM tasks WHERE id=?", (task_id,)).fetchone()
        old_values = {}
        if old_row:
            # Handle marked_mom which might not exist in older databases
            try:
                marked_mom_val = bool(old_row['marked_mom'])
            except (KeyError, IndexError):
                marked_mom_val = False
            
            old_values = {
                'title': old_row['title'],
                'pinned': bool(old_row['pinned']),
                'waiting': bool(old_row['waiting']),
                'done': bool(old_row['done']),
                'marked_mom': marked_mom_val
            }
        
        prev_done_val = None
        if done is not None and done:
            if old_row:
                prev_done_val = old_row['done']
        sets = []
        params: List = []
        
        # Track changes for logging
        changes = {}
        
        if title is not None:
            sets.append("title=?"); params.append(title)
            if old_values.get('title') != title:
                changes['title'] = (old_values.get('title'), title)
        if pinned is not None:
            sets.append("pinned=?"); params.append(1 if pinned else 0)
            if old_values.get('pinned') != pinned:
                changes['pinned'] = (old_values.get('pinned'), pinned)
        if waiting is not None:
            sets.append("waiting=?"); params.append(1 if waiting else 0)
            if old_values.get('waiting') != waiting:
                changes['waiting'] = (old_values.get('waiting'), waiting)
        if done is not None:
            sets.append("done=?"); params.append(1 if done else 0)
            if old_values.get('done') != done:
                changes['done'] = (old_values.get('done'), done)
            # Clear force_visibility when task is marked as complete
            if done:
                sets.append("force_visibility=?"); params.append(0)
                changes['force_visibility'] = ('cleared when marked complete', 0)
        if marked_mom is not None:
            sets.append("marked_mom=?"); params.append(1 if marked_mom else 0)
            if old_values.get('marked_mom') != marked_mom:
                changes['marked_mom'] = (old_values.get('marked_mom'), marked_mom)
        
        if not sets:
            return
            
        sets.append("updated_at=CURRENT_TIMESTAMP")
        sql = f"UPDATE tasks SET {', '.join(sets)} WHERE id=?"; params.append(task_id)
        self.conn.execute(sql, params)
        
        # Append completion date stamp to notes on first completion
        if done and prev_done_val == 0:
            note_row = self.conn.execute("SELECT content_html, plain_text FROM notes WHERE task_id=?", (task_id,)).fetchone()
            if note_row:
                html = note_row['content_html'] or ""; plain = note_row['plain_text'] or ""
                today = datetime.date.today()
                date_str = today.isoformat()
                
                # Calculate custom week format: wkYYWW.D
                year_short = today.strftime("%y")  # YY: last two digits of year
                week_num = today.isocalendar()[1]   # WW: ISO week number
                weekday = today.isocalendar()[2]    # D: ISO weekday (1=Monday, 7=Sunday)
                custom_date = f"wk{year_short}{week_num:02d}.{weekday}"
                
                stamp_html = f"<p>---<br><b>Completed:</b> {date_str} ({custom_date})</p>"
                stamp_plain = f"\n---\nCompleted: {date_str} ({custom_date})"
                html += stamp_html; plain += stamp_plain
                self.conn.execute("UPDATE notes SET content_html=?, plain_text=?, updated_at=CURRENT_TIMESTAMP WHERE task_id=?", (html, plain, task_id))
        
        self.conn.commit()
        
        # Update recurrence schedule for completion-based recurring tasks
        if 'done' in changes:
            self._update_recurrence_on_done_change(task_id, bool(changes['done'][1]))
        
        # Log changes
        for field, (old_val, new_val) in changes.items():
            activity_logger.log_task_updated(task_id, field, old_val, new_val)

    def _update_recurrence_on_done_change(self, task_id: int, became_done: bool):
        """Update recurrence schedule when a completion-based task changes done state."""
        try:
            row = self.conn.execute(
                "SELECT recurring, recurrence_rule FROM tasks WHERE id=?",
                (task_id,)
            ).fetchone()
            if not row or not bool(row['recurring']):
                return
            rule = _parse_recurrence_rule(row['recurrence_rule'])
            if not rule or rule.get("mode") != RECURRENCE_MODE_COMPLETION:
                return

            now_dt = datetime.datetime.now()
            if became_done:
                start_dt = now_dt
                next_dt = _compute_next_after_completion(rule, start_dt)
                next_at = _format_recurrence_datetime(next_dt) if next_dt else None
                self.conn.execute(
                    """
                    UPDATE tasks
                    SET recurrence_start_at=?,
                        recurrence_next_at=?,
                        updated_at=CURRENT_TIMESTAMP
                    WHERE id=?
                    """,
                    (_format_recurrence_datetime(start_dt), next_at, task_id)
                )
            else:
                self.conn.execute(
                    "UPDATE tasks SET recurrence_next_at=NULL, updated_at=CURRENT_TIMESTAMP WHERE id=?",
                    (task_id,)
                )
            self.conn.commit()
        except Exception as e:
            print(f"Warning: Could not update recurrence schedule: {e}")

    def remove_task(self, task_id: int):
        # Get task info for logging and project checking
        old_row = self.conn.execute("SELECT title, project_id FROM tasks WHERE id=?", (task_id,)).fetchone()
        old_title = old_row['title'] if old_row else None
        project_id = old_row['project_id'] if old_row else None

        # delete attachments files first
        seen_paths = set()
        for row in list(self.conn.execute("SELECT filename FROM attachments WHERE task_id=?", (task_id,))):
            fpath = self._attachment_abs_path(row['filename'])
            if not fpath or fpath in seen_paths:
                continue
            seen_paths.add(fpath)
            # The same image can sit in another task's note (cut / paste between notes): keep the file
            shared = self.conn.execute(
                "SELECT 1 FROM attachments WHERE filename=? AND task_id<>? LIMIT 1", (row['filename'], task_id)
            ).fetchone()
            if shared:
                continue
            try:
                if os.path.isfile(fpath):
                    os.remove(fpath)
            except Exception:
                pass
        self.conn.execute("DELETE FROM tasks WHERE id=?", (task_id,))
        self.conn.commit()

        if project_id and self.is_manual_sort_enabled(project_id):
            self.ensure_manual_task_order(project_id, fallback_to_auto=True)
        
        # Log task deletion
        if old_title:
            activity_logger.log_task_deleted(task_id, old_title)

    # Notes
    def get_note(self, task_id: int) -> Tuple[str, str]:
        row = self.conn.execute("SELECT content_html, plain_text FROM notes WHERE task_id=?", (task_id,)).fetchone()
        if row is None:
            self.conn.execute(
                "INSERT INTO notes(task_id, content_html, plain_text, updated_at) VALUES (?, '', '', CURRENT_TIMESTAMP)",
                (task_id,))
            self.conn.commit()
            return "", ""
        return row['content_html'] or "", row['plain_text'] or ""

    def _db_dir(self) -> str:
        return os.path.dirname(os.path.abspath(self.path))

    def _attachment_abs_path(self, filename: Optional[str], attach_dir: Optional[str] = None) -> str:
        if not filename:
            return ""
        candidate = str(filename).strip()
        if not candidate:
            return ""
        if candidate.startswith("file://"):
            candidate = QUrl(candidate).toLocalFile()
        if os.path.isabs(candidate):
            return os.path.normpath(candidate)

        target_attach_dir = attach_dir or get_attach_dir(self.path)
        if "/" not in candidate and "\\" not in candidate:
            return os.path.join(target_attach_dir, candidate)
        return os.path.normpath(os.path.join(self._db_dir(), candidate.replace("/", os.sep)))

    def _normalize_local_note_image_path(self, src: Optional[str], attach_dir: Optional[str] = None) -> Optional[str]:
        if not src:
            return None

        candidate = str(src).strip()
        if not candidate or candidate.startswith(("http://", "https://", "data:", "mailto:")):
            return None

        target_attach_dir = os.path.abspath(attach_dir or get_attach_dir(self.path))
        abs_path = os.path.abspath(self._attachment_abs_path(candidate, target_attach_dir))
        if not os.path.isfile(abs_path):
            return None

        try:
            if os.path.commonpath([abs_path, target_attach_dir]) != target_attach_dir:
                return None
        except ValueError:
            return None

        return os.path.relpath(abs_path, self._db_dir()).replace(os.sep, "/")

    def _collect_note_attachments(self, html: str, attach_dir: Optional[str] = None) -> List[Dict[str, Any]]:
        attachments: List[Dict[str, Any]] = []
        seen: set[str] = set()
        if not html:
            return attachments

        target_attach_dir = attach_dir or get_attach_dir(self.path)
        for src in IMG_SRC_RE.findall(html):
            rel_path = self._normalize_local_note_image_path(src, target_attach_dir)
            if not rel_path or rel_path in seen:
                continue
            seen.add(rel_path)

            abs_path = self._attachment_abs_path(rel_path, target_attach_dir)
            mime_type = mimetypes.guess_type(abs_path)[0] or "application/octet-stream"
            width = None
            height = None
            try:
                with Image.open(abs_path) as image:
                    width, height = image.size
            except Exception:
                pass

            attachments.append({
                "filename": rel_path,
                "mime_type": mime_type,
                "width": width,
                "height": height,
            })
        return attachments

    def _replace_task_attachments(self, task_id: int, attachments: List[Dict[str, Any]]):
        self.conn.execute("DELETE FROM attachments WHERE task_id=?", (task_id,))
        for item in attachments:
            self.conn.execute(
                """
                INSERT INTO attachments(task_id, filename, mime_type, width, height)
                VALUES (?, ?, ?, ?, ?)
                """,
                (task_id, item["filename"], item["mime_type"], item["width"], item["height"])
            )

    def _write_note(self, task_id: int, html: str, plain: Optional[str], attach_dir: Optional[str] = None):
        html = html or ""
        plain_text = plain if plain is not None else html_to_text(html)
        self.conn.execute(
            "INSERT OR IGNORE INTO notes(task_id, content_html, plain_text, updated_at) VALUES (?, '', '', CURRENT_TIMESTAMP)",
            (task_id,)
        )
        self.conn.execute(
            "UPDATE notes SET content_html=?, plain_text=?, updated_at=CURRENT_TIMESTAMP WHERE task_id=?",
            (html, plain_text or "", task_id)
        )
        self._replace_task_attachments(task_id, self._collect_note_attachments(html, attach_dir))

    def save_note(self, task_id: int, html: str, plain: Optional[str], attach_dir: Optional[str] = None):
        with self.conn:
            self._write_note(task_id, html, plain, attach_dir)

    def sync_note_attachments(self, task_id: int, attach_dir: Optional[str] = None, html: Optional[str] = None):
        if html is None:
            row = self.conn.execute("SELECT content_html FROM notes WHERE task_id=?", (task_id,)).fetchone()
            html = row['content_html'] if row else ""
        with self.conn:
            self._replace_task_attachments(task_id, self._collect_note_attachments(html or "", attach_dir))

    def backfill_note_attachments(self, attach_dir: Optional[str] = None):
        rows = list(self.conn.execute("SELECT task_id, content_html FROM notes"))
        if not rows:
            return
        with self.conn:
            for row in rows:
                self._replace_task_attachments(
                    row['task_id'],
                    self._collect_note_attachments(row['content_html'] or "", attach_dir)
                )

    def _clone_note_local_images(self, html: str, attach_dir: Optional[str] = None) -> str:
        if not html:
            return html or ""

        target_attach_dir = attach_dir or get_attach_dir(self.path)
        cloned_paths: Dict[str, str] = {}

        def _replace(match):
            src = match.group(1)
            rel_path = self._normalize_local_note_image_path(src, target_attach_dir)
            if not rel_path:
                return match.group(0)

            cloned_rel_path = cloned_paths.get(rel_path)
            if cloned_rel_path is None:
                source_abs_path = self._attachment_abs_path(rel_path, target_attach_dir)
                ext = os.path.splitext(source_abs_path)[1] or ".png"
                new_name = f"{uuid.uuid4().hex}{ext}"
                dest_abs_path = os.path.join(target_attach_dir, new_name)
                shutil.copy2(source_abs_path, dest_abs_path)
                cloned_rel_path = os.path.relpath(dest_abs_path, self._db_dir()).replace(os.sep, "/")
                cloned_paths[rel_path] = cloned_rel_path

            return match.group(0).replace(src, cloned_rel_path, 1)

        return IMG_SRC_RE.sub(_replace, html)

    def duplicate_task(self, task_id: int, attach_dir: Optional[str] = None, title_suffix: str = " (copy)") -> int:
        original_row = self.conn.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
        if original_row is None:
            raise ValueError(f"Task {task_id} was not found.")

        original = dict(original_row) if not isinstance(original_row, sqlite3.Row) else {key: original_row[key] for key in original_row.keys()}
        attach_dir = attach_dir or get_attach_dir(self.path)
        original_title = (original.get('title') or 'Untitled task').strip()
        new_title = f"{original_title}{title_suffix}"
        project_id = int(original.get('project_id'))
        manual_sort = self.is_manual_sort_enabled(project_id)
        before_task_id = None
        if manual_sort:
            current_order = self.ensure_manual_task_order(project_id, fallback_to_auto=True)
            try:
                source_index = current_order.index(task_id)
            except ValueError:
                source_index = -1
            if source_index >= 0 and source_index + 1 < len(current_order):
                before_task_id = current_order[source_index + 1]

        with self.conn:
            insert_cursor = self.conn.execute(
                """
                INSERT INTO tasks (
                    project_id, title, pinned, waiting, done, force_visibility,
                    due_date, manual_order, marked_mom,
                    recurring, recurrence_rule, recurrence_start_at, recurrence_next_at, recurrence_last_at,
                    created_at, updated_at
                ) VALUES (
                    ?, ?, ?, ?, ?, ?, ?, ?, ?,
                    ?, ?, ?, ?, ?,
                    CURRENT_TIMESTAMP, CURRENT_TIMESTAMP
                )
                """,
                (
                    project_id,
                    new_title,
                    original.get('pinned', 0),
                    original.get('waiting', 0),
                    original.get('done', 0),
                    original.get('force_visibility', 0),
                    original.get('due_date'),
                    None,
                    original.get('marked_mom', 0),
                    original.get('recurring', 0),
                    original.get('recurrence_rule'),
                    original.get('recurrence_start_at'),
                    original.get('recurrence_next_at'),
                    original.get('recurrence_last_at')
                )
            )
            new_task_id = insert_cursor.lastrowid

            stakeholder_rows = self.conn.execute(
                "SELECT stakeholder_id FROM task_stakeholders WHERE task_id=?",
                (task_id,)
            ).fetchall()
            for stakeholder_row in stakeholder_rows:
                stakeholder_id = stakeholder_row['stakeholder_id'] if isinstance(stakeholder_row, sqlite3.Row) else stakeholder_row[0]
                self.conn.execute(
                    "INSERT INTO task_stakeholders(task_id, stakeholder_id) VALUES (?, ?)",
                    (new_task_id, stakeholder_id)
                )

            note_row = self.conn.execute(
                "SELECT content_html, plain_text FROM notes WHERE task_id=?",
                (task_id,)
            ).fetchone()
            note_html = ""
            note_plain = ""
            if note_row:
                note_html = note_row['content_html'] if isinstance(note_row, sqlite3.Row) else note_row[0]
                note_plain = note_row['plain_text'] if isinstance(note_row, sqlite3.Row) else note_row[1]
            cloned_html = self._clone_note_local_images(note_html or "", attach_dir)
            cloned_plain = note_plain if note_plain else html_to_text(cloned_html)
            self._write_note(new_task_id, cloned_html, cloned_plain, attach_dir)

        if manual_sort:
            self.insert_task_ids_in_manual_order(project_id, [new_task_id], before_task_id=before_task_id)

        activity_logger.log_task_created(new_task_id, new_title)
        return new_task_id

    def move_tasks_to_project(self, task_ids: List[int], target_project_id: int) -> List[int]:
        if not task_ids:
            return []

        placeholders = ",".join("?" for _ in task_ids)
        task_rows = {
            row['id']: row
            for row in self.conn.execute(
                f"SELECT id, project_id FROM tasks WHERE id IN ({placeholders})",
                task_ids
            )
        }

        moved_task_ids: List[int] = []
        source_project_ids: set[int] = set()
        with self.conn:
            for task_id in task_ids:
                row = task_rows.get(task_id)
                if row is None:
                    continue
                source_project_ids.add(int(row['project_id']))
                self.conn.execute(
                    """
                    UPDATE tasks
                    SET project_id=?,
                        force_visibility=0,
                        manual_order=NULL,
                        updated_at=CURRENT_TIMESTAMP
                    WHERE id=?
                    """,
                    (target_project_id, task_id)
                )
                moved_task_ids.append(task_id)

        if self.is_manual_sort_enabled(target_project_id):
            self.insert_task_ids_in_manual_order(target_project_id, moved_task_ids)

        for source_project_id in source_project_ids:
            if source_project_id != target_project_id and self.is_manual_sort_enabled(source_project_id):
                self.ensure_manual_task_order(source_project_id, fallback_to_auto=True)

        return moved_task_ids

    # Note templates
    def list_note_templates(self) -> List[sqlite3.Row]:
        return list(self.conn.execute(
            "SELECT id, title, content_html, is_default "
            "FROM note_templates ORDER BY is_default DESC, title COLLATE NOCASE"
        ))

    def get_default_note_template(self) -> Optional[sqlite3.Row]:
        return self.conn.execute(
            "SELECT id, title, content_html FROM note_templates WHERE is_default=1 LIMIT 1"
        ).fetchone()

    def add_note_template(self, title: str, content_html: str, is_default: bool = False) -> int:
        clean_title = (title or "").strip()
        if not clean_title:
            raise ValueError("Template title cannot be empty.")
        with self.conn:
            if is_default:
                self.conn.execute("UPDATE note_templates SET is_default=0 WHERE is_default=1")
            cur = self.conn.execute(
                "INSERT INTO note_templates(title, content_html, is_default, updated_at) VALUES (?, ?, ?, CURRENT_TIMESTAMP)",
                (clean_title, content_html or "", 1 if is_default else 0)
            )
        return cur.lastrowid

    def update_note_template(self, template_id: int, title: str, content_html: str, is_default: bool = False):
        clean_title = (title or "").strip()
        if not clean_title:
            raise ValueError("Template title cannot be empty.")
        with self.conn:
            if is_default:
                self.conn.execute(
                    "UPDATE note_templates SET is_default=0 WHERE is_default=1 AND id<>?",
                    (template_id,)
                )
            self.conn.execute(
                "UPDATE note_templates SET title=?, content_html=?, is_default=?, updated_at=CURRENT_TIMESTAMP WHERE id=?",
                (clean_title, content_html or "", 1 if is_default else 0, template_id)
            )

    def delete_note_template(self, template_id: int):
        self.conn.execute("DELETE FROM note_templates WHERE id=?", (template_id,))
        self.conn.commit()

    # Stakeholders
    def get_stakeholders_for_task(self, task_id: int) -> List[str]:
        sql = """
            SELECT s.name FROM stakeholders s
            JOIN task_stakeholders ts ON ts.stakeholder_id = s.id
            WHERE ts.task_id=? ORDER BY s.name COLLATE NOCASE
        """
        return [r['name'] for r in self.conn.execute(sql, (task_id,))]

    def set_stakeholders_for_task(self, task_id: int, names: List[str]):
        # Names are unique without regard to case, so "Anna" and "anna" are one stakeholder
        unique_names: List[str] = []
        seen: set[str] = set()
        for raw in names:
            name = (raw or "").strip()
            if name and name.casefold() not in seen:
                seen.add(name.casefold())
                unique_names.append(name)
        with self.conn:
            ids: List[int] = []
            for n in unique_names:
                self.conn.execute("INSERT OR IGNORE INTO stakeholders(name) VALUES (?)", (n,))
                row = self.conn.execute("SELECT id FROM stakeholders WHERE name=?", (n,)).fetchone()
                if row and row['id'] not in ids:
                    ids.append(row['id'])
            self.conn.execute("DELETE FROM task_stakeholders WHERE task_id=?", (task_id,))
            for sid in ids:
                self.conn.execute("INSERT INTO task_stakeholders(task_id, stakeholder_id) VALUES (?, ?)", (task_id, sid))

    # Search (LIKE-based partial, case- and accent-insensitive; see ui/search.py for the syntax)
    def search_tasks(self, query: "search_query.Query", pinned_only: bool = False) -> List[sqlite3.Row]:
        """Tasks matching query across all projects. Completed tasks are included unless the query
        says is:open; the caller decides whether to show them."""
        conditions: List[str] = []
        params: List = []
        like = "LIKE ? ESCAPE '\\'"
        text_fields = ("FOLD(p.title)", "FOLD(t.title)", "FOLD(COALESCE(n.plain_text, ''))")

        # Every word / phrase must occur in the project name, the task title or the notes
        for term in query.folded_terms():
            conditions.append("(" + " OR ".join(f"{f} {like}" for f in text_fields) + ")")
            params.extend([search_query.like_pattern(term)] * len(text_fields))
        # -word: none of them may contain it
        for term in query.excluded:
            conditions.append("NOT (" + " OR ".join(f"{f} {like}" for f in text_fields) + ")")
            params.extend([search_query.like_pattern(search_query.fold(term))] * len(text_fields))
        # @name: any of the stakeholders
        if query.stakeholders:
            ors = []
            for name in query.stakeholders:
                ors.append(
                    "EXISTS (SELECT 1 FROM task_stakeholders ts JOIN stakeholders s ON s.id=ts.stakeholder_id "
                    f"WHERE ts.task_id=t.id AND FOLD(s.name) {like})"
                )
                params.append(search_query.like_pattern(search_query.fold(name)))
            conditions.append("(" + " OR ".join(ors) + ")")
        # in:project: any of the projects
        if query.projects:
            conditions.append("(" + " OR ".join(f"FOLD(p.title) {like}" for _ in query.projects) + ")")
            params.extend(search_query.like_pattern(search_query.fold(name)) for name in query.projects)
        if query.status == 'done':
            conditions.append("t.done=1")
        elif query.status == 'open':
            conditions.append("t.done=0")
        if pinned_only:
            conditions.append("t.pinned=1")

        # Tasks just added (force_visibility) stay listed whatever the query
        where = "(" + (" AND ".join(conditions) or "1=1") + ") OR t.force_visibility=1"
        sql = (
            "SELECT t.* FROM tasks t JOIN projects p ON p.id=t.project_id LEFT JOIN notes n ON n.task_id=t.id "
            f"WHERE {where} ORDER BY t.done ASC, t.pinned DESC, t.title COLLATE NOCASE ASC"
        )
        return list(self.conn.execute(sql, params))

    def notes_plain_text(self, task_ids: List[int]) -> Dict[int, str]:
        """Plain text of the notes of some tasks (search excerpts and ranking)."""
        texts: Dict[int, str] = {}
        for chunk_start in range(0, len(task_ids), 500):
            chunk = list(task_ids[chunk_start:chunk_start + 500])
            marks = ",".join("?" * len(chunk))
            for r in self.conn.execute(f"SELECT task_id, plain_text FROM notes WHERE task_id IN ({marks})", chunk):
                texts[r['task_id']] = r['plain_text'] or ""
        return texts


    # ----- row snapshots for undo -----
    def snapshot_tasks(self, task_ids: List[int], project_id: Optional[int] = None) -> Dict[str, Any]:
        """Copy the rows behind some tasks (and optionally their project) so a change can be undone."""
        snapshot: Dict[str, Any] = {'project': None, 'tasks': [], 'notes': [], 'attachments': [], 'stakeholders': {}}
        if project_id is not None:
            row = self.conn.execute("SELECT * FROM projects WHERE id=?", (project_id,)).fetchone()
            snapshot['project'] = dict(row) if row else None
        for chunk_start in range(0, len(task_ids), 500):
            chunk = list(task_ids[chunk_start:chunk_start + 500])
            marks = ",".join("?" * len(chunk))
            snapshot['tasks'] += [dict(r) for r in self.conn.execute(f"SELECT * FROM tasks WHERE id IN ({marks})", chunk)]
            snapshot['notes'] += [dict(r) for r in self.conn.execute(f"SELECT * FROM notes WHERE task_id IN ({marks})", chunk)]
            snapshot['attachments'] += [dict(r) for r in self.conn.execute(f"SELECT * FROM attachments WHERE task_id IN ({marks})", chunk)]
            for r in self.conn.execute(
                "SELECT ts.task_id, s.name FROM task_stakeholders ts JOIN stakeholders s ON s.id = ts.stakeholder_id "
                f"WHERE ts.task_id IN ({marks})", chunk
            ):
                snapshot['stakeholders'].setdefault(r['task_id'], []).append(r['name'])
        return snapshot

    def _restore_rows(self, table: str, rows: List[Dict[str, Any]]):
        if not rows:
            return
        valid = {r[1] for r in self.conn.execute(f"PRAGMA table_info({table})")}
        for row in rows:
            columns = [c for c in row.keys() if c in valid]
            self.conn.execute(
                f"INSERT OR REPLACE INTO {table} ({', '.join(columns)}) VALUES ({', '.join('?' * len(columns))})",
                [row[c] for c in columns]
            )

    # What an undo may put back. A full restore is only right for rows that were deleted;
    # for a completion or a move, later edits to the task and its note must survive.
    UNDO_TASK_COLUMNS = {
        'completion': ('done', 'force_visibility', 'recurrence_start_at', 'recurrence_next_at', 'recurrence_last_at'),
        'move': ('project_id', 'manual_order', 'force_visibility'),
        'flags': ('pinned', 'due_date'),  # clearing Focus / due dates in bulk
    }

    def restore_snapshot(self, snapshot: Dict[str, Any], scope: str = 'full'):
        """Undo using the rows captured by snapshot_tasks.

        scope 'full' re-creates deleted tasks / projects with their notes, attachments and
        stakeholders. 'completion' and 'move' only reset the columns that action changed, on
        tasks that still exist; 'completion' also takes the completion stamp off the note
        when nothing else was written to the note since.
        """
        if scope in self.UNDO_TASK_COLUMNS:
            self._restore_task_columns(snapshot, scope)
            return
        try:
            if snapshot.get('project'):
                # INSERT OR REPLACE would cascade-delete the project's tasks, so only add it when missing
                exists = self.conn.execute("SELECT 1 FROM projects WHERE id=?", (snapshot['project']['id'],)).fetchone()
                if not exists:
                    self._restore_rows('projects', [snapshot['project']])
            for task in snapshot.get('tasks', []):
                exists = self.conn.execute("SELECT 1 FROM tasks WHERE id=?", (task['id'],)).fetchone()
                if exists:
                    valid = {r[1] for r in self.conn.execute("PRAGMA table_info(tasks)")}
                    columns = [c for c in task.keys() if c in valid and c != 'id']
                    self.conn.execute(
                        f"UPDATE tasks SET {', '.join(c + '=?' for c in columns)} WHERE id=?",
                        [task[c] for c in columns] + [task['id']]
                    )
                else:
                    self._restore_rows('tasks', [task])
            self._restore_rows('notes', snapshot.get('notes', []))
            self._restore_rows('attachments', snapshot.get('attachments', []))
            self.conn.commit()
        except Exception:
            self.conn.rollback()
            raise
        for task_id, names in snapshot.get('stakeholders', {}).items():
            self.set_stakeholders_for_task(task_id, names)

    def _restore_task_columns(self, snapshot: Dict[str, Any], scope: str):
        columns = self.UNDO_TASK_COLUMNS[scope]
        notes_by_task = {note['task_id']: note for note in snapshot.get('notes', [])}
        with self.conn:
            for task in snapshot.get('tasks', []):
                if not self.conn.execute("SELECT 1 FROM tasks WHERE id=?", (task['id'],)).fetchone():
                    continue  # deleted since; undoing this action must not bring it back
                if scope == 'move' and not self.conn.execute(
                        "SELECT 1 FROM projects WHERE id=?", (task.get('project_id'),)).fetchone():
                    continue  # the project it came from is gone
                present = [c for c in columns if c in task]
                self.conn.execute(
                    f"UPDATE tasks SET {', '.join(c + '=?' for c in present)}, updated_at=CURRENT_TIMESTAMP WHERE id=?",
                    [task[c] for c in present] + [task['id']]
                )
                if scope == 'completion':
                    self._remove_completion_stamp(task['id'], notes_by_task.get(task['id']))

    def _remove_completion_stamp(self, task_id: int, note_before: Optional[Dict[str, Any]]):
        """Take back the stamp update_task() appended, unless the note was edited afterwards."""
        current = self.conn.execute("SELECT content_html, plain_text FROM notes WHERE task_id=?", (task_id,)).fetchone()
        if current is None:
            return
        html_before = (note_before or {}).get('content_html') or ""
        plain_before = (note_before or {}).get('plain_text') or ""
        html_now = current['content_html'] or ""
        if not html_now.startswith(html_before) or not COMPLETION_STAMP_HTML_RE.fullmatch(html_now[len(html_before):]):
            return
        plain_now = current['plain_text'] or ""
        plain_restored = plain_before
        if not (plain_now.startswith(plain_before) and COMPLETION_STAMP_PLAIN_RE.fullmatch(plain_now[len(plain_before):])):
            plain_restored = html_to_text(html_before)
        self.conn.execute(
            "UPDATE notes SET content_html=?, plain_text=?, updated_at=CURRENT_TIMESTAMP WHERE task_id=?",
            (html_before, plain_restored, task_id)
        )

    # Open-task filters behind the sidebar smart views (alias t = tasks)
    SMART_VIEW_CONDITIONS = {
        'today': "COALESCE(TRIM(t.due_date), '') != '' AND substr(t.due_date, 1, 10) <= :today",
        'pinned': "t.pinned = 1",
        'waiting': "t.waiting = 1",
        'mom': "COALESCE(t.marked_mom, 0) = 1",
        'triage': "COALESCE(TRIM(t.due_date), '') = ''",
    }

    def list_tasks_for_smart_view(self, key: str, exclude_hidden_projects: bool = False) -> List[sqlite3.Row]:
        """Open tasks across all projects that belong to a smart view."""
        condition = self.SMART_VIEW_CONDITIONS.get(key)
        if condition is None:
            return []
        hidden_clause = " AND p.hidden = 0" if exclude_hidden_projects else ""
        order = "substr(t.due_date, 1, 10) ASC, t.title COLLATE NOCASE ASC" if key == 'today' else \
            "COALESCE(t.marked_mom, 0) DESC, t.pinned DESC, t.title COLLATE NOCASE ASC"
        sql = (
            "SELECT t.* FROM tasks t JOIN projects p ON p.id = t.project_id "
            f"WHERE t.done = 0 AND {condition}{hidden_clause} ORDER BY {order}"
        )
        return list(self.conn.execute(sql, {'today': datetime.date.today().isoformat()}))

    def smart_view_counts(self, exclude_hidden_projects: bool = False) -> Dict[str, int]:
        """Open-task count per smart view, plus 'overdue' (due before today)."""
        hidden_clause = " AND p.hidden = 0" if exclude_hidden_projects else ""
        columns = [
            f"COALESCE(SUM(CASE WHEN {condition} THEN 1 ELSE 0 END), 0) AS {key}"
            for key, condition in self.SMART_VIEW_CONDITIONS.items()
        ]
        columns.append(
            "COALESCE(SUM(CASE WHEN COALESCE(TRIM(t.due_date), '') != '' AND substr(t.due_date, 1, 10) < :today "
            "THEN 1 ELSE 0 END), 0) AS overdue"
        )
        sql = (
            f"SELECT {', '.join(columns)} FROM tasks t JOIN projects p ON p.id = t.project_id "
            f"WHERE t.done = 0{hidden_clause}"
        )
        row = self.conn.execute(sql, {'today': datetime.date.today().isoformat()}).fetchone()
        return {key: int(row[key] or 0) for key in row.keys()} if row else {}

    def clear_all_force_visibility(self):
        """Clear the force_visibility flag of tasks (projects no longer use theirs)"""
        try:
            self.conn.execute("UPDATE tasks SET force_visibility = 0 WHERE force_visibility = 1")
            self.conn.commit()
        except Exception as e:
            print(f"Error clearing force_visibility: {e}")

    def list_projects_with_stats(self):
        return list(self.conn.execute(
            """
            SELECT p.*,
                   COUNT(t.id) AS task_count,
                   COUNT(CASE WHEN t.done=1 THEN 1 END) AS done_count,
                   COUNT(CASE WHEN t.done=0 THEN 1 END) AS open_count,
                   COUNT(CASE WHEN t.done=0 AND COALESCE(TRIM(t.due_date), '') != ''
                              AND substr(t.due_date, 1, 10) < :today THEN 1 END) AS overdue_count,
                   COUNT(CASE WHEN t.done=0 AND substr(t.due_date, 1, 10) = :today THEN 1 END) AS due_today_count
            FROM projects p
            LEFT JOIN tasks t ON t.project_id=p.id
            GROUP BY p.id
            ORDER BY COALESCE(p.starred, 0) DESC, p.title COLLATE NOCASE
            """, {'today': datetime.date.today().isoformat()}))


def ensure_db_seed(db: DB):
    # Seed only if empty
    cur = db.conn.execute("SELECT COUNT(*) FROM projects")
    if cur.fetchone()[0] == 0:
        pid = db.add_project("Project Alpha")
        t1 = db.add_task(pid, "Fix pipeline")
        t2 = db.add_task(pid, "Update docs");  db.update_task(t2, done=False, pinned=True)  # demonstrate pinned task
        db.save_note(t1, "<h3>Pipeline Fix</h3><p>Re-run CI and address failing step.</p>",
                        "Pipeline Fix Re-run CI and address failing step.")
        db.save_note(t2, "<p>Docs live in /docs.</p>", "Docs live in /docs.")

# -------------------------- Models --------------------------

class ProjectListModel(QAbstractTableModel):
    COL_TITLE = 0  # Define column index for title
    renameRejected = pyqtSignal(str)  # the title that is already taken
    reviewChanged = pyqtSignal()      # a review cycle started, ended or a project was marked reviewed

    def __init__(self, db: DB, settings: QSettings = None):
        super().__init__()
        self.db = db
        self.settings = settings
        self.rows: List[sqlite3.Row] = []
        self.include_done: bool = False
        self._search_filter_ids: Optional[set[int]] = None  # when not None restrict to these project ids
        self.project_name_filter: str = ""  # Filter projects by name (case-insensitive, AND terms)
        # Review state is read from the database on every reload (see DB.start_review)
        self._review_active: bool = False
        self._review_pending: set[int] = set()
        self._review_skipped: List[int] = []  # skipped this session: moved to the end of the order
        # Sidebar support: rows hidden by the visibility rules are kept in hidden_rows and are
        # appended to rows (from hidden_start on) only while include_hidden_rows is set.
        self.include_hidden_rows: bool = False
        self.hidden_rows: List[dict] = []
        self.hidden_start: int = 0
        # While searching, keep the normal project list and expose match counts instead of
        # narrowing the list to the matching projects.
        self.search_keeps_all_projects: bool = False
        self.search_match_counts: Optional[Dict[int, int]] = None
        # The project that is open stays listed even when the visibility rules would hide it
        # (for example after its last open task is completed).
        self.keep_visible_id: Optional[int] = None
        self.kept_by_pin: bool = False
        self.reload()

    def set_include_done(self, include_done: bool):
        if self.include_done != include_done:
            self.include_done = include_done
            self.reload()

    def refresh_visibility(self):  # force reload after task status changes
        self.reload()

    def set_search_filter(self, project_ids: Optional[set[int]]):
        if project_ids is None and self._search_filter_ids is None:
            return
        if project_ids is not None and self._search_filter_ids == project_ids:
            return
        self._search_filter_ids = project_ids
        self.reload()
    
    def set_search_matches(self, counts: Optional[Dict[int, int]]):
        """Search mode for the sidebar: counts maps project id -> number of matching tasks."""
        self.search_match_counts = dict(counts) if counts is not None else None
        ids = set(counts.keys()) if counts is not None else None
        if ids == self._search_filter_ids:
            if self.rows:
                self.dataChanged.emit(self.index(0, 0), self.index(len(self.rows) - 1, 0))
            return
        self._search_filter_ids = ids
        self.reload()

    def is_hidden_row(self, row: int) -> bool:
        return row >= self.hidden_start

    STAT_FIELDS = ('task_count', 'done_count', 'open_count', 'overdue_count', 'due_today_count')

    def refresh_counts(self):
        """Re-read the task counts in place (no reset): due dates and completions change them."""
        fresh = {r['id']: r for r in self.db.list_projects_with_stats()}
        changed_rows = []
        for i, r in enumerate(self.rows):
            new = fresh.get(r['id'])
            if new is None:
                continue
            if any(r.get(field) != new[field] for field in self.STAT_FIELDS):
                for field in self.STAT_FIELDS:
                    r[field] = new[field]
                changed_rows.append(i)
        for r in self.hidden_rows:
            new = fresh.get(r['id'])
            if new is not None:
                for field in self.STAT_FIELDS:
                    r[field] = new[field]
        for i in changed_rows:
            self.dataChanged.emit(self.index(i, 0), self.index(i, 0), [Qt.ItemDataRole.DisplayRole, Qt.ItemDataRole.ToolTipRole])

    def starred_count(self) -> int:
        """Starred projects at the top of the listed (not hidden) section (counted in reload)."""
        return getattr(self, '_starred_count', 0)

    def set_project_name_filter(self, filter_text: str):
        """Filter projects by name (case-insensitive, AND mode for multiple terms)."""
        if self.project_name_filter != filter_text:
            self.project_name_filter = filter_text
            self.reload()

    def reload(self):
        self.beginResetModel()
        all_rows = self.db.list_projects_with_stats()
        # Store rows as mutable dicts for fast stat adjustments
        all_rows = [dict(r) for r in all_rows]
        all_project_ids = {r['id'] for r in all_rows}
        
        # Apply project name filter first (if set): every typed word must match a word of the name
        if self.project_name_filter:
            all_rows = [r for r in all_rows if name_match(self.project_name_filter, r['title']) is not None]
        
        auto_hide = self.auto_hide_finished()
        if self._search_filter_ids is not None and not self.search_keeps_all_projects:
            self.rows = [r for r in all_rows if r['id'] in self._search_filter_ids]
        else:
            filtered_rows = []
            kept_row = None
            for r in all_rows:
                if self.keep_visible_id is not None and r['id'] == self.keep_visible_id:
                    kept_row = r
                if self._is_listed(r, auto_hide):
                    filtered_rows.append(r)
            self.kept_by_pin = kept_row is not None and all(r['id'] != kept_row['id'] for r in filtered_rows)
            if self.kept_by_pin:
                shown_ids = {r['id'] for r in filtered_rows} | {kept_row['id']}
                filtered_rows = [r for r in all_rows if r['id'] in shown_ids]
            if self._search_filter_ids is not None:
                # Sidebar search mode: normal list plus any matching project that is otherwise hidden
                shown_ids = {r['id'] for r in filtered_rows} | set(self._search_filter_ids)
                filtered_rows = [r for r in all_rows if r['id'] in shown_ids]
            self.rows = filtered_rows
        visible_ids = {r['id'] for r in self.rows}
        self.hidden_rows = [r for r in all_rows if r['id'] not in visible_ids]
        self.hidden_start = len(self.rows)
        if self.include_hidden_rows and self._search_filter_ids is None:
            self.rows = self.rows + self.hidden_rows
        self._load_review_state()
        self._starred_count = sum(1 for r in self.rows[:self.hidden_start] if r.get('starred'))
        self._rebuild_index()
        self.endResetModel()

    def auto_hide_finished(self) -> bool:
        """Preferences → "Hide projects with no open tasks" (default on)."""
        if not self.settings:
            return True
        return str(self.settings.value('Preferences/AutoHideFinishedProjects', 'true')).lower() in ('true', '1')

    def _is_listed(self, r: dict, auto_hide: bool) -> bool:
        """Visibility rules, in order: hidden by hand → HIDDEN; no tasks yet → listed;
        no open tasks while auto-hide is on (and completed work is not shown) → HIDDEN."""
        if r.get('hidden'):
            return False
        if not r.get('task_count'):
            return True
        if auto_hide and not self.include_done and not r.get('open_count'):
            return False
        return True

    def _load_review_state(self):
        try:
            self._review_active = self.db.review_active()
            self._review_pending = self.db.review_pending_ids() if self._review_active else set()
        except sqlite3.Error:
            self._review_active, self._review_pending = False, set()
        self._review_skipped = [pid for pid in self._review_skipped if pid in self._review_pending]

    def _repaint_review(self):
        if self.rows:
            self.dataChanged.emit(self.index(0, 0), self.index(len(self.rows) - 1, 0), [Qt.ItemDataRole.FontRole])
        self.reviewChanged.emit()

    def review_scope(self, include_hidden: bool) -> List[int]:
        """Projects a new review covers: every listed project (whatever the name filter or the
        HIDDEN section show right now), plus the hidden ones when include_hidden."""
        auto_hide = self.auto_hide_finished()
        rows = [dict(r) for r in self.db.list_projects_with_stats()]
        return [r['id'] for r in rows if include_hidden or self._is_listed(r, auto_hide)]

    def hidden_project_count(self) -> int:
        auto_hide = self.auto_hide_finished()
        return sum(1 for r in self.db.list_projects_with_stats() if not self._is_listed(dict(r), auto_hide))

    def start_review_cycle(self, project_ids: Optional[List[int]] = None, include_hidden: bool = False,
                           views: Optional[List[str]] = None):
        """Begin a review: the projects are bold until each is marked reviewed."""
        if project_ids is None:
            project_ids = self.review_scope(include_hidden)
        self.db.start_review(project_ids, include_hidden, views or [])
        self._review_skipped = []
        self._load_review_state()
        self._repaint_review()

    def end_review_cycle(self) -> Optional[Dict[str, Any]]:
        """Stop the review; returns the state DB.restore_review() needs to undo it."""
        if not self._review_active:
            return None
        state = self.db.end_review()
        self._load_review_state()
        self._repaint_review()
        return state

    def restore_review_cycle(self, state: Dict[str, Any]):
        self.db.restore_review(state)
        self._load_review_state()
        self._repaint_review()

    def mark_review_completed(self, project_id: int):
        """Mark a single project as reviewed within the current review."""
        if not self._review_active or project_id not in self._review_pending:
            return
        self.db.mark_project_reviewed(project_id)
        self._review_pending.discard(project_id)
        row = self.row_for_project(project_id)
        if row >= 0:
            idx = self.index(row, 0)
            self.dataChanged.emit(idx, idx, [Qt.ItemDataRole.FontRole])
        self.reviewChanged.emit()

    def skip_review(self, project_id: int):
        """Leave a project for later in this review (it moves to the end of the order)."""
        if project_id in self._review_pending:
            self._review_skipped = [pid for pid in self._review_skipped if pid != project_id] + [project_id]
            self.reviewChanged.emit()

    def review_remaining_ids(self) -> List[int]:
        """Projects still to review, least recently reviewed first; pinned ones first among
        equals, projects without open tasks last, skipped ones at the very end."""
        if not self._review_active or not self._review_pending:
            return []
        rows = {r['id']: r for r in self.db.list_projects_with_stats() if r['id'] in self._review_pending}
        skipped = {pid: i for i, pid in enumerate(self._review_skipped)}

        def key(pid):
            r = rows[pid]
            return (pid in skipped, skipped.get(pid, 0), not (r['open_count'] or 0),
                    r['last_reviewed_at'] or '', not r['starred'], r['title'].lower())
        return sorted(rows, key=key)

    def is_review_active(self) -> bool:
        return self._review_active

    def is_project_in_review(self, project_id: int) -> bool:
        return self._review_active and project_id in self._review_pending

    def _rebuild_index(self):
        self._index_by_id = {r['id']: i for i, r in enumerate(self.rows)}

    def apply_task_done_toggle(self, project_id: int, became_done: bool):
        # Adjust stats for a single project without full reload
        idx = self._index_by_id.get(project_id)
        if idx is None:
            return None
        
        # Safety check: ensure index is still valid
        # This is critical because indices can shift when other rows are deleted
        if idx < 0 or idx >= len(self.rows):
            return None
        
        # Verify the row at this index is actually the project we're looking for
        r = self.rows[idx]
        if r.get('id') != project_id:
            # Index is stale, project has moved or been deleted
            return None
            
        r['done_count'] = max(0, r.get('done_count', 0) + (1 if became_done else -1))
        r['open_count'] = max(0, r.get('task_count', 0) - r['done_count'])
        
        # Only trigger view updates if the change affects visibility
        should_remove = not self._is_listed(r, self.auto_hide_finished()) and project_id != self.keep_visible_id
        
        if should_remove:
            # Use a full reload to keep indices consistent and avoid removal glitches
            removed_row = idx
            try:
                self.reload()
            finally:
                # Return the row the project occupied before it was removed
                return ('removed', project_id, removed_row)
        else:
            # Verify index is still valid before emitting signal
            if idx >= 0 and idx < len(self.rows):
                # Use minimal data change notification to avoid disrupting active edits
                self.dataChanged.emit(self.index(idx, 0), self.index(idx, 0), [Qt.ItemDataRole.DisplayRole, Qt.ItemDataRole.ForegroundRole])
            return None

    # --- Newly re-added required abstract + helper methods ---
    def rowCount(self, parent=QModelIndex()):
        return 0 if parent.isValid() else len(self.rows)

    def columnCount(self, parent=QModelIndex()):
        return 1

    def headerData(self, section, orientation, role=Qt.ItemDataRole.DisplayRole):
        if orientation == Qt.Orientation.Horizontal and role == Qt.ItemDataRole.DisplayRole:
            return "Projects"
        return None

    def data(self, index: QModelIndex, role=Qt.ItemDataRole.DisplayRole):
        if not index.isValid():
            return None
        r = self.rows[index.row()]
        if role in (Qt.ItemDataRole.DisplayRole, Qt.ItemDataRole.EditRole):
            if role == Qt.ItemDataRole.EditRole:
                # For editing, return just the clean title without task counts
                return r['title']
            else:
                # For display, format with task counts
                title = r['title']
                tc, dc = r.get('task_count', 0), r.get('done_count', 0)
                return f"{title} ({dc}/{tc})" if tc else title
        if role == Qt.ItemDataRole.ForegroundRole:
            tc, dc = r.get('task_count', 0), r.get('done_count', 0)
            if tc and dc == tc:
                colr = QApplication.palette().color(QPalette.ColorRole.WindowText)
                colr.setAlpha(140)
                return colr
        if role == Qt.ItemDataRole.ToolTipRole:
            parts = [f"{r.get('open_count') or 0} open"]
            if r.get('overdue_count'):
                parts.append(f"{r['overdue_count']} overdue")
            if r.get('due_today_count'):
                parts.append(f"{r['due_today_count']} due today")
            return f"{r['title']}\n{' · '.join(parts)}"
        if role == Qt.ItemDataRole.FontRole and self.is_project_in_review(r['id']):
            font = QApplication.font()
            font.setBold(True)
            return font
        return None

    def flags(self, index: QModelIndex):
        if not index.isValid():
            return Qt.ItemFlag.ItemIsEnabled
        return Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable | Qt.ItemFlag.ItemIsEditable

    def setData(self, index: QModelIndex, value, role=Qt.ItemDataRole.EditRole):
        if not index.isValid() or role != Qt.ItemDataRole.EditRole:
            return False
        r = self.rows[index.row()]
        new_title = str(value).strip() or 'Untitled'
        if new_title != r['title']:
            taken = self.db.conn.execute(
                "SELECT 1 FROM projects WHERE title=? COLLATE NOCASE AND id<>?", (new_title, r['id'])).fetchone()
            if taken:
                self.renameRejected.emit(new_title)
                return False
            try:
                self.db.update_project_title(r['id'], new_title)
            except sqlite3.IntegrityError:
                self.renameRejected.emit(new_title)
                return False
            except Exception:
                return False
            r['title'] = new_title
            self.dataChanged.emit(index, index, [Qt.ItemDataRole.DisplayRole, Qt.ItemDataRole.EditRole])
        return True

    # Convenience helpers used by MainWindow
    def project_id_at(self, row: int) -> Optional[int]:
        if 0 <= row < len(self.rows):
            return self.rows[row]['id']
        return None

    def row_for_project(self, project_id: int) -> int:
        return self._index_by_id.get(project_id, -1)

    def add_project(self, title: str) -> int:
        pid = self.db.add_project(title)
        self.reload()
        return self.row_for_project(pid)

    def remove_project_at(self, row: int):
        if 0 <= row < len(self.rows):
            pid = self.rows[row]['id']
            self.db.remove_project(pid)
            self.reload()

class TaskTableModel(QAbstractTableModel):
    COL_TITLE = 0
    COL_PINNED = 1
    COL_DONE = 2

    HEADERS = ["Title", "Focus", "Done"]

    def __init__(self, db: DB):
        super().__init__()
        self.db = db
        self.rows: List[sqlite3.Row] = []
        self.project_id: Optional[int] = None
        self.include_done: bool = False
        self.pinned_only: bool = False
        self.show_no_due_dates: bool = False
        # Sidebar smart view key ('today', 'pinned', ...) when the list spans all projects
        self.smart_view: Optional[str] = None
        # Tasks edited out of the open smart view stay listed (dimmed) until the view is left
        self.lingering_ids: set = set()
        self._lingering_shown: set = set()
        # The redesigned list always loads completed tasks (they sit in a collapsible group)
        self.always_include_done: bool = False
        # Presentation state for the grouped list
        self.groups: List[Dict[str, Any]] = []
        self._row_group: List[int] = []
        self.collapsed: set = {'done'}
        self._project_titles: Dict[int, str] = {}
        self._stakeholders: Dict[int, List[str]] = {}
        self._manual_mode: bool = False
        self._row_contexts: List[Optional[Dict[str, Any]]] = []
        self._search_groups: Optional[List[Tuple[int, str, List[sqlite3.Row]]]] = None
        self._search_details: Dict[int, Dict[str, Any]] = {}  # task id -> title_spans, excerpt
        # Hooks injected by MainWindow to support search-mode editing
        self.is_search_active: Callable[[], bool] = lambda: False
        self.refresh_search: Callable[[], None] = lambda: None
        # Settings reference for preferences (will be set by MainWindow)
        self.settings: Optional[QSettings] = None

    tasksChanged = pyqtSignal()
    groupsChanged = pyqtSignal()
    taskCompleted = pyqtSignal(int)  # task_id, when completed from the list
    taskAboutToComplete = pyqtSignal(int)  # task_id, before the list's complete box writes to the DB
    taskCompletionStamped = pyqtSignal(int)  # task_id
    doneToggled = pyqtSignal(int, bool)  # project_id, became_done

    def show_project(self, project_id: Optional[int], include_done: bool):
        """Switch the list to a project (leaves any smart view)."""
        self.lingering_ids.clear()
        self.smart_view = None
        self.set_context(project_id, include_done, False)

    def set_context(self, project_id: Optional[int], include_done: bool, pinned_only: bool = False):
        # A smart view stays active until show_project() is called; older call sites only refresh it
        if self.smart_view is not None:
            self.reload()
            return
        self.project_id = project_id
        self.include_done = include_done
        self.pinned_only = pinned_only
        self.reload()

    def set_smart_view(self, key: Optional[str]):
        self.lingering_ids.clear()  # opening (or re-opening) a view shows it as it is now
        self.smart_view = key
        self.project_id = None
        self.pinned_only = False
        self.reload()

    def _task_value(self, task, key: str, default=None):
        try:
            return task[key]
        except Exception:
            return default

    def apply_secondary_filters(self, tasks: List[sqlite3.Row]) -> List[sqlite3.Row]:
        filtered_tasks = list(tasks)
        if self.show_no_due_dates:
            filtered_tasks = [
                task for task in filtered_tasks
                if not str(self._task_value(task, 'due_date', '') or '').strip()
            ]
        return filtered_tasks

    def reload(self):
        # Check if any editors are currently active and defer reload if needed
        if hasattr(self, '_defer_reload_flag') and self._defer_reload_flag:
            if not hasattr(self, '_pending_reload_timer'):
                self._pending_reload_timer = QTimer()
                self._pending_reload_timer.setSingleShot(True)
                self._pending_reload_timer.timeout.connect(self._perform_deferred_reload)
            self._pending_reload_timer.start(300)  # Increased delay for more stable editing
            return
            
        self._perform_reload()
        
    def _perform_reload(self):
        """Perform the actual model reload with proper signal handling."""
        # Don't reload if we're already in the middle of a reload
        if hasattr(self, '_reloading') and self._reloading:
            return
            
        # Search results are assembled by the main window
        if self.is_search_active():
            self.refresh_search()
            return

        self._reloading = True
        try:
            self.beginResetModel()
            try:
                self._search_groups = None
                self._lingering_shown = set()
                if self.smart_view:
                    self.rows = self.db.list_tasks_for_smart_view(self.smart_view)
                    self._add_lingering_rows()
                elif self.project_id is None:
                    self.rows = []
                else:
                    all_tasks = self.db.list_tasks(self.project_id, self.include_done or self.always_include_done, self.pinned_only)
                    self.rows = self.apply_secondary_filters(all_tasks)
                self._arrange_rows()
            finally:
                self.endResetModel()
            
            # Emit signals after model is fully updated
            self.tasksChanged.emit()
        finally:
            self._reloading = False
        
    def linger(self, task_ids):
        """Keep these tasks in the open smart view even if an edit takes them out of it."""
        if self.smart_view:
            self.lingering_ids.update(tid for tid in task_ids if tid)

    def _add_lingering_rows(self):
        listed = {row['id'] for row in self.rows}
        missing = [tid for tid in self.lingering_ids if tid not in listed]
        if not missing:
            return
        marks = ",".join("?" * len(missing))
        # Completed or deleted tasks still leave at once
        extra = list(self.db.conn.execute(f"SELECT * FROM tasks WHERE done = 0 AND id IN ({marks})", missing))
        self.rows = list(self.rows) + extra
        self._lingering_shown = {row['id'] for row in extra}

    # ----- grouping and row presentation -----
    @staticmethod
    def _title_key(task) -> str:
        return str(task['title'] or '').casefold()

    def _flag(self, task, key: str) -> bool:
        return bool(self._task_value(task, key, 0) or 0)

    def _status_groups(self, tasks: List[sqlite3.Row], include_done: bool, keep_order: bool = False) -> List[Tuple[str, Optional[str], List[sqlite3.Row], bool]]:
        """Meeting notes / Focus / Next up / Waiting / Completed, each sorted by title."""
        open_tasks = [t for t in tasks if not bool(t['done'])]
        order = (lambda items: items) if keep_order else (lambda items: sorted(items, key=self._title_key))
        mom = [t for t in open_tasks if self._flag(t, 'marked_mom')]
        pinned = [t for t in open_tasks if not self._flag(t, 'marked_mom') and bool(t['pinned'])]
        next_up = [t for t in open_tasks if not self._flag(t, 'marked_mom') and not bool(t['pinned']) and not self._flag(t, 'waiting')]
        waiting = [t for t in open_tasks if not self._flag(t, 'marked_mom') and not bool(t['pinned']) and self._flag(t, 'waiting')]
        groups = [
            ('mom', "Meeting notes", order(mom), False),
            ('pinned', "Focus", order(pinned), False),
            ('next', "Next up", order(next_up), False),
            ('waiting', "Waiting", order(waiting), False),
        ]
        if include_done:
            groups.append(('done', "Completed", order([t for t in tasks if bool(t['done'])]), True))
        return groups

    def _due_key(self, task) -> Tuple[int, str, str]:
        """Overdue (oldest first), due today, due later (soonest first), no due date; then title."""
        today = datetime.date.today().isoformat()
        due = str(task['due_date'] or '').strip()[:10]
        if not due:
            bucket = 3
        elif due < today:
            bucket = 0
        elif due == today:
            bucket = 1
        else:
            bucket = 2
        return bucket, due, self._title_key(task)

    def _focus_groups(self, tasks: List[sqlite3.Row]) -> List[Tuple[str, Optional[str], List[sqlite3.Row], bool]]:
        """Focus view: Meeting notes, then Focus (actionable), then Waiting, each by due date."""
        open_tasks = [t for t in tasks if not bool(t['done'])]
        mom = [t for t in open_tasks if self._flag(t, 'marked_mom')]
        rest = [t for t in open_tasks if not self._flag(t, 'marked_mom')]
        return [
            ('mom', "Meeting notes", sorted(mom, key=self._due_key), False),
            ('pinned', "Focus", sorted((t for t in rest if not self._flag(t, 'waiting')), key=self._due_key), False),
            ('waiting', "Waiting", sorted((t for t in rest if self._flag(t, 'waiting')), key=self._due_key), False),
        ]

    def _arrange_rows(self):
        """Order self.rows by group and rebuild the group table used by the list delegate."""
        tasks = list(self.rows)
        self._manual_mode = False
        if self._search_groups is not None:
            # One group per project, rows in relevance order with completed results last
            spec = [
                (f"p{project_id}", title, sorted(rows, key=lambda t: bool(t['done'])), False)
                for project_id, title, rows in self._search_groups
            ]
        elif self.smart_view == 'today':
            today = datetime.date.today().isoformat()
            overdue = sorted((t for t in tasks if str(t['due_date'] or '')[:10] < today),
                             key=lambda t: (str(t['due_date'] or '')[:10], self._title_key(t)))
            due_today = sorted((t for t in tasks if str(t['due_date'] or '')[:10] >= today), key=self._title_key)
            spec = [('overdue', "Overdue", overdue, False), ('today', "Today", due_today, False)]
        elif self.smart_view == 'pinned':
            spec = self._focus_groups(tasks)
        elif self.smart_view:
            spec = self._status_groups(tasks, include_done=False)
        elif self.project_id is not None and self.db.is_manual_sort_enabled(self.project_id):
            self._manual_mode = True
            spec = [
                ('all', None, [t for t in tasks if not bool(t['done'])], False),
                ('done', "Completed", [t for t in tasks if bool(t['done'])], True),
            ]
        else:
            spec = self._status_groups(tasks, include_done=True)

        rows: List[sqlite3.Row] = []
        groups: List[Dict[str, Any]] = []
        row_group: List[int] = []
        for key, label, items, collapsible in spec:
            if not items:
                continue
            groups.append({'key': key, 'label': label, 'start': len(rows), 'count': len(items), 'collapsible': collapsible})
            row_group.extend([len(groups) - 1] * len(items))
            rows.extend(items)
        self.rows = rows
        self.groups = groups
        self._row_group = row_group
        self._refresh_row_caches()

    def _refresh_row_caches(self):
        """Project titles and stakeholder names for the rows on screen (one query each)."""
        self._row_contexts = []
        self._project_titles = {}
        self._stakeholders = {}
        task_ids = [r['id'] for r in self.rows]
        if not task_ids:
            return
        try:
            self._project_titles = {r['id']: r['title'] for r in self.db.conn.execute("SELECT id, title FROM projects")}
            for chunk_start in range(0, len(task_ids), 500):
                chunk = task_ids[chunk_start:chunk_start + 500]
                marks = ",".join("?" * len(chunk))
                for r in self.db.conn.execute(
                    "SELECT ts.task_id, s.name FROM task_stakeholders ts JOIN stakeholders s ON s.id = ts.stakeholder_id "
                    f"WHERE ts.task_id IN ({marks}) ORDER BY s.name COLLATE NOCASE", chunk
                ):
                    self._stakeholders.setdefault(r['task_id'], []).append(r['name'])
        except Exception as e:
            print(f"Error loading task list details: {e}")

    def refresh_row_details(self):
        """Re-read stakeholders / project titles without reloading the rows."""
        self._refresh_row_caches()
        if self.rows:
            self.dataChanged.emit(self.index(0, 0), self.index(len(self.rows) - 1, 0))

    def set_search_results(self, groups: List[Tuple[int, str, List[sqlite3.Row]]],
                           details: Optional[Dict[int, Dict[str, Any]]] = None):
        """Show search results: one group per project (project id, title, matching rows), in order.
        details maps task id -> 'title_spans' (matched letters) and 'excerpt' (notes line, spans)."""
        self.beginResetModel()
        self._search_groups = list(groups)
        self._search_details = dict(details or {})
        self.project_id = None
        self.rows = []
        self._arrange_rows()
        self.endResetModel()

    def _group_for_row(self, row: int) -> Optional[Dict[str, Any]]:
        if 0 <= row < len(self._row_group):
            return self.groups[self._row_group[row]]
        return None

    def group_header(self, row: int) -> Optional[Dict[str, Any]]:
        """Header info when row is the first row of a labelled group, else None."""
        group = self._group_for_row(row)
        if group is None or group['start'] != row or group['label'] is None:
            return None
        return {
            'key': group['key'], 'label': group['label'], 'count': group['count'],
            'collapsible': group['collapsible'], 'collapsed': group['collapsible'] and group['key'] in self.collapsed,
        }

    def is_row_collapsed(self, row: int) -> bool:
        group = self._group_for_row(row)
        return bool(group and group['collapsible'] and group['key'] in self.collapsed)

    def toggle_group(self, key: str):
        if key in self.collapsed:
            self.collapsed.discard(key)
        else:
            self.collapsed.add(key)
        self.groupsChanged.emit()

    def set_collapsed(self, collapsed: set):
        """Use the collapsed-group set of the view being shown (mutated in place by toggle_group)."""
        self.collapsed = collapsed
        self.groupsChanged.emit()

    def first_done_row(self) -> int:
        """Row index where the Completed group starts (rowCount when there is none)."""
        for group in self.groups:
            if group['key'] == 'done':
                return group['start']
        return len(self.rows)

    def open_count(self) -> int:
        return sum(1 for r in self.rows if not bool(r['done']))

    def row_context(self, row: int) -> Dict[str, Any]:
        """Everything the row delegate needs to paint a task. Built once per row and load."""
        cache = self._row_contexts
        if len(cache) != len(self.rows):
            cache = self._row_contexts = [None] * len(self.rows)
        context = cache[row]
        if context is None:
            context = cache[row] = self._build_row_context(row)
        return context

    def _build_row_context(self, row: int) -> Dict[str, Any]:
        task = self.rows[row]
        done = bool(task['done'])
        due_label, due_state = due_info(self._task_value(task, 'due_date'))
        repeat_label = ""
        if self._flag(task, 'recurring'):
            repeat_label = _recurrence_short_label(_parse_recurrence_rule(self._task_value(task, 'recurrence_rule')))
        show_project = bool(self.smart_view) and self._search_groups is None
        return {
            'id': task['id'],
            'title': str(task['title'] or ''),
            'done': done,
            'pinned': bool(task['pinned']),
            'waiting': self._flag(task, 'waiting'),
            'mom': self._flag(task, 'marked_mom'),
            'due_label': due_label,
            'due_state': due_state,
            'repeat_label': repeat_label,
            'people': self._stakeholders.get(task['id'], []),
            'project_title': self._project_titles.get(task['project_id']) if show_project else None,
            'grip': self._manual_mode and not done,
            'lingering': task['id'] in self._lingering_shown,
            **(self._search_details.get(task['id'], {}) if self._search_groups is not None else {}),
        }

    def toggle_task_flag(self, row: int, flag: str):
        """Toggle done / pinned / waiting from the row's inline controls."""
        if not (0 <= row < len(self.rows)):
            return
        if flag == 'done':
            self.setData(self.index(row, self.COL_DONE), None, Qt.ItemDataRole.CheckStateRole)
        elif flag == 'pinned':
            self.setData(self.index(row, self.COL_PINNED), None, Qt.ItemDataRole.CheckStateRole)
        elif flag == 'waiting':
            task = self.rows[row]
            self.db.update_task(task['id'], waiting=not self._flag(task, 'waiting'))
            self.reload()
            self.tasksChanged.emit()

    def _perform_deferred_reload(self):
        """Perform reload that was deferred due to active editors"""
        if hasattr(self, '_defer_reload_flag') and not self._defer_reload_flag:
            self._perform_reload()

    def rowCount(self, parent=QModelIndex()):
        return len(self.rows)

    def columnCount(self, parent=QModelIndex()):
        # The list shows a single column, so selecting a row selects the whole row.
        # COL_PINNED / COL_DONE stay addressable through index() for setData().
        return 1

    def index(self, row, column=0, parent=QModelIndex()):
        if parent.isValid() or not (0 <= row < len(self.rows)) or not (0 <= column < len(self.HEADERS)):
            return QModelIndex()
        return self.createIndex(row, column)

    def headerData(self, section, orientation, role=Qt.ItemDataRole.DisplayRole):
        if orientation == Qt.Orientation.Horizontal and role == Qt.ItemDataRole.DisplayRole:
            return self.HEADERS[section]
        return None

    def data(self, index: QModelIndex, role=Qt.ItemDataRole.DisplayRole):
        if not index.isValid():
            return None
        row = self.rows[index.row()]
        col = index.column()
        # Display / edit text
        if role in (Qt.ItemDataRole.DisplayRole, Qt.ItemDataRole.EditRole):
            if col == self.COL_TITLE:
                if role == Qt.ItemDataRole.EditRole:
                    return row['title']
                title = row['title']
                try:
                    is_recurring = bool(row['recurring'])
                except (KeyError, IndexError):
                    is_recurring = False
                if is_recurring:
                    title = f"[R] {title}"
                return title
            if col in (self.COL_PINNED, self.COL_DONE):
                return ""  # checkbox only
        # Checkbox state
        if role == Qt.ItemDataRole.CheckStateRole and col in (self.COL_PINNED, self.COL_DONE):
            if col == self.COL_PINNED:
                state_val = bool(row['pinned'])
            else:
                state_val = bool(row['done'])
            return Qt.CheckState.Checked if state_val else Qt.CheckState.Unchecked
        # Background color for MoM tasks
        if role == Qt.ItemDataRole.BackgroundRole:
            try:
                is_mom = bool(row['marked_mom'])
                if is_mom:
                    return QColor(255, 255, 200)  # Light yellow background
            except (KeyError, IndexError):
                pass
        # Fade completed tasks
        if role == Qt.ItemDataRole.ForegroundRole:
            # Check if task is completed and should be faded
            if bool(row['done']) and (self.include_done or self.is_search_active()):
                pal = QApplication.palette()
                c = pal.color(QPalette.ColorRole.WindowText)
                c.setAlpha(128)
                return c

        # Tooltip for recurring tasks
        if role == Qt.ItemDataRole.ToolTipRole and col == self.COL_TITLE:
            try:
                if bool(row['recurring']):
                    rule = _parse_recurrence_rule(row['recurrence_rule'])
                    tooltip = _format_recurrence_tooltip(rule, row['recurrence_next_at'])
                    return tooltip
            except Exception:
                return None
        return None

    def setData(self, index: QModelIndex, value, role=Qt.ItemDataRole.EditRole):
        if not index.isValid():
            return False
        row = self.rows[index.row()]; col = index.column()
        project_id = row['project_id'] if 'project_id' in row.keys() else self.project_id
        
        if col == self.COL_TITLE and role == Qt.ItemDataRole.EditRole:
            title = str(value).strip() or 'Untitled task'
            if title != row['title']:
                self.db.update_task(row['id'], title=title)
                fresh = self.db.conn.execute("SELECT * FROM tasks WHERE id=?", (row['id'],)).fetchone()
                self.rows[index.row()] = fresh
                self._row_contexts = []
                self.dataChanged.emit(index, index, [Qt.ItemDataRole.DisplayRole, Qt.ItemDataRole.EditRole])
            return True
            
        if col in (self.COL_PINNED, self.COL_DONE) and role in (Qt.ItemDataRole.CheckStateRole, Qt.ItemDataRole.EditRole):
            # Store current state before making database changes
            old_done_state = bool(row['done'])
            
            if col == self.COL_PINNED:
                current = bool(row['pinned']); newval = not current
                task_id = row['id']
                
                # Update pinned state in database
                self.db.update_task(task_id, pinned=newval)
                
                # If manual sort is enabled, reorder tasks to move pinned/unpinned task
                if self.project_id is not None and self.db.is_manual_sort_enabled(self.project_id):
                    current_order = self.db.ensure_manual_task_order(self.project_id, fallback_to_auto=True)
                    if task_id in current_order:
                        if newval:
                            before_task_id = current_order[0] if current_order and current_order[0] != task_id else None
                            self.db.insert_task_ids_in_manual_order(
                                self.project_id,
                                [task_id],
                                before_task_id=before_task_id
                            )
                        else:
                            remaining_ids = [tid for tid in current_order if tid != task_id]
                            first_non_pinned_id = None
                            for tid in remaining_ids:
                                pinned_row = self.db.conn.execute("SELECT pinned FROM tasks WHERE id=?", (tid,)).fetchone()
                                if not pinned_row or not bool(pinned_row['pinned']):
                                    first_non_pinned_id = tid
                                    break
                            self.db.insert_task_ids_in_manual_order(
                                self.project_id,
                                [task_id],
                                before_task_id=first_non_pinned_id
                            )
                
                # Reload to reflect new sort position (pinned tasks appear at top)
                self.reload()
                
                # Emit task changed signal for project model updates
                self.tasksChanged.emit()
                
            else:  # COL_DONE
                current = bool(row['done']); newval = not current
                if not getattr(self, 'before_complete', lambda _tid: True)(row['id']):
                    return False
                if newval:
                    self.taskAboutToComplete.emit(row['id'])
                
                # Update database first
                self.db.update_task(row['id'], done=newval)
                
                # Handle view updates based on inclusion settings
                if self.always_include_done:
                    # The task moves between its status group and Completed
                    if project_id is not None:
                        self.doneToggled.emit(project_id, newval)
                    if newval:
                        self.taskCompleted.emit(row['id'])
                    if not self.is_search_active():
                        self.reload()
                elif (not self.include_done) and (not self.is_search_active()) and (not old_done_state) and newval:
                    # Task is being marked complete and we're hiding completed tasks
                    # Remove this specific task from view
                    self.beginRemoveRows(QModelIndex(), index.row(), index.row())
                    del self.rows[index.row()]
                    self._row_contexts = []
                    self.endRemoveRows()
                    
                    # Emit signals for project model updates
                    self.tasksChanged.emit()
                    if not self.is_search_active() and project_id is not None:
                        self.doneToggled.emit(project_id, newval)
                else:
                    # Task stays visible, just update its data
                    fresh = self.db.conn.execute("SELECT * FROM tasks WHERE id=?", (row['id'],)).fetchone()
                    self.rows[index.row()] = fresh
                    self._row_contexts = []
                    self.dataChanged.emit(index, index, [Qt.ItemDataRole.CheckStateRole, Qt.ItemDataRole.DisplayRole, Qt.ItemDataRole.ForegroundRole])
                    
                    # Still emit project update signals
                    if not self.is_search_active() and project_id is not None:
                        self.doneToggled.emit(project_id, newval)
            
            # Only refresh search if we're in search mode
            if self.is_search_active():
                # Defer search refresh to avoid conflicts with active editors
                QTimer.singleShot(50, self.refresh_search)
            return True
            
        return False

    # --- Helper methods used by MainWindow ---
    def task_id_at(self, row: int) -> Optional[int]:
        if 0 <= row < len(self.rows):
            return self.rows[row]['id']
        return None
    
    # --- Drag-and-drop support for manual sorting ---
    def supportedDropActions(self):
        return Qt.DropAction.MoveAction
    
    def flags(self, index: QModelIndex):
        if not index.isValid():
            return Qt.ItemFlag.ItemIsEnabled
        f = Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable
        if index.column() == self.COL_TITLE:
            f |= Qt.ItemFlag.ItemIsEditable
        if index.column() in (self.COL_PINNED, self.COL_DONE):
            f |= Qt.ItemFlag.ItemIsUserCheckable
        
        # Any task can be dragged onto a sidebar project; dropping inside the list (reordering)
        # only in manual sort. _arrange_rows records the mode on every load, so this does not
        # query the database for each row Qt asks about
        f |= Qt.ItemFlag.ItemIsDragEnabled
        if self.project_id is not None and self._manual_mode:
            f |= Qt.ItemFlag.ItemIsDropEnabled
        
        return f
    
    def mimeTypes(self):
        return ['application/x-task-row']
    
    def mimeData(self, indexes):
        if not indexes:
            return None
        selected_rows = sorted({index.row() for index in indexes if index.isValid()})
        task_ids = [self.task_id_at(row) for row in selected_rows]
        task_ids = [task_id for task_id in task_ids if task_id is not None]
        if not task_ids:
            return None
        mime_data = QMimeData()
        mime_data.setData('application/x-task-row', json.dumps({"task_ids": task_ids}).encode('utf-8'))
        return mime_data
    
    def dropMimeData(self, data, action, row, column, parent):
        if action != Qt.DropAction.MoveAction:
            return False
        
        if not data.hasFormat('application/x-task-row'):
            return False

        if self.project_id is None or not self.db.is_manual_sort_enabled(self.project_id):
            return False

        try:
            payload = json.loads(bytes(data.data('application/x-task-row')).decode('utf-8'))
        except Exception:
            return False

        moving_task_ids = payload.get("task_ids") if isinstance(payload, dict) else None
        if not isinstance(moving_task_ids, list):
            return False
        visible_task_ids = [row_data['id'] for row_data in self.rows]
        moving_task_ids = [task_id for task_id in moving_task_ids if task_id in visible_task_ids]
        if not moving_task_ids:
            return False
        moving_task_id_set = set(moving_task_ids)
        
        # Determine drop row
        if row != -1:
            drop_row = row
        elif parent.isValid():
            drop_row = parent.row()
        else:
            drop_row = self.rowCount()

        drop_row = max(0, min(drop_row, len(visible_task_ids)))
        moving_rows = [idx for idx, task_id in enumerate(visible_task_ids) if task_id in moving_task_id_set]
        remaining_visible_ids = [task_id for task_id in visible_task_ids if task_id not in moving_task_id_set]
        adjusted_drop_row = drop_row - sum(1 for source_row in moving_rows if source_row < drop_row)
        adjusted_drop_row = max(0, min(adjusted_drop_row, len(remaining_visible_ids)))
        new_visible_order = (
            remaining_visible_ids[:adjusted_drop_row]
            + moving_task_ids
            + remaining_visible_ids[adjusted_drop_row:]
        )
        if new_visible_order == visible_task_ids:
            return False

        before_task_id = None
        if adjusted_drop_row < len(remaining_visible_ids):
            before_task_id = remaining_visible_ids[adjusted_drop_row]
        self.db.insert_task_ids_in_manual_order(
            self.project_id,
            moving_task_ids,
            before_task_id=before_task_id
        )
        
        # Reload model
        self.reload()
        
        return True

    def add_task(self, title: str) -> int:
        if self.project_id is None:
            return -1
        tid = self.db.add_task(self.project_id, title)
        self.reload()
        # Find row for new task
        for i, r in enumerate(self.rows):
            if r['id'] == tid:
                return i
        return -1

    def remove_task_at(self, row: int):
        if 0 <= row < len(self.rows):
            tid = self.rows[row]['id']
            self.db.remove_task(tid)
            self.reload()

# -------------------------- Visual Crop Support --------------------------

class _CropLabel(QLabel):
    def __init__(self, pixmap: QPixmap, scale_factor: float, parent=None):
        super().__init__(parent)
        self.setPixmap(pixmap)
        self._rubber = QRubberBand(QRubberBand.Shape.Rectangle, self)
        self._origin = None
        self.scale_factor = scale_factor
        self._dragging = False
        self._last_rect: Optional[QRect] = None  # store last drawn display-space rect
        self.setCursor(Qt.CursorShape.CrossCursor)
        self._rubber.setStyleSheet("QWidget { border: 2px solid #5980a6; background: rgba(0,120,215,40); }")

    def mousePressEvent(self, e):
        if e.button() != Qt.MouseButton.LeftButton:
            return
        self._origin = e.pos()
        self._rubber.setGeometry(QRect(self._origin, self._origin))
        self._rubber.show()
        self._dragging = True
        self._last_rect = QRect(self._origin, self._origin)

    def mouseMoveEvent(self, e):
        if not self._dragging or self._origin is None:
            return
        rect = QRect(self._origin, e.pos()).normalized()
        self._rubber.setGeometry(rect)
        self._last_rect = rect

    def mouseReleaseEvent(self, e):
        if e.button() == Qt.MouseButton.LeftButton:
            self._dragging = False
            if self._origin is not None:
                rect = QRect(self._origin, e.pos()).normalized()
                self._last_rect = rect if rect.width() and rect.height() else self._last_rect

    def selected_rect_original(self) -> Optional[QRect]:
        # Use stored rect even if rubber band lost visibility state
        if not self._last_rect or self._last_rect.width() < 1 or self._last_rect.height() < 1:
            return None
        r = self._last_rect
        x = int(round(r.x() * self.scale_factor))
        y = int(round(r.y() * self.scale_factor))
        w = max(1, int(round(r.width() * self.scale_factor)))
        h = max(1, int(round(r.height() * self.scale_factor)))
        return QRect(x, y, w, h)

class CropDialog(QDialog):
    def __init__(self, image_path: str, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Crop Image")
        self.image_path = image_path

        pix = QPixmap(image_path)
        ow, oh = pix.width(), pix.height()

        # Fit preview window, preserve aspect
        max_w, max_h = 1000, 700
        scale = min(1.0, min(max_w / max(1, ow), max_h / max(1, oh)))
        disp_w, disp_h = int(ow * scale), int(oh * scale)
        disp_pix = pix.scaled(disp_w, disp_h, Qt.AspectRatioMode.KeepAspectRatio,
                              Qt.TransformationMode.SmoothTransformation)

        self.label = _CropLabel(disp_pix, (1.0 / scale) if scale else 1.0, self)
        self.label.setFixedSize(disp_pix.size())
        layout = QVBoxLayout(self)
        layout.addWidget(self.label)

        btns = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel, parent=self)
        btns.accepted.connect(self.accept)
        btns.rejected.connect(self.reject)
        layout.addWidget(btns)

    def selected_rect(self) -> Optional[QRect]:
        return self.label.selected_rect_original()

# -------------------------- Notes Editor --------------------------

class NotesDocument(QTextDocument):
    """Custom document that handles image loading from database-specific folders."""
    def __init__(self, attach_dir: str, parent=None):
        super().__init__(parent)
        self.attach_dir = attach_dir
        self.db_dir = os.path.dirname(attach_dir)
        
    def loadResource(self, resource_type, name):
        """Override to load images from database-specific attachments folder."""
        if resource_type == QTextDocument.ResourceType.ImageResource:
            url_str = name.toString() if hasattr(name, 'toString') else str(name)
            
            # Handle file:// URLs
            if url_str.startswith('file://'):
                # Extract the path from file:// URL
                from PyQt6.QtCore import QUrl
                url = QUrl(url_str)
                local_path = url.toLocalFile()
                
                # Check if it's a relative path that needs to be resolved
                if local_path and os.path.basename(os.path.dirname(local_path)).endswith('_attachments'):
                    # Extract just the filename and look in the correct attach_dir
                    filename = os.path.basename(local_path)
                    abs_path = os.path.join(self.attach_dir, filename)
                else:
                    abs_path = local_path
            else:
                # Handle relative paths
                abs_path = os.path.join(self.db_dir, url_str)
            
            if abs_path and os.path.exists(abs_path):
                try:
                    from PyQt6.QtGui import QImage
                    qimg = QImage(abs_path)
                    if not qimg.isNull():
                        return qimg
                except Exception:
                    pass
        
        # Fall back to default behavior
        return super().loadResource(resource_type, name)

# Note highlight: accent-200. Yellow highlights from older notes are still recognised.
HIGHLIGHT_COLOR = QColor(214, 235, 255)
LEGACY_HIGHLIGHT_COLOR = QColor(255, 255, 0)
NOTES_HEADING_POINT_SIZE = 15.75   # 21px


def _is_highlight_color(color: QColor) -> bool:
    try:
        if color.alpha() <= 0:
            return False
        rgb = (color.red(), color.green(), color.blue())
        return rgb in ((HIGHLIGHT_COLOR.red(), HIGHLIGHT_COLOR.green(), HIGHLIGHT_COLOR.blue()), (255, 255, 0))
    except Exception:
        return False


COMMON_FORMAT_TOOLBAR_STYLESHEET = """
    QToolBar {
        border: 1px solid #d6d6d7;
        background: transparent;
        spacing: 2px;
        padding: 2px;
    }
    QToolButton {
        border: 1px solid transparent;
        padding: 3px;
        margin: 0px;
    }
    QToolButton:hover {
        background: #e3e3e4;
    }
    QToolButton:pressed {
        background: #d5d5d6;
    }
    QToolButton:checked {
        background: #d6ebff;
        border-color: #94bce3;
    }
"""


class SharedRichTextEditor(QTextEdit):
    """Shared rich-text editor behavior used by notes and templates."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setAcceptRichText(True)
        self.setUndoRedoEnabled(True)
        self._init_common_format_actions()
        self._apply_action_icons()
        self.cursorPositionChanged.connect(self._sync_format_action_state)
        try:
            self.currentCharFormatChanged.connect(lambda _fmt: self._sync_format_action_state())
        except Exception:
            pass
        self._apply_default_formatting()
        self._sync_format_action_state()

    def _create_editor_action(
        self,
        text: str,
        tooltip: str,
        handler,
        *,
        checkable: bool = False,
        shortcut: Optional[QKeySequence | str] = None,
        shortcuts: Optional[List[QKeySequence | str]] = None
    ) -> QAction:
        action = QAction(text, self)
        action.setToolTip(tooltip)
        action.setCheckable(checkable)

        if shortcut is not None:
            action.setShortcut(shortcut if isinstance(shortcut, QKeySequence) else QKeySequence(shortcut))
            action.setShortcutContext(Qt.ShortcutContext.WidgetWithChildrenShortcut)
        elif shortcuts:
            resolved_shortcuts = [
                shortcut_item if isinstance(shortcut_item, QKeySequence) else QKeySequence(shortcut_item)
                for shortcut_item in shortcuts
            ]
            action.setShortcuts(resolved_shortcuts)
            action.setShortcutContext(Qt.ShortcutContext.WidgetWithChildrenShortcut)

        action.triggered.connect(handler)
        self.addAction(action)
        return action

    def _init_common_format_actions(self):
        self.action_undo = self._create_editor_action(
            "Undo",
            "Undo",
            self.undo,
            shortcut=QKeySequence.StandardKey.Undo
        )
        self.action_redo = self._create_editor_action(
            "Redo",
            "Redo",
            self.redo,
            shortcuts=[QKeySequence.StandardKey.Redo, "Ctrl+Y"]
        )

        self.action_bold = self._create_editor_action(
            "Bold",
            "Bold (Ctrl+B)",
            self.toggle_bold,
            checkable=True,
            shortcut=QKeySequence.StandardKey.Bold
        )
        self.action_italic = self._create_editor_action(
            "Italic",
            "Italic (Ctrl+I)",
            self.toggle_italic,
            checkable=True,
            shortcut=QKeySequence.StandardKey.Italic
        )
        self.action_underline = self._create_editor_action(
            "Underline",
            "Underline (Ctrl+U)",
            self.toggle_underline,
            checkable=True,
            shortcut=QKeySequence.StandardKey.Underline
        )
        self.action_strikethrough = self._create_editor_action(
            "Strikethrough",
            "Strikethrough (Ctrl+Shift+X)",
            self.toggle_strikethrough,
            checkable=True,
            shortcut="Ctrl+Shift+X"
        )
        self.action_highlight = self._create_editor_action(
            "Highlight",
            "Highlight (Ctrl+Shift+H)",
            self.toggle_highlight,
            checkable=True,
            shortcut="Ctrl+Shift+H"
        )
        self.action_clear_formatting = self._create_editor_action(
            "Clear formatting",
            "Clear formatting from selected text",
            self.clear_formatting
        )

        self.action_decrease_text = self._create_editor_action(
            "A- Decrease Text Size",
            "Decrease text size",
            lambda: self._adjust_text_size(-1),
            shortcut=QKeySequence.StandardKey.ZoomOut
        )
        self.action_increase_text = self._create_editor_action(
            "A+ Increase Text Size",
            "Increase text size",
            lambda: self._adjust_text_size(+1),
            shortcut=QKeySequence.StandardKey.ZoomIn
        )

        self.action_heading = self._create_editor_action(
            "Heading",
            "Heading (H3)",
            self.toggle_heading,
            checkable=True
        )
        self.action_bulleted_list = self._create_editor_action(
            "Bulleted list",
            "Bulleted list",
            self.insert_bulleted_list
        )
        self.action_numbered_list = self._create_editor_action(
            "Numbered list",
            "Numbered list",
            self.insert_numbered_list
        )

        try:
            self.action_undo.setEnabled(False)
            self.action_redo.setEnabled(False)
            self.undoAvailable.connect(self.action_undo.setEnabled)
            self.redoAvailable.connect(self.action_redo.setEnabled)
        except Exception:
            pass

    def create_format_toolbar(self, parent=None) -> QToolBar:
        toolbar = QToolBar(parent or self)
        toolbar.setMovable(False)
        toolbar.setFloatable(False)
        toolbar.setIconSize(QSize(16, 16))
        toolbar.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonIconOnly)
        toolbar.setContextMenuPolicy(Qt.ContextMenuPolicy.PreventContextMenu)
        toolbar.setStyleSheet(COMMON_FORMAT_TOOLBAR_STYLESHEET)

        toolbar.addAction(self.action_undo)
        toolbar.addAction(self.action_redo)
        toolbar.addSeparator()
        toolbar.addAction(self.action_bold)
        toolbar.addAction(self.action_italic)
        toolbar.addAction(self.action_underline)
        toolbar.addAction(self.action_strikethrough)
        toolbar.addAction(self.action_highlight)
        toolbar.addAction(self.action_clear_formatting)
        toolbar.addSeparator()
        toolbar.addAction(self.action_decrease_text)
        toolbar.addAction(self.action_increase_text)
        toolbar.addSeparator()
        toolbar.addAction(self.action_bulleted_list)
        toolbar.addAction(self.action_numbered_list)

        return toolbar

    def _apply_default_formatting(self):
        """Set default formatting: 11pt, black text, no highlight."""
        doc_font = QFont()
        doc_font.setPointSizeF(11.0)
        doc_font.setFamily(T.FONT_BODY)
        self.document().setDefaultFont(doc_font)

        fmt = self.currentCharFormat()
        fmt.setFontPointSize(11.0)
        fmt.clearForeground()  # theme text colour
        fmt.setBackground(QColor(0, 0, 0, 0))
        self.setCurrentCharFormat(fmt)

    def _build_text_action_icon(
        self,
        label: str,
        *,
        bold: bool = False,
        italic: bool = False,
        underline: bool = False,
        strike: bool = False,
        yellow_bg: bool = False,
        slash: bool = False
    ) -> QIcon:
        size = 18
        pixmap = QPixmap(size, size)
        pixmap.fill(Qt.GlobalColor.transparent)
        painter = QPainter(pixmap)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        painter.setRenderHint(QPainter.RenderHint.TextAntialiasing, True)

        if yellow_bg:
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QColor(255, 241, 118))
            painter.drawRoundedRect(1, 1, size - 2, size - 2, 3, 3)

        font = QFont(self.font())
        font.setBold(bold)
        font.setItalic(italic)
        font.setUnderline(underline)
        font.setPointSizeF(8.5 if len(label) > 1 else 9.5)
        painter.setFont(font)
        painter.setPen(QPen(QColor(36, 41, 47)))
        painter.drawText(pixmap.rect(), Qt.AlignmentFlag.AlignCenter, label)

        if strike:
            painter.setPen(QPen(QColor(36, 41, 47), 1.5))
            mid_y = size // 2
            painter.drawLine(3, mid_y, size - 3, mid_y)

        if slash:
            painter.setPen(QPen(QColor(183, 28, 28), 1.6))
            painter.drawLine(3, size - 4, size - 4, 3)

        painter.end()
        return QIcon(pixmap)

    def _build_list_action_icon(self, *, ordered: bool) -> QIcon:
        size = 18
        pixmap = QPixmap(size, size)
        pixmap.fill(Qt.GlobalColor.transparent)
        painter = QPainter(pixmap)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        painter.setRenderHint(QPainter.RenderHint.TextAntialiasing, True)
        painter.setPen(QPen(QColor(36, 41, 47), 1.3))

        if ordered:
            font = QFont(self.font())
            font.setPointSizeF(6.5)
            painter.setFont(font)
            painter.drawText(QRect(1, 1, 5, 5), Qt.AlignmentFlag.AlignCenter, "1")
            painter.drawText(QRect(1, 6, 5, 5), Qt.AlignmentFlag.AlignCenter, "2")
            painter.drawText(QRect(1, 11, 5, 5), Qt.AlignmentFlag.AlignCenter, "3")
        else:
            painter.setBrush(QColor(36, 41, 47))
            for y in (4, 9, 14):
                painter.drawEllipse(QRect(2, y - 1, 3, 3))

        for y in (4, 9, 14):
            painter.drawLine(7, y, 15, y)

        painter.end()
        return QIcon(pixmap)

    def _apply_action_icons(self):
        style = self.style()
        self.action_undo.setIcon(style.standardIcon(QStyle.StandardPixmap.SP_ArrowBack))
        self.action_redo.setIcon(style.standardIcon(QStyle.StandardPixmap.SP_ArrowForward))
        self.action_bold.setIcon(lucide_icon("bold", 16))
        self.action_italic.setIcon(lucide_icon("italic", 16))
        self.action_underline.setIcon(lucide_icon("underline", 16))
        self.action_strikethrough.setIcon(lucide_icon("strike", 16))
        self.action_highlight.setIcon(lucide_icon("hilite", 16))
        self.action_clear_formatting.setIcon(self._build_text_action_icon("Tx", slash=True))
        self.action_decrease_text.setIcon(self._build_text_action_icon("A-", bold=True))
        self.action_increase_text.setIcon(self._build_text_action_icon("A+", bold=True))
        self.action_heading.setIcon(lucide_icon("heading", 16))
        self.action_bulleted_list.setIcon(lucide_icon("ul", 16))
        self.action_numbered_list.setIcon(lucide_icon("ol", 16))

    def _selection_has_format(self, matcher):
        cursor = self.textCursor()
        if not cursor.hasSelection():
            return matcher(cursor.charFormat())
        return any(matcher(fmt) for _, _, fmt in self._selected_format_runs(cursor))

    def _selected_format_runs(self, cursor):
        start, end = cursor.selectionStart(), cursor.selectionEnd()
        block = self.document().findBlock(start)
        runs = []
        while block.isValid() and block.position() < end:
            it = block.begin()
            while not it.atEnd():
                fragment = it.fragment()
                if fragment.isValid():
                    a, b = max(start, fragment.position()), min(end, fragment.position() + fragment.length())
                    if a < b and not fragment.charFormat().isImageFormat():
                        runs.append((a, b, fragment.charFormat()))
                it += 1
            block = block.next()
        return runs

    def _apply_char_format(self, updater):
        cursor = self.textCursor()
        if cursor.hasSelection():
            runs = self._selected_format_runs(cursor)
            cursor.beginEditBlock()
            for start, end, current in runs:
                changed = QTextCharFormat(current)
                updater(changed)
                cursor.setPosition(start)
                cursor.setPosition(end, QTextCursor.MoveMode.KeepAnchor)
                cursor.setCharFormat(changed)
            cursor.endEditBlock()
        else:
            current = self.currentCharFormat()
            updater(current)
            self.mergeCurrentCharFormat(current)

    def _toggle_char_format(
        self,
        matcher: Callable[[QTextCharFormat], bool],
        updater: Callable[[QTextCharFormat, bool], None]
    ):
        new_state = not self._selection_has_format(matcher)
        self._apply_char_format(lambda fmt: updater(fmt, new_state))
        self._after_common_format_change()
        self._sync_format_action_state()

    def _is_yellow_highlight(self, fmt: QTextCharFormat) -> bool:
        try:
            return _is_highlight_color(fmt.background().color())
        except Exception:
            return False

    def _block_is_heading(self) -> bool:
        return self.textCursor().blockFormat().headingLevel() == 3

    def toggle_heading(self):
        """Toggle H3 on the paragraphs touched by the cursor or selection."""
        from PyQt6.QtGui import QTextBlockFormat
        cursor = self.textCursor()
        make_heading = not self._block_is_heading()
        start, end = cursor.selectionStart(), cursor.selectionEnd()
        work = QTextCursor(cursor)
        work.beginEditBlock()
        work.setPosition(start)
        work.movePosition(QTextCursor.MoveOperation.StartOfBlock)
        work.setPosition(end, QTextCursor.MoveMode.KeepAnchor)
        work.movePosition(QTextCursor.MoveOperation.EndOfBlock, QTextCursor.MoveMode.KeepAnchor)
        block_fmt = QTextBlockFormat()
        block_fmt.setHeadingLevel(3 if make_heading else 0)
        work.mergeBlockFormat(block_fmt)
        char_fmt = QTextCharFormat()
        if make_heading:
            char_fmt.setFontFamilies([T.FONT_HEADING])
            char_fmt.setFontPointSize(NOTES_HEADING_POINT_SIZE)
            char_fmt.setFontWeight(QFont.Weight.DemiBold)
        else:
            char_fmt.setFontFamilies([T.FONT_BODY])
            char_fmt.setFontPointSize(11.0)
            char_fmt.setFontWeight(QFont.Weight.Normal)
        work.mergeCharFormat(char_fmt)
        work.endEditBlock()
        self.mergeCurrentCharFormat(char_fmt)
        self._after_common_format_change()
        self._sync_format_action_state()

    def _effective_point_size(self) -> float:
        try:
            size = self.fontPointSize()
            if size and size > 0:
                return float(size)
        except Exception:
            pass
        try:
            size = self.currentCharFormat().fontPointSize()
            if size and size > 0:
                return float(size)
        except Exception:
            pass
        try:
            size = self.font().pointSizeF()
            if size and size > 0:
                return float(size)
        except Exception:
            pass
        return 11.0

    def toggle_bold(self):
        self._toggle_char_format(
            lambda fmt: fmt.fontWeight() >= QFont.Weight.Bold,
            lambda fmt, enabled: fmt.setFontWeight(QFont.Weight.Bold if enabled else QFont.Weight.Normal)
        )

    def toggle_italic(self):
        self._toggle_char_format(
            lambda fmt: fmt.fontItalic(),
            lambda fmt, enabled: fmt.setFontItalic(enabled)
        )

    def toggle_underline(self):
        self._toggle_char_format(
            lambda fmt: fmt.fontUnderline(),
            lambda fmt, enabled: fmt.setFontUnderline(enabled)
        )

    def toggle_strikethrough(self):
        self._toggle_char_format(
            lambda fmt: fmt.fontStrikeOut(),
            lambda fmt, enabled: fmt.setFontStrikeOut(enabled)
        )

    def toggle_highlight(self):
        self._toggle_char_format(
            self._is_yellow_highlight,
            lambda fmt, enabled: fmt.setBackground(QColor(HIGHLIGHT_COLOR) if enabled else QColor(0, 0, 0, 0))
        )

    def _adjust_text_size(self, delta):
        def adjust(fmt):
            size = fmt.fontPointSize() or self._effective_point_size()
            fmt.setFontPointSize(max(8.0, min(72.0, size + delta)))
        self._apply_char_format(adjust)
        self._after_common_format_change()
        self._sync_format_action_state()

    def insert_bulleted_list(self):
        cursor = self.textCursor()
        list_fmt = QTextListFormat()
        list_fmt.setStyle(QTextListFormat.Style.ListDisc)
        cursor.createList(list_fmt)
        self.setTextCursor(cursor)
        self._after_common_format_change()
        self._sync_format_action_state()

    def insert_numbered_list(self):
        cursor = self.textCursor()
        list_fmt = QTextListFormat()
        list_fmt.setStyle(QTextListFormat.Style.ListDecimal)
        cursor.createList(list_fmt)
        self.setTextCursor(cursor)
        self._after_common_format_change()
        self._sync_format_action_state()

    def clear_formatting(self):
        cursor = self.textCursor()
        if not cursor.hasSelection():
            return

        try:
            fragment = cursor.selection()
            cleaned_html = self._strip_text_formatting_preserve_images(fragment.toHtml())
            cursor.insertHtml(cleaned_html)
            self._after_common_format_change()
            self._sync_format_action_state()
        except Exception as e:
            QMessageBox.warning(self, "Clear formatting", f"Failed to clear formatting: {e}")

    def _strip_text_formatting_preserve_images(self, html: str) -> str:
        img_tags = []
        img_pattern = r'<img[^>]*>'

        def preserve_img(match):
            img_tags.append(match.group(0))
            return f'__IMG_PLACEHOLDER_{len(img_tags)-1}__'

        html_no_imgs = re.sub(img_pattern, preserve_img, html, flags=re.IGNORECASE)
        html_no_imgs = re.sub(
            r'</?(?:b|strong|i|em|u|s|strike|sup|sub|small|big|font|span|mark)[^>]*>',
            '',
            html_no_imgs,
            flags=re.IGNORECASE
        )
        html_no_imgs = re.sub(r'\s+style\s*=\s*["\'][^"\']*["\']', '', html_no_imgs, flags=re.IGNORECASE)
        html_no_imgs = re.sub(r'\s+(?:color|size|face|bgcolor|align)\s*=\s*["\'][^"\']*["\']', '', html_no_imgs, flags=re.IGNORECASE)

        for i, img_tag in enumerate(img_tags):
            html_no_imgs = html_no_imgs.replace(f'__IMG_PLACEHOLDER_{i}__', img_tag)

        return html_no_imgs

    def _sync_format_action_state(self):
        state_actions = [
            getattr(self, 'action_bold', None),
            getattr(self, 'action_italic', None),
            getattr(self, 'action_underline', None),
            getattr(self, 'action_strikethrough', None),
            getattr(self, 'action_highlight', None),
        ]
        state_actions = [action for action in state_actions if action is not None]
        if not state_actions:
            return

        for action in state_actions:
            action.blockSignals(True)

        try:
            self.action_bold.setChecked(self._selection_has_format(lambda fmt: fmt.fontWeight() >= QFont.Weight.Bold))
            self.action_italic.setChecked(self._selection_has_format(lambda fmt: fmt.fontItalic()))
            self.action_underline.setChecked(self._selection_has_format(lambda fmt: fmt.fontUnderline()))
            self.action_strikethrough.setChecked(self._selection_has_format(lambda fmt: fmt.fontStrikeOut()))
            self.action_highlight.setChecked(self._selection_has_format(self._is_yellow_highlight))
            if hasattr(self, 'action_heading'):
                self.action_heading.setChecked(self._block_is_heading())
            if hasattr(self, 'action_checklist'):
                self.action_checklist.setChecked(self.is_checklist_block(self.textCursor().block()))
        finally:
            for action in state_actions:
                action.blockSignals(False)

    def setHtml(self, html: str):
        super().setHtml(html)
        if not html or html.strip() in ("", "<p></p>", "<p><br></p>"):
            self._apply_default_formatting()
            cursor = self.textCursor()
            cursor.movePosition(QTextCursor.MoveOperation.Start)
            self.setTextCursor(cursor)
        self._sync_format_action_state()

    def focusInEvent(self, e):
        super().focusInEvent(e)
        if self.toPlainText().strip() == "" or self.document().isEmpty():
            self._apply_default_formatting()
        self._sync_format_action_state()

    def _after_common_format_change(self):
        """Hook for subclasses that need to react to formatting actions."""
        pass


class NotesEditor(NotesFeaturesMixin, SharedRichTextEditor):
    noteSaved = pyqtSignal()        # complete HTML/text/attachment transaction committed
    saveStateChanged = pyqtSignal(str, str)
    findStateChanged = pyqtSignal(int, int)
    imageSelectionChanged = pyqtSignal(bool)
    imageResizeChanged = pyqtSignal(int, int)
    searchStateChanged = pyqtSignal(int, int)


    def __init__(self, db: DB, attach_dir: str, parent=None):
        super().__init__(parent)
        self.db = db
        self.attach_dir = attach_dir
        self.task_id: Optional[int] = None
        self._loading_note = False
        self._edited_since_load = False
        self.save_state = 'saved'
        self.save_error = ''
        self._note_positions = {}
        self._position_restore_timer = QTimer(self)
        self._position_restore_timer.setSingleShot(True)
        self._position_restore_timer.timeout.connect(self._restore_note_position)
        self.assets = NoteAssets(attach_dir)

        # Create and set custom document
        custom_doc = NotesDocument(attach_dir, self)
        
        # Set document base URL to database directory for resolving relative image paths
        db_dir = os.path.dirname(attach_dir)
        from PyQt6.QtCore import QUrl
        custom_doc.setBaseUrl(QUrl.fromLocalFile(db_dir + '/'))
        
        self.setDocument(custom_doc)
        
        self.setAcceptDrops(True)
        self.setAcceptRichText(True)
        self.setUndoRedoEnabled(True)

        # autosave
        self._autosave_timer = QTimer(self)
        self._autosave_timer.setInterval(600)
        self._autosave_timer.setSingleShot(True)
        self._autosave_timer.timeout.connect(self._autosave_timeout)
        self.textChanged.connect(self._schedule_autosave)
        # Compatibility timer: all entrypoints now use the same atomic save.
        self._html_save_timer = QTimer(self)
        self._html_save_timer.setInterval(1800)
        self._html_save_timer.setSingleShot(True)
        self._html_save_timer.timeout.connect(self._save_full_html)

        # Image actions
        self.action_resize = QAction("Resize Image…", self)
        self.action_resize.triggered.connect(self.resize_image_at_cursor)

        self.action_crop_visual = QAction("Crop Image…", self)
        self.action_crop_visual.triggered.connect(self.crop_image_visual)

        self.action_cutimg = QAction("Cut Image", self)
        self.action_cutimg.triggered.connect(self.cut_image_at_cursor)

        self.action_remove_image = QAction('Remove image', self)
        self.action_remove_image.triggered.connect(self.remove_image_at_cursor)
        self.cursorPositionChanged.connect(self._sync_image_selection)
        self.selectionChanged.connect(self._sync_image_selection)

        # Paste plain text action
        self.action_paste_plain = QAction("Paste Plain Text", self)
        self.action_paste_plain.setShortcut(QKeySequence("Ctrl+Shift+V"))
        self.action_paste_plain.triggered.connect(self.paste_plain_text)

        # Copy image at cursor
        self.action_copy_image = QAction("Copy image", self)
        self.action_copy_image.triggered.connect(self.copy_image_at_cursor)

        # Copy selected content (text + images)
        self.action_copy_selection = QAction("Copy", self)
        self.action_copy_selection.setShortcut(QKeySequence.StandardKey.Copy)
        self.action_copy_selection.triggered.connect(self.copy_selection)

        self.addAction(self.action_paste_plain)
        self.addAction(self.action_copy_selection)
        self.copyAvailable.connect(self.action_copy_selection.setEnabled)

        # Use custom context menu for better grouping and dynamic enabling
        self.setContextMenuPolicy(Qt.ContextMenuPolicy.DefaultContextMenu)
        self.addAction(self.action_crop_visual)
        self.addAction(self.action_resize)
        self.addAction(self.action_cutimg)
        # Avoid duplicate Paste Plain in menu

        self._resize = ResizeSession()
        self._resize_scroll_timer = QTimer(self)
        self._resize_scroll_timer.setInterval(30)
        self._resize_scroll_timer.timeout.connect(self._resize_autoscroll)
        self.document().contentsChange.connect(self._resize_document_changed)
        # Track last hashes
        self._last_plain_hash = ""; self._last_html_hash = ""
        # Set base URL so relative image src paths resolve
        try:
            self.document().setBaseUrl(QUrl.fromLocalFile(db_dir + os.sep))
        except Exception:
            pass

        # Links, checklists, find and line spacing (ui/notes_features.py)
        self.action_checklist = self._create_editor_action(
            "Checklist", "Checklist (Ctrl+Shift+C ticks an item)", self.toggle_checklist, checkable=True)
        self.action_checklist.setIcon(lucide_icon("checklist", 16))
        self.action_toggle_checked = self._create_editor_action(
            "Tick / untick item", "Tick or untick the checklist item", self.toggle_checked, shortcut="Ctrl+Shift+C")
        self.action_link = self._create_editor_action("Link…", "Insert or edit a link (Ctrl+L)", self.edit_link, shortcut="Ctrl+L")
        self.action_link.setIcon(lucide_icon("link", 16))
        self.action_remove_link = self._create_editor_action("Remove link", "Remove the link, keep the text", self.remove_link)
        self.action_open_link = self._create_editor_action("Open link", "Open the link in the browser", self._open_link_at_cursor)
        self.init_notes_features()

        # Re-apply defaults after replacing the document.
        self._apply_default_formatting()
        self._sync_format_action_state()

    def _open_link_at_cursor(self):
        existing = self.anchor_range_at_cursor()
        if existing:
            self.open_link(existing[2])

    def createMimeDataFromSelection(self):
        mime = super().createMimeDataFromSelection()
        if mime.hasHtml():
            mime.setHtml(self.assets.embed_html(notes_html.strip_line_height(mime.html())))
        mime.setData(INTERNAL_MIME, b'1')
        return mime

    def populate_template_menu(self, menu: QMenu) -> int:
        """Fill menu with one action per stored note template. Returns how many were added."""
        try:
            templates = self.db.list_note_templates() if self.db else []
        except Exception:
            templates = []
        for template in templates:
            title = (template['title'] or '').strip() or "Untitled template"
            html = template['content_html'] or ""
            action = menu.addAction(title)
            action.triggered.connect(lambda _checked=False, content=html: self.insert_template(content))
        return len(templates)

    def has_content(self) -> bool:
        """True when the note has text or an image."""
        return bool(self.toPlainText().strip()) or "\ufffc" in self.toPlainText()

    def _apply_default_formatting(self):
        """Set default formatting: 11pt, theme text colour, no highlight."""
        # Set the document's default font to ensure consistency
        doc_font = QFont()
        doc_font.setPointSizeF(11.0)
        doc_font.setFamily(T.FONT_BODY)
        self.document().setDefaultFont(doc_font)
        
        # Also set the current character format for new typing
        fmt = self.currentCharFormat()
        fmt.setFontPointSize(11.0)
        fmt.clearForeground()  # theme text colour
        fmt.setBackground(QColor(0, 0, 0, 0))  # No highlight (transparent)
        self.setCurrentCharFormat(fmt)

    def _build_text_action_icon(
        self,
        label: str,
        *,
        bold: bool = False,
        italic: bool = False,
        underline: bool = False,
        strike: bool = False,
        yellow_bg: bool = False,
        slash: bool = False
    ) -> QIcon:
        """Create a compact icon for notes formatting actions."""
        size = 18
        pixmap = QPixmap(size, size)
        pixmap.fill(Qt.GlobalColor.transparent)
        painter = QPainter(pixmap)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        painter.setRenderHint(QPainter.RenderHint.TextAntialiasing, True)

        if yellow_bg:
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QColor(255, 241, 118))
            painter.drawRoundedRect(1, 1, size - 2, size - 2, 3, 3)

        font = QFont(self.font())
        font.setBold(bold)
        font.setItalic(italic)
        font.setUnderline(underline)
        font.setPointSizeF(8.5 if len(label) > 1 else 9.5)
        painter.setFont(font)
        painter.setPen(QPen(QColor(36, 41, 47)))
        painter.drawText(pixmap.rect(), Qt.AlignmentFlag.AlignCenter, label)

        if strike:
            painter.setPen(QPen(QColor(36, 41, 47), 1.5))
            mid_y = size // 2
            painter.drawLine(3, mid_y, size - 3, mid_y)

        if slash:
            painter.setPen(QPen(QColor(183, 28, 28), 1.6))
            painter.drawLine(3, size - 4, size - 4, 3)

        painter.end()
        return QIcon(pixmap)

    def _apply_action_icons(self):
        """Assign function-appropriate icons to editor actions."""
        super()._apply_action_icons()

    def contextMenuEvent(self, e):  # override
        """Build a structured context menu with logical sections and dynamic enabling."""
        clicked = self.cursorForPosition(e.pos())
        selected = self.textCursor()
        if not selected.hasSelection() or not (selected.selectionStart() <= clicked.position() < selected.selectionEnd()):
            self.setTextCursor(clicked)
        menu = QMenu(self)

        # Determine if cursor is on or adjacent to an image
        has_image = self._cursor_on_or_adjacent_to_image()

        # 1) Undo / Redo
        menu.addAction(self.action_undo)
        menu.addAction(self.action_redo)
        menu.addSeparator()

        # 2) Text formatting
        menu.addAction(self.action_bold)
        menu.addAction(self.action_strikethrough)
        menu.addAction(self.action_increase_text)
        menu.addAction(self.action_decrease_text)
        menu.addAction(self.action_highlight)
        menu.addAction(self.action_checklist)
        menu.addSeparator()

        # 2b) Links
        on_link = self.anchor_range_at_cursor() is not None
        menu.addAction(self.action_link)
        self.action_open_link.setEnabled(on_link)
        self.action_remove_link.setEnabled(on_link)
        if on_link:
            menu.addAction(self.action_open_link)
            menu.addAction(self.action_remove_link)
        menu.addSeparator()

        # 3) Template insertion
        self._add_insert_template_submenu(menu)
        menu.addSeparator()

        # 4) Copy selection
        has_selection = self.textCursor().hasSelection()
        self.action_copy_selection.setEnabled(has_selection)
        menu.addAction(self.action_copy_selection)
        menu.addSeparator()

        # 5) Image tools (Copy image first as requested item)
        self.action_copy_image.setEnabled(has_image)
        self.action_resize.setEnabled(has_image)
        self.action_crop_visual.setEnabled(has_image)
        self.action_cutimg.setEnabled(has_image)
        self.action_remove_image.setEnabled(has_image)
        menu.addAction(self.action_copy_image)
        menu.addAction(self.action_resize)
        menu.addAction(self.action_crop_visual)
        menu.addAction(self.action_cutimg)
        menu.addAction(self.action_remove_image)
        menu.addSeparator()

        # 6) Clipboard actions and formatting
        has_selection = self.textCursor().hasSelection()
        self.action_clear_formatting.setEnabled(has_selection)
        menu.addAction(self.action_clear_formatting)
        menu.addAction(self.action_paste_plain)

        menu.exec(e.globalPos())
        menu.deleteLater()

    def _add_insert_template_submenu(self, menu: QMenu):
        """Add the Insert Template submenu populated from stored templates."""
        template_menu = menu.addMenu("Insert Template")
        if not self.db:
            template_menu.setEnabled(False)
            return

        if self.populate_template_menu(template_menu) == 0:
            empty_action = template_menu.addAction("No templates")
            empty_action.setEnabled(False)

    def insert_template(self, template_html: str):
        """Insert template HTML at the current cursor position."""
        if template_html is None:
            return
        cursor = self.textCursor()
        cursor.insertHtml(template_html)
        self.setTextCursor(cursor)
        self._schedule_autosave()
        QTimer.singleShot(0, self._ensure_cursor_visibility)

    def _cursor_on_or_adjacent_to_image(self):
        return self._image_cursor() is not None

    def paste_plain_text(self):
        clipboard = QApplication.clipboard()
        mime_data = clipboard.mimeData()
        
        if mime_data.hasText():
            plain_text = mime_data.text()
            cursor = self.textCursor()
            cursor.insertText(plain_text)
            self._schedule_autosave()




    def _image_path_at_or_adjacent_to_cursor(self):
        return self._current_image_path()

    def copy_image_at_cursor(self):
        source = self._current_image_path()
        if source:
            image = self.assets.image(source)
            if not image.isNull():
                QApplication.clipboard().setImage(image)
                return True
        return False

    def copy_selection(self):
        if self.textCursor().hasSelection():
            QApplication.clipboard().setMimeData(self.createMimeDataFromSelection())




    def _handle_indent(self):
        self._indent_selected_lines(self.textCursor(), indent=True)

    def _handle_outdent(self):
        self._indent_selected_lines(self.textCursor(), indent=False)

    def _indent_selected_lines(self, cursor, indent=True):
        cursor.beginEditBlock()
        grouped_lists = {}
        for block in self._selected_blocks():
            work = QTextCursor(block)
            text_list = block.textList()
            if text_list is not None:
                fmt = text_list.format()
                level = fmt.indent() + (1 if indent else -1)
                if level <= 0:
                    text_list.remove(block)
                    block_format = work.blockFormat()
                    block_format.setIndent(0)
                    block_format.setMarker(QTextBlockFormat.MarkerType.NoMarker)
                    work.setBlockFormat(block_format)
                else:
                    key = text_list.objectIndex()
                    fmt.setIndent(level)
                    if key not in grouped_lists:
                        grouped_lists[key] = work.createList(fmt)
                    else:
                        grouped_lists[key].add(block)
            else:
                fmt = work.blockFormat()
                fmt.setIndent(max(0, fmt.indent() + (1 if indent else -1)))
                work.setBlockFormat(fmt)
        cursor.endEditBlock()

    def _remove_indentation_at_cursor(self, cursor):
        self._indent_selected_lines(cursor, indent=False)

    # --- autosave machinery ---
    def _schedule_autosave(self):
        if self._loading_note or self.task_id is None:
            return
        self._edited_since_load = True
        self._set_save_state('dirty')
        self._autosave_timer.start()

    def _set_save_state(self, state, error=''):
        self.save_state, self.save_error = state, error
        self.saveStateChanged.emit(state, error)

    def _after_common_format_change(self):
        self._schedule_autosave()

    def _autosave_timeout(self):
        self.save_now()

    def setHtml(self, html: str):
        """Override setHtml to apply default formatting after setting content."""
        super().setHtml(html)
        # Apply default formatting for new typing, especially when content is empty
        if not html or html.strip() in ("", "<p></p>", "<p><br></p>"):
            self._apply_default_formatting()
            # Move cursor to start and ensure formatting is applied
            cursor = self.textCursor()
            cursor.movePosition(cursor.MoveOperation.Start)
            self.setTextCursor(cursor)

    def save_all_now(self):
        return self._save_full_html()

    def flush_pending(self):
        return self._save_full_html()

    def _load_html(self, html: str):
        """Show stored note HTML: themed fonts and colour, on-screen line spacing, clean undo stack."""
        self.cancel_image_resize()
        self._loading_note = True
        self.blockSignals(True)
        try:
            self.document().setUndoRedoEnabled(False)
            self.setHtml(notes_html.normalize_for_display(html or ""))
            self._register_images_from_html(html or "")
            self.apply_line_spacing(whole_document=True)
            self.document().setUndoRedoEnabled(True)
            self.document().setModified(False)
        finally:
            self.blockSignals(False)
            self._loading_note = False
        # Opening a note never counts as a change. The baseline hash is not computed here
        # (that would serialise the whole note); "no change signalled since loading" stands
        # in for it until the first real edit is saved.
        self._last_html_hash = None
        self._edited_since_load = False
        self._set_save_state('saved')
        self._refresh_highlights_after_edit()
        self._sync_image_selection()

    def set_task(self, task_id: Optional[int]):
        # Keep what was typed in the note that is being left
        if not self.flush_pending():
            return False
        self._end_inline_resize(commit=False)
        if self.task_id is not None:
            cursor = self.textCursor()
            self._note_positions[self.task_id] = (cursor.position(), cursor.anchor(), self.verticalScrollBar().value())
        self.task_id = task_id
        self._position_restore_timer.stop()
        self._restoring_note_position = False
        self._autosave_timer.stop(); self._html_save_timer.stop()
        self.clear_find()
        if task_id is None:
            self._loading_note = True
            try:
                self.setHtml("")
            finally:
                self._loading_note = False
            self._last_plain_hash = self._last_html_hash = ""
            # Clear cursor positioning after clearing content
            self._sync_image_selection()
            return True
        html, _plain = self.db.get_note(task_id)
        self._load_html(html)
        self._last_plain_hash = hashlib.md5((_plain or "").encode('utf-8','ignore')).hexdigest()
        saved = self._note_positions.get(task_id)
        if saved:
            position, anchor, scroll = saved
            cursor = self.textCursor()
            last = self.document().characterCount() - 1
            cursor.setPosition(min(anchor, last))
            cursor.setPosition(min(position, last), QTextCursor.MoveMode.KeepAnchor)
            self.setTextCursor(cursor)
            self.verticalScrollBar().setValue(scroll)
            self._restoring_note_position = True
            self._restore_scroll_target = scroll
            self._position_restore_timer.start(50)
        return True

    def _restore_note_position(self):
        if self._restoring_note_position:
            self.verticalScrollBar().setValue(self._restore_scroll_target)
            self._restoring_note_position = False

    def rebind_context(self, db: DB, attach_dir: str, reload_current_task: bool = False):
        """Rebind the editor to a new database and attachments directory."""
        if db is not self.db:
            # The open note belongs to the database being left. Save it there and let go of the
            # task id, so nothing typed for it can be written to the same id in the new database.
            if not self.save_all_now():
                return False
            self._note_positions.clear()
            self.task_id = None
            self._autosave_timer.stop(); self._html_save_timer.stop()
            self._last_plain_hash = self._last_html_hash = ""
        self.db = db
        self.attach_dir = attach_dir
        self.assets = NoteAssets(attach_dir)
        db_dir = os.path.dirname(attach_dir)

        doc = self.document()
        if isinstance(doc, NotesDocument):
            doc.attach_dir = attach_dir
            doc.db_dir = db_dir
        try:
            doc.setBaseUrl(QUrl.fromLocalFile(db_dir + os.sep))
        except Exception:
            pass

        if reload_current_task and self.task_id is not None:
            self.set_task(self.task_id)

    def save_now(self):
        return self._save_full_html()

    def _save_full_html(self):
        self._autosave_timer.stop()
        self._html_save_timer.stop()
        if self.task_id is None or not self._edited_since_load:
            return True
        self._set_save_state('saving')
        try:
            if not self.db or not self.db.conn:
                raise RuntimeError('The database is not available.')
            html = notes_html.strip_line_height(self.toHtml())
            plain = self.toPlainText()
            digest = hashlib.md5(html.encode('utf-8')).hexdigest()
            if digest != self._last_html_hash:
                self.db.save_note(self.task_id, html, plain, self.attach_dir)
                self._last_html_hash = digest
                self._last_plain_hash = hashlib.md5(plain.encode('utf-8')).hexdigest()
                self._edited_since_load = False
                self._set_save_state('saved')
                self.noteSaved.emit()
            else:
                self._edited_since_load = False
                self._set_save_state('saved')
            return True
        except Exception as error:
            self._set_save_state('error', str(error))
            return False

    def cleanup_resources(self):
        self._position_restore_timer.stop()
        self._highlight_timer.stop()
        self._end_inline_resize(commit=False)
        result = self.save_all_now()
        self._autosave_timer.stop()
        self._html_save_timer.stop()
        return result

    def discard_unsaved(self):
        if self.save_state != 'error':
            return
        answer = QMessageBox.question(self, 'Discard unsaved edits',
            'Discard the unsaved changes to this note? Export first if you need to keep a copy.',
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No)
        if answer != QMessageBox.StandardButton.Yes:
            return
        try:
            html, plain = self.db.get_note(self.task_id)
        except Exception:
            # The user explicitly discarded the in-memory draft; an unavailable DB
            # must not trap them in a window they can neither close nor switch away from.
            html, plain = '', ''
        self._autosave_timer.stop()
        self._html_save_timer.stop()
        self._load_html(html)
        self._last_plain_hash = hashlib.md5(plain.encode('utf-8')).hexdigest()

    def export_unsaved(self):
        path, _ = QFileDialog.getSaveFileName(self, 'Export note', '', 'HTML (*.html)')
        if not path:
            return
        try:
            content = self.assets.embed_html(notes_html.strip_line_height(self.toHtml()))
            with open(path, 'w', encoding='utf-8') as output:
                output.write(content)
        except Exception as error:
            QMessageBox.warning(self, 'Export note', str(error))

    def reload_html(self):
        """Reload current task's HTML from DB (used after external image edits)."""
        if self.task_id is None:
            return
        if not self.flush_pending():
            return False
        html, plain = self.db.get_note(self.task_id)
        self._load_html(html)
        self._last_plain_hash = hashlib.md5((plain or "").encode('utf-8','ignore')).hexdigest()

    def _image_cursor(self, cursor=None):
        cursor = QTextCursor(cursor or self.textCursor())
        positions = [cursor.selectionStart()] if cursor.hasSelection() else [cursor.position(), cursor.position() - 1]
        for position in positions:
            if position < 0 or position >= self.document().characterCount() - 1:
                continue
            probe = QTextCursor(self.document())
            probe.setPosition(position)
            probe.movePosition(QTextCursor.MoveOperation.NextCharacter, QTextCursor.MoveMode.KeepAnchor)
            if probe.charFormat().isImageFormat():
                return probe
        return None

    def _current_image_path(self):
        cursor = self._image_cursor()
        return cursor.charFormat().toImageFormat().name() if cursor else None

    def _sync_image_selection(self):
        self.imageSelectionChanged.emit(self._image_cursor() is not None)

    def remove_image_at_cursor(self):
        cursor = self._image_cursor()
        if cursor:
            cursor.beginEditBlock()
            cursor.removeSelectedText()
            cursor.endEditBlock()
            self.setTextCursor(cursor)

    def set_image_dimensions(self, width, height, cursor=None):
        cursor = cursor or self._image_cursor()
        if cursor is None:
            return
        fmt = cursor.charFormat().toImageFormat()
        width, height = constrained_size(width, height)
        image = self.assets.image(fmt.name())
        if (fmt.width() or image.width(), fmt.height() or image.height()) == (width, height):
            return
        fmt.setWidth(width)
        fmt.setHeight(height)
        cursor.beginEditBlock()
        cursor.setCharFormat(fmt)
        cursor.endEditBlock()
        self.setTextCursor(cursor)
        self._sync_image_selection()

    def set_image_scale(self, percent):
        cursor = self._image_cursor()
        if cursor:
            fmt = cursor.charFormat().toImageFormat()
            image = self.assets.image(fmt.name())
            if not image.isNull():
                width, height = fmt.width() or image.width(), fmt.height() or image.height()
                # Percent measures width relative to source; keep current proportions.
                target = image.width() * percent / 100
                self.set_image_dimensions(*constrained_size(target, height, width / height))

    def restore_image_proportions(self):
        cursor = self._image_cursor()
        if cursor:
            fmt = cursor.charFormat().toImageFormat()
            image = self.assets.image(fmt.name())
            if not image.isNull():
                self.set_image_dimensions(*constrained_size(fmt.width() or image.width(),
                                         image.height(), image.width() / image.height()))

    def actual_image_size(self):
        image = self.assets.image(self._current_image_path() or '')
        if not image.isNull():
            self.set_image_dimensions(image.width(), image.height())

    def fit_image_width(self):
        if self.is_resizing_image:
            width, height = self._resize.current_size
            self.preview_image_size(min(width, max(1, self.viewport().width() - 24)), height,
                                    dimension='width', force_lock=True)
            return
        cursor = self._image_cursor()
        if cursor:
            fmt = cursor.charFormat().toImageFormat()
            image = self.assets.image(fmt.name())
            if not image.isNull():
                width, height = fmt.width() or image.width(), fmt.height() or image.height()
                self.set_image_dimensions(*constrained_size(
                    min(width, max(1, self.viewport().width() - 24)), height, width / height))

    def _register_images_from_html(self, html: str):
        """Register all images found in HTML as resources so they can be displayed"""
        import re
        # Find all img tags with src attributes
        img_pattern = r'<img[^>]+src=["\']([^"\']+)["\']'
        for match in re.finditer(img_pattern, html):
            rel_path = match.group(1)
            # Process both old 'attachments/' and new database-specific paths
            if '/' in rel_path and ('attachments' in rel_path or rel_path.split('/')[0].endswith('_attachments')):
                abs_path = self._resolve_image_path(rel_path)
                if os.path.exists(abs_path):
                    try:
                        qimg = QImage(abs_path)
                        if not qimg.isNull():
                            self.document().addResource(QTextDocument.ResourceType.ImageResource, QUrl(rel_path), qimg)
                    except Exception:
                        pass

    def _resolve_image_path(self, rel_path):
        return str(self.assets.resolve(rel_path))

    def _register_image_resource(self, rel_path: str, abs_path: str) -> QImage:
        try:
            qimg = QImage(abs_path)
            if not qimg.isNull():
                self.document().addResource(QTextDocument.ResourceType.ImageResource, QUrl(rel_path), qimg)
            return qimg
        except Exception:
            return QImage()

    def _insert_image_file(self, abs_path: str, rel_path: str) -> bool:
        qimg = self._register_image_resource(rel_path, abs_path)
        if qimg.isNull():
            return False
        from PyQt6.QtGui import QTextImageFormat
        img_fmt = QTextImageFormat()
        img_fmt.setName(rel_path)
        w, h = qimg.width(), qimg.height()
        max_w = max(40, self.viewport().width() - 24)
        if w > max_w and w > 0:
            scale = max_w / w
            w, h = int(w * scale), int(h * scale)
        if w > 0:
            img_fmt.setWidth(w)
        if h > 0:
            img_fmt.setHeight(h)
        cur = self.textCursor()
        cur.insertImage(img_fmt)
        return True

    def canInsertFromMimeData(self, source: QMimeData) -> bool:  # override
        if source.hasImage():
            return True
        if source.hasUrls():
            for u in source.urls():
                if u.isLocalFile() and os.path.splitext(u.toLocalFile())[1].lower() in ('.png','.jpg','.jpeg','.gif','.bmp','.webp'):
                    return True
        for fmt in ('image/png','image/jpeg'):
            if source.hasFormat(fmt):
                return True
        return super().canInsertFromMimeData(source)

    def insertFromMimeData(self, source):
        try:
            if source.hasHtml():
                content = source.html()
                if not source.hasFormat(INTERNAL_MIME):
                    content = notes_html.sanitize_pasted_html(content)
                if content:
                    content = self.assets.import_html(content)
                    cursor = self.textCursor()
                    start = cursor.selectionStart()
                    cursor.beginEditBlock()
                    cursor.insertHtml(content)
                    end = cursor.position()
                    block = self.document().findBlock(start)
                    images = []
                    while block.isValid() and block.position() < end:
                        iterator = block.begin()
                        while not iterator.atEnd():
                            fragment = iterator.fragment()
                            if fragment.isValid() and start <= fragment.position() < end and fragment.charFormat().isImageFormat():
                                images.append((fragment.position(), fragment.charFormat().toImageFormat()))
                            iterator += 1
                        block = block.next()
                    for position, fmt in images:
                        image = self.assets.image(fmt.name())
                        width, height = fmt.width() or image.width(), fmt.height() or image.height()
                        available = max(40, self.viewport().width() - 24)
                        if width > available:
                            height = height * available / width
                            width = available
                        fmt.setWidth(width)
                        fmt.setHeight(height)
                        image_cursor = QTextCursor(self.document())
                        image_cursor.setPosition(position)
                        image_cursor.movePosition(QTextCursor.MoveOperation.NextCharacter, QTextCursor.MoveMode.KeepAnchor)
                        image_cursor.setCharFormat(fmt)
                    cursor.endEditBlock()
                    self.setTextCursor(cursor)
                elif source.hasText():
                    self.insertPlainText(source.text())
                return
            images = []
            if source.hasUrls():
                for url in source.urls():
                    if url.isLocalFile():
                        image = QImage(url.toLocalFile())
                        if not image.isNull():
                            images.append(image)
            if not images and source.hasImage():
                image = source.imageData()
                if isinstance(image, QPixmap):
                    image = image.toImage()
                if isinstance(image, QImage) and not image.isNull():
                    images.append(image)
            if not images:
                for name in ('image/png', 'image/jpeg'):
                    if source.hasFormat(name):
                        image = QImage.fromData(source.data(name))
                        if not image.isNull():
                            images.append(image)
                            break
            if images:
                sources = [self.assets.store(image) for image in images]
                cursor = self.textCursor()
                cursor.beginEditBlock()
                for rel in sources:
                    self._insert_image_file(str(self.assets.resolve(rel)), rel)
                cursor.endEditBlock()
            elif source.hasText():
                self.insertPlainText(source.text())
        except Exception as error:
            QMessageBox.warning(self, 'Paste content', str(error))

    def resize_image_at_cursor(self):
        path = self._current_image_path()
        if not path:
            QMessageBox.information(self, "Resize Image", "Place the cursor on an image first.")
            return
        self._start_inline_resize(path)

    def crop_image_visual(self):
        cursor = self._image_cursor()
        if cursor is None:
            return
        source = cursor.charFormat().toImageFormat().name()
        image = self.assets.image(source)
        if image.isNull():
            QMessageBox.warning(self, 'Crop image', 'The image could not be loaded.')
            return
        with tempfile.TemporaryDirectory() as work:
            preview = os.path.join(work, 'preview.png')
            image.save(preview)
            dialog = CropDialog(preview, self)
            if dialog.exec() != QDialog.DialogCode.Accepted:
                return
            rect = dialog.selected_rect()
        if rect is None:
            return
        rect = rect.intersected(image.rect())
        if rect.isEmpty():
            return
        try:
            cropped = image.copy(rect)
            rel = self.assets.store(cropped)
            self._register_image_resource(rel, str(self.assets.resolve(rel)))
            fmt = cursor.charFormat().toImageFormat()
            scale = (fmt.width() or image.width()) / image.width()
            fmt.setName(rel)
            fmt.setWidth(max(1, round(cropped.width() * scale)))
            fmt.setHeight(max(1, round(cropped.height() * scale)))
            cursor.beginEditBlock()
            cursor.setCharFormat(fmt)
            cursor.endEditBlock()
            self.setTextCursor(cursor)
        except Exception as error:
            QMessageBox.warning(self, 'Crop image', str(error))

    def cut_image_at_cursor(self):
        if self.copy_image_at_cursor():
            self.remove_image_at_cursor()

    @property
    def is_resizing_image(self):
        return self._resize.active

    def apply_image_resize(self):
        self._end_inline_resize(True)

    def cancel_image_resize(self):
        self._end_inline_resize(False)

    def set_image_resize_locked(self, locked):
        self._resize.locked = locked
        if self._resize.active:
            w, h = self._resize.current_size
            self._resize.aspect = w / h

    def preview_image_size(self, width, height, dimension='width', force_lock=False):
        if not self._resize.active:
            return
        ratio = self._resize.aspect if self._resize.locked or force_lock else None
        if force_lock:
            w, h = self._resize.current_size
            ratio = w / h
        if ratio and dimension == 'height':
            width = height * ratio
        self._resize.current_size = constrained_size(width, height, ratio)
        self._update_resize_rubber()
        self.imageResizeChanged.emit(*self._resize.current_size)

    def _resize_document_changed(self, *_):
        # Includes toolbar formatting, paste, Undo/Redo and programmatic edits.
        if self._resize.active:
            self.cancel_image_resize()

    def _start_inline_resize(self, path):
        self._end_inline_resize(False)
        cursor = self._image_cursor()
        image = self.assets.image(path)
        if cursor is None or image.isNull():
            return
        fmt = cursor.charFormat().toImageFormat()
        width, height = int(fmt.width() or image.width()), int(fmt.height() or image.height())
        self._resize.active = True
        self._resize.path = path
        self._resize.origin_size = (width, height)
        self._resize.current_size = (width, height)
        self._resize.aspect = width / max(1, height)
        self._resize.cursor = QTextCursor(cursor)
        self._resize.revision = self.document().revision()
        self._resize.preview = ImageResizePreview(image, self.viewport())
        self._resize.preview.show()
        self._resize.rubber = QWidget(self.viewport())
        self._resize.rubber.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        self._resize.rubber.setStyleSheet(f'border:2px solid {T.ACCENT}; background:transparent;')
        self._resize.handle = QWidget(self.viewport())
        self._resize.handle.setStyleSheet(f'background:{T.ACCENT};')
        self._resize.handle.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        self._update_resize_rubber()
        self._resize.rubber.show()
        self._resize.handle.show()
        self.setFocus()
        self._sync_image_selection()
        self.imageResizeChanged.emit(width, height)

    def _position_resize_handle(self):
        if not (self._resize.handle and self._resize.rubber):
            return
        g = self._resize.rubber.geometry()
        sz = 12  # handle size
        # Position handle at bottom-right corner with slight overlap
        self._resize.handle.setGeometry(
            g.right() - sz + 2, 
            g.bottom() - sz + 2, 
            sz, 
            sz
        )
        # Ensure handle is visible and on top
        self._resize.handle.raise_()
        self._resize.handle.show()

    def _update_resize_rubber(self):
        if not self._resize.active or not self._resize.cursor:
            return
        cursor = QTextCursor(self._resize.cursor)
        cursor.setPosition(cursor.selectionStart())
        rect = self.cursorRect(cursor)
        width, height = self._resize.current_size
        original_height = self._resize.origin_size[1]
        line = cursor.block().layout().lineForTextPosition(cursor.positionInBlock())
        # cursorRect includes the descent of adjacent text; images sit on the baseline.
        baseline = rect.top() + (line.ascent() if line.isValid() else rect.height())
        self._resize.anchor = QRect(rect.left(), round(baseline - original_height), width, height)
        original_width, original_height = self._resize.origin_size
        self._resize.preview.target_size = QSize(width, height)
        self._resize.preview.setGeometry(QRect(self._resize.anchor.topLeft(),
            QSize(max(width, original_width), max(height, original_height))))
        self._resize.preview.update()
        self._resize.rubber.setGeometry(self._resize.anchor)
        self._resize.rubber.raise_()
        self._position_resize_handle()

    def mousePressEvent(self, e):
        if self._resize.active and e.button() == Qt.MouseButton.LeftButton:
            # Check if clicking on resize handle
            if self._resize.handle and self._resize.handle.isVisible():
                handle_rect = self._resize.handle.geometry()
                click_pos = e.position().toPoint()
                if handle_rect.contains(click_pos):
                    self._resize.dragging = True
                    self._resize.drag_point = click_pos
                    self._resize.pointer = click_pos
                    self._resize.drag_size = self._resize.current_size
                    self._resize.drag_scroll = (self.horizontalScrollBar().value(), self.verticalScrollBar().value())
                    self._resize_scroll_timer.start()
                    e.accept()
                    return
            # If not on handle but in resize mode, end resize and allow normal cursor placement
            self._end_inline_resize(commit=False)
            # Don't return here - let the normal mouse handling proceed
        
        # Always call parent to ensure proper cursor placement
        super().mousePressEvent(e)
        
        # Force cursor visibility and position update after click
        if e.button() == Qt.MouseButton.LeftButton and not self._resize.active:
            # Ensure cursor is visible and properly positioned
            QTimer.singleShot(0, self._ensure_cursor_visibility)

    def mouseMoveEvent(self, e):
        if self._resize.active and self._resize.dragging and self._resize.anchor and self._resize.rubber:
            self._resize.pointer = e.position().toPoint()
            self._resize.shift = bool(e.modifiers() & Qt.KeyboardModifier.ShiftModifier)
            self._resize_from_pointer()
            e.accept()
            return

        if not self._resize.active:
            href = self.link_at(e.position().toPoint())
            ctrl = bool(e.modifiers() & Qt.KeyboardModifier.ControlModifier)
            self.viewport().setCursor(Qt.CursorShape.PointingHandCursor if (href and ctrl) else Qt.CursorShape.IBeamCursor)
            tip = f"{href}\n{CTRL_LABEL}+click to open" if href else ""
            if self.viewport().toolTip() != tip:
                self.viewport().setToolTip(tip)

        super().mouseMoveEvent(e)

    def _resize_from_pointer(self):
        state = self._resize
        if not state.active or not state.dragging:
            return
        dx = state.pointer.x() - state.drag_point.x() + self.horizontalScrollBar().value() - state.drag_scroll[0]
        dy = state.pointer.y() - state.drag_point.y() + self.verticalScrollBar().value() - state.drag_scroll[1]
        w, h = state.drag_size
        width, height = w + dx, h + dy
        ratio = w / h if state.locked and not state.shift else None
        if ratio:
            # Project pointer delta onto the original diagonal for a smooth scale.
            scale = 1 + (dx * w + dy * h) / (w * w + h * h)
            width = w * scale
        state.current_size = constrained_size(width, height, ratio)
        self._update_resize_rubber()
        self.imageResizeChanged.emit(*state.current_size)

    def _resize_autoscroll(self):
        state = self._resize
        if not state.active or not state.dragging:
            self._resize_scroll_timer.stop()
            return
        for bar, value, extent in (
                (self.horizontalScrollBar(), state.pointer.x(), self.viewport().width()),
                (self.verticalScrollBar(), state.pointer.y(), self.viewport().height())):
            delta = -15 if value < 30 else 15 if value > extent - 30 else 0
            if delta:
                bar.setValue(bar.value() + delta)
        self._resize_from_pointer()

    def mouseReleaseEvent(self, e):
        if self._resize.active and self._resize.dragging and e.button() == Qt.MouseButton.LeftButton:
            self._resize.dragging = False
            self._resize_scroll_timer.stop()
            e.accept()
            return
        
        if (e.button() == Qt.MouseButton.LeftButton and not self._resize.active
                and bool(e.modifiers() & Qt.KeyboardModifier.ControlModifier) and not self.textCursor().hasSelection()):
            href = self.link_at(e.position().toPoint())
            if href:
                self.open_link(href)
                e.accept()
                return

        # Handle normal mouse release first
        super().mouseReleaseEvent(e)
        
        # Ensure cursor positioning is correct after mouse release
        if e.button() == Qt.MouseButton.LeftButton and not self._resize.active:
            # Small delay to allow Qt's mouse handling to complete
            QTimer.singleShot(5, self._ensure_cursor_visibility)

    def _end_inline_resize(self, commit):
        state = self._resize
        if not state.active:
            return
        self._resize_scroll_timer.stop()
        cursor, size = state.cursor, state.current_size
        valid = (cursor and cursor.hasSelection() and
                 cursor.selectionEnd() - cursor.selectionStart() == 1 and
                 cursor.selectedText() == '\ufffc' and
                 self.document().revision() == state.revision and
                 cursor.charFormat().isImageFormat() and
                 cursor.charFormat().toImageFormat().name() == state.path)
        self._resize = ResizeSession(locked=state.locked)
        for widget in (state.rubber, state.handle, state.preview):
            if widget:
                widget.hide()
                widget.deleteLater()
        if commit and valid and size:
            self.set_image_dimensions(*size, cursor=cursor)
        self.viewport().unsetCursor()
        self._sync_image_selection()

    def _ensure_cursor_visibility(self):
        """Ensure the text cursor is properly positioned and visible after user interaction."""
        if self._resize.active or getattr(self, '_restoring_note_position', False):
            return
        
        try:
            # Get current cursor and ensure it's properly positioned
            cursor = self.textCursor()
            
            # Force cursor to be visible at its current position
            self.ensureCursorVisible()
            
            # Update cursor display to prevent phantom cursor issues
            self.viewport().update()
            
            # Force a brief focus to ensure cursor blinks properly
            if not self.hasFocus():
                return
            
            # Validate cursor position is within document bounds
            doc_length = self.document().characterCount()
            if cursor.position() > doc_length - 1:
                cursor.setPosition(max(0, doc_length - 1))
                self.setTextCursor(cursor)
            
            # Force cursor blink restart for visual feedback
            self.setAttribute(Qt.WidgetAttribute.WA_InputMethodEnabled, True)
            
        except Exception as e:
            # Silently handle any cursor positioning errors
            print(f"Cursor positioning error: {e}")

    def keyPressEvent(self, e):
        # Apply default formatting if typing in empty editor
        if (e.text() and e.text().isprintable() and 
            (self.toPlainText().strip() == "" or self.document().isEmpty())):
            self._apply_default_formatting()
            
        if self.is_resizing_image:
            if e.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
                self.apply_image_resize(); e.accept(); return
            if e.key() == Qt.Key.Key_Escape:
                self.cancel_image_resize(); e.accept(); return
            if e.key() != Qt.Key.Key_Shift:
                self.cancel_image_resize()

        # Handle tab key events for better indentation
        if e.key() in (Qt.Key.Key_Tab, Qt.Key.Key_Backtab) and not (e.modifiers() & (Qt.KeyboardModifier.ControlModifier | Qt.KeyboardModifier.AltModifier)):
            if e.key() == Qt.Key.Key_Backtab or e.modifiers() & Qt.KeyboardModifier.ShiftModifier:
                # Shift+Tab: outdent
                self._handle_outdent()
            else:
                # Tab: indent
                self._handle_indent()
            e.accept()
            return
            
        plain_key = not (e.modifiers() & (Qt.KeyboardModifier.ControlModifier | Qt.KeyboardModifier.AltModifier | Qt.KeyboardModifier.MetaModifier))
        if plain_key and e.key() == Qt.Key.Key_Space:
            # "- ", "1. ", "# ", "[] " at the start of a line
            if self.try_autoformat_on_space():
                e.accept(); return
        if plain_key and e.key() in (Qt.Key.Key_Space, Qt.Key.Key_Return, Qt.Key.Key_Enter):
            self.autolink_before_cursor()
        if e.matches(QKeySequence.StandardKey.Bold):
            self.toggle_bold(); e.accept(); return
        if e.matches(QKeySequence.StandardKey.Italic):
            self.toggle_italic(); e.accept(); return
        if e.matches(QKeySequence.StandardKey.Underline):
            self.toggle_underline(); e.accept(); return
        if (
            e.key() == Qt.Key.Key_X and
            bool(e.modifiers() & Qt.KeyboardModifier.ControlModifier) and
            bool(e.modifiers() & Qt.KeyboardModifier.ShiftModifier)
        ):
            self.toggle_strikethrough(); e.accept(); return
        if (
            e.key() == Qt.Key.Key_H and
            bool(e.modifiers() & Qt.KeyboardModifier.ControlModifier) and
            bool(e.modifiers() & Qt.KeyboardModifier.ShiftModifier)
        ):
            self.toggle_highlight(); e.accept(); return
        
        # Handle normal key presses
        super().keyPressEvent(e)
        
        # Ensure cursor visibility after navigation keys
        if e.key() in (Qt.Key.Key_Home, Qt.Key.Key_End, Qt.Key.Key_Up, Qt.Key.Key_Down, 
                      Qt.Key.Key_Left, Qt.Key.Key_Right, Qt.Key.Key_PageUp, Qt.Key.Key_PageDown):
            QTimer.singleShot(0, self._ensure_cursor_visibility)

    def focusInEvent(self, e):
        """Handle focus events to ensure proper cursor behavior."""
        super().focusInEvent(e)
        if not self._resize.active:
            # Apply default formatting if focusing on empty editor
            if (self.toPlainText().strip() == "" or self.document().isEmpty()):
                self._apply_default_formatting()
            # Ensure cursor is visible when gaining focus
            QTimer.singleShot(10, self._ensure_cursor_visibility)

    def focusOutEvent(self, e):
        self._save_full_html()
        super().focusOutEvent(e)
        self.save_now()

    def dragEnterEvent(self, e):
        if self.canInsertFromMimeData(e.mimeData()):
            e.acceptProposedAction()
        else:
            super().dragEnterEvent(e)

    def dropEvent(self, e):
        if e.source() in (self, self.viewport()):
            # Qt handles moving the selected fragment and adjusts the destination offset.
            super().dropEvent(e)
            return
        if self.canInsertFromMimeData(e.mimeData()):
            self.setTextCursor(self.cursorForPosition(e.position().toPoint()))
            self.insertFromMimeData(e.mimeData())
            e.acceptProposedAction()
        else:
            super().dropEvent(e)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        if getattr(self, '_resize', None) and self._resize.active:
            QTimer.singleShot(0, self._update_resize_rubber)

    def scrollContentsBy(self, dx, dy):
        # Update resize overlay when scrolling
        if self._resize.active:
            QTimer.singleShot(10, self._update_resize_rubber)
        super().scrollContentsBy(dx, dy)

# -------------------------- Move Task Dialog --------------------------

class SidebarProjectDelegate(ExpandingLineEditDelegate):
    """Paints a project row in the sidebar: folder icon, elided title and the open/match count."""

    def __init__(self, parent=None, is_searching: Callable[[], bool] = lambda: False):
        super().__init__(parent)
        self._is_searching = is_searching

    def paint(self, painter, option, index):
        model = index.model()
        try:
            row = model.rows[index.row()]
        except (IndexError, AttributeError):
            return
        task_count, done_count = row.get('task_count', 0) or 0, row.get('done_count', 0) or 0
        match_counts = getattr(model, 'search_match_counts', None)
        searching = self._is_searching() and match_counts is not None
        view = option.widget
        if searching:
            matches = match_counts.get(row['id'], 0)
            count, dim, active = (str(matches) if matches else None), matches == 0, False
        else:
            open_count = task_count - done_count
            count, dim = (str(open_count) if open_count > 0 else None), False
            active = view is not None and view.currentIndex() == index
        # Overdue work adds an alert marker next to the count; work due today adds a dot
        overdue = not searching and bool(row.get('overdue_count'))
        due_today = not searching and not overdue and bool(row.get('due_today_count'))
        hover = bool(option.state & QStyle.StateFlag.State_MouseOver)
        pressed = hover and bool(QApplication.mouseButtons() & Qt.MouseButton.LeftButton)
        if view is not None and getattr(view, 'context_row', -1) == index.row():
            hover = True  # its context menu is open
        if view is not None and getattr(view, 'drop_row', -1) == index.row():
            active = True  # tasks dragged over this project
        starred = bool(row.get('starred'))
        paint_sidebar_row(
            painter, option.rect, icon_name="pin" if starred else "folder", text=row['title'], count=count,
            alert=overdue, dot=due_today, active=active,
            hover=hover, pressed=pressed, dim=dim, bold=model.is_project_in_review(row['id']),
            faded=bool(task_count and task_count == done_count),
            focused=view is not None and view.hasFocus() and view.currentIndex() == index
        )
        starred_count = model.starred_count()
        if 0 < starred_count == index.row() < model.hidden_start:
            # Hairline between the starred projects and the rest
            painter.save()
            painter.setPen(QColor(T.DIVIDER))
            painter.drawLine(option.rect.left() + 8, option.rect.top(), option.rect.right() - 8, option.rect.top())
            painter.restore()

    def sizeHint(self, option, index):
        return QSize(option.rect.width(), SIDEBAR_ROW_HEIGHT)

    def updateEditorGeometry(self, editor, option, index):
        editor.setGeometry(option.rect.adjusted(26, 1, -2, -1))


class CheckBoxDelegate(QStyledItemDelegate):
    """Custom delegate to center checkboxes in table cells."""
    def __init__(self, parent=None):
        super().__init__(parent)

    def paint(self, painter, option, index):
        # Only paint checkboxes for checkbox columns
        if index.model() and hasattr(index.model(), 'flags'):
            flags = index.model().flags(index)
            if flags & Qt.ItemFlag.ItemIsUserCheckable:
                # Get checkbox state
                checked = index.data(Qt.ItemDataRole.CheckStateRole) == Qt.CheckState.Checked
                
                # Calculate centered checkbox position
                checkbox_size = 18
                rect = option.rect
                checkbox_rect = QRect(
                    rect.x() + (rect.width() - checkbox_size) // 2,
                    rect.y() + (rect.height() - checkbox_size) // 2,
                    checkbox_size,
                    checkbox_size
                )
                
                # Draw background if selected
                if option.state & QStyle.StateFlag.State_Selected:
                    painter.fillRect(option.rect, option.palette.highlight())
                
                # Draw custom Microsoft To Do style checkbox
                painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
                
                # Define colors (matching the theme)
                accent_color = QColor(37, 100, 207)  # #5980a6
                accent_hover = QColor(30, 85, 177)   # #597ea3
                border_color = QColor(37, 100, 207)
                bg_color = QColor(255, 255, 255) if not checked else accent_color
                hover_bg = QColor(230, 241, 254)  # #eef6ff
                
                # Check if mouse is hovering (approximate)
                is_hover = option.state & QStyle.StateFlag.State_MouseOver
                
                # Draw checkbox background
                if checked:
                    painter.setBrush(accent_hover if is_hover else accent_color)
                    painter.setPen(QPen(accent_hover if is_hover else border_color, 2))
                else:
                    if is_hover:
                        painter.setBrush(hover_bg)
                    else:
                        painter.setBrush(bg_color)
                    painter.setPen(QPen(border_color, 2))
                
                # Draw rounded rectangle
                painter.drawRoundedRect(checkbox_rect, 4, 4)
                
                # Draw checkmark if checked
                if checked:
                    painter.setPen(QPen(QColor(255,  255, 255), 2, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap, Qt.PenJoinStyle.RoundJoin))
                    # Draw checkmark path
                    check_rect = checkbox_rect.adjusted(4, 4, -4, -4)
                    painter.drawLine(
                        check_rect.left() + 2, 
                        check_rect.center().y(),
                        check_rect.center().x() - 1, 
                        check_rect.bottom() - 3
                    )
                    painter.drawLine(
                        check_rect.center().x() - 1, 
                        check_rect.bottom() - 3,
                        check_rect.right() - 2, 
                        check_rect.top() + 1
                    )
                
                return
        
        # Fall back to default painting for non-checkbox cells
        super().paint(painter, option, index)

    def editorEvent(self, event, model, option, index):
        # Handle checkbox clicks
        if (model and hasattr(model, 'flags') and 
            (model.flags(index) & Qt.ItemFlag.ItemIsUserCheckable) and
            event.type() in (event.Type.MouseButtonRelease, event.Type.MouseButtonDblClick)):
            
            # Toggle the checkbox state
            current_state = index.data(Qt.ItemDataRole.CheckStateRole)
            new_state = Qt.CheckState.Unchecked if current_state == Qt.CheckState.Checked else Qt.CheckState.Checked
            return model.setData(index, new_state, Qt.ItemDataRole.CheckStateRole)
        
        return super().editorEvent(event, model, option, index)

# -------------------------- Template Dialog --------------------------

class TemplateEditDialog(QDialog):
    """Dialog used for creating and editing rich-text note templates."""

    def __init__(
        self,
        parent=None,
        *,
        title_text: str = "",
        content_html: str = "",
        is_default: bool = False,
        is_edit: bool = False
    ):
        super().__init__(parent)
        self.setWindowTitle("Edit Template" if is_edit else "Add Template")
        self.setModal(True)
        self.resize(640, 520)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 16, 16, 16)
        layout.setSpacing(10)

        title_label = QLabel("Title")
        title_label.setStyleSheet("font-weight: 600; color: #1d1f20;")
        layout.addWidget(title_label)

        self.title_edit = QLineEdit()
        self.title_edit.setPlaceholderText("Template title")
        self.title_edit.setText(title_text or "")
        layout.addWidget(self.title_edit)

        content_label = QLabel("Template content")
        content_label.setStyleSheet("font-weight: 600; color: #1d1f20; margin-top: 6px;")
        layout.addWidget(content_label)

        self.content_edit = SharedRichTextEditor(self)
        self.content_edit.setHtml(content_html or "")
        self.format_toolbar = self.content_edit.create_format_toolbar(self)
        layout.addWidget(self.format_toolbar)
        layout.addWidget(self.content_edit, 1)

        self.default_checkbox = QCheckBox("Set as default template for new tasks")
        self.default_checkbox.setChecked(bool(is_default))
        layout.addWidget(self.default_checkbox)

        button_box = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        button_box.accepted.connect(self._on_accept)
        button_box.rejected.connect(self.reject)
        layout.addWidget(button_box)

    def _on_accept(self):
        if not self.title_edit.text().strip():
            QMessageBox.warning(self, "Template title required", "Please enter a title for this template.")
            self.title_edit.setFocus()
            return
        self.accept()

    def get_values(self) -> Tuple[str, str, bool]:
        return self.title_edit.text().strip(), self.content_edit.toHtml(), self.default_checkbox.isChecked()


class BulkTaskImportDialog(QDialog):
    """Modal dialog for importing one plain-text task per line."""

    DRAFT_SETTINGS_KEY = "BulkTaskImport/DraftText"

    def __init__(self, parent, settings: QSettings):
        super().__init__(parent)
        self._settings = settings
        self._save_timer = QTimer(self)
        self._save_timer.setInterval(500)
        self._save_timer.setSingleShot(True)
        self._save_timer.timeout.connect(self._save_draft)

        self.setWindowTitle("Bulk Import Tasks")
        self.setModal(True)
        self.resize(620, 460)
        self.setMinimumWidth(520)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(18, 18, 18, 18)
        layout.setSpacing(10)

        heading = QLabel("Bulk import tasks")
        heading.setStyleSheet("font-size: 16px; font-weight: 600; color: #1d1f20;")
        layout.addWidget(heading)

        self.project_label = QLabel("")
        self.project_label.setWordWrap(True)
        layout.addWidget(self.project_label)

        self.text_edit = QPlainTextEdit()
        self.text_edit.setPlaceholderText(
            "Paste or type one task per line here."
        )
        self.text_edit.setTabChangesFocus(False)
        self.text_edit.setStyleSheet("""
            QPlainTextEdit {
                background: #f2f2f3;
                border: 1px solid #d6d6d7;
                border-radius: 0;
                padding: 10px 12px;
                color: #1d1f20;
                selection-background-color: #d6ebff;
            }
            QPlainTextEdit:focus {
                border: 1px solid #94bce3;
            }
        """)
        self.text_edit.textChanged.connect(self._on_text_changed)
        layout.addWidget(self.text_edit, 1)

        helper_block = QWidget()
        helper_layout = QVBoxLayout(helper_block)
        helper_layout.setContentsMargins(2, 0, 2, 0)
        helper_layout.setSpacing(4)

        self.instruction_label = QLabel("Each non-empty line will be imported as a separate task.")
        self.instruction_label.setWordWrap(True)
        self.instruction_label.setStyleSheet("color: #5d5d60; font-size: 11px;")
        helper_layout.addWidget(self.instruction_label)

        self.draft_status_label = QLabel(
            "Draft text autosaves while you type and stays available until you clear it or complete an import."
        )
        self.draft_status_label.setWordWrap(True)
        self.draft_status_label.setStyleSheet("color: #5d5d60; font-size: 11px;")
        helper_layout.addWidget(self.draft_status_label)

        layout.addWidget(helper_block)

        button_row = QHBoxLayout()
        button_row.setContentsMargins(0, 0, 0, 0)
        button_row.setSpacing(8)

        self.clear_button = QPushButton("Clear")
        self.clear_button.setStyleSheet("""
            QPushButton {
                background-color: #f2f2f3;
                border: 1px solid #d6d6d7;
                border-radius: 0;
                padding: 6px 14px;
                color: #1d2d3d;
            }
            QPushButton:hover {
                background-color: #e3e3e4;
            }
        """)
        self.clear_button.clicked.connect(self._clear_text_and_draft)
        button_row.addWidget(self.clear_button)

        button_row.addStretch()

        self.import_button = QPushButton("Import")
        self.import_button.setEnabled(False)
        self.import_button.setDefault(True)
        self.import_button.setStyleSheet("""
            QPushButton {
                background-color: #5980a6;
                border: 1px solid #5980a6;
                border-radius: 0;
                padding: 6px 16px;
                color: #f2f2f3;
                font-weight: 500;
            }
            QPushButton:hover {
                background-color: #597ea3;
            }
            QPushButton:disabled {
                background-color: #b7b7ba;
                border-color: #b7b7ba;
                color: #e3e3e4;
            }
        """)
        self.import_button.clicked.connect(self._import_tasks)
        button_row.addWidget(self.import_button)

        self.exit_button = QPushButton("Close")
        self.exit_button.setStyleSheet("""
            QPushButton {
                background-color: #f2f2f3;
                border: 1px solid #d6d6d7;
                border-radius: 0;
                padding: 6px 16px;
                color: #1d1f20;
            }
            QPushButton:hover {
                background-color: #e3e3e4;
            }
        """)
        self.exit_button.clicked.connect(self.reject)
        button_row.addWidget(self.exit_button)

        layout.addLayout(button_row)

        self._load_draft()
        self._update_project_label()
        self._update_import_button_state()
        self.text_edit.setFocus()

    def _main_window(self):
        return self.parent()

    def _saved_draft_text(self) -> str:
        try:
            return self._settings.value(self.DRAFT_SETTINGS_KEY, "") or ""
        except Exception:
            return ""

    def _current_project_details(self) -> Tuple[Optional[int], str]:
        main_window = self._main_window()
        if main_window and hasattr(main_window, '_selected_project_details'):
            return main_window._selected_project_details()
        return None, ""

    def _normalized_lines(self) -> List[str]:
        return [line.strip() for line in self.text_edit.toPlainText().splitlines() if line.strip()]

    def _update_project_label(self):
        _project_id, project_title = self._current_project_details()
        if project_title:
            self.project_label.setText(f"Import target: {project_title}")
            self.project_label.setStyleSheet("color: #1d1f20; font-size: 12px; font-weight: 600;")
        else:
            self.project_label.setText("Import target: No project selected")
            self.project_label.setStyleSheet("color: #1d2d3d; font-size: 12px; font-weight: 600;")

    def _update_import_button_state(self):
        self.import_button.setEnabled(bool(self._normalized_lines()))

    def _update_draft_status(self, restored: bool = False):
        if restored:
            self.draft_status_label.setText(
                "Saved draft restored. Draft text autosaves while you type and stays available until you clear it or complete an import."
            )
        else:
            self.draft_status_label.setText(
                "Draft text autosaves while you type and stays available until you clear it or complete an import."
            )

    def _load_draft(self):
        draft_text = self._saved_draft_text()
        if draft_text:
            self.text_edit.setPlainText(draft_text)
            self._update_draft_status(restored=True)
        else:
            self._update_draft_status(restored=False)

    def _save_draft(self):
        try:
            self._settings.setValue(self.DRAFT_SETTINGS_KEY, self.text_edit.toPlainText())
            self._settings.sync()
        except Exception:
            pass

    def _clear_saved_draft(self):
        try:
            self._settings.remove(self.DRAFT_SETTINGS_KEY)
            self._settings.sync()
        except Exception:
            pass

    def _on_text_changed(self):
        self._save_timer.start()
        self._update_import_button_state()

    def _clear_text_and_draft(self):
        has_content = bool(self.text_edit.toPlainText().strip())
        has_saved_draft = bool(self._saved_draft_text())
        if has_content or has_saved_draft:
            reply = QMessageBox.question(
                self,
                "Clear Bulk Import Text",
                "Clear the current text and delete the saved draft?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No
            )
            if reply != QMessageBox.StandardButton.Yes:
                return

        self._save_timer.stop()
        self.text_edit.blockSignals(True)
        self.text_edit.clear()
        self.text_edit.blockSignals(False)
        self._clear_saved_draft()
        self._update_draft_status(restored=False)
        self._update_import_button_state()

    def _import_tasks(self):
        self._update_project_label()
        lines = self._normalized_lines()
        if not lines:
            self._update_import_button_state()
            return

        main_window = self._main_window()
        if not main_window or not hasattr(main_window, '_perform_bulk_task_import'):
            QMessageBox.critical(self, "Bulk Import", "Bulk import is unavailable.")
            return

        if main_window._perform_bulk_task_import(lines):
            self._save_timer.stop()
            self._clear_saved_draft()
            self.accept()

    def reject(self):
        self._save_timer.stop()
        self._save_draft()
        super().reject()

    def closeEvent(self, e):
        self._save_timer.stop()
        self._save_draft()
        super().closeEvent(e)


# -------------------------- Preferences Dialog --------------------------

class PreferencesDialog(QDialog):
    """Dialog for application preferences with category sidebar."""
    
    def __init__(self, settings: QSettings, parent=None, db=None, main_window=None):
        super().__init__(parent)
        self.settings = settings
        self.db = db
        self.main_window = main_window
        self.setWindowTitle("Preferences")
        self.setModal(True)
        self.resize(720, 560)
        
        # Main layout
        main_layout = QHBoxLayout(self)
        main_layout.setSpacing(0)
        main_layout.setContentsMargins(0, 0, 0, 0)
        
        # Left sidebar for categories
        self.category_list = QListView()
        self.category_list.setFixedWidth(180)
        self.category_list.setSpacing(2)
        self.category_list.setStyleSheet("""
            QListView {
                background: #f2f2f3;
                border: none;
                border-right: 1px solid #d6d6d7;
                outline: none;
            }
            QListView::item {
                padding: 12px 16px;
                border-radius: 0;
                margin: 4px 8px;
                color: #1d1f20;
            }
            QListView::item:hover {
                background: #eef6ff;
            }
            QListView::item:selected {
                background: #5980a6;
                color: #f2f2f3;
                font-weight: 500;
            }
        """)
        
        # Category model
        from PyQt6.QtCore import QStringListModel
        categories = ["Task Sorting", "Projects", "Planning", "Templates"]
        
        # Load available addons dynamically
        self.addons = {}
        self.addon_widgets = {}
        self._load_addons()
        
        # Add addon categories
        for addon_name, addon in self.addons.items():
            if hasattr(addon, 'preferences_category_name'):
                categories.append(addon.preferences_category_name)
        
        self.category_model = QStringListModel(categories)
        self.category_list.setModel(self.category_model)
        self.category_list.selectionModel().currentChanged.connect(self.on_category_changed)
        
        # Right panel for settings with scroll area
        self.settings_scroll = QScrollArea()
        self.settings_scroll.setWidgetResizable(True)
        self.settings_scroll.setFrameShape(QScrollArea.Shape.NoFrame)
        self.settings_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.settings_scroll.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        
        self.settings_stack = QWidget()
        self.settings_layout = QVBoxLayout(self.settings_stack)
        self.settings_layout.setContentsMargins(24, 24, 24, 24)
        self.settings_layout.setSpacing(16)
        
        # Container for current settings page
        self.current_settings_widget = None
        
        # Set the settings stack as the scroll area's widget
        self.settings_scroll.setWidget(self.settings_stack)
        
        # Right column: scrollable page on top, buttons pinned below it
        right_column = QVBoxLayout()
        right_column.setContentsMargins(0, 0, 0, 0)
        right_column.setSpacing(0)
        right_column.addWidget(self.settings_scroll, 1)

        main_layout.addWidget(self.category_list)
        main_layout.addLayout(right_column, 1)
        
        # Button box at bottom
        button_layout = QHBoxLayout()
        button_layout.setContentsMargins(24, 12, 24, 20)
        button_layout.setSpacing(8)
        
        self.ok_button = QPushButton("OK")
        self.ok_button.setDefault(True)
        self.ok_button.clicked.connect(self.accept)
        
        self.cancel_button = QPushButton("Cancel")
        self.cancel_button.clicked.connect(self.reject)
        
        self.apply_button = QPushButton("Apply")
        self.apply_button.clicked.connect(self.apply_settings)
        
        button_layout.addStretch()
        button_layout.addWidget(self.ok_button)
        button_layout.addWidget(self.cancel_button)
        button_layout.addWidget(self.apply_button)
        
        self.settings_layout.addStretch()
        right_column.addLayout(button_layout)
        
        # Select first category
        self.category_list.setCurrentIndex(self.category_model.index(0, 0))
        
        # Apply theme styling
        self._apply_theme_styles()
    
    def _load_addons(self):
        """Dynamically load all available addons from the addons folder."""
        import importlib
        import pkgutil
        
        try:
            import addons
            addon_path = addons.__path__
            
            # Discover all modules in addons package
            for importer, modname, ispkg in pkgutil.iter_modules(addon_path):
                if modname.startswith('_'):
                    continue  # Skip private modules
                
                try:
                    # Import the addon module
                    module = importlib.import_module(f'addons.{modname}')
                    
                    # Look for addon instance (convention: module_name without _addon suffix)
                    addon_instance_name = modname.replace('_addon', '_addon')
                    
                    # Try common patterns for addon instance
                    for attr_name in dir(module):
                        obj = getattr(module, attr_name)
                        # Check if it's an addon instance (has required methods)
                        # Don't check functions/classes themselves, only instances
                        if (not attr_name.startswith('_') and 
                            not isinstance(obj, type) and
                            (hasattr(obj, 'create_preferences_widget') or 
                             hasattr(obj, 'register_menu_items'))):
                            self.addons[modname] = obj
                            break
                            
                except ImportError as e:
                    print(f"Could not load addon {modname}: {e}")
                    continue
                    
        except (ImportError, AttributeError):
            pass  # No addons folder or not importable
    
    def on_category_changed(self, current, previous):
        """Handle category selection change."""
        if not current.isValid():
            return
        
        # Save current settings before switching
        self._save_current_category_settings()
        
        category = self.category_model.data(current, Qt.ItemDataRole.DisplayRole)
        
        # Remove current settings widget if exists
        if self.current_settings_widget:
            self.settings_layout.removeWidget(self.current_settings_widget)
            self.current_settings_widget.hide()
            self.current_settings_widget.deleteLater()
            self.current_settings_widget = None
        
        # Create new settings widget based on category
        if category == "Task Sorting":
            self.current_settings_widget = self._create_items_sort_settings()
        elif category == "Projects":
            self.current_settings_widget = self._create_project_visibility_settings()
        elif category == "Planning":
            self.current_settings_widget = self._create_due_dates_settings()
        elif category == "Templates":
            self.current_settings_widget = self._create_templates_settings()
        else:
            # Check if it's an addon category
            for addon_name, addon in self.addons.items():
                if hasattr(addon, 'preferences_category_name') and addon.preferences_category_name == category:
                    self.current_settings_widget = addon.create_preferences_widget(self.settings, self)
                    self.addon_widgets[addon_name] = self.current_settings_widget
                    break
        
        # Insert at the beginning (before stretch and buttons)
        if self.current_settings_widget:
            self.settings_layout.insertWidget(0, self.current_settings_widget)
    
    def _save_current_category_settings(self):
        """Save settings from the current category before switching."""
        try:
            # Save Task Sorting preference if combo box exists and is valid
            if hasattr(self, 'sort_mode_combo') and self.sort_mode_combo is not None:
                try:
                    manual_sort = (self.sort_mode_combo.currentIndex() == 1)
                    self.settings.setValue('Preferences/DefaultManualSort', 'true' if manual_sort else 'false')
                except RuntimeError:
                    # Widget has been deleted, skip
                    pass
            
            self._save_visibility_pref()
            
            self._save_planning_settings()
            
            # Save addon settings
            for addon_name, addon_widget in self.addon_widgets.items():
                if addon_name in self.addons:
                    try:
                        self.addons[addon_name].save_preferences(addon_widget, self.settings)
                    except (RuntimeError, AttributeError):
                        pass
            
            self.settings.sync()
        except Exception as e:
            print(f"Error saving settings: {e}")

    def _save_planning_settings(self):
        """Persist snooze option preferences if widgets exist."""
        time_edit = getattr(self, 'recurrence_time_edit', None)
        if time_edit is not None:
            try:
                self.settings.setValue(RECURRENCE_DEFAULT_TIME_KEY, time_edit.time().toString("HH:mm"))
            except RuntimeError:
                pass  # widget already deleted
        # Each checkbox may belong to a page that is no longer shown, so save them independently
        for attr, key in (('feature_links_checkbox', 'Preferences/ProjectFeatureLinksEnabled'),
                          ('periodic_review_checkbox', 'Preferences/ReviewViewsFirst'),
                          ('periodic_review_auto_checkbox', 'Preferences/PeriodicReviewAutoMark')):
            checkbox = getattr(self, attr, None)
            if checkbox is None:
                continue
            try:
                self.settings.setValue(key, 'true' if checkbox.isChecked() else 'false')
            except RuntimeError:
                pass  # widget already deleted

        widgets = (
            'snooze_today_checkbox',
            'snooze_later_today_checkbox',
            'snooze_later_today_hours',
            'snooze_tomorrow_checkbox',
            'snooze_next_week_checkbox',
            'snooze_weekend_checkbox'
        )
        if not all(hasattr(self, name) for name in widgets):
            return
        try:
            self.settings.setValue('Preferences/SnoozeTodayEnabled', 'true' if self.snooze_today_checkbox.isChecked() else 'false')
            self.settings.setValue('Preferences/SnoozeLaterTodayEnabled', 'true' if self.snooze_later_today_checkbox.isChecked() else 'false')
            self.settings.setValue('Preferences/SnoozeLaterTodayHours', str(self.snooze_later_today_hours.value()))
            self.settings.setValue('Preferences/SnoozeTomorrowEnabled', 'true' if self.snooze_tomorrow_checkbox.isChecked() else 'false')
            self.settings.setValue('Preferences/SnoozeNextWeekEnabled', 'true' if self.snooze_next_week_checkbox.isChecked() else 'false')
            self.settings.setValue('Preferences/SnoozeWeekendEnabled', 'true' if self.snooze_weekend_checkbox.isChecked() else 'false')
        except RuntimeError:
            pass
    
    # Shared building blocks so every page looks the same
    _HINT_INDENT = 28

    def _page(self, title: str, description: str) -> Tuple[QWidget, QVBoxLayout]:
        widget = QWidget()
        layout = QVBoxLayout(widget)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(6)
        title_label = QLabel(title)
        title_label.setStyleSheet("font-size: 18px; font-weight: 600; color: #1d1f20;")
        layout.addWidget(title_label)
        desc_label = QLabel(description)
        desc_label.setStyleSheet("color: #5d5d60; margin-bottom: 10px;")
        desc_label.setWordWrap(True)
        layout.addWidget(desc_label)
        return widget, layout

    @staticmethod
    def _section(layout: QVBoxLayout, text: str):
        label = QLabel(text.upper())
        label.setStyleSheet(
            "color: #5980a6; font-size: 11px; font-weight: 600; letter-spacing: 1px;"
            " padding-top: 12px; padding-bottom: 4px; border-bottom: 1px solid #d6d6d7;"
        )
        layout.addWidget(label)
        layout.addSpacing(4)

    def _checkbox(self, layout: QVBoxLayout, text: str, hint: str = "") -> 'QCheckBox':
        from PyQt6.QtWidgets import QCheckBox
        checkbox = QCheckBox(text)
        checkbox.setStyleSheet("""
            QCheckBox { font-weight: 500; color: #1d1f20; spacing: 10px; }
            QCheckBox::indicator { width: 18px; height: 18px; }
        """)
        layout.addWidget(checkbox)
        if hint:
            self._hint(layout, hint)
        else:
            layout.addSpacing(6)
        return checkbox

    def _hint(self, layout: QVBoxLayout, text: str):
        label = QLabel(text)
        label.setStyleSheet(f"color: #5d5d60; font-size: 12px; margin-left: {self._HINT_INDENT}px; margin-bottom: 10px;")
        label.setWordWrap(True)
        layout.addWidget(label)

    def _create_items_sort_settings(self) -> QWidget:
        """Create settings widget for Task Sorting preferences."""
        widget, layout = self._page("Task Sorting", "How tasks are ordered when you create a new project.")
        
        # Sort mode selection
        sort_group = QWidget()
        sort_layout = QVBoxLayout(sort_group)
        sort_layout.setContentsMargins(0, 0, 0, 0)
        sort_layout.setSpacing(8)
        
        sort_label = QLabel("Default order")
        sort_label.setStyleSheet("font-weight: 500; color: #1d1f20;")
        sort_layout.addWidget(sort_label)
        
        # Dropdown (ComboBox) for sort mode
        self.sort_mode_combo = QComboBox()
        self.sort_mode_combo.addItem("Auto (grouped by status)", "auto")
        self.sort_mode_combo.addItem("Manual (drag to reorder)", "manual")
        self.sort_mode_combo.setItemData(0, "MoM tasks first, then Focus tasks, then A–Z", Qt.ItemDataRole.ToolTipRole)
        self.sort_mode_combo.setItemData(1, "Drag tasks into the order you want", Qt.ItemDataRole.ToolTipRole)
        
        sort_layout.addWidget(self.sort_mode_combo)
        
        # Apply to all projects button
        sort_layout.addSpacing(12)
        apply_all_hint = QLabel("Existing projects keep their own order. Use the button below to switch them all.")
        apply_all_hint.setStyleSheet("color: #5d5d60; font-size: 12px;")
        apply_all_hint.setWordWrap(True)
        sort_layout.addWidget(apply_all_hint)

        apply_all_btn = QPushButton("Use for all existing projects")
        apply_all_btn.setStyleSheet("""
            QPushButton {
                background: #f2f2f3;
                color: #1d1f20;
                border: 1px solid #d6d6d7;
                padding: 8px 16px;
                border-radius: 0;
                font-weight: 500;
            }
            QPushButton:hover {
                background: #d5d5d6;
                border-color: #5980a6;
            }
            QPushButton:pressed {
                background: #d6d6d7;
            }
        """)
        sort_layout.addWidget(apply_all_btn, 0, Qt.AlignmentFlag.AlignLeft)
        apply_all_btn.clicked.connect(self._apply_to_all_projects)
        
        layout.addWidget(sort_group)
        
        # Load current setting
        default_manual_sort = self.settings.value('Preferences/DefaultManualSort', 'false') in ('true', '1', 'True')
        if default_manual_sort:
            self.sort_mode_combo.setCurrentIndex(1)  # Drag to Reorder
        else:
            self.sort_mode_combo.setCurrentIndex(0)  # Smart Sort
        
        layout.addStretch()
        
        return widget
    
    def _apply_to_all_projects(self):
        """Apply the current sort mode to all projects."""
        from PyQt6.QtWidgets import QMessageBox
        
        # Get the current selection
        manual_sort = (self.sort_mode_combo.currentIndex() == 1)
        mode_name = "Manual" if manual_sort else "Auto"
        
        # Confirm with user
        reply = QMessageBox.question(
            self,
            "Change all projects",
            f"Switch every project to {mode_name} sorting?\n\n"
            f"Any manual order you set up will be lost.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No
        )
        
        if reply == QMessageBox.StandardButton.Yes:
            # Use the main window's db instance to ensure we're updating the same database
            db_to_use = self.main_window.db if (hasattr(self, 'main_window') and self.main_window) else self.db
            
            # Apply to all projects in database
            db_to_use.set_all_projects_sort_mode(manual_sort)
            
            # Save the setting as the new default in settings.ini
            self.settings.setValue('Preferences/DefaultManualSort', 'true' if manual_sort else 'false')
            self.settings.sync()
            
            # Update the main window if it exists
            if hasattr(self, 'main_window') and self.main_window:
                # Get the current project ID from the project view selection
                idx = self.main_window.project_view.currentIndex()
                current_project_id = None
                if idx.isValid():
                    current_project_id = self.main_window.project_model.project_id_at(idx.row())
                
                if current_project_id:
                    # Get the actual sort mode value from database after update
                    actual_manual_sort = db_to_use.is_manual_sort_enabled(current_project_id)
                    
                    # Directly update the combo box with signals blocked
                    if hasattr(self.main_window, 'sort_order_combo'):
                        combo = self.main_window.sort_order_combo
                        combo.blockSignals(True)
                        combo.setCurrentIndex(1 if actual_manual_sort else 0)
                        combo.blockSignals(False)
                    
                    # Directly update the menu action
                    if hasattr(self.main_window, 'act_manual_sort'):
                        self.main_window.act_manual_sort.blockSignals(True)
                        self.main_window.act_manual_sort.setChecked(actual_manual_sort)
                        self.main_window.act_manual_sort.blockSignals(False)
                    
                    # Update drag-and-drop behavior
                    self.main_window.task_view.set_manual_reorder(bool(actual_manual_sort))
                    
                    # Refresh the task list with the new sort mode
                    self.main_window.task_model.set_context(
                        current_project_id, 
                        self.main_window.include_done, 
                        self.main_window.pinned_only
                    )
                        
                    # Process pending events to ensure UI updates
                    QApplication.processEvents()
            
            QMessageBox.information(
                self,
                "Done",
                f"All projects now use {mode_name} sorting.",
                QMessageBox.StandardButton.Ok
            )
    
    def _create_project_visibility_settings(self) -> QWidget:
        """Create settings widget for Project Visibility preferences."""
        widget, layout = self._page("Projects", "What the project sidebar shows and what you can do from it.")

        self._section(layout, "Sidebar")
        self.auto_hide_checkbox = self._checkbox(
            layout, "Hide finished projects",
            "When every task in a project is done, the project moves to HIDDEN at the bottom of "
            "the sidebar. It comes back as soon as it gets an open task again.\n"
            "Always shown: projects with no tasks yet, the project you have open, and all projects "
            "while Completed Tasks is turned on."
        )
        auto_hide = str(self.settings.value('Preferences/AutoHideFinishedProjects', 'true')).lower() in ('true', '1')
        self.auto_hide_checkbox.setChecked(auto_hide)

        self._section(layout, "Links")
        self.feature_links_checkbox = self._checkbox(
            layout, "Feature links",
            "Adds 'Visit feature link' and 'Edit links' to the project right-click menu."
        )
        self.feature_links_checkbox.setChecked(_settings_bool(self.settings, 'Preferences/ProjectFeatureLinksEnabled', False))

        layout.addStretch()
        return widget

    def _save_visibility_pref(self):
        """Store the auto-hide checkbox and refresh the sidebar."""
        checkbox = getattr(self, 'auto_hide_checkbox', None)
        if checkbox is None:
            return
        try:
            self.settings.setValue('Preferences/AutoHideFinishedProjects', 'true' if checkbox.isChecked() else 'false')
            if getattr(self, 'main_window', None):
                self.main_window.project_model.reload()
        except RuntimeError:
            pass  # widget already deleted

    def _create_due_dates_settings(self) -> QWidget:
        """Create settings widget for Due Dates preferences."""
        widget, layout = self._page("Planning", "Weekly review and snooze shortcuts.")

        self._section(layout, "Review")
        self.periodic_review_checkbox = self._checkbox(
            layout, "Start with the Due && overdue, Waiting and No due date views",
            "Project → Start Review goes through these three views before your projects. "
            "Applies from your next review."
        )
        self.periodic_review_auto_checkbox = self._checkbox(
            layout, "Mark review steps as done automatically",
            "A project or view counts as done once you've looked at it for 3 seconds and then move on, "
            "end the review or close the app. Otherwise use Reviewed → next (Ctrl+Shift+R)."
        )

        # Snooze options group
        self._section(layout, "Repeat")
        repeat_row = QHBoxLayout()
        repeat_row.setContentsMargins(0, 0, 0, 0)
        repeat_row.setSpacing(10)
        repeat_label = QLabel("Default time for repeating tasks")
        repeat_label.setStyleSheet("font-weight: 500; color: #1d1f20;")
        self.recurrence_time_edit = QTimeEdit()
        self.recurrence_time_edit.setDisplayFormat("HH:mm")
        self.recurrence_time_edit.setFixedWidth(80)
        self.recurrence_time_edit.setTime(QTime.fromString(recurrence_default_time(self.settings), "HH:mm"))
        repeat_row.addWidget(repeat_label)
        repeat_row.addWidget(self.recurrence_time_edit)
        repeat_row.addStretch(1)
        layout.addLayout(repeat_row)
        repeat_hint = QLabel("New repeating tasks start at this time. Existing ones keep theirs.")
        repeat_hint.setStyleSheet("color: #5d5d60; font-size: 12px; margin-bottom: 10px;")
        repeat_hint.setWordWrap(True)
        layout.addWidget(repeat_hint)

        self._section(layout, "Snooze")
        snooze_layout = QVBoxLayout()
        snooze_layout.setContentsMargins(0, 0, 0, 0)
        snooze_layout.setSpacing(6)

        snooze_desc = QLabel("Quick choices under Due date in the task menu. Set Due Date… (Ctrl+D) picks any date.")
        snooze_desc.setStyleSheet("color: #5d5d60; font-size: 12px; margin-bottom: 4px;")
        snooze_desc.setWordWrap(True)
        snooze_layout.addWidget(snooze_desc)

        checkbox_style = """
            QCheckBox {
                font-weight: 500;
                color: #1d1f20;
                spacing: 10px;
            }
            QCheckBox::indicator {
                width: 18px;
                height: 18px;
            }
        """

        row_container = QWidget()
        row_layout = QVBoxLayout(row_container)
        row_layout.setContentsMargins(0, 0, 0, 0)
        row_layout.setSpacing(8)

        self.snooze_today_checkbox = QCheckBox("Today")
        self.snooze_today_checkbox.setStyleSheet(checkbox_style)
        row_layout.addWidget(self.snooze_today_checkbox)

        later_today_row = QWidget()
        later_today_layout = QHBoxLayout(later_today_row)
        later_today_layout.setContentsMargins(0, 0, 0, 0)
        later_today_layout.setSpacing(8)
        self.snooze_later_today_checkbox = QCheckBox("Later today")
        self.snooze_later_today_checkbox.setStyleSheet(checkbox_style)
        later_today_layout.addWidget(self.snooze_later_today_checkbox)
        hours_label = QLabel("in")
        hours_label.setStyleSheet("color: #5d5d60; font-size: 12px;")
        later_today_layout.addWidget(hours_label)
        self.snooze_later_today_hours = QSpinBox()
        self.snooze_later_today_hours.setRange(1, 12)
        self.snooze_later_today_hours.setSuffix(" h")
        self.snooze_later_today_hours.setFixedWidth(64)
        later_today_layout.addWidget(self.snooze_later_today_hours)
        later_today_layout.addStretch()
        row_layout.addWidget(later_today_row)

        self.snooze_tomorrow_checkbox = QCheckBox("Tomorrow")
        self.snooze_tomorrow_checkbox.setStyleSheet(checkbox_style)
        row_layout.addWidget(self.snooze_tomorrow_checkbox)

        self.snooze_next_week_checkbox = QCheckBox("Next week")
        self.snooze_next_week_checkbox.setStyleSheet(checkbox_style)
        row_layout.addWidget(self.snooze_next_week_checkbox)

        self.snooze_weekend_checkbox = QCheckBox("Weekend")
        self.snooze_weekend_checkbox.setStyleSheet(checkbox_style)
        row_layout.addWidget(self.snooze_weekend_checkbox)

        weekend_hint = QLabel("Shows as 'This weekend' on weekdays and 'Next weekend' on Saturday and Sunday.")
        weekend_hint.setStyleSheet(f"color: #5d5d60; font-size: 12px; margin-left: {self._HINT_INDENT}px;")
        weekend_hint.setWordWrap(True)
        row_layout.addWidget(weekend_hint)

        snooze_layout.addWidget(row_container)
        layout.addLayout(snooze_layout)
        
        self.periodic_review_checkbox.setChecked(_settings_bool(self.settings, 'Preferences/ReviewViewsFirst', False))
        self.periodic_review_auto_checkbox.setChecked(_settings_bool(self.settings, 'Preferences/PeriodicReviewAutoMark', False))
        
        snooze_prefs = load_snooze_preferences(self.settings)
        self.snooze_today_checkbox.setChecked(snooze_prefs['today_enabled'])
        self.snooze_later_today_checkbox.setChecked(snooze_prefs['later_today_enabled'])
        self.snooze_later_today_hours.setValue(snooze_prefs['later_today_hours'])
        self.snooze_tomorrow_checkbox.setChecked(snooze_prefs['tomorrow_enabled'])
        self.snooze_next_week_checkbox.setChecked(snooze_prefs['next_week_enabled'])
        self.snooze_weekend_checkbox.setChecked(snooze_prefs['weekend_enabled'])
        self.snooze_later_today_hours.setEnabled(snooze_prefs['later_today_enabled'])
        self.snooze_later_today_checkbox.toggled.connect(self.snooze_later_today_hours.setEnabled)
        
        layout.addStretch()
        
        return widget

    def _templates_db(self) -> Optional[DB]:
        """Return the active DB instance used for template persistence."""
        if hasattr(self, 'main_window') and self.main_window and hasattr(self.main_window, 'db'):
            return self.main_window.db
        return self.db

    def _create_templates_settings(self) -> QWidget:
        """Create settings widget for note template management."""
        widget, layout = self._page(
            "Templates",
            "Reusable notes you can drop in from the notes right-click menu. "
            "The default one fills new tasks automatically. Double-click to edit."
        )

        from PyQt6.QtCore import QStringListModel
        self.templates_data: List[Dict[str, Any]] = []
        self.templates_list_model = QStringListModel([])
        self.templates_list = QListView()
        self.templates_list.setModel(self.templates_list_model)
        self.templates_list.setSelectionMode(QListView.SelectionMode.SingleSelection)
        self.templates_list.setEditTriggers(QTableView.EditTrigger.NoEditTriggers)
        self.templates_list.setSpacing(2)
        self.templates_list.setMinimumHeight(180)
        self.templates_list.setStyleSheet("""
            QListView { border: 1px solid #d6d6d7; background: #ffffff; outline: none; }
            QListView::item { padding: 6px 8px; color: #1d1f20; }
            QListView::item:selected { background: #dce8f4; color: #1d1f20; }
        """)
        self.templates_list.doubleClicked.connect(lambda _index: self._edit_selected_template())
        layout.addWidget(self.templates_list, 1)

        button_row = QHBoxLayout()
        button_row.setContentsMargins(0, 8, 0, 0)
        button_row.setSpacing(8)

        self.template_add_button = QPushButton("New…")
        self.template_edit_button = QPushButton("Edit…")
        self.template_delete_button = QPushButton("Delete")
        self.template_add_button.clicked.connect(self._add_template)
        self.template_edit_button.clicked.connect(self._edit_selected_template)
        self.template_delete_button.clicked.connect(self._delete_selected_template)

        button_row.addWidget(self.template_add_button)
        button_row.addWidget(self.template_edit_button)
        button_row.addWidget(self.template_delete_button)
        button_row.addStretch()
        layout.addLayout(button_row)

        try:
            self.templates_list.selectionModel().currentChanged.connect(lambda _c, _p: self._update_template_actions())
        except Exception:
            pass

        db = self._templates_db()
        has_db = db is not None
        self.templates_list.setEnabled(has_db)
        self.template_add_button.setEnabled(has_db)
        self.template_edit_button.setEnabled(False)
        self.template_delete_button.setEnabled(False)
        if has_db:
            self._reload_templates()
        else:
            info_label = QLabel("Open a database to manage templates.")
            info_label.setWordWrap(True)
            info_label.setStyleSheet("color: #1d2d3d;")
            layout.addWidget(info_label)

        return widget

    def _reload_templates(self, selected_template_id: Optional[int] = None):
        """Reload templates from the database and refresh the list view."""
        if not hasattr(self, 'templates_list_model') or self.templates_list_model is None:
            return
        db = self._templates_db()
        if db is None:
            self.templates_data = []
            self.templates_list_model.setStringList([])
            self._update_template_actions()
            return

        if selected_template_id is None:
            selected = self._selected_template()
            selected_template_id = selected['id'] if selected else None

        try:
            rows = db.list_note_templates()
        except Exception as e:
            print(f"Error loading templates: {e}")
            rows = []

        self.templates_data = [
            {
                'id': row['id'],
                'title': row['title'] or '',
                'content_html': row['content_html'] or '',
                'is_default': bool(row['is_default']) if 'is_default' in row.keys() else False
            }
            for row in rows
        ]
        self.templates_list_model.setStringList([
            f"{item['title'] or 'Untitled template'}{'   ★ default' if item.get('is_default') else ''}"
            for item in self.templates_data
        ])

        if self.templates_data:
            target_row = 0
            if selected_template_id is not None:
                for idx, item in enumerate(self.templates_data):
                    if item['id'] == selected_template_id:
                        target_row = idx
                        break
            self.templates_list.setCurrentIndex(self.templates_list_model.index(target_row, 0))
        self._update_template_actions()

    def _selected_template(self) -> Optional[Dict[str, Any]]:
        """Return the currently selected template item."""
        if not hasattr(self, 'templates_list') or self.templates_list is None:
            return None
        index = self.templates_list.currentIndex()
        if not index.isValid():
            return None
        row = index.row()
        if row < 0 or row >= len(getattr(self, 'templates_data', [])):
            return None
        return self.templates_data[row]

    def _update_template_actions(self):
        has_selection = self._selected_template() is not None
        if hasattr(self, 'template_edit_button') and self.template_edit_button is not None:
            self.template_edit_button.setEnabled(has_selection)
        if hasattr(self, 'template_delete_button') and self.template_delete_button is not None:
            self.template_delete_button.setEnabled(has_selection)

    def _open_template_dialog(self, template: Optional[Dict[str, Any]] = None):
        db = self._templates_db()
        if db is None:
            QMessageBox.warning(self, "Templates unavailable", "No database is currently open.")
            return

        dialog = TemplateEditDialog(
            self,
            title_text=template['title'] if template else "",
            content_html=template['content_html'] if template else "",
            is_default=bool(template['is_default']) if template else False,
            is_edit=template is not None
        )
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return

        title, content_html, is_default = dialog.get_values()
        try:
            if template is None:
                new_id = db.add_note_template(title, content_html, is_default=is_default)
                self._reload_templates(selected_template_id=new_id)
            else:
                db.update_note_template(template['id'], title, content_html, is_default=is_default)
                self._reload_templates(selected_template_id=template['id'])
        except ValueError as ve:
            QMessageBox.warning(self, "Invalid template", str(ve))
        except sqlite3.IntegrityError:
            QMessageBox.warning(
                self,
                "Template title already exists",
                "There's already a template with this title. Pick another one."
            )
        except Exception as e:
            QMessageBox.warning(self, "Template error", f"Could not save template.\n\n{e}")

    def _add_template(self):
        self._open_template_dialog(None)

    def _edit_selected_template(self):
        template = self._selected_template()
        if template is None:
            return
        self._open_template_dialog(template)

    def _delete_selected_template(self):
        template = self._selected_template()
        if template is None:
            return

        reply = QMessageBox.question(
            self,
            "Delete template",
            f"Delete '{template['title']}'? This can't be undone.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No
        )
        if reply != QMessageBox.StandardButton.Yes:
            return

        db = self._templates_db()
        if db is None:
            QMessageBox.warning(self, "Templates unavailable", "No database is currently open.")
            return

        try:
            db.delete_note_template(template['id'])
            self._reload_templates()
        except Exception as e:
            QMessageBox.warning(self, "Template error", f"Could not delete template.\n\n{e}")
    
    def apply_settings(self):
        """Apply current settings without closing dialog."""
        try:
            # Save Task Sorting preference
            if hasattr(self, 'sort_mode_combo') and self.sort_mode_combo is not None:
                try:
                    # Index 0 = Smart Sort (auto), Index 1 = Drag to Reorder (manual)
                    manual_sort = (self.sort_mode_combo.currentIndex() == 1)
                    self.settings.setValue('Preferences/DefaultManualSort', 'true' if manual_sort else 'false')
                except RuntimeError:
                    # Widget has been deleted, skip
                    pass
            
            self._save_visibility_pref()
            
            self._save_planning_settings()
            
            # Save addon settings
            for addon_name, addon_widget in self.addon_widgets.items():
                if addon_name in self.addons:
                    try:
                        self.addons[addon_name].save_preferences(addon_widget, self.settings)
                    except (RuntimeError, AttributeError):
                        pass
            
            self.settings.sync()
        except Exception as e:
            print(f"Error applying settings: {e}")

    
    def accept(self):
        """Save settings and close dialog."""
        self.apply_settings()
        super().accept()
    
    def _apply_theme_styles(self):
        """The dialog uses the application theme (ui/theme.qss)."""
        self.setStyleSheet("")

class MainWindow(QMainWindow):
    def __init__(self, db: DB):
        super().__init__()
        self.db = db
        self.setWindowTitle("Project Notes")
        
        # Set application icon
        self._set_application_icon()
        
        # Set initial window title with database name
        db_name = os.path.basename(db.path)
        self.setWindowTitle(f"Project Notes - {db_name}")
        
        # Migrate legacy folders if needed (before setting up directories)
        migrate_legacy_folders(db.path)
        
        # Set database-specific directories
        self.attach_dir = get_attach_dir(db.path)
        self.backup_dir = get_backup_dir(db.path)
        self.db.backfill_note_attachments(self.attach_dir)

        self.resize(1200, 700)
        
        # Load available addons dynamically
        self.addons = {}
        self._load_addons()

        splitter = QSplitter(Qt.Orientation.Horizontal)
        left_panel = QWidget(); mid_panel = QWidget(); right_panel = QWidget()
        splitter.addWidget(left_panel); splitter.addWidget(mid_panel); splitter.addWidget(right_panel)
        splitter.setSizes([232, 420, 548])
        splitter.setHandleWidth(1)
        splitter.setChildrenCollapsible(False)
        self.splitter = splitter  # store reference
        self.top_bar = TopBar()
        central = QWidget()
        central_layout = QVBoxLayout(central)
        central_layout.setContentsMargins(0, 0, 0, 0)
        central_layout.setSpacing(0)
        central_layout.addWidget(self.top_bar)
        central_layout.addWidget(splitter, 1)
        self.setCentralWidget(central)
        # Settings for splitter persistence
        self._settings = QSettings(SETTINGS_PATH, QSettings.Format.IniFormat)
        self._feature_links_settings_key = self._build_feature_link_settings_key(db.path)
        self._project_feature_links = self._load_project_feature_links()
        self._splitter_save_timer = QTimer(self); self._splitter_save_timer.setSingleShot(True); self._splitter_save_timer.setInterval(300)
        splitter.splitterMoved.connect(lambda *_: self._splitter_save_timer.start())
        self._splitter_save_timer.timeout.connect(self._save_splitter_sizes)
        QTimer.singleShot(0, self._restore_splitter_sizes)

        # Window size persistence
        self._window_save_timer = QTimer(self); self._window_save_timer.setSingleShot(True); self._window_save_timer.setInterval(400)
        self._window_save_timer.timeout.connect(self._save_window_state)
        QTimer.singleShot(0, self._restore_window_state)
        
        # Enhanced tracking for multi-monitor restore (Windows 11 fix)
        self._geometry_before_minimize = None
        self._screen_before_minimize = None
        self._was_minimized = False
        self._restore_pending = False

        # Restore always-on-top preference
        always_on_top = self._settings.value('MainWindow/AlwaysOnTop', 'false') in ('true','1','True')
        self._apply_always_on_top(always_on_top)

        # "Pinned only" is now the Focus smart view; the flag stays for addons and is always off
        pinned_only_saved = False
        self.pinned_only = False
        self.current_view: Tuple[str, Any] = ('smart', 'today')
        self._last_project_id: Optional[int] = None

        # Models/Views
        self.project_model = ProjectListModel(self.db, self._settings)
        self.project_model.search_keeps_all_projects = True
        self.project_model.include_hidden_rows = self._settings.value('MainWindow/HiddenExpanded', 'false') in ('true', '1', 'True')
        self.project_model.reload()
        self.project_view = FlatListView("visible"); self.project_view.setModel(self.project_model)
        self.project_view.setUniformItemSizes(True)  # uniform for performance
        self.project_view.setEditTriggers(QListView.EditTrigger.DoubleClicked | QListView.EditTrigger.EditKeyPressed)
        self.project_view.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.project_view.customContextMenuRequested.connect(self.show_project_context_menu)

        # Hidden projects share the model and the selection with the main list
        self.hidden_project_view = FlatListView("hidden"); self.hidden_project_view.setModel(self.project_model)
        self.hidden_project_view.setSelectionModel(self.project_view.selectionModel())
        self.hidden_project_view.setUniformItemSizes(True)
        self.hidden_project_view.setEditTriggers(QListView.EditTrigger.DoubleClicked | QListView.EditTrigger.EditKeyPressed)
        self.hidden_project_view.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.hidden_project_view.customContextMenuRequested.connect(self._on_hidden_project_context_menu)

        # Project name filter (toggled from the PROJECTS header)
        self.project_filter_edit = FilterEdit()
        self._project_filter_timer = QTimer(self)
        self._project_filter_timer.setSingleShot(True)
        self._project_filter_timer.setInterval(120)
        self._project_filter_timer.timeout.connect(self._apply_project_filter)
        self.project_filter_edit.textChanged.connect(self.on_project_filter_changed)

        self.sidebar = Sidebar(self.project_view, self.hidden_project_view, self.project_filter_edit)
        self.sidebar.set_hidden_expanded(self.project_model.include_hidden_rows)
        self.sidebar.smartViewRequested.connect(lambda key: self.set_current_view(('smart', key)))
        self.sidebar.newProjectSubmitted.connect(self._create_project_from_sidebar)
        self.sidebar.hiddenToggled.connect(self._on_hidden_section_toggled)
        self.sidebar.filterAccepted.connect(self._open_first_filtered_project)
        self.project_view.tasksDropped.connect(self._move_dropped_tasks)
        self.sidebar.taskListRequested.connect(self._focus_task_list)
        self.sidebar.reviewNextRequested.connect(self.open_next_review_project)
        self.sidebar.reviewEndRequested.connect(self.end_review)
        self.project_model.reviewChanged.connect(self._on_review_changed)
        self.hidden_project_view.tasksDropped.connect(self._move_dropped_tasks)
        self.project_model.modelReset.connect(self._on_project_model_reset)
        self.project_model.renameRejected.connect(
            lambda title: self.toast.show_message(f"A project named '{title}' already exists"))

        self.projects_header = self.sidebar.projects_header
        self.projects_label = self.sidebar.projects_label
        self.projects_header.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.projects_header.customContextMenuRequested.connect(self.show_projects_header_context_menu)

        self.task_model = TaskTableModel(self.db)
        self.task_model.settings = self._settings  # Pass settings reference for preferences
        self.task_model.always_include_done = True
        # Collapsed groups are remembered per view; Completed starts collapsed
        self._collapsed_groups: Dict[Tuple[str, Any], set] = {}
        self.task_view = TaskListView()
        self.task_row_delegate = TaskRowDelegate(self.task_view)
        self.task_view.setItemDelegate(self.task_row_delegate)
        self.task_view.setItemDelegateForColumn(TaskTableModel.COL_TITLE, self.task_row_delegate)
        self.task_view.setModel(self.task_model)
        self.task_view.selectionLost.connect(self._on_task_selection_lost)

        # Set up context menu for task view
        self.task_view.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.task_view.customContextMenuRequested.connect(self.show_task_context_menu)

        self.detail_state = TaskDetailState()
        self.notes = NotesEditor(self.db, self.attach_dir)
        self.detail = TaskDetailPanel(self.notes)
        for action, icon_name in (
            (self.notes.action_bold, 'bold'), (self.notes.action_italic, 'italic'),
            (self.notes.action_bulleted_list, 'ul'), (self.notes.action_checklist, 'checklist')):
            self.detail.add_toolbar_action(action, icon_name)
        styles = QMenu(self.detail)
        paragraph = styles.addAction('Paragraph')
        paragraph.triggered.connect(lambda: self.notes.toggle_heading() if self.notes._block_is_heading() else None)
        styles.addAction(self.notes.action_heading)
        styles.addSeparator()
        for action in (self.notes.action_underline, self.notes.action_strikethrough,
                       self.notes.action_numbered_list, self.notes.action_highlight):
            styles.addAction(action)
        self.detail.add_toolbar_menu('Text style', styles)
        self.detail.add_toolbar_separator()
        self.detail.add_template_button(self.notes.populate_template_menu)
        self.detail.add_find_button()
        self.notes.noteSaved.connect(self._on_note_saved)
        self._note_meta_timer = QTimer(self)
        self._note_meta_timer.setInterval(60000)
        self._note_meta_timer.timeout.connect(self._update_note_meta)
        self._note_meta_timer.start()
        self.notes_format_toolbar = self.detail.notes_toolbar
        self.notes_header = self.detail
        self.notes_maximize_btn = self.detail.focus_button

        self.detail.completeClicked.connect(self.mark_task_complete)
        self.detail.pinToggled.connect(lambda _on: self._toggle_current_task_flag('pinned'))
        self.detail.waitingToggled.connect(lambda _on: self._toggle_current_task_flag('waiting'))
        self.detail.momToggled.connect(lambda _on: self.toggle_mark_task_as_mom())
        self.detail.focusClicked.connect(self.toggle_notes_maximized)
        self.detail.titleCommitted.connect(self._on_detail_title_committed)
        self.detail.projectClicked.connect(self._open_detail_project)
        self.detail.dueDateChanged.connect(self._on_detail_due_changed)
        self.detail.repeatChosen.connect(self._apply_recurrence_preset)
        self.detail.stakeholdersChanged.connect(self._on_detail_stakeholders_changed)
        self.detail.more_menu.aboutToShow.connect(self._populate_task_more_menu)

        # Focus mode state (was "notes maximization")
        self._notes_maximized = False
        self._stored_splitter_sizes = None
        self._stored_left_panel_visible = True
        self._stored_mid_panel_visible = True

        # Initially disable right pane until a task is selected
        self._update_right_pane_state(False)

        # List header: title, subtitle, Auto | Manual and the project menu
        self.list_header = TaskListHeader()
        self.tasks_header = self.list_header
        self.tasks_label = self.list_header.title_label
        self.sort_order_combo = self.list_header.sort_control
        self.sort_order_combo.currentIndexChanged.connect(self.on_sort_order_changed)
        self.list_header.project_menu.aboutToShow.connect(self._populate_project_menu)

        self.feature_link_button = QPushButton("Link to feature")
        self.feature_link_button.setFlat(True)
        self.feature_link_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.feature_link_button.setStyleSheet(
            "QPushButton{color:#416180;border:none;background:transparent;font-family:'Barlow';font-weight:500;"
            "font-size:13px;padding:0;margin:0;text-decoration:underline}"
            "QPushButton:hover{color:#2c455d;background:transparent}"
        )
        self.feature_link_button.clicked.connect(lambda: self._visit_project_feature_link(self._active_feature_link_pid) if self._active_feature_link_pid is not None else None)
        self.feature_link_button.setVisible(False)
        self._active_feature_link_pid: Optional[int] = None
        self.list_header.add_subtitle_widget(self.feature_link_button)

        # Quick-add row, always visible above the list (hidden while searching)
        self.quick_add = QuickAddRow()
        self.quick_add.submitted.connect(self._quick_add_task)
        self.quick_add.targetRequested.connect(self._choose_quick_add_target)
        self.quick_add.targetResetRequested.connect(self._reset_quick_add_target)
        # Project picked for quick-add, shared by all smart views; kept for the session (not saved)
        self._quick_add_pick: Optional[int] = None
        self._recent_move_targets: List[int] = []  # last projects tasks were moved to (session only)
        self.quick_add.escaped.connect(lambda: self.task_view.setFocus())
        self.task_model.modelReset.connect(self._update_list_header)
        self.task_model.modelReset.connect(self._sync_detail_header)
        self.task_model.dataChanged.connect(lambda *_: self._sync_detail_header())
        self.task_model.dataChanged.connect(lambda *_: self._update_list_header())

        # Layouts
        l = QVBoxLayout(left_panel); l.setContentsMargins(0, 0, 0, 0); l.addWidget(self.sidebar)
        m = QVBoxLayout(mid_panel); m.setContentsMargins(0, 0, 0, 0); m.setSpacing(0)
        self.review_panel = ReviewPanel()
        self.review_panel.setVisible(False)
        self.review_panel.nextRequested.connect(self.open_next_review_project)
        self.review_panel.skipRequested.connect(lambda: self.skip_review_project())
        self.review_panel.hideRequested.connect(self._review_hide_project)
        self.review_panel.clearFocusRequested.connect(lambda: self._clear_project_flag('pinned'))
        self.review_panel.clearDueRequested.connect(lambda: self._clear_project_flag('due_date'))
        self.review_panel.chipClicked.connect(self._select_review_tasks)
        self.task_model.modelReset.connect(lambda: self._sync_review_panel())
        m.addWidget(self.list_header); m.addWidget(self.review_panel); m.addWidget(self.quick_add); m.addWidget(self.task_view, 1)
        mid_panel.setMinimumWidth(300)
        r = QVBoxLayout(right_panel); r.setContentsMargins(0, 0, 0, 0); r.addWidget(self.detail)

        # Connections
        self.project_view.selectionModel().selectionChanged.connect(self.on_project_selected)
        self.task_view.selectionModel().selectionChanged.connect(self.on_task_selected)
        # Due date save timer (triggered from popup dialog)

        # UI: menu + toolbar + search
        self._create_actions()
        self._create_menu()
        self._reclaim_project_list_context_menu()
        self._create_toolbar()

        # State
        self.include_done = False
        self.pinned_only = False
        self.show_no_due_dates = False
        self.search_terms: List[str] = []
        self.search_stakeholder_terms: List[str] = []
        self.search_query: Optional[search_query.Query] = None
        self._search_results_by_project = {}
        self._applying_search = False
        self._suppress_tasks_refresh = False
        
        # Focus preservation state
        self._preserve_project_id: Optional[int] = None
        self._preserve_task_id: Optional[int] = None
        # One-shot flag to avoid restoring focus after intentional filter clearing
        self._suppress_search_restore = False

        # Create status bar early to ensure it's available for status updates
        self._create_status_bar()
        
        # Init selection: restore the last view, falling back to the first project
        saved_view = self._load_saved_view()
        if saved_view is not None:
            self.current_view = saved_view
        elif self.project_model.rowCount() > 0:
            self.current_view = ('project', self.project_model.project_id_at(0))
        self._apply_current_view()
        self._on_review_changed()  # a review saved in the database shows its strip and panel again

        self.task_model.tasksChanged.connect(self.on_tasks_changed)
        self.task_model.taskCompletionStamped.connect(self.on_task_completion_stamped)
        self.task_model.doneToggled.connect(self.on_task_done_toggled)
        self.task_model.taskCompleted.connect(self._on_task_completed_in_list)
        self.task_model.taskAboutToComplete.connect(self._on_task_about_to_complete)
        self.task_model.before_complete = lambda tid: self._save_open_note_for([tid])

        # Undo: row snapshots on a QUndoStack, surfaced through the toast
        self.toast = Toast(self)
        self.undo_stack = QUndoStack(self)
        self.undo_stack.setUndoLimit(20)
        self.undo_stack.canUndoChanged.connect(self.act_undo_last.setEnabled)
        self._pending_complete_snapshots: Dict[int, Dict[str, Any]] = {}
        self._undo_attachment_dirs: List[str] = []
        self._handling_tasks_changed = False  # reentrancy guard

        # Inject search hooks into task model
        self.task_model.is_search_active = self._is_searching
        self.task_model.refresh_search = self.apply_search_filter

        self._first_show = True  # for centering on initial display
        # Apply custom delegates for better inline editor sizing and centered checkboxes
        self.project_view.setItemDelegate(SidebarProjectDelegate(self.project_view, self._is_searching))
        self.hidden_project_view.setItemDelegate(SidebarProjectDelegate(self.hidden_project_view, self._is_searching))
        # Initialize defer reload flag
        self.task_model._defer_reload_flag = False
        
        # Task addition state to handle rapid additions
        self._adding_task = False
        
        # Apply theme
        self._apply_theme_styles()

        # Recurrence processing timer
        self._recurrence_timer = QTimer(self)
        self._recurrence_timer.setInterval(60000)
        self._recurrence_timer.timeout.connect(self._process_due_recurrences)
        self._recurrence_timer.start()
        QTimer.singleShot(500, self._process_due_recurrences)
        
        # Set focus to search field on startup
        QTimer.singleShot(0, lambda: self.search_edit.setFocus())

    def _build_feature_link_settings_key(self, db_path: str) -> str:
        """Build a settings key scoped to the current database path."""
        try:
            digest = hashlib.sha1(os.path.abspath(db_path).encode('utf-8')).hexdigest()[:10]
            return f"ProjectFeatureLinks/{digest}"
        except Exception:
            return "ProjectFeatureLinks/default"

    def _load_project_feature_links(self) -> Dict[int, str]:
        """Load per-project feature links from settings (scoped by database)."""
        mapping: Dict[int, str] = {}
        try:
            raw_value = self._settings.value(self._feature_links_settings_key, "{}")
            raw_dict = raw_value
            if isinstance(raw_value, str):
                try:
                    raw_dict = json.loads(raw_value)
                except Exception:
                    raw_dict = {}
            if isinstance(raw_dict, dict):
                for key, value in raw_dict.items():
                    try:
                        pid = int(key)
                        if value:
                            mapping[pid] = str(value)
                    except (ValueError, TypeError):
                        continue
        except Exception as e:
            print(f"Failed to load project feature links: {e}")
        return mapping

    def _save_project_feature_links(self):
        """Persist current feature links to settings."""
        try:
            serializable = {str(pid): url for pid, url in self._project_feature_links.items()}
            self._settings.setValue(self._feature_links_settings_key, json.dumps(serializable))
            self._settings.sync()
        except Exception as e:
            print(f"Failed to save project feature links: {e}")

    def _set_project_feature_link(self, project_id: int, url: Optional[str]):
        """Store or clear the feature link for a project."""
        if url:
            self._project_feature_links[project_id] = url
        else:
            self._project_feature_links.pop(project_id, None)
        self._save_project_feature_links()

    def _feature_links_enabled(self) -> bool:
        """Return whether project feature links are enabled in preferences."""
        return _settings_bool(self._settings, 'Preferences/ProjectFeatureLinksEnabled', False)

    def _project_review_auto_mark_enabled(self) -> bool:
        """Preferences → Planning: mark a project reviewed when you move on from it."""
        return _settings_bool(self._settings, 'Preferences/PeriodicReviewAutoMark', False)

    def _update_feature_link_for_project(self, project_id: Optional[int]):
        """Update the feature link button visibility based on current selection and preference."""
        link = None
        if project_id is not None and self._feature_links_enabled():
            link = self._project_feature_links.get(project_id)
            if link and not self._is_valid_feature_link(link):
                link = None

        self._active_feature_link_pid = project_id if link else None
        self.feature_link_button.setVisible(bool(link))

    def _load_addons(self):
        """Dynamically load all available addons from the addons folder."""
        import importlib
        import pkgutil
        
        try:
            import addons
            addon_path = addons.__path__
            
            # Discover all modules in addons package
            for importer, modname, ispkg in pkgutil.iter_modules(addon_path):
                if modname.startswith('_'):
                    continue  # Skip private modules
                
                try:
                    # Import the addon module
                    module = importlib.import_module(f'addons.{modname}')
                    
                    # Look for addon instance
                    for attr_name in dir(module):
                        obj = getattr(module, attr_name)
                        # Check if it's an addon instance (not a class/function)
                        if (not attr_name.startswith('_') and 
                            not isinstance(obj, type) and
                            (hasattr(obj, 'register_menu_items') or 
                             hasattr(obj, 'create_preferences_widget'))):
                            self.addons[modname] = obj
                            break
                            
                except ImportError as e:
                    print(f"Could not load addon {modname}: {e}")
                    continue
                    
        except (ImportError, AttributeError):
            pass  # No addons folder or not importable

    def _apply_theme_styles(self):
        """Apply the Project Notes theme (ui/theme.qss) to the application."""
        ui_theme.apply(QApplication.instance())

        # Configure view-specific styling
        self.project_view.setSpacing(0)

    def apply_theme(self):
        self._apply_theme_styles()

    def _set_application_icon(self):
        """Set the application icon from file or create it programmatically"""
        try:
            # Try to load icon from file first
            icon_path = os.path.join(APP_DIR, 'app_icon.png')
            if os.path.exists(icon_path):
                icon = QIcon(icon_path)
                self.setWindowIcon(icon)
                QApplication.setWindowIcon(icon)  # Set for all windows
                return
        except Exception:
            pass
        
        # If icon file doesn't exist, draw the brand mark
        try:
            icon = brand_icon()
            self.setWindowIcon(icon)
            QApplication.setWindowIcon(icon)
        except Exception as e:
            print(f"Could not set application icon: {e}")

    def on_task_done_toggled(self, project_id: int, became_done: bool):
        # Incrementally update project stats; the open project stays listed (keep_visible_id)
        self.project_model.apply_task_done_toggle(project_id, became_done)
        if self.current_view[0] == 'project' and not self._is_searching():
            self._restore_project_highlight()
        self._update_comprehensive_status()

    def _process_due_recurrences(self):
        if getattr(self, '_processing_recurrences', False):
            return
        self._processing_recurrences = True
        try:
            now_dt = datetime.datetime.now()
            now_str = _format_recurrence_datetime(now_dt)
            due_tasks = self.db.list_due_recurring_tasks(now_str)
            if not due_tasks:
                return

            affected_projects: set[int] = set()
            for row in due_tasks:
                rule = _parse_recurrence_rule(row['recurrence_rule'])
                if not rule:
                    continue
                next_dt = _parse_recurrence_datetime(row['recurrence_next_at'])
                if not next_dt:
                    continue
                due_date_raw = row['due_date']
                if isinstance(due_date_raw, str):
                    due_date_text = due_date_raw.strip()
                else:
                    due_date_text = None
                is_done = bool(row['done'])
                if is_done and due_date_text:
                    adjusted_due_date = now_str
                elif not due_date_text:
                    adjusted_due_date = now_str
                else:
                    parsed_due = _parse_recurrence_datetime(due_date_text)
                    if parsed_due and parsed_due > now_dt:
                        adjusted_due_date = now_str
                    else:
                        adjusted_due_date = due_date_text
                mode = rule.get("mode", RECURRENCE_MODE_FIXED)
                start_dt = _parse_recurrence_datetime(row['recurrence_start_at']) or next_dt

                if mode == RECURRENCE_MODE_COMPLETION:
                    new_next_at = None
                else:
                    if not start_dt:
                        continue
                    next_occurrence = _compute_next_recurrence(rule, start_dt, now_dt)
                    new_next_at = _format_recurrence_datetime(next_occurrence) if next_occurrence else None

                self.db.apply_recurrence_occurrence(
                    row['id'],
                    next_at=new_next_at,
                    last_at=_format_recurrence_datetime(next_dt),
                    due_date=adjusted_due_date,
                )
                affected_projects.add(row['project_id'])
                self._reorder_recurrence_pinned_task(row['project_id'], row['id'])

            if affected_projects:
                self.project_model.refresh_visibility()
                if self._is_searching():
                    self.apply_search_filter()
                else:
                    # A smart view can show the task too, not only its own project
                    self.task_model.reload()
                self._update_comprehensive_status()

                current_idx = self.task_view.currentIndex()
                current_tid = self.task_model.task_id_at(current_idx.row()) if current_idx.isValid() else None
                if current_tid is not None:
                    self.detail_state.recurrence = self.db.get_task_recurrence(current_tid)
                    self._sync_detail_recurrence()
        except Exception as e:
            print(f"Warning: Could not process recurrences: {e}")
        finally:
            self._processing_recurrences = False

    def _reorder_recurrence_pinned_task(self, project_id: int, task_id: int):
        if not self.db.is_manual_sort_enabled(project_id):
            return
        try:
            current_order = self.db.ensure_manual_task_order(project_id, fallback_to_auto=True)
            if task_id not in current_order:
                return
            # Already at the top — nothing to do.  Passing before_task_id=None to
            # insert_task_ids_in_manual_order means "insert at end", which would
            # incorrectly move the task from position 0 to the last position.
            if current_order[0] == task_id:
                return
            before_task_id = current_order[0]
            self.db.insert_task_ids_in_manual_order(project_id, [task_id], before_task_id=before_task_id)
        except Exception as e:
            print(f"Warning: Could not reorder recurring task {task_id}: {e}")

    def _equalize_checkbox_columns(self):
        """Kept for compatibility: the task list no longer has checkbox columns."""
        return

    def _create_notes_format_toolbar(self) -> QToolBar:
        """Create a toolbar bound directly to NotesEditor formatting actions."""
        return self.notes.create_format_toolbar(self)

    def _update_notes_maximize_icon(self, is_maximized: bool):
        """Reflect focus mode on the detail panel's focus button."""
        self.detail.set_focus_mode(is_maximized)

    def toggle_notes_maximized(self):
        """Toggle focus mode: the sidebar and the task list collapse, the detail fills the window."""
        if self._notes_maximized:
            self._restore_notes_view()
        else:
            self._maximize_notes_view()

    def _maximize_notes_view(self):
        """Maximize the notes editor to cover the entire main window."""
        # Store current state
        self._stored_splitter_sizes = self.splitter.sizes()
        self._stored_left_panel_visible = self.splitter.widget(0).isVisible()
        self._stored_mid_panel_visible = self.splitter.widget(1).isVisible()
        
        # Hide left and middle panels
        self.splitter.widget(0).setVisible(False)  # Projects panel
        self.splitter.widget(1).setVisible(False)  # Tasks panel
        
        # Update state and icon
        self._notes_maximized = True
        self._update_notes_maximize_icon(True)

    def _restore_notes_view(self):
        """Restore the notes editor to its normal size within the splitter."""
        # Restore panel visibility
        if self._stored_left_panel_visible:
            self.splitter.widget(0).setVisible(True)  # Projects panel
        if self._stored_mid_panel_visible:
            self.splitter.widget(1).setVisible(True)  # Tasks panel
        
        # Restore splitter sizes
        if self._stored_splitter_sizes:
            self.splitter.setSizes(self._stored_splitter_sizes)
        
        # Update state and icon
        self._notes_maximized = False
        self._update_notes_maximize_icon(False)

    # ----- UI helpers -----
    def _make_green_check_icon(self) -> QIcon:
        # Name kept for addons; the theme has a single accent, so the check is no longer green
        return lucide_icon("check", 15, T.ACCENT_700)

    def _make_snooze_icon(self) -> QIcon:
        return lucide_icon("clock", 15, T.NEUTRAL_700)

    def _create_actions(self):
        style = self.style()
        self.act_add_project = QAction(lucide_icon("plus"), "New Project…", self)
        self.act_add_project.setShortcut(QKeySequence("Ctrl+Shift+N"))
        self.act_go_to_project = QAction(lucide_icon("search"), "Go to Project…", self)
        self.act_go_to_project.setShortcut(QKeySequence("Ctrl+P"))
        self.act_go_to_project.triggered.connect(self.open_project_switcher)
        self.act_add_task = QAction(lucide_icon("plus"), "Add Task", self)
        self.act_bulk_import_tasks = QAction(lucide_icon("ul"), "Bulk Import Tasks...", self)
        self.act_remove_task = QAction(lucide_icon("trash"), "Remove Task(s)", self)
        self.act_mark_complete = QAction("Mark as Complete", self)
        self.act_mark_complete.setIcon(lucide_icon("check"))
        
        # Add the move task action
        self.act_move_task = QAction("Move to Project...", self)
        self.act_move_task.setIcon(lucide_icon("arrow"))
        self.act_move_task.triggered.connect(self.move_selected_tasks)

        # Add duplicate task action
        self.act_duplicate_tasks = QAction("Duplicate Task(s)", self)
        self.act_duplicate_tasks.setIcon(lucide_icon("copy"))
        self.act_duplicate_tasks.triggered.connect(self.duplicate_selected_tasks)
        
        # Add the mark as MoM action
        self.act_mark_mom = QAction("Mark as Minutes of Meeting", self)
        self.act_mark_mom.setIcon(lucide_icon("mom"))
        self.act_mark_mom.triggered.connect(self.toggle_mark_task_as_mom)
        self.act_mark_mom.setEnabled(False)  # Initially disabled until a task is selected

        # Recurrence configuration action
        self.act_recurrence = QAction("Recurrence...", self)
        self.act_recurrence.setIcon(lucide_icon("repeat"))
        self.act_recurrence.triggered.connect(self._open_recurrence_dialog)
        self.act_recurrence.setEnabled(False)

        self.act_pick_due = QAction("Set Due Date…", self)
        self.act_pick_due.setIcon(lucide_icon("cal"))
        self.act_pick_due.setShortcut(QKeySequence("Ctrl+D"))
        self.act_pick_due.triggered.connect(lambda _checked=False: self.pick_due_date_for_selected())
        self.act_pick_due.setEnabled(False)
        
        self.act_toggle_done = QAction("Completed Tasks", self, checkable=True)
        self.act_show_pinned_filter = QAction("Focus Tasks", self, checkable=True)
        self.act_show_no_due_dates = QAction("Missing Due Dates", self, checkable=True)
        self.act_search_include_done = QAction("Include Completed in Search", self, checkable=True)
        self.act_search_include_done.setShortcut(QKeySequence("Alt+C"))
        self.act_search_include_done.triggered.connect(self.set_search_include_done)
        self.act_always_on_top = QAction("Always on Top", self, checkable=True)
        self.act_always_on_top.toggled.connect(self.toggle_always_on_top)
        
        # Manual sort override action
        self.act_manual_sort = QAction("Manual Sort Override", self, checkable=True)
        self.act_manual_sort.triggered.connect(self.toggle_manual_sort)

        # Toggle visibility action

        # Periodic projects review action
        self.act_start_periodic_review = QAction("Start Review…", self)
        self.act_start_periodic_review.triggered.connect(self.start_periodic_projects_review)
        self.act_next_review_project = QAction("Reviewed → Next", self)
        self.act_next_review_project.setShortcut(QKeySequence("Ctrl+Shift+R"))
        self.act_next_review_project.triggered.connect(self.open_next_review_project)

        self.act_add_project.triggered.connect(self.add_project)

        self.act_add_task.triggered.connect(self.add_task)
        self.act_bulk_import_tasks.triggered.connect(self.open_bulk_task_import_dialog)
        self.act_remove_task.triggered.connect(self.remove_selected_task)
        self.act_mark_complete.triggered.connect(self.mark_task_complete)
        self.act_toggle_done.triggered.connect(self.toggle_show_completed)
        self.act_show_pinned_filter.triggered.connect(self.toggle_pinned_only)
        self.act_show_no_due_dates.triggered.connect(self.toggle_show_no_due_dates)

        self.act_undo_last = QAction("Undo Last Action", self)
        self.act_undo_last.setEnabled(False)
        self.act_undo_last.triggered.connect(self.undo_last_action)

        self.act_add_task.setShortcuts([QKeySequence("Ctrl+N"), QKeySequence("Ctrl+S")])
        self.act_mark_complete.setText("Complete / Reopen")
        self.act_mark_complete.setShortcuts([QKeySequence("Ctrl+Return"), QKeySequence("Ctrl+Enter")])

    def _create_menu(self):
        menubar = self.menuBar()
        mFile = menubar.addMenu("File")
        self.act_new_database = mFile.addAction("New Database…", self.new_database)
        self.act_open_database = mFile.addAction("Open Database…", self.open_database)
        mFile.addSeparator()
        mFile.addAction("Preferences…", self.show_preferences)
        mFile.addSeparator(); mFile.addAction("Exit", self.close)

        # Rebuilt each time it opens: the open project's actions, the same as its context menu
        self.menu_project = menubar.addMenu("Project")
        self.menu_project.aboutToShow.connect(self._populate_menubar_project_menu)
        self._populate_menubar_project_menu()
        # Keep these shortcuts working while the menu is closed
        for action in (self.act_add_project, self.act_go_to_project, self.act_next_review_project):
            self.addAction(action)

        mTask = menubar.addMenu("Task")
        mTask.addAction(self.act_add_task)
        mTask.addAction(self.act_bulk_import_tasks)
        mTask.addAction(self.act_remove_task)
        mTask.addAction(self.act_undo_last)
        mTask.addSeparator()
        mTask.addAction(self.act_mark_complete)
        mTask.addAction(self.act_move_task)  # Add move action to menu
        mTask.addAction(self.act_duplicate_tasks)
        mTask.addSeparator()
        mTask.addAction(self.act_mark_mom)  # Add mark as MoM action to menu
        mTask.addAction(self.act_pick_due)
        mTask.addAction(self.act_recurrence)

        mView = menubar.addMenu("View")
        mView.addAction(self.act_always_on_top)
        mView.addSeparator()
        mView.addAction(self.act_toggle_done)
        mView.addAction(self.act_show_pinned_filter)
        mView.addAction(self.act_show_no_due_dates)
        mView.addAction(self.act_search_include_done)
        
        # Set initial checked state for "Always on Top" based on settings
        always_on_top = self._settings.value('MainWindow/AlwaysOnTop', 'false') in ('true','1','True')
        self.act_always_on_top.setChecked(always_on_top)

        # Set initial checked state for "Show Pinned Only" based on settings
        self.act_show_pinned_filter.setChecked(self.pinned_only)

        mTools = menubar.addMenu("Tools")
        mTools.addAction("Copy Filtered Tasks to Clipboard", self.copy_filtered_tasks_to_clipboard)
        mTools.addAction("Export Filtered Tasks to Text File…", self.export_filtered_tasks_to_file)
        mTools.addSeparator()
        mTools.addAction("Export Filtered Tasks to CSV…", self.export_filtered_tasks_to_csv)
        mTools.addAction("Import Tasks from CSV…", self.import_tasks_from_csv)
        
        # Add addon menu items
        addon_items_added = False
        for addon_name, addon in self.addons.items():
            if hasattr(addon, 'register_menu_items'):
                if not addon_items_added:
                    mTools.addSeparator()
                    addon_items_added = True
                addon.register_menu_items(mTools, self)
        
        mTools.addSeparator()
        mTools.addAction("Remove Focus from Incomplete Tasks", self.remove_pins_from_incomplete_tasks)
        mTools.addAction("Clear Due Dates from Incomplete Tasks", self.remove_due_dates_from_incomplete_tasks)
        mTools.addSeparator()
        mTools.addAction("Open Attachments Folder", self.open_attachments_folder)

        # Add Help menu with update functionality
        mHelp = menubar.addMenu("Help")
        mHelp.addAction("Open Activity Log", self.open_activity_log)
        mHelp.addSeparator()
        mHelp.addAction("About", self.show_about)

    def _create_toolbar(self):
        """Wire up the flat top bar (brand, database switcher, search, saved indicator)."""
        bar = self.top_bar

        self.search_edit = bar.search
        self.search_edit.textChanged.connect(self.on_search_changed)
        self.search_edit.clearRequested.connect(self.clear_all_filters)
        self.search_edit.escapePressed.connect(self._on_search_escape)
        # Whether search results include completed tasks: a remembered preference, off by default
        self.search_include_done = self._settings.value('Search/IncludeCompleted', 'false') in ('true', '1', 'True')
        self._search_kept_done_ids: set[int] = set()  # completed while this query was shown; stay listed
        self._search_hidden_done_count = 0
        self.search_edit.set_include_done(self.search_include_done)
        self.act_search_include_done.setChecked(self.search_include_done)
        self.search_edit.includeDoneToggled.connect(self.set_search_include_done)
        self.search_edit.recentRequested.connect(self._show_recent_searches)
        # Typing runs the search once the keys pause, not on every key
        self._search_debounce = QTimer(self)
        self._search_debounce.setSingleShot(True)
        self._search_debounce.setInterval(150)
        self._search_debounce.timeout.connect(self._run_search)

        bar.set_database_name(os.path.basename(self.db.path))
        bar.db_menu.aboutToShow.connect(self._populate_database_menu)
        self._remember_recent_database(self.db.path)

        # "Saving…" while any debounced save is pending
        self._save_state_timer = QTimer(self)
        self._save_state_timer.setInterval(120)
        self._save_state_timer.timeout.connect(self._refresh_save_state)
        self._save_state_timer.start()

        # Window-level keys (Ctrl/Cmd+K from anywhere, N, J/K, Delete, Esc) arrive through the
        # single application event filter in ui/menus.py
        ui_menus.set_key_handler(self._app_key_event)

    def _pending_save_timers(self) -> List[QTimer]:
        timers = []
        notes = getattr(self, 'notes', None)
        # The editor arms its timers while loading with no task; nothing is being saved then
        if notes is not None and getattr(notes, 'task_id', None) is not None:
            timers.append(getattr(notes, '_autosave_timer', None))
            timers.append(getattr(notes, '_html_save_timer', None))
        return [t for t in timers if t is not None]

    def _refresh_save_state(self):
        try:
            saving = any(t.isActive() for t in self._pending_save_timers())
        except RuntimeError:
            saving = False
        error = self.notes.save_error if self.notes.save_state == 'error' else ''
        self.top_bar.saved.set_error(error)
        self.top_bar.saved.set_saving(saving or self.notes.save_state in ('dirty', 'saving'))

    def focus_search(self):
        self.search_edit.setFocus(Qt.FocusReason.ShortcutFocusReason)
        self.search_edit.selectAll()

    def _on_search_escape(self):
        if self.search_edit.text():
            self.search_edit.clear()
        else:
            self.task_view.setFocus()

    def eventFilter(self, obj, event):
        # Object-level filter on the status label only
        if event.type() == QEvent.Type.ToolTip and obj is getattr(self, 'status_comprehensive', None):
            parts = [self._status_project_tooltip, self.status_task_info.toolTip() or self.status_task_info.text()]
            obj.setToolTip("\n\n".join(part for part in parts if part))
            return False
        return super().eventFilter(obj, event)

    def _app_key_event(self, obj, event) -> bool:
        """Every key press in the application passes through here. True consumes it."""
        key = event.key()
        if (key == Qt.Key.Key_Escape and getattr(self, '_notes_maximized', False)
                and self.isActiveWindow() and QApplication.activeModalWidget() is None and QApplication.activePopupWidget() is None
                and not isinstance(QApplication.focusWidget(), (QLineEdit, QComboBox, QDateEdit, QSpinBox))
                and QApplication.focusWidget() is not self.detail.title_edit):
            self._restore_notes_view()
            return False
        if isinstance(obj, QWidget) and obj.window() is self and self._handle_global_key(event):
            return True
        if key == Qt.Key.Key_K:
            mods = event.modifiers() & ~Qt.KeyboardModifier.KeypadModifier
            if mods == Qt.KeyboardModifier.ControlModifier and self.isActiveWindow() and QApplication.activeModalWidget() is None:
                self.focus_search()
                return True
        return False

    # ----- database switcher -----
    def _recent_databases(self) -> List[str]:
        raw = self._settings.value('Database/RecentPaths', [])
        if isinstance(raw, str):
            raw = [raw] if raw else []
        return [str(path) for path in (raw or []) if path]

    def _remember_recent_database(self, path: str):
        try:
            path = os.path.abspath(path)
            recent = [p for p in self._recent_databases() if os.path.abspath(p) != path]
            recent.insert(0, path)
            self._settings.setValue('Database/RecentPaths', recent[:8])
            self._settings.sync()
        except Exception as e:
            print(f"Could not store recent database: {e}")

    def _populate_database_menu(self):
        menu = self.top_bar.db_menu
        menu.clear()
        current = os.path.abspath(getattr(self.db, 'path', ''))
        recent = [p for p in self._recent_databases() if os.path.exists(p)]
        if recent:
            menu.addSection("Recent")
            for path in recent:
                is_current = os.path.abspath(path) == current
                action = menu.addAction(lucide_icon("check" if is_current else "db", 15, T.NEUTRAL_700), os.path.basename(path))
                action.setToolTip(path)
                action.setEnabled(not is_current)
                action.triggered.connect(lambda _checked=False, p=path: self.open_database_path(p))
            menu.addSeparator()
        menu.setToolTipsVisible(True)
        menu.addAction(self.act_new_database)
        menu.addAction(self.act_open_database)

    def open_database_path(self, path: str) -> bool:
        """Open a known database file (recent list). Returns True when the switch succeeded."""
        if not os.path.exists(path):
            QMessageBox.warning(self, "Open Database", f"The database file no longer exists:\n{path}")
            return False
        if os.path.abspath(path) == os.path.abspath(getattr(self.db, 'path', '')):
            return True
        try:
            new_db = DB(path)
            self._switch_to_database(new_db, path)
            return True
        except DatabaseLockException as e:
            QMessageBox.warning(self, "Database Locked", str(e))
        except Exception as e:
            QMessageBox.critical(self, "Error", f"Failed to open database: {e}")
        return False

    def _create_status_bar(self):
        """Status bar: open/overdue counts, the active filter and shortcut hints."""
        status_bar = self.statusBar()
        status_bar.clearMessage()
        status_bar.setSizeGripEnabled(False)

        container = QWidget()
        container.setObjectName("StatusBarRow")
        row = QHBoxLayout(container)
        row.setContentsMargins(14, 5, 14, 5)
        row.setSpacing(20)

        # Left: "{open} open · {overdue} overdue"
        self.status_comprehensive = QLabel("")
        self.status_comprehensive.installEventFilter(self)
        # Middle: active search / filter
        self.status_filter = QLabel("")
        self.status_filter.setVisible(False)
        # Selected-task details live in the tooltip of the counts label
        self.status_task_info = QLabel("")
        self.status_task_info.setVisible(False)
        self._status_project_tooltip = ""

        hints = kbd_hints([("N", "new"), ("↑↓", "move"), (f"{CTRL_LABEL} ↵", "complete"), (f"{CTRL_LABEL} K", "search")])

        row.addWidget(self.status_comprehensive)
        row.addWidget(self.status_filter)
        row.addStretch(1)
        row.addWidget(hints)
        status_bar.addWidget(container, 1)

        # Connect signals to update status bar (with error handling)
        try:
            self.project_model.modelReset.connect(self._update_comprehensive_status)
            self.project_model.dataChanged.connect(self._update_comprehensive_status)
            self.task_model.modelReset.connect(self._update_comprehensive_status)
            self.task_model.dataChanged.connect(self._update_comprehensive_status)
        except Exception as e:
            print(f"Warning: Could not connect status bar signals: {e}")

        # Initial update with delay to ensure everything is initialized
        QTimer.singleShot(200, self._update_comprehensive_status)

    def _status_counts(self) -> Tuple[int, int]:
        """(open tasks, overdue open tasks) across the whole database."""
        today = datetime.date.today().isoformat()
        row = self.db.conn.execute(
            "SELECT COUNT(*) AS open_count, "
            "COALESCE(SUM(CASE WHEN due_date IS NOT NULL AND TRIM(due_date) != '' AND substr(due_date, 1, 10) < ? THEN 1 ELSE 0 END), 0) AS overdue_count "
            "FROM tasks WHERE done=0",
            (today,)
        ).fetchone()
        return int(row['open_count'] or 0), int(row['overdue_count'] or 0)

    def _active_filter_labels(self) -> List[str]:
        """Human-readable names of the search query and filters that are narrowing the lists."""
        labels = []
        query = self.search_edit.text().strip() if hasattr(self, 'search_edit') else ""
        if query:
            labels.append(f"“{query}”")
            # Search covers every project, whatever view was open before
            if getattr(self, 'search_include_done', False):
                labels.append("Completed included")
            return labels
        kind, value = getattr(self, 'current_view', (None, None))
        if kind == 'smart':
            labels.append(SMART_VIEW_TITLES.get(value, str(value)))
        if self.include_done:
            labels.append("Completed shown")
        return labels

    # ----- Actions -----
    def new_database(self):
        path, _ = QFileDialog.getSaveFileName(
            self, 
            "Create New Database", 
            APP_DIR, 
            "SQLite DB (*.sqlite *.db);;All files (*.*)"
        )
        if not path:
            return
        
        # Ensure .sqlite extension if no extension provided
        if not os.path.splitext(path)[1]:
            path += '.sqlite'

        current_db_path = os.path.abspath(getattr(self.db, 'path', ''))
        if current_db_path and os.path.abspath(path) == current_db_path:
            QMessageBox.warning(
                self,
                "Create New Database",
                "The selected path is the database that is currently open.\nChoose a different file path."
            )
            return
        
        try:
            # Create new database
            if os.path.exists(path):
                reply = QMessageBox.question(
                    self, 
                    "File Exists", 
                    f"The file '{os.path.basename(path)}' already exists. Do you want to replace it?",
                    QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                    QMessageBox.StandardButton.No
                )
                if reply != QMessageBox.StandardButton.Yes:
                    return
                os.remove(path)

            new_db = DB(path)
            self._switch_to_database(new_db, path)
            
        except Exception as e:
            if isinstance(e, DatabaseLockException):
                QMessageBox.warning(self, "Database Locked", str(e))
                return
            QMessageBox.critical(self, "Error", f"Failed to create database: {e}")

    def open_database(self):
        """Open a different database, handling lock conflicts."""
        while True:  # Loop until user cancels or successfully opens a database
            path, _ = QFileDialog.getOpenFileName(self, "Open SQLite DB", APP_DIR, "SQLite DB (*.sqlite *.db);;All files (*.*)")
            if not path:
                return

            current_db_path = os.path.abspath(getattr(self.db, 'path', ''))
            if current_db_path and os.path.abspath(path) == current_db_path:
                QMessageBox.information(self, "Open Database", "That database is already open.")
                return
            
            try:
                # Open existing database
                new_db = DB(path)
                self._switch_to_database(new_db, path)
                return  # Success, exit the loop
                
            except DatabaseLockException as e:
                # Database is locked, show error and ask if user wants to choose another
                reply = QMessageBox.question(
                    self, 
                    "Database Locked", 
                    f"{str(e)}\n\nWould you like to select a different database?",
                    QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                    QMessageBox.StandardButton.Yes
                )
                if reply != QMessageBox.StandardButton.Yes:
                    return
                # Continue loop to show file dialog again
                
            except Exception as e:
                QMessageBox.critical(self, "Error", f"Failed to open database: {e}")
                return

    def show_preferences(self):
        """Show the preferences dialog."""
        dialog = PreferencesDialog(self._settings, self, self.db, self)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            # Preferences were saved, no additional action needed
            idx = self.project_view.currentIndex()
            pid = self.project_model.project_id_at(idx.row()) if idx.isValid() else None
            self._update_feature_link_for_project(pid)
    
    def _switch_to_database(self, new_db: DB, path: str):
        """Atomically switch to a new database and keep the current session usable on failure."""
        self.detail.chips._commit_input()
        if not self.notes.flush_pending():
            new_db.close()
            return False
        old_db = getattr(self, 'db', None)
        old_attach_dir = getattr(self, 'attach_dir', None)
        old_backup_dir = getattr(self, 'backup_dir', None)
        old_feature_links_key = getattr(self, '_feature_links_settings_key', None)
        old_feature_links = dict(getattr(self, '_project_feature_links', {}))
        old_window_title = self.windowTitle()
        old_project_index = self.project_view.currentIndex() if hasattr(self, 'project_view') else QModelIndex()
        old_project_id = self.project_model.project_id_at(old_project_index.row()) if old_project_index.isValid() else None
        old_task_index = self.task_view.currentIndex() if hasattr(self, 'task_view') else QModelIndex()
        old_task_id = self.task_model.task_id_at(old_task_index.row()) if old_task_index.isValid() else None

        global database
        try:
            migrate_legacy_folders(path)
            new_attach_dir = get_attach_dir(path)
            new_backup_dir = get_backup_dir(path)
            new_db.backfill_note_attachments(new_attach_dir)

            database = new_db
            self.db = new_db
            self.attach_dir = new_attach_dir
            self.backup_dir = new_backup_dir
            self._feature_links_settings_key = self._build_feature_link_settings_key(path)
            self._project_feature_links = self._load_project_feature_links()

            self.project_model.db = new_db
            self.task_model.db = new_db
            self.notes.rebind_context(new_db, new_attach_dir, reload_current_task=False)

            try:
                self._settings.setValue('Database/LastPath', path)
                self._settings.sync()
            except Exception:
                pass

            # Clear search state that belongs to the old database before reloading
            # the project model, so its _search_filter_ids are not applied to new DB IDs.
            self.search_terms = []
            self.search_stakeholder_terms = []
            self.search_query = None
            self._search_debounce.stop()
            self._search_results_by_project.clear()
            self.project_model._search_filter_ids = None
            self.project_model.search_match_counts = None
            self.project_model.keep_visible_id = None
            self.project_model.project_name_filter = ""
            # Ids from the old file mean nothing here: undo history, the open view, collapsed groups
            self._clear_undo_history()
            self._collapsed_groups.clear()
            self._quick_add_pick = None
            self._recent_move_targets = []
            self._last_project_id = None
            self.search_edit.blockSignals(True)
            self.search_edit.clear()
            self.search_edit.blockSignals(False)
            if hasattr(self, 'project_filter_edit'):
                self.project_filter_edit.blockSignals(True)
                self.project_filter_edit.clear()
                self.project_filter_edit.blockSignals(False)

            self.project_model.reload()
            self.project_view.clearSelection()
            self.task_model.set_context(None, self.include_done, self.pinned_only)
            self.task_view.clearSelection()
            if self.notes.set_task(None) is False:
                return
            self._update_right_pane_state(False)
            self.detail_state.due_date = None
            self._sync_detail_due()
            self.detail_state.recurrence = None
            self._sync_detail_recurrence()

            # Open a view that exists in this database
            if self.project_model.rowCount() > 0:
                self.current_view = ('project', self.project_model.project_id_at(0))
            else:
                self.current_view = ('smart', 'today')
            self._apply_current_view()
            self._save_current_view()

            db_name = os.path.basename(path)
            self.setWindowTitle(f"Project Notes - {db_name}")
            self.top_bar.set_database_name(db_name)
            self._remember_recent_database(path)
            self._update_feature_link_for_project(None)
            self._update_comprehensive_status()
            QTimer.singleShot(200, self._process_due_recurrences)
        except Exception:
            if new_db is not old_db:
                try:
                    new_db.close()
                except Exception:
                    pass

            database = old_db
            self.db = old_db
            if old_attach_dir is not None:
                self.attach_dir = old_attach_dir
            if old_backup_dir is not None:
                self.backup_dir = old_backup_dir
            if old_feature_links_key is not None:
                self._feature_links_settings_key = old_feature_links_key
            self._project_feature_links = old_feature_links

            if old_db is not None:
                self.project_model.db = old_db
                self.task_model.db = old_db
                if old_attach_dir is not None:
                    self.notes.rebind_context(old_db, old_attach_dir, reload_current_task=False)
                self.project_model.reload()

                restored_project_id = None
                if old_project_id is not None:
                    row = self.project_model.row_for_project(old_project_id)
                    if row >= 0:
                        project_index = self.project_model.index(row, 0)
                        self.project_view.setCurrentIndex(project_index)
                        self.project_view.scrollTo(project_index, QListView.ScrollHint.EnsureVisible)
                        restored_project_id = old_project_id
                if restored_project_id is None and self.project_model.rowCount() > 0:
                    fallback_index = self.project_model.index(0, 0)
                    self.project_view.setCurrentIndex(fallback_index)
                    self.project_view.scrollTo(fallback_index, QListView.ScrollHint.EnsureVisible)
                    restored_project_id = self.project_model.project_id_at(0)

                self.task_model.set_context(restored_project_id, self.include_done, self.pinned_only)
                if old_task_id is not None:
                    self._select_task_if_visible(old_task_id)
                else:
                    self.task_view.clearSelection()
                    if self.notes.set_task(None) is False:
                        return
                    self._update_right_pane_state(False)

                self._update_feature_link_for_project(restored_project_id)
                self._update_comprehensive_status()

            self.setWindowTitle(old_window_title)
            raise
        else:
            if old_db is not None and old_db is not new_db:
                old_db.close()

    def _load_database_from_settings(self):
        """Load database from settings, with fallback to default"""
        try:
            # Try to get last used database path from settings
            last_path = self._settings.value('Database/LastPath', DB_PATH)
            
            # If it's the same as current default and exists, use it
            if last_path and os.path.exists(last_path):
                try:
                    return DB(last_path), last_path
                except Exception:
                    pass  # Fall through to error handling
            
            # If last path doesn't exist or failed to open, try default path
            if os.path.exists(DB_PATH):
                try:
                    db = DB(DB_PATH)
                    # Update settings to reflect we're using default
                    self._settings.setValue('Database/LastPath', DB_PATH)
                    self._settings.sync()
                    return db, DB_PATH
                except Exception:
                    pass
            
            # If default doesn't exist, create it
            try:
                db = DB(DB_PATH)
                self._settings.setValue('Database/LastPath', DB_PATH)
                self._settings.sync()
                return db, DB_PATH
            except Exception:
                pass
                
        except Exception:
            pass
        
        # If all else fails, prompt user to select database
        QMessageBox.warning(
            None, 
            "Database Not Found", 
            "Could not find or open the database. Please select a database file."
        )
        
        path, _ = QFileDialog.getOpenFileName(
            None, 
            "Open SQLite DB", 
            APP_DIR, 
            "SQLite DB (*.sqlite *.db);;All files (*.*)"
        )
        
        if path and os.path.exists(path):
            try:
                db = DB(path)
                self._settings.setValue('Database/LastPath', path)
                self._settings.sync()
                return db, path
            except Exception as e:
                QMessageBox.critical(None, "Error", f"Failed to open selected database: {e}")
        
        # Final fallback: create default database
        try:
            db = DB(DB_PATH)
            self._settings.setValue('Database/LastPath', DB_PATH)
            self._settings.sync()
            return db, DB_PATH
        except Exception as e:
            QMessageBox.critical(None, "Fatal Error", f"Could not create or open any database: {e}")
            sys.exit(1)

    def open_attachments_folder(self):
        os.makedirs(self.attach_dir, exist_ok=True)
        if sys.platform.startswith('darwin'):
            os.system(f'open "{self.attach_dir}"')
        elif os.name == 'nt':
            os.startfile(self.attach_dir)
        else:
            os.system(f'xdg-open "{self.attach_dir}"')

    def _export_tasks_for_project(self, project_id: int) -> List[sqlite3.Row]:
        """Tasks of one project for the "filtered tasks" exports; a smart view narrows them to its tasks."""
        tasks = self.db.list_tasks(project_id, self.include_done, False)
        if self.current_view[0] == 'smart':
            view_ids = {r['id'] for r in self.db.list_tasks_for_smart_view(self.current_view[1])}
            tasks = [t for t in tasks if t['id'] in view_ids]
        return tasks

    def copy_project_tasks_as_text(self, project_id: Optional[int] = None):
        """Project menu: copy the open tasks of a project (default: the open one) as a plain list."""
        if isinstance(project_id, bool):
            project_id = None  # QAction.triggered passes its checked state
        if project_id is None:
            project_id = self._current_project_id()
        if project_id is None:
            return
        titles = [str(t['title']) for t in self.task_model.rows if not bool(t['done'])] \
            if self.task_model.project_id == project_id else \
            [str(t['title']) for t in self.db.list_tasks(project_id, False, False)]
        if not titles:
            self.toast.show_message("No open tasks to copy")
            return
        QApplication.clipboard().setText("\n".join(f"- {title}" for title in titles))
        self.toast.show_message("Copied open tasks")

    def copy_filtered_tasks_to_clipboard(self):
        """Copy currently filtered tasks to clipboard"""
        export_text = self._generate_filtered_tasks_export()
        
        if not export_text.strip():
            QMessageBox.information(self, "Copy to Clipboard", "No tasks match the current filters.")
            return
        
        try:
            clipboard = QApplication.clipboard()
            clipboard.setText(export_text)
            self.toast.show_message("Copied filtered tasks")
        except Exception as e:
            QMessageBox.critical(self, "Copy Error", f"Failed to copy to clipboard:\n{str(e)}")

    def export_filtered_tasks_to_file(self):
        """Export currently filtered tasks to a text file"""
        export_text = self._generate_filtered_tasks_export()
        
        if not export_text.strip():
            QMessageBox.information(self, "Export Tasks", "No tasks match the current filters.")
            return
        
        # Get downloads folder path
        try:
            downloads_path = os.path.join(os.path.expanduser("~"), "Downloads")
            if not os.path.exists(downloads_path):
                downloads_path = os.path.expanduser("~")  # Fallback to home directory
        except Exception:
            downloads_path = os.path.expanduser("~")
        
        # Show save dialog
        file_path, _ = QFileDialog.getSaveFileName(
            self,
            "Export Filtered Tasks",
            os.path.join(downloads_path, "tasks_export.txt"),
            "Text files (*.txt);;All files (*.*)"
        )
        
        if not file_path:
            return
        
        try:
            # Write tasks to file
            with open(file_path, 'w', encoding='utf-8') as f:
                f.write(export_text)
            
            QMessageBox.information(self, "Export Complete", f"Filtered tasks exported to:\n{file_path}")
            
        except Exception as e:
            QMessageBox.critical(self, "Export Error", f"Failed to export tasks:\n{str(e)}")

    def _generate_filtered_tasks_export(self) -> str:
        """Generate plain text export of currently filtered tasks"""
        # Get all filtered tasks organized by project, preserving project order
        filtered_tasks_by_project = []  # List of tuples: (project_title, tasks)
        
        if self._is_searching():
            # Use search results - maintain order from project model
            for project_row in self.project_model.rows:
                project_id = project_row['id']
                if project_id in self._search_results_by_project:
                    tasks = self._search_results_by_project[project_id]
                    if tasks:  # Only include projects with visible tasks
                        filtered_tasks_by_project.append((project_row['title'], tasks))
        else:
            # Use current project/task model state - maintain project list order
            for project_row in self.project_model.rows:
                project_id = project_row['id']
                project_title = project_row['title']
                
                # Get tasks for this project using current filters
                tasks = self._export_tasks_for_project(project_id)
                if tasks:  # Only include projects with visible tasks
                    filtered_tasks_by_project.append((project_title, tasks))
        
        # Generate export text
        if not filtered_tasks_by_project:
            return ""
        
        # Create header with filter information
        export_lines = []
        export_lines.append("Filtered Tasks Export")
        export_lines.append("=" * 50)
        export_lines.append("")
        
        # Add filter description
        filter_desc = []
        if self.search_terms:
            filter_desc.append(f"Search terms: {', '.join(self.search_terms)}")
        if self.search_stakeholder_terms:
            filter_desc.append(f"Stakeholder terms: {', '.join(self.search_stakeholder_terms)}")
        if self.current_view[0] == 'smart':
            filter_desc.append(f"View: {SMART_VIEW_TITLES.get(self.current_view[1], self.current_view[1])}")
        if self.include_done:
            filter_desc.append("Including: Completed tasks")
        else:
            filter_desc.append("Excluding: Completed tasks")
        
        if filter_desc:
            export_lines.append("Applied filters:")
            for desc in filter_desc:
                export_lines.append(f"  • {desc}")
            export_lines.append("")
        
        # Add tasks organized by project (maintaining display order)
        total_tasks = 0
        for project_title, tasks in filtered_tasks_by_project:
            export_lines.append(f"Project: {project_title}")
            export_lines.append("-" * (len(project_title) + 9))
            
            for task in tasks:
                task_line = f"  • {task['title']}"
                
                # Collect all indicators (due date + status)
                indicators = []
                
                # Add due date if available
                due_date_str = task['due_date'] if 'due_date' in task.keys() else None
                if due_date_str:
                    try:
                        date_part = due_date_str.split()[0] if ' ' in due_date_str else due_date_str
                        indicators.append(f"📅 {date_part}")
                    except Exception:
                        pass
                
                # Add status indicators
                if bool(task['pinned']):
                    indicators.append("🎯 Focus")
                if bool(task['done']):
                    indicators.append("✅ Done")
                # Add MoM indicator
                try:
                    if bool(task['marked_mom']):
                        indicators.append("📝 MoM")
                except (KeyError, IndexError):
                    pass
                
                if indicators:
                    task_line += f" [{', '.join(indicators)}]"
                
                # Add stakeholders for each task
                try:
                    stakeholders = self.db.get_stakeholders_for_task(task['id'])
                    if stakeholders:
                        task_line += f" | Stakeholders: {', '.join(stakeholders)}"
                except Exception:
                    pass  # Continue without stakeholders if query fails
                
                export_lines.append(task_line)
                total_tasks += 1
            
            export_lines.append("")
        
        # Add summary
        project_count = len(filtered_tasks_by_project)
        export_lines.append(f"Summary: {total_tasks} tasks across {project_count} project(s)")
        export_lines.append(f"Exported on: {datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
        
        return "\n".join(export_lines)

    def export_filtered_tasks_to_csv(self):
        """Export currently filtered tasks to a CSV file with images"""
        # Get all filtered tasks
        filtered_tasks_data = self._get_filtered_tasks_data()
        
        if not filtered_tasks_data:
            QMessageBox.information(self, "Export Tasks", "No tasks match the current filters.")
            return
        
        # Get downloads folder path
        try:
            downloads_path = os.path.join(os.path.expanduser("~"), "Downloads")
            if not os.path.exists(downloads_path):
                downloads_path = os.path.expanduser("~")  # Fallback to home directory
        except Exception:
            downloads_path = os.path.expanduser("~")
        
        # Show save dialog
        file_path, _ = QFileDialog.getSaveFileName(
            self,
            "Export Filtered Tasks to CSV",
            os.path.join(downloads_path, "tasks_export.csv"),
            "CSV files (*.csv);;All files (*.*)"
        )
        
        if not file_path:
            return
        
        try:
            import csv
            
            # Create images folder alongside CSV
            csv_dir = os.path.dirname(file_path)
            csv_name = os.path.splitext(os.path.basename(file_path))[0]
            images_dir = os.path.join(csv_dir, f"{csv_name}_images")
            
            exported_images = {}  # Track copied images: original_path -> new_filename
            
            # Process tasks and handle images
            processed_tasks_data = []
            for task_data in filtered_tasks_data:
                # Process notes HTML to extract and copy images
                notes_html = task_data.get('notes_html', '')
                if notes_html:
                    processed_html, images_copied = self._process_export_images(
                        notes_html, images_dir, exported_images
                    )
                    task_data['notes_html'] = processed_html
                
                processed_tasks_data.append(task_data)
            
            # Write to a temporary file first, so a failed export never replaces an existing file
            temp_path = f"{file_path}.{uuid.uuid4().hex[:8]}.tmp"
            with open(temp_path, 'w', newline='', encoding='utf-8') as csvfile:
                # Use pipe delimiter to avoid conflicts with commas in content
                writer = csv.DictWriter(csvfile, fieldnames=CSV_EXPORT_FIELDS, delimiter='|', quoting=csv.QUOTE_MINIMAL)
                
                # Write header
                writer.writeheader()
                
                # Write task data with content sanitization
                for task_data in processed_tasks_data:
                    # Sanitize data to prevent delimiter conflicts
                    sanitized_data = {}
                    for key, value in task_data.items():
                        if isinstance(value, str):
                            # Replace any pipe characters with a safe alternative
                            sanitized_value = value.replace('|', '¦')  # Replace pipe with broken bar
                            sanitized_data[key] = sanitized_value
                        else:
                            sanitized_data[key] = value
                    writer.writerow(sanitized_data)
            os.replace(temp_path, file_path)
            temp_path = None
            
            # Show results
            result_msg = f"Filtered tasks exported to CSV:\n{file_path}\n\nExported {len(processed_tasks_data)} tasks"
            if exported_images:
                result_msg += f" with {len(exported_images)} images.\n\nImages folder: {images_dir}"
            else:
                result_msg += "."
            
            QMessageBox.information(self, "Export Complete", result_msg)
            
        except Exception as e:
            QMessageBox.critical(self, "Export Error", f"Failed to export tasks to CSV:\n{str(e)}")
        finally:
            try:
                if 'temp_path' in locals() and temp_path and os.path.exists(temp_path):
                    os.remove(temp_path)
            except Exception:
                pass

    def _process_export_images(self, html_content: str, images_dir: str, exported_images: dict) -> Tuple[str, int]:
        """Process HTML content to extract and copy images for export"""
        if not html_content:
            return html_content, 0
        
        import re
        
        # Find all image tags with src attributes
        img_pattern = r'<img[^>]*src=["\']([^"\']+)["\'][^>]*>'
        images_found = re.findall(img_pattern, html_content, re.IGNORECASE)
        
        if not images_found:
            return html_content, 0
        
        # Create images directory if needed
        if images_found and not os.path.exists(images_dir):
            os.makedirs(images_dir, exist_ok=True)
        
        modified_html = html_content
        images_copied_count = 0
        
        for img_src in images_found:
            try:
                # Note images are stored relative to the database's folder, not the application's
                abs_img_path = self.db._attachment_abs_path(img_src, self.attach_dir)
                
                if not os.path.exists(abs_img_path):
                    continue
                
                # Check if we've already copied this image
                if abs_img_path in exported_images:
                    new_filename = exported_images[abs_img_path]
                else:
                    # Generate unique filename
                    original_filename = os.path.basename(abs_img_path)
                    name, ext = os.path.splitext(original_filename)
                    counter = 1
                    new_filename = original_filename
                    
                    while os.path.exists(os.path.join(images_dir, new_filename)):
                        new_filename = f"{name}_{counter}{ext}"
                        counter += 1
                    
                    # Copy the image file
                    dest_path = os.path.join(images_dir, new_filename)
                    shutil.copy2(abs_img_path, dest_path)
                    exported_images[abs_img_path] = new_filename
                    images_copied_count += 1
                
                # Update HTML to reference the new location
                old_src_pattern = re.escape(img_src)
                new_src = f"./{os.path.basename(images_dir)}/{new_filename}"
                modified_html = re.sub(
                    f'(<img[^>]*src=["\']){old_src_pattern}(["\'][^>]*>)',
                    f'\\1{new_src}\\2',
                    modified_html,
                    flags=re.IGNORECASE
                )
                
            except Exception as e:
                print(f"Error processing image {img_src}: {e}")
                continue
        
        return modified_html, images_copied_count

    def import_tasks_from_csv(self):
        """Import tasks from a CSV file with image support"""
        # Show file dialog
        file_path, _ = QFileDialog.getOpenFileName(
            self,
            "Import Tasks from CSV",
            os.path.expanduser("~"),
            "CSV files (*.csv);;All files (*.*)"
        )
        
        if not file_path:
            return
        
        try:
            import csv
            imported_tasks = []
            created_projects = []
            errors = []
            warnings = []
            imported_images = []
            
            # Check for images folder
            csv_dir = os.path.dirname(file_path)
            csv_name = os.path.splitext(os.path.basename(file_path))[0]
            images_dir = os.path.join(csv_dir, f"{csv_name}_images")
            
            # utf-8-sig also reads files saved with a byte-order mark (Excel)
            with open(file_path, 'r', newline='', encoding='utf-8-sig') as csvfile:
                header_line = csvfile.readline()
                csvfile.seek(0)
                reader = csv.DictReader(csvfile, delimiter=detect_csv_delimiter(header_line))
                fieldnames = set(reader.fieldnames or [])
                
                # Validate required columns
                required_columns = ['project_title', 'task_title']
                if not all(col in fieldnames for col in required_columns):
                    missing = [col for col in required_columns if col not in fieldnames]
                    QMessageBox.critical(
                        self, 
                        "Import Error", 
                        f"CSV file is missing required columns: {', '.join(missing)}\n\n"
                        f"Required columns: {', '.join(required_columns)}\n"
                        f"Found columns: {', '.join(reader.fieldnames or [])}"
                    )
                    return
                
                # Process each row
                for row_num, row in enumerate(reader, start=2):  # Start at 2 because of header
                    try:
                        # Sanitize data back (reverse the export sanitization)
                        sanitized_row = {}
                        for key, value in row.items():
                            if isinstance(value, str) and value:
                                # Replace broken bar back to pipe character
                                sanitized_value = value.replace('¦', '|')
                                sanitized_row[key] = sanitized_value
                            else:
                                sanitized_row[key] = value
                        
                        project_title = (sanitized_row.get('project_title', '') or '').strip()
                        task_title = (sanitized_row.get('task_title', '') or '').strip()
                        
                        if not project_title or not task_title:
                            errors.append(f"Row {row_num}: Missing project_title or task_title")
                            continue
                        
                        # Find or create project
                        project_id = None
                        for proj_row in self.db.list_projects():
                            if proj_row['title'].lower() == project_title.lower():
                                project_id = proj_row['id']
                                break
                        
                        if project_id is None:
                            # Create new project
                            project_id = self.db.add_project(project_title)
                            created_projects.append(project_title)
                        
                        # Read every cell before anything is created, so a malformed row
                        # is rejected whole instead of leaving a half-imported task behind
                        def cell(name: str) -> str:
                            value = sanitized_row.get(name)
                            return value.strip() if isinstance(value, str) else ''

                        def flag(name: str) -> bool:
                            return cell(name).lower() in ('true', '1', 'yes', 'y')

                        pinned, done = flag('pinned'), flag('done')
                        waiting, marked_mom = flag('waiting'), flag('marked_mom')
                        stakeholder_names = [name.strip() for name in re.split(r'[,;|]+', cell('stakeholders')) if name.strip()]
                        recurrence_rule_text = cell('recurrence_rule')
                        import_recurrence = flag('recurring') and _parse_recurrence_rule(recurrence_rule_text) is not None
                        if flag('recurring') and not import_recurrence:
                            warnings.append(f"Row {row_num}: Invalid recurrence_rule. Imported the task without recurrence.")

                        # Create task
                        task_id = self.db.add_task(project_id, task_title)

                        if pinned or done or waiting or marked_mom:
                            self.db.set_imported_task_state(task_id, pinned=pinned, done=done, waiting=waiting, marked_mom=marked_mom)

                        if stakeholder_names:
                            self.db.set_stakeholders_for_task(task_id, stakeholder_names)

                        if import_recurrence:
                            def recurrence_time(name: str) -> Optional[str]:
                                parsed = _parse_recurrence_datetime(cell(name))
                                return _format_recurrence_datetime(parsed) if parsed else None
                            self.db.set_task_recurrence(
                                task_id, recurring=True, rule_text=recurrence_rule_text,
                                start_at=recurrence_time('recurrence_start_at'),
                                next_at=recurrence_time('recurrence_next_at'),
                                last_at=recurrence_time('recurrence_last_at')
                            )
                        
                        # Handle notes with images                        # Handle notes with images
                        notes_html = (sanitized_row.get('notes_html', '') or '').strip()
                        notes_plain = (sanitized_row.get('notes_plain', '') or '').strip()
                        
                        if notes_html:
                            # Process HTML to import images
                            processed_html, images_count = self._process_import_images(notes_html, images_dir)
                            if images_count > 0:
                                imported_images.extend([f"Task '{task_title}': {images_count} images"])
                            # The exported plain text has its line breaks flattened; derive it from the HTML
                            self.db.save_note(task_id, processed_html, None, self.attach_dir)
                        elif notes_plain:
                            # Convert plain text to basic HTML
                            notes_html = notes_plain.replace('\n', '<br>')
                            if not notes_html.startswith('<'):
                                notes_html = f"<p>{notes_html}</p>"
                            self.db.save_note(task_id, notes_html, notes_plain, self.attach_dir)

                        imported_due_date = CSV_METADATA_UNSET
                        if 'due_date' in fieldnames:
                            raw_due_date = (sanitized_row.get('due_date', '') or '').strip()
                            if not raw_due_date:
                                imported_due_date = None
                            elif _parse_flexible_datetime(raw_due_date, allow_date_only=True) is None:
                                warnings.append(
                                    f"Row {row_num}: Invalid due_date '{raw_due_date}'. Kept the task without restoring that due date."
                                )
                            else:
                                imported_due_date = raw_due_date

                        imported_created_at = CSV_METADATA_UNSET
                        if 'created_at' in fieldnames:
                            raw_created_at = (sanitized_row.get('created_at', '') or '').strip()
                            if raw_created_at:
                                if _parse_flexible_datetime(raw_created_at) is None:
                                    warnings.append(
                                        f"Row {row_num}: Invalid created_at '{raw_created_at}'. Kept the import-time created timestamp."
                                    )
                                else:
                                    imported_created_at = raw_created_at

                        imported_updated_at = CSV_METADATA_UNSET
                        if 'updated_at' in fieldnames:
                            raw_updated_at = (sanitized_row.get('updated_at', '') or '').strip()
                            if not raw_updated_at:
                                imported_updated_at = None
                            elif _parse_flexible_datetime(raw_updated_at) is None:
                                warnings.append(
                                    f"Row {row_num}: Invalid updated_at '{raw_updated_at}'. Kept the import-time updated timestamp."
                                )
                            else:
                                imported_updated_at = raw_updated_at

                        self.db.restore_imported_task_metadata(
                            task_id,
                            due_date=imported_due_date,
                            created_at=imported_created_at,
                            updated_at=imported_updated_at
                        )
                        
                        imported_tasks.append({
                            'project': project_title,
                            'task': task_title,
                            'task_id': task_id
                        })
                        
                    except Exception as e:
                        errors.append(f"Row {row_num}: {str(e)}")
            
            # Show results
            if imported_tasks or errors or warnings:
                result_message = []
                
                if imported_tasks:
                    result_message.append(f"Successfully imported {len(imported_tasks)} tasks.")
                    
                    if created_projects:
                        result_message.append(f"\nCreated {len(created_projects)} new projects:")
                        for proj in set(created_projects):
                            result_message.append(f"  • {proj}")
                    
                    if imported_images:
                        result_message.append(f"\nImported images:")
                        for img_info in imported_images:
                            result_message.append(f"  • {img_info}")
                
                if warnings:
                    result_message.append(f"\n{len(warnings)} warning(s):")
                    for warning in warnings[:10]:
                        result_message.append(f"  • {warning}")
                    if len(warnings) > 10:
                        result_message.append(f"  • ... and {len(warnings) - 10} more warnings")

                if errors:
                    result_message.append(f"\n{len(errors)} errors occurred:")
                    for error in errors[:10]:  # Show first 10 errors
                        result_message.append(f"  • {error}")
                    if len(errors) > 10:
                        result_message.append(f"  • ... and {len(errors) - 10} more errors")
                
                message_type = QMessageBox.Icon.Warning if errors or warnings else QMessageBox.Icon.Information
                msg_box = QMessageBox(message_type, "Import Results", "\n".join(result_message), parent=self)
                msg_box.exec()
                
                # Refresh UI to show imported tasks
                if imported_tasks:
                    self.project_model.reload()
                    
                    # If we have current filters/search, reapply them to show matching imported tasks
                    if self._is_searching():
                        self.apply_search_filter()
                    else:
                        # If no search active, try to select a project that has new tasks
                        if created_projects:
                            # Find and select the first created project
                            first_project = created_projects[0]
                            for row in range(self.project_model.rowCount()):
                                proj_id = self.project_model.project_id_at(row)
                                if proj_id:
                                    proj_data = next((p for p in self.project_model.rows if p['id'] == proj_id), None)
                                    if proj_data and proj_data['title'] == first_project:
                                        self.project_view.setCurrentIndex(self.project_model.index(row, 0))
                                        break
            else:
                QMessageBox.information(self, "Import Complete", "No tasks were imported. Please check the CSV file format.")
                
        except Exception as e:
            QMessageBox.critical(self, "Import Error", f"Failed to import tasks from CSV:\n{str(e)}")

    def remove_pins_from_incomplete_tasks(self, checked=False, *, skip_confirmation=False):
        """Tools: take every incomplete task out of Focus (asks first; the notice can undo it)."""
        self._clear_flag_everywhere('pinned', skip_confirmation)

    def _clear_flag_everywhere(self, column: str, skip_confirmation: bool):
        condition = "pinned=1" if column == 'pinned' else "COALESCE(TRIM(due_date), '') != ''"
        ids = [r['id'] for r in self.db.conn.execute(f"SELECT id FROM tasks WHERE done=0 AND {condition}")]
        what = "in Focus" if column == 'pinned' else "with a due date"
        if not ids:
            self.toast.show_message(f"No incomplete tasks {what}")
            return
        if not skip_confirmation:
            action = "take them out of Focus" if column == 'pinned' else "remove their due dates"
            reply = QMessageBox.question(
                self, "Remove Focus" if column == 'pinned' else "Remove Due Dates",
                f"{len(ids)} incomplete task{'s are' if len(ids) != 1 else ' is'} {what}, in all projects.\n\n"
                f"Do you want to {action}? Completed tasks stay as they are.",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No, QMessageBox.StandardButton.No)
            if reply != QMessageBox.StandardButton.Yes:
                return
        snapshot = self.db.snapshot_tasks(ids)
        value = 0 if column == 'pinned' else None
        with self.db.conn:
            self.db.conn.executemany(f"UPDATE tasks SET {column}=?, updated_at=CURRENT_TIMESTAMP WHERE id=?",
                                     [(value, task_id) for task_id in ids])
        self._refresh_lists()
        done = "out of Focus" if column == 'pinned' else "without due dates"
        self._push_undo(f"{len(ids)} task{'s' if len(ids) != 1 else ''} now {done}", snapshot, scope='flags')

    def remove_due_dates_from_incomplete_tasks(self, checked=False, *, skip_confirmation=False):
        """Tools: remove the due date of every incomplete task (asks first; the notice can undo it)."""
        self._clear_flag_everywhere('due_date', skip_confirmation)

    def start_periodic_projects_review(self):
        """Project → Start Review: one click; asks only whether to include hidden projects.
        While a review runs it continues it instead."""
        model = self.project_model
        if model.is_review_active():
            self.open_next_review_project()
            return
        include_hidden = False
        hidden = model.hidden_project_count()
        if hidden:
            box = QMessageBox(QMessageBox.Icon.Question, "Start Review",
                              f"Include the {hidden} hidden project{'s' if hidden != 1 else ''} in this review?",
                              QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No
                              | QMessageBox.StandardButton.Cancel, self)
            box.button(QMessageBox.StandardButton.Yes).setText("Include hidden")
            box.button(QMessageBox.StandardButton.No).setText("Listed projects only")
            box.setDefaultButton(QMessageBox.StandardButton.No)
            answer = box.exec()
            if answer == QMessageBox.StandardButton.Cancel:
                return
            include_hidden = answer == QMessageBox.StandardButton.Yes
        scope = model.review_scope(include_hidden)
        if not scope:
            self.toast.show_message("No projects to review")
            return
        views = ['today', 'waiting', 'triage'] if _settings_bool(self._settings, 'Preferences/ReviewViewsFirst', False) else []
        self._review_view_open = None
        model.start_review_cycle(scope, include_hidden, views)
        # Open the first step; nothing counts as reviewed yet (Next would mark the open project)
        if views:
            self._review_view_open = views[0]
            self.set_current_view(('smart', views[0]))
            model.reviewChanged.emit()
        else:
            remaining = model.review_remaining_ids()
            if remaining:
                self.set_current_view(('project', remaining[0]))

    def end_review(self):
        """End button / Project → End Review: stop now, with a summary and Undo."""
        model = self.project_model
        if not model.is_review_active():
            return
        self._leave_review_visit()  # the step on screen counts if auto-mark applies
        if not model.is_review_active():
            return  # that was the last step: the review finished on its own
        summary = self.db.review_summary()
        state = model.end_review_cycle()
        self.toast.show_message(self._review_summary_text("Review ended", summary),
                                undo=lambda st=state: model.restore_review_cycle(st))

    def _review_summary_text(self, head: str, summary: Dict[str, int]) -> str:
        parts = [f"reviewed {summary['reviewed']} of {summary['total']}"]
        if summary['completed']:
            parts.append(f"completed {summary['completed']} task{'s' if summary['completed'] != 1 else ''}")
        cleared = []
        if summary['focus_cleared']:
            cleared.append(f"{summary['focus_cleared']} Focus")
        if summary['due_cleared']:
            cleared.append(f"{summary['due_cleared']} due date{'s' if summary['due_cleared'] != 1 else ''}")
        if cleared:
            parts.append("cleared " + " and ".join(cleared))
        return f"{head} · " + " · ".join(parts)

    def skip_review_project(self, project_id: Optional[int] = None):
        """Leave a project for later in this review and open the next one."""
        if project_id is None and self.current_view[0] == 'project':
            project_id = self.current_view[1]
        if project_id is None or not self.project_model.is_project_in_review(project_id):
            return
        self._review_visit = None  # skipping is not reviewing
        self.project_model.skip_review(project_id)
        remaining = [pid for pid in self.project_model.review_remaining_ids() if pid != project_id]
        if self.current_view == ('project', project_id) and remaining:
            self.set_current_view(('project', remaining[0]))

    def _process_import_images(self, html_content: str, images_dir: str) -> Tuple[str, int]:
        """Process HTML content to import images from export folder"""
        if not html_content or not os.path.exists(images_dir):
            return html_content, 0
        
        import re
        
        # Find all image tags that reference the images folder
        img_pattern = rf'<img[^>]*src=["\']\./{re.escape(os.path.basename(images_dir))}/([^"\']+)["\'][^>]*>'
        matches = re.findall(img_pattern, html_content, re.IGNORECASE)
        
        if not matches:
            return html_content, 0
        
        modified_html = html_content
        images_imported_count = 0
        
        for img_filename in matches:
            try:
                # Source image path
                src_img_path = os.path.join(images_dir, img_filename)
                
                if not os.path.exists(src_img_path):
                    continue
                
                # Generate unique filename in attachments directory
                name, ext = os.path.splitext(img_filename)
                counter = 1
                new_filename = img_filename
                
                while os.path.exists(os.path.join(self.attach_dir, new_filename)):
                    new_filename = f"{name}_{counter}{ext}"
                    counter += 1
                
                # Copy image to attachments directory
                dest_path = os.path.join(self.attach_dir, new_filename)
                shutil.copy2(src_img_path, dest_path)
                images_imported_count += 1
                
                # Update HTML to reference the new location
                old_src_pattern = re.escape(f"./{os.path.basename(images_dir)}/{img_filename}")
                folder_name = os.path.basename(self.attach_dir)
                new_src = f"{folder_name}/{new_filename}"
                modified_html = re.sub(
                    f'(<img[^>]*src=["\']){old_src_pattern}(["\'][^>]*>)',
                    f'\\1{new_src}\\2',
                    modified_html,
                    flags=re.IGNORECASE
                )
                
            except Exception as e:
                print(f"Error importing image {img_filename}: {e}")
                continue
        
        return modified_html, images_imported_count

    def _get_filtered_tasks_data(self) -> List[dict]:
        """Get currently filtered tasks with all relevant data for CSV export"""
        filtered_tasks_data = []
        
        if self._is_searching():
            # Use search results - maintain order from project model
            for project_row in self.project_model.rows:
                project_id = project_row['id']
                if project_id in self._search_results_by_project:
                    tasks = self._search_results_by_project[project_id]
                    if tasks:  # Only include projects with visible tasks
                        project_title = project_row['title']
                        for task in tasks:
                            task_data = self._extract_task_data(task, project_title)
                            filtered_tasks_data.append(task_data)
        else:
            # Use current project/task model state - maintain project list order
            for project_row in self.project_model.rows:
                project_id = project_row['id']
                project_title = project_row['title']
                
                # Get tasks for this project using current filters
                tasks = self._export_tasks_for_project(project_id)
                for task in tasks:
                    task_data = self._extract_task_data(task, project_title)
                    filtered_tasks_data.append(task_data)
        
        return filtered_tasks_data

    def _extract_task_data(self, task, project_title: str) -> dict:
        """Extract all relevant data from a task for CSV export"""
        try:
            stakeholders = self.db.get_stakeholders_for_task(task['id'])
            stakeholders_text = ', '.join(stakeholders) if stakeholders else ''
        except Exception:
            stakeholders_text = ''
        
        try:
            notes_html, notes_plain = self.db.get_note(task['id'])
        except Exception:
            notes_html, notes_plain = '', ''
        
        # Helper function to safely get values from sqlite3.Row objects
        def safe_get(row, key, default=None):
            try:
                return row[key] if key in row.keys() else default
            except (KeyError, IndexError):
                return default
        
        return {
            'project_title': project_title,
            'task_title': task['title'],
            'pinned': 'True' if bool(safe_get(task, 'pinned', 0)) else 'False',
            'waiting': 'True' if bool(safe_get(task, 'waiting', 0)) else 'False',
            'marked_mom': 'True' if bool(safe_get(task, 'marked_mom', 0)) else 'False',
            'done': 'True' if bool(safe_get(task, 'done', 0)) else 'False',
            'due_date': safe_get(task, 'due_date', ''),
            'recurring': 'True' if bool(safe_get(task, 'recurring', 0)) else 'False',
            'recurrence_rule': safe_get(task, 'recurrence_rule', '') or '',
            'recurrence_start_at': safe_get(task, 'recurrence_start_at', '') or '',
            'recurrence_next_at': safe_get(task, 'recurrence_next_at', '') or '',
            'recurrence_last_at': safe_get(task, 'recurrence_last_at', '') or '',
            'stakeholders': stakeholders_text,
            'notes_html': notes_html or '',  # Include HTML content for image processing
            'notes_plain': (notes_plain or '').replace('\n', ' | '),  # Replace newlines for CSV
            'created_at': safe_get(task, 'created_at', ''),
            'updated_at': safe_get(task, 'updated_at', '')
        }

    def show_about(self):
        """Help → About: version, file locations and versions for bug reports."""
        locations = [("Database", os.path.abspath(self.db.path)),
                     ("Settings", os.path.abspath(SETTINGS_PATH)),
                     ("Attachments", os.path.abspath(get_attach_dir(self.db.path)))]
        AboutDialog(APP_DIR, locations, self).exec()

    def navigate_to_task(self, project_id: int, task_id: int):
        """Navigate to a specific task in the main window"""
        # Close any active editors so clearing filters isn't deferred
        self._close_active_editors()

        # Clear search terms and filters before navigating
        self._suppress_search_restore = True
        self.clear_all_filters()

        self._navigate_to_task_after_filters(project_id, task_id)

    def _navigate_to_task_after_filters(self, project_id: int, task_id: int):
        """Navigate to a task once search/filters have been cleared."""
        if self._applying_search:
            QTimer.singleShot(25, lambda: self._navigate_to_task_after_filters(project_id, task_id))
            return

        if getattr(self, '_suppress_search_restore', False):
            self._suppress_search_restore = False

        # Opening the project also opens HIDDEN or clears the name filter when needed
        self.project_model.reload()
        self._show_project(project_id)
        if self.current_view == ('project', project_id):
            QTimer.singleShot(100, lambda: self._select_task_after_project_change(task_id))

    def _select_moved_task(self, task_id: int):
        """Helper to select a moved task in the target project."""
        try:
            for row in range(self.task_model.rowCount()):
                if self.task_model.task_id_at(row) == task_id:
                    self.task_view.selectRow(row)
                    self.task_view.scrollTo(self.task_model.index(row, 0), QTableView.ScrollHint.EnsureVisible)
                    break
        except Exception as e:
            print(f"Error selecting moved task: {e}")

    def _select_task_after_project_change(self, task_id: int):
        """Helper to select task after project selection has been processed"""
        for row in range(self.task_model.rowCount()):
            if self.task_model.task_id_at(row) == task_id:
                self.task_view.selectRow(row)
                self.task_view.scrollTo(self.task_model.index(row, 0))
                break

    def open_project_switcher(self):
        """Ctrl+P: pick any project (listed or hidden) by typing part of its name."""
        if not hasattr(self, '_project_switcher'):
            self._project_switcher = ProjectSwitcher(self)
            self._project_switcher.projectChosen.connect(self._go_to_project)
        self._project_switcher.open(self._switcher_projects())

    def _switcher_projects(self) -> List[dict]:
        """Every project, listed and hidden, for the project pickers."""
        hidden_ids = {r['id'] for r in self.project_model.hidden_rows}
        projects = []
        for row in self.db.list_projects_with_stats():
            project = dict(row)
            project['hidden_in_sidebar'] = project['id'] in hidden_ids
            projects.append(project)
        return projects

    # ----- quick-add target in smart views
    def _quick_add_target_id(self) -> Optional[int]:
        """Project the quick-add row saves to: the open project, or in the smart views the project
        picked this session, else the last project opened (the same for every smart view)."""
        if self.current_view[0] == 'smart' and self._quick_add_pick is not None:
            if self._project_title(self._quick_add_pick) is not None:
                return self._quick_add_pick
            self._quick_add_pick = None  # deleted since
        return self._quick_add_project_id()

    def _project_title(self, project_id: Optional[int]) -> Optional[str]:
        if project_id is None:
            return None
        model = self.project_model
        row = model.row_for_project(project_id)
        if row >= 0:
            return model.rows[row]['title']
        for r in model.hidden_rows:
            if r['id'] == project_id:
                return r['title']
        found = self.db.conn.execute("SELECT title FROM projects WHERE id=?", (project_id,)).fetchone()
        return found['title'] if found else None

    def _sync_quick_add_target(self):
        """Show where a quick-added task will go (chip in smart views, placeholder in projects)."""
        if not hasattr(self, 'quick_add'):
            return
        kind, value = self.current_view
        if kind == 'project':
            self.quick_add.set_target(self._project_title(value) or "this project")
            return
        title = self._project_title(self._quick_add_target_id())
        if title is None:
            self.quick_add.set_target("this view")
        else:
            self.quick_add.set_target(title, chip=True, chosen=self._quick_add_pick is not None)

    def _choose_quick_add_target(self):
        """Chip click / Ctrl+P in the quick-add row: pick the project for new tasks."""
        if self.current_view[0] != 'smart':
            return
        if not hasattr(self, '_target_picker'):
            self._target_picker = ProjectSwitcher(self)
            self._target_picker.projectChosen.connect(self._set_quick_add_target)
        self._target_picker.open(self._switcher_projects(), placeholder="Add new tasks to…",
                                 anchor=self.quick_add.target_chip, current_id=self._quick_add_target_id())

    def _set_quick_add_target(self, project_id: int):
        if self.current_view[0] != 'smart':
            return
        self._quick_add_pick = project_id
        self._sync_quick_add_target()
        self.quick_add.focus_input()

    def _reset_quick_add_target(self):
        """Chip → Use the last opened project: drop the picked project (in every smart view)."""
        self._quick_add_pick = None
        self._sync_quick_add_target()

    def _go_to_project(self, project_id: int):
        self._show_project(project_id)
        self._focus_task_list()

    def _show_project(self, project_id: int):
        """Open a project wherever it is listed: in HIDDEN, or behind the project name filter."""
        model = self.project_model
        listed = model.row_for_project(project_id) >= 0 or any(r['id'] == project_id for r in model.hidden_rows)
        if not listed and model.project_name_filter:
            # The name filter hides it: clear the filter first
            self.sidebar.filter_button.setChecked(False)
            self._project_filter_timer.stop()
            self._apply_project_filter()
        self.set_current_view(('project', project_id))

    def _focus_task_list(self):
        rows = self.task_view.visible_rows()
        if rows and not self.task_view.currentIndex().isValid():
            self._select_task_row(rows[0])
        self.task_view.setFocus()

    def _on_review_changed(self):
        model = self.project_model
        if not model.is_review_active():
            self.sidebar.set_review_status(None)
            self._sync_review_panel()
            return
        remaining = model.review_remaining_ids()
        views = self.db.review_views_pending()
        if not remaining and not views:
            # Last project reviewed: finish with a summary (no undo: nothing was given up)
            summary = self.db.review_summary()
            QTimer.singleShot(0, model.end_review_cycle)
            self.toast.show_message(self._review_summary_text("Review complete", summary))
            return
        if views:
            total = 3
            step = total - len(views) + 1
            self.sidebar.set_review_status(f"REVIEW · {SMART_VIEW_TITLES.get(views[0], views[0])}",
                                           f"Step {step} of {total + 1}: the {SMART_VIEW_TITLES.get(views[0], views[0])} view, "
                                           f"then {len(remaining)} project{'s' if len(remaining) != 1 else ''}")
        else:
            self.sidebar.set_review_status(f"REVIEW · {len(remaining)}",
                                           f"{len(remaining)} project{'s' if len(remaining) != 1 else ''} left to review")
        self._sync_review_panel()

    def open_next_review_project(self):
        """Reviewed → next (Ctrl+Shift+R): finish the open step and open the next one.

        With "views first", the steps start with Due & overdue, Waiting and No due date; then
        each project still to review, least recently reviewed first."""
        model = self.project_model
        if not model.is_review_active():
            return
        views = self.db.review_views_pending()
        if views:
            if getattr(self, '_review_view_open', None) == views[0] and self.current_view == ('smart', views[0]):
                self._review_visit = None
                self.db.pop_review_view()  # this view is done
                views = views[1:]
            if views:
                self._review_view_open = views[0]
                self.set_current_view(('smart', views[0]))
                model.reviewChanged.emit()
                return
            model.reviewChanged.emit()
        remaining = model.review_remaining_ids()
        current = self.current_view[1] if self.current_view[0] == 'project' else None
        if current in remaining:
            position = remaining.index(current)
            following = remaining[position + 1:] + remaining[:position]
            self._review_visit = None
            # Marking the last one ends the review (see _on_review_changed)
            self.mark_project_review_completed(current)
            remaining = [pid for pid in following if model.is_project_in_review(pid)]
        if remaining:
            self.set_current_view(('project', remaining[0]))

    # ----- "mark reviewed when I move on" (Preferences → Planning)
    REVIEW_VISIT_SECONDS = 3.0

    def _leave_review_visit(self, next_view: Optional[Tuple[str, Any]] = None):
        """Called when the view changes, the review ends or the app closes: the review step
        being left (a project, or the current review view) counts as done if it was looked at
        long enough and auto-mark is on."""
        visit = getattr(self, '_review_visit', None)
        if visit is None or (next_view is not None and tuple(next_view) == visit[0]):
            return
        self._review_visit = None
        (kind, value), opened_at = visit
        if not self._project_review_auto_mark_enabled() or time.monotonic() - opened_at < self.REVIEW_VISIT_SECONDS:
            return
        if kind == 'project' and self.project_model.is_project_in_review(value):
            self.mark_project_review_completed(value)
        elif kind == 'smart' and self.project_model.is_review_active():
            views = self.db.review_views_pending()
            if views and views[0] == value:
                self.db.pop_review_view()
                self.project_model.reviewChanged.emit()

    def _is_review_step(self, view: Tuple[str, Any]) -> bool:
        kind, value = view
        if kind == 'project':
            return self.project_model.is_project_in_review(value)
        if kind == 'smart' and self.project_model.is_review_active():
            views = self.db.review_views_pending()
            return bool(views) and views[0] == value
        return False

    def _note_review_visit(self):
        view = tuple(self.current_view)
        if self._is_review_step(view):
            visit = getattr(self, '_review_visit', None)
            if visit is None or visit[0] != view:
                self._review_visit = (view, time.monotonic())
        else:
            self._review_visit = None

    def _sync_review_panel(self):
        """Show the review panel while the open project is still to review."""
        panel = getattr(self, 'review_panel', None)
        if panel is None:
            return
        kind, value = self.current_view
        show = not self._is_searching() and self._is_review_step((kind, value))
        if show and kind == 'project':
            panel.set_stats(self.db.project_review_stats(value), len(self.project_model.review_remaining_ids()))
        elif show:
            views = self.db.review_views_pending()
            panel.set_view_step(SMART_VIEW_TITLES.get(value, str(value)), 3 - len(views) + 1, 3,
                                len(self.project_model.review_remaining_ids()))
        panel.setVisible(show)

    def _review_hide_project(self):
        """Review panel → Hide project: count it as reviewed, move on, then hide it."""
        if self.current_view[0] != 'project':
            return
        project_id = self.current_view[1]
        self.open_next_review_project()
        self.toggle_project_visibility(project_id)

    def _select_review_tasks(self, kind: str):
        """Review panel chip: select the project's open tasks that are overdue / waiting / ..."""
        today = datetime.date.today().isoformat()

        def matches(task) -> bool:
            if bool(task['done']):
                return False
            due = str(task['due_date'] or '').strip()
            if kind == 'overdue':
                return bool(due) and due[:10] < today
            if kind == 'waiting':
                return bool(task['waiting'])
            if kind == 'undated':
                return not due
            if kind == 'focus':
                return bool(task['pinned'])
            return False

        rows = [row for row in range(self.task_model.rowCount())
                if self.task_model.task_id_at(row) is not None and matches(self.task_model.rows[row])]
        if not rows:
            return
        selection = self.task_view.selectionModel()
        selection.clearSelection()
        self.task_view.setCurrentIndex(self.task_model.index(rows[0], 0))
        for row in rows:
            selection.select(self.task_model.index(row, 0),
                             QItemSelectionModel.SelectionFlag.Select | QItemSelectionModel.SelectionFlag.Rows)
        self.task_view.scrollTo(self.task_model.index(rows[0], 0))
        self.task_view.setFocus()

    def _clear_project_flag(self, column: str):
        """Review panel: take the open project's open tasks out of Focus, or remove their due dates."""
        if self.current_view[0] != 'project':
            return
        project_id = self.current_view[1]
        condition = "pinned=1" if column == 'pinned' else "COALESCE(TRIM(due_date), '') != ''"
        ids = [r['id'] for r in self.db.conn.execute(
            f"SELECT id FROM tasks WHERE project_id=? AND done=0 AND {condition}", (project_id,))]
        if not ids:
            return
        snapshot = self.db.snapshot_tasks(ids)
        value = 0 if column == 'pinned' else None
        with self.db.conn:
            self.db.conn.executemany(f"UPDATE tasks SET {column}=?, updated_at=CURRENT_TIMESTAMP WHERE id=?",
                                     [(value, task_id) for task_id in ids])
        counter = 'review_focus_cleared' if column == 'pinned' else 'review_due_cleared'
        self.db.add_review_count(counter, len(ids))
        self._refresh_lists()
        what = "from Focus" if column == 'pinned' else "of their due dates"
        self._push_undo(f"Cleared {len(ids)} task{'s' if len(ids) != 1 else ''} {what}", snapshot, scope='flags',
                        after_restore=lambda c=counter, n=len(ids): self.db.add_review_count(c, -n))

    def mark_project_review_completed(self, project_id: int):
        """Clear the review highlight for a single project."""
        if not self.project_model.is_review_active():
            return
        self.project_model.mark_review_completed(project_id)

    def show_projects_header_context_menu(self, position):
        """Right-click on the PROJECTS header: list-level actions."""
        self._exec_project_list_menu(self.projects_header.mapToGlobal(position))

    def _exec_project_list_menu(self, global_pos: QPoint):
        """Actions on the project list itself (header or empty space right-click)."""
        menu = QMenu(self)
        new_action = menu.addAction(lucide_icon("plus", 15, T.NEUTRAL_700), "New project", self.add_project)
        new_action.setShortcut(QKeySequence("Ctrl+Shift+N"))
        go_action = menu.addAction(lucide_icon("search", 15, T.NEUTRAL_700), "Go to project…", self.open_project_switcher)
        go_action.setShortcut(QKeySequence("Ctrl+P"))
        menu.addAction(lucide_icon("search", 15, T.NEUTRAL_700), "Filter projects by name",
                       lambda: self.sidebar.filter_button.setChecked(True))
        menu.exec(global_pos)

    def _reclaim_project_list_context_menu(self):
        """Restore app ownership of the project list context menu after addons hook into it."""
        try:
            self.project_view.customContextMenuRequested.disconnect()
        except TypeError:
            pass
        self.project_view.customContextMenuRequested.connect(self._show_combined_project_context_menu)

    def _resolve_project_context_request(self, position) -> Optional[Tuple[QModelIndex, int, QPoint]]:
        """The project under the cursor of a project-list right-click (None on empty space)."""
        index = self.project_view.indexAt(position)
        if not index.isValid():
            return None
        project_id = self.project_model.project_id_at(index.row())
        if project_id is None:
            return None
        return index, project_id, self.project_view.viewport().mapToGlobal(position)

    def _build_project_menu(self, menu: QMenu, project_id: Optional[int], *, label: bool = False,
                            title_case: bool = False):
        """The project actions, the same in the sidebar context menu, the list header's menu
        button and the menu bar (title_case). project_id None (no project open) adds them disabled."""
        def text(sentence: str, title: str) -> str:
            return title if title_case else sentence

        def add(icon_name: Optional[str], caption: str, slot, shortcut: Optional[str] = None):
            action = menu.addAction(caption)
            if icon_name:
                action.setIcon(lucide_icon(icon_name, 15, T.NEUTRAL_700))
            if shortcut:
                action.setShortcut(QKeySequence(shortcut))  # shown as a hint; the menu is short-lived
            action.setEnabled(project_id is not None)
            if project_id is not None:
                action.triggered.connect(lambda _checked=False: slot())
            return action

        title = self._project_title(project_id) if project_id is not None else None
        if label:
            menu_section_label(menu, title or "No project open")

        if project_id is not None and self.project_model.is_project_in_review(project_id):
            review_action = menu.addAction(self._make_green_check_icon(), text("Mark reviewed", "Mark Reviewed"))
            review_action.triggered.connect(lambda _checked=False, pid=project_id: self.mark_project_review_completed(pid))
            skip_action = menu.addAction(text("Skip for now", "Skip for Now"))
            skip_action.triggered.connect(lambda _checked=False, pid=project_id: self.skip_review_project(pid))
            menu.addSeparator()

        pid = project_id
        row = self.db.conn.execute("SELECT starred, hidden, manual_sort_enabled FROM projects WHERE id=?",
                                   (pid,)).fetchone() if pid is not None else None
        pinned, hidden, manual = (bool(row['starred']), bool(row['hidden']), bool(row['manual_sort_enabled'])) \
            if row else (False, False, False)

        add("plus", text("New task in project", "New Task in Project"), lambda: self._new_task_in_project(pid))
        add("edit", text("Rename…", "Rename…"), lambda: self.rename_project(pid), "F2")
        add("pin", text("Unpin project", "Unpin Project") if pinned else text("Pin to top", "Pin to Top"),
            lambda: self.set_project_starred(pid, not pinned))
        add("eyeoff", text("Unhide project", "Unhide Project") if hidden else text("Hide project", "Hide Project"),
            lambda: self.toggle_project_visibility(pid))
        order = add("grip", text("Manual task order", "Manual Task Order"), lambda: self.set_project_manual_sort(pid, not manual))
        order.setCheckable(True)
        order.setChecked(manual)
        add("copy", text("Copy open tasks", "Copy Open Tasks"), lambda: self.copy_project_tasks_as_text(pid))

        if project_id is not None and self._feature_links_enabled():
            menu.addSeparator()
            if self._project_feature_links.get(project_id):
                add("link", text("Visit feature link", "Visit Feature Link"), lambda: self._visit_project_feature_link(pid))
            add(None, text("Edit feature link…", "Edit Feature Link…"), lambda: self._edit_project_feature_link(pid))

        menu.addSeparator()
        add("trash", text("Delete project…", "Delete Project…"), lambda: self.remove_selected_project(pid), "Del")

    def set_project_manual_sort(self, project_id: int, manual: bool):
        """Auto / manual task order for a project (the list header's Auto | Manual control)."""
        if self.current_view == ('project', project_id) and not self._is_searching():
            # The open project: the header control applies it and reloads the list
            self.sort_order_combo.setCurrentIndex(1 if manual else 0)
        else:
            self.db.set_manual_sort_enabled(project_id, manual)
        self.toast.show_message(f"Manual task order {'on' if manual else 'off'} for {self._project_title(project_id)}")

    def set_project_starred(self, project_id: int, starred: bool):
        """Pin a project to the top of PROJECTS (projects.starred), or unpin it."""
        self.db.set_project_starred(project_id, starred)
        self.project_model.reload()
        if self.current_view[0] == 'project':
            self._select_project_silently(self.current_view[1])
        title = self._project_title(project_id)
        self.toast.show_message(f"Pinned {title} to the top" if starred else f"Unpinned {title}")

    def _new_task_in_project(self, project_id: int):
        if self.current_view != ('project', project_id):
            self.set_current_view(('project', project_id))
        self.add_task()

    def _show_combined_project_context_menu(self, position):
        """Right-click on the project list: the project's menu, without opening the project."""
        resolved = self._resolve_project_context_request(position)
        if resolved is None:
            self._exec_project_list_menu(self.project_view.viewport().mapToGlobal(position))
            return
        index, project_id, global_pos = resolved
        self._exec_project_context_menu(self.project_view, index.row(), project_id, global_pos)

    def _exec_project_context_menu(self, view: QListView, row: int, project_id: int, global_pos: QPoint):
        menu = QMenu(self)
        self._build_project_menu(menu, project_id, label=True)
        view.context_row = row  # keep the row highlighted while its menu is open
        view.viewport().update()
        try:
            menu.exec(global_pos)
        finally:
            view.context_row = -1
            view.viewport().update()

    def show_project_context_menu(self, position):
        """Compatibility wrapper for project list context-menu handling."""
        self._show_combined_project_context_menu(position)

    def _is_valid_feature_link(self, url_text: str) -> bool:
        """Validate that the provided URL is an http(s) link with a host."""
        if not url_text:
            return False
        url = QUrl(url_text.strip())
        return url.isValid() and url.scheme().lower() in ("http", "https") and bool(url.host())

    def _edit_project_feature_link(self, project_id: int):
        """Open a dialog to edit the feature link for a project."""
        project_title = ""
        try:
            row = self.project_model.row_for_project(project_id)
            if 0 <= row < len(self.project_model.rows):
                project_title = self.project_model.rows[row].get('title', '')
        except Exception:
            project_title = ""

        dialog = QDialog(self)
        dialog.setWindowTitle("Edit Links")
        dialog.setModal(True)
        dialog.setMinimumWidth(420)

        layout = QVBoxLayout(dialog)
        layout.setContentsMargins(14, 14, 14, 14)
        layout.setSpacing(10)

        heading = QLabel("Feature link")
        heading.setStyleSheet("font-size: 15px; font-weight: 600; color: #1d1f20;")
        layout.addWidget(heading)

        if project_title:
            subtitle = QLabel(f"Add a link for '{project_title}'.")
        else:
            subtitle = QLabel("Add a link for this project.")
        subtitle.setStyleSheet("color: #5d5d60;")
        subtitle.setWordWrap(True)
        layout.addWidget(subtitle)

        link_edit = QLineEdit()
        link_edit.setPlaceholderText("https://example.com/feature")
        link_edit.setText(self._project_feature_links.get(project_id, ""))
        link_edit.setMinimumHeight(32)
        link_edit.setStyleSheet("""
            QLineEdit {
                padding: 6px 8px;
                border: 1px solid #d6d6d7;
                border-radius: 0;
                font-size: 13px;
            }
            QLineEdit:focus {
                border-color: #5980a6;
            }
        """)
        layout.addWidget(link_edit)

        button_box = QDialogButtonBox()

        clear_btn = button_box.addButton("Clear", QDialogButtonBox.ButtonRole.ActionRole)
        clear_btn.setStyleSheet("""
            QPushButton {
                background-color: #f2f2f3;
                border: 1px solid #d6d6d7;
                border-radius: 0;
                padding: 6px 12px;
                color: #1d2d3d;
            }
            QPushButton:hover {
                background-color: #e3e3e4;
            }
        """)

        save_btn = button_box.addButton("Save", QDialogButtonBox.ButtonRole.AcceptRole)
        save_btn.setStyleSheet("""
            QPushButton {
                background-color: #5980a6;
                border: 1px solid #5980a6;
                border-radius: 0;
                padding: 6px 16px;
                color: #f2f2f3;
                font-weight: 500;
            }
            QPushButton:hover {
                background-color: #597ea3;
            }
        """)

        cancel_btn = button_box.addButton(QDialogButtonBox.StandardButton.Cancel)
        cancel_btn.setStyleSheet("""
            QPushButton {
                background-color: #f2f2f3;
                border: 1px solid #d6d6d7;
                border-radius: 0;
                padding: 6px 16px;
                color: #1d1f20;
            }
            QPushButton:hover {
                background-color: #e3e3e4;
            }
        """)

        layout.addWidget(button_box)

        def save_link():
            url_text = link_edit.text().strip()
            if not url_text:
                self._set_project_feature_link(project_id, None)
                dialog.accept()
                return
            if not self._is_valid_feature_link(url_text):
                QMessageBox.warning(self, "Invalid Link", "Enter a valid http(s) link for this project.")
                return
            self._set_project_feature_link(project_id, url_text)
            dialog.accept()
            current_idx = self.project_view.currentIndex()
            current_pid = self.project_model.project_id_at(current_idx.row()) if current_idx.isValid() else None
            if current_pid == project_id:
                self._update_feature_link_for_project(project_id)

        def clear_link():
            link_edit.clear()
            self._set_project_feature_link(project_id, None)
            dialog.accept()
            current_idx = self.project_view.currentIndex()
            current_pid = self.project_model.project_id_at(current_idx.row()) if current_idx.isValid() else None
            if current_pid == project_id:
                self._update_feature_link_for_project(project_id)

        link_edit.returnPressed.connect(save_link)
        save_btn.clicked.connect(save_link)
        clear_btn.clicked.connect(clear_link)
        button_box.rejected.connect(dialog.reject)

        dialog.exec()

    def _visit_project_feature_link(self, project_id: int):
        """Open the stored feature link in the system browser."""
        url_text = self._project_feature_links.get(project_id)
        if not url_text:
            QMessageBox.information(self, "Feature Link", "No feature link set for this project.")
            return
        if not self._is_valid_feature_link(url_text):
            QMessageBox.warning(self, "Feature Link", "Stored feature link is not a valid http(s) URL.")
            return
        url = QUrl(url_text)
        if not QDesktopServices.openUrl(url):
            QMessageBox.warning(self, "Feature Link", "Could not open the link in the default browser.")

    def show_task_context_menu(self, position):
        """Show context menu for task view."""
        index = self.task_view.indexAt(position)
        if not self._apply_task_context_menu_selection(index):
            return

        selected_rows = []
        selection_model = self.task_view.selectionModel()
        if selection_model:
            selected_indexes = selection_model.selectedRows()
            selected_rows = [idx.row() for idx in selected_indexes if idx.isValid()]
        
        if not selected_rows:
            return
        
        # Create context menu
        from PyQt6.QtWidgets import QMenu
        context_menu = QMenu(self)
        
        # Add task-specific actions
        context_menu.addAction(self.act_mark_complete)
        context_menu.addSeparator()
        
        # Add Snooze submenu with icon respecting preferences
        snooze_menu = context_menu.addMenu("Due date")
        snooze_menu.setIcon(self._make_snooze_icon())
        self._populate_task_snooze_menu(snooze_menu)
        snooze_menu.addSeparator()
        snooze_menu.addAction(self.act_pick_due)
        # QAction.triggered passes a bool; accept it to avoid TypeError in lambda
        snooze_menu.addAction("Remove due date", lambda _checked=False: self.snooze_selected_tasks("clear"))
        
        context_menu.addSeparator()
        context_menu.addAction(self.act_recurrence)
        context_menu.addSeparator()
        context_menu.addAction(self.act_move_task)
        context_menu.addAction(self.act_duplicate_tasks)
        context_menu.addSeparator()
        context_menu.addAction(self.act_mark_mom)
        context_menu.addSeparator()
        context_menu.addAction(self.act_remove_task)
        
        # Show the context menu
        global_position = self.task_view.viewport().mapToGlobal(position)
        context_menu.exec(global_position)

    def _apply_task_context_menu_selection(self, clicked_index: QModelIndex) -> bool:
        """Align the current task selection with the row that opened the context menu."""
        if not clicked_index.isValid():
            return False

        selection_model = self.task_view.selectionModel()
        if selection_model is None:
            return False

        target_index = self.task_model.index(clicked_index.row(), TaskTableModel.COL_TITLE)
        if not target_index.isValid():
            return False

        clicked_row_selected = any(idx.row() == clicked_index.row() for idx in selection_model.selectedRows())
        if clicked_row_selected:
            selection_model.setCurrentIndex(target_index, QItemSelectionModel.SelectionFlag.NoUpdate)
        else:
            selection_model.select(
                target_index,
                QItemSelectionModel.SelectionFlag.ClearAndSelect | QItemSelectionModel.SelectionFlag.Rows
            )
            selection_model.setCurrentIndex(target_index, QItemSelectionModel.SelectionFlag.NoUpdate)

        self.task_view.scrollTo(target_index, QTableView.ScrollHint.EnsureVisible)
        return True

    def _populate_task_snooze_menu(self, menu: QMenu) -> int:
        """Insert snooze actions based on current preferences."""
        prefs = load_snooze_preferences(self._settings)
        options: List[Tuple[str, str]] = []
        if prefs.get('today_enabled', True):
            options.append(('today', "Today"))
        if prefs.get('later_today_enabled', True):
            options.append(('later_today', format_later_today_label(prefs.get('later_today_hours', 2))))
        if prefs.get('tomorrow_enabled', True):
            options.append(('tomorrow', "Tomorrow"))
        if prefs.get('next_week_enabled', True):
            options.append(('next_week', "Next Week"))
        if prefs.get('weekend_enabled', False):
            options.append(('weekend', get_weekend_option_label()))
        for snooze_type, label in options:
            # QAction.triggered passes a bool; accept it to avoid TypeError
            menu.addAction(label, lambda _checked=False, st=snooze_type: self.snooze_selected_tasks(st))
        return len(options)

    def move_selected_tasks(self, target_project_id: Optional[int] = None):
        """Move the selected task(s) to a different project (asks for the project when none is given)."""
        if isinstance(target_project_id, bool):
            target_project_id = None  # QAction.triggered passes its checked state
        # Get currently selected tasks
        selected_rows = []
        selection_model = self.task_view.selectionModel()
        if selection_model:
            selected_indexes = selection_model.selectedRows()
            selected_rows = sorted({idx.row() for idx in selected_indexes if idx.isValid()})
        
        if not selected_rows:
            QMessageBox.information(self, "Move Tasks", "No tasks selected.")
            return
        
        # Get task details
        tasks_to_move = []
        task_titles = []
        current_project_id = None
        
        try:
            for row in selected_rows:
                task_id = self.task_model.task_id_at(row)
                if task_id and row < len(self.task_model.rows):
                    task_row = self.task_model.rows[row]
                    task_title = task_row['title']
                    project_id = task_row['project_id']
                    
                    tasks_to_move.append({
                        'id': task_id,
                        'title': task_title,
                        'project_id': project_id
                    })
                    task_titles.append(task_title)
                    
                    if current_project_id is None:
                        current_project_id = project_id
            
            if not tasks_to_move:
                QMessageBox.warning(self, "Move Tasks", "No valid tasks found to move.")
                return
            
            if target_project_id is None:
                projects = [p for p in self._switcher_projects() if p['id'] != current_project_id]
                dialog = MoveTasksDialog(projects, task_titles, self._project_title(current_project_id),
                                         self._recent_move_targets, self)
                if dialog.exec() != QDialog.DialogCode.Accepted:
                    return
                if dialog.create_title:
                    target_project_id = self.db.add_project(dialog.create_title)
                    default_manual = self._settings.value('Preferences/DefaultManualSort', 'false') in ('true', '1', 'True')
                    self.db.set_manual_sort_enabled(target_project_id, default_manual)
                    self.project_model.reload()
                else:
                    target_project_id = dialog.selected_project_id
            if not target_project_id or target_project_id == current_project_id:
                return
            
            # Get project names for logging and validation
            current_project_row = self.db.conn.execute(
                "SELECT title FROM projects WHERE id=?", (current_project_id,)
            ).fetchone()
            if not current_project_row:
                QMessageBox.critical(self, "Move Tasks", "Source project no longer exists.")
                return
            current_project_name = current_project_row['title']
            
            target_project_row = self.db.conn.execute(
                "SELECT title FROM projects WHERE id=?", (target_project_id,)
            ).fetchone()
            if not target_project_row:
                QMessageBox.critical(self, "Move Tasks", "Target project no longer exists.")
                return
            target_project_name = target_project_row['title']
            

            
            move_snapshot = self.db.snapshot_tasks([task_info['id'] for task_info in tasks_to_move])
            moved_task_ids = self.db.move_tasks_to_project(
                [task_info['id'] for task_info in tasks_to_move],
                target_project_id
            )
            moved_id_set = set(moved_task_ids)
            moved_count = len(moved_task_ids)
            for task_info in tasks_to_move:
                if task_info['id'] not in moved_id_set:
                    continue
                activity_logger._log_entry(
                    "INFO", "task_moved",
                    id=task_info['id'],
                    title=task_info['title'],
                    from_project=current_project_name,
                    to_project=target_project_name
                )
            
            if moved_count > 0:
                # Stay in the current view: the moved task stays selected when it is still listed
                anchor_row = max(0, self.task_view.currentIndex().row())
                self._refresh_lists(select_task_id=moved_task_ids[0], anchor_row=anchor_row)
                
                self._push_undo(f"Moved to {target_project_name}", move_snapshot, select_task_id=moved_task_ids[0], scope='move')
                # Recent targets come first in the move dialog and the task menu (this session)
                self._recent_move_targets = ([target_project_id] + [pid for pid in self._recent_move_targets
                                                                    if pid != target_project_id])[:3]
            else:
                QMessageBox.warning(self, "Move Tasks", "Failed to move any tasks.")
                
        except Exception as e:
            QMessageBox.critical(self, "Move Tasks", f"Failed to move tasks: {e}")

    def duplicate_selected_tasks(self):
        """Duplicate selected tasks within their respective projects."""
        selection_model = self.task_view.selectionModel()
        selected_rows = []
        if selection_model:
            selected_indexes = selection_model.selectedRows()
            selected_rows = sorted({idx.row() for idx in selected_indexes if idx.isValid()})

        if not selected_rows:
            QMessageBox.information(self, "Duplicate Task(s)", "No tasks selected.")
            return

        task_ids_to_duplicate = []
        for row in selected_rows:
            task_id = self.task_model.task_id_at(row)
            if task_id is not None:
                task_ids_to_duplicate.append(task_id)

        if not task_ids_to_duplicate:
            QMessageBox.warning(self, "Duplicate Task(s)", "Unable to load the selected tasks.")
            return

        new_task_ids = []
        duplicated_count = 0

        try:
            task_ids_for_duplication = list(task_ids_to_duplicate)
            current_project_id = self.task_model.project_id
            if current_project_id is not None and self.db.is_manual_sort_enabled(current_project_id):
                current_order = self.db.ensure_manual_task_order(current_project_id, fallback_to_auto=True)
                order_map = {task_id: position for position, task_id in enumerate(current_order)}
                task_ids_for_duplication.sort(key=lambda task_id: order_map.get(task_id, -1), reverse=True)

            duplicated_by_source: Dict[int, int] = {}
            for task_id in task_ids_for_duplication:
                new_task_id = self.db.duplicate_task(task_id, self.attach_dir)
                duplicated_by_source[task_id] = new_task_id
                new_task_ids.append(new_task_id)
                duplicated_count += 1
            new_task_ids = [duplicated_by_source[task_id] for task_id in task_ids_to_duplicate if task_id in duplicated_by_source]
        except Exception as e:
            QMessageBox.critical(self, "Duplicate Task(s)", f"Failed to duplicate tasks:\n{e}")
            return

        if duplicated_count == 0:
            QMessageBox.information(self, "Duplicate Task(s)", "No tasks were duplicated.")
            return

        # Refresh UI and status
        self.project_model.reload()
        if self._is_searching():
            self.apply_search_filter()
        else:
            self.task_model.reload()
            if new_task_ids:
                last_new_id = new_task_ids[-1]
                QTimer.singleShot(50, lambda tid=last_new_id: self._restore_task_selection(tid))

        self._update_comprehensive_status()

        QMessageBox.information(
            self,
            "Duplicate Task(s)",
            f"Duplicated {duplicated_count} task(s)."
        )

    def pick_due_date_for_selected(self):
        """Task menu → Set Due Date… (Ctrl+D): pick any date for the selected task(s)."""
        selection_model = self.task_view.selectionModel()
        rows = sorted({idx.row() for idx in selection_model.selectedRows() if idx.isValid()}) if selection_model else []
        if not rows:
            return
        dues = [str(self.task_model.rows[r]['due_date'] or '').strip()[:10] for r in rows if r < len(self.task_model.rows)]
        current = QDate.fromString(dues[0], "yyyy-MM-dd") if dues and dues[0] and len(set(dues)) == 1 else None
        dialog = DueDateDialog(self, current=current, task_count=len(rows), can_remove=any(dues))
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        date = dialog.result_date()
        if date is None:
            self.snooze_selected_tasks("clear")
        else:
            self.snooze_selected_tasks("custom", date.toString("yyyy-MM-dd") + " 00:00:00")

    def snooze_selected_tasks(self, snooze_type: str, custom_date: Optional[str] = None):
        """Snooze selected tasks by setting their due date ('custom' uses custom_date)."""
        # Get currently selected tasks
        selected_rows = []
        selection_model = self.task_view.selectionModel()
        if selection_model:
            selected_indexes = selection_model.selectedRows()
            selected_rows = [idx.row() for idx in selected_indexes if idx.isValid()]
        
        if not selected_rows:
            QMessageBox.information(self, "Snooze Tasks", "No tasks selected.")
            return
        
        # Calculate the target date based on snooze type
        from PyQt6.QtCore import QDate
        snooze_prefs = load_snooze_preferences(self._settings)

        if snooze_type == "clear":
            date_str = None
        elif snooze_type == "custom":
            date_str = custom_date
        else:
            date_str = compute_snooze_due_datetime(snooze_type, snooze_prefs.get('later_today_hours', 2))
            if date_str is None:
                QMessageBox.warning(self, "Snooze Tasks", "Unable to calculate the requested snooze time.")
                return
        
        # Update due dates for all selected tasks
        task_count = 0
        try:
            current_index = self.task_view.currentIndex()
            current_task_id = self.task_model.task_id_at(current_index.row()) if current_index.isValid() else None

            self.task_model.linger(self.task_model.task_id_at(row) for row in selected_rows)
            for row in selected_rows:
                task_id = self.task_model.task_id_at(row)
                if task_id:
                    self.db.set_task_due_date(task_id, date_str)
                    task_count += 1

            # Refresh the current task's due date display if selected
            if current_task_id:
                if date_str:
                    date_part = date_str.split()[0]
                    year, month, day = map(int, date_part.split('-'))
                    self.detail_state.due_date = QDate(year, month, day)
                else:
                    self.detail_state.due_date = None

            if task_count:
                self._refresh_after_task_metadata_change(current_task_id)

        except Exception as e:
            QMessageBox.critical(self, "Snooze Tasks", f"Failed to snooze tasks: {e}")

    def add_project(self):
        """Start the inline "New project" editor in the sidebar."""
        if self.splitter.widget(0).isVisible():
            self.sidebar.begin_new_project()
        else:
            self._create_project_from_sidebar("New Project")

    def edit_selected_project(self):
        self.rename_project(self._current_project_id())

    def rename_project(self, project_id: Optional[int]):
        """Start the inline rename of a project's sidebar row (opening HIDDEN if it is there)."""
        if project_id is None:
            return
        model = self.project_model
        row = model.row_for_project(project_id)
        if row < 0 and any(r['id'] == project_id for r in model.hidden_rows):
            self.sidebar.set_hidden_expanded(True)
            self._on_hidden_section_toggled(True, reapply=False)
            row = model.row_for_project(project_id)
        if row < 0:
            return
        view = self.hidden_project_view if model.is_hidden_row(row) else self.project_view
        index = model.index(row, 0)
        view.scrollTo(index)
        view.edit(index)

    def remove_selected_project(self, project_id: Optional[int] = None):
        if not self.notes.flush_pending():
            return
        """Delete a project (default: the open one) after one confirmation; the toast can undo it."""
        if isinstance(project_id, bool):
            project_id = None  # QAction.triggered passes its checked state
        if project_id is None:
            project_id = self._current_project_id()
        row = self.project_model.row_for_project(project_id) if project_id is not None else -1
        if row < 0 and project_id is not None:
            # In the collapsed HIDDEN section: no row in the model, but it can still be deleted
            title = self._project_title(project_id)
        elif row >= 0:
            title = self.project_model.rows[row]['title']
        if project_id is not None and (row >= 0 or title is not None):
            was_open = self.current_view == ('project', project_id)

            try:
                task_count = self.db.conn.execute(
                    "SELECT COUNT(*) FROM tasks WHERE project_id=?",
                    (project_id,)
                ).fetchone()[0]
            except Exception:
                task_count = 0

            # One confirmation: the toast that follows can undo the delete
            message = f"Delete '{title}'"
            if task_count > 0:
                message += f" and its {task_count} task{'s' if task_count != 1 else ''}"
            message += "?\nYou can undo this from the notice that follows."
            confirmation = QMessageBox.question(
                self, "Delete Project", message,
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No
            )
            if confirmation != QMessageBox.StandardButton.Yes:
                return

            # Proceed with deletion (rows are snapshotted so the toast can undo it)
            project_task_ids = [r['id'] for r in self.db.conn.execute("SELECT id FROM tasks WHERE project_id=?", (project_id,))]
            if self.notes.task_id in project_task_ids:
                if self.notes.set_task(None) is False:
                    return
            project_snapshot = self.db.snapshot_tasks(project_task_ids, project_id=project_id)
            removed_feature_link = self._project_feature_links.get(project_id)
            self.db.remove_project(project_id)
            self.project_model.reload()
            if project_id in self._project_feature_links:
                self._project_feature_links.pop(project_id, None)
                self._save_project_feature_links()

            def _restore_feature_link(pid=project_id, link=removed_feature_link):
                if link:
                    self._project_feature_links[pid] = link
                    self._save_project_feature_links()
            if was_open:
                # The removed project was the current view; fall back to Due & overdue
                self.task_model.set_context(None, self.include_done)
                self.set_current_view(('smart', 'today'))
            else:
                self._apply_current_view()
            self._push_undo("Project deleted", project_snapshot, view=('project', project_id), after_restore=_restore_feature_link)

    def add_task(self):
        """Add Task (menu, Ctrl+N): put the cursor in the quick-add row."""
        if self._is_searching():
            self._clear_search_silently()
            self._apply_current_view()
        if not self.splitter.widget(1).isVisible():
            return
        self.quick_add.focus_input()

    def _selected_project_details(self) -> Tuple[Optional[int], str]:
        idx = self.project_view.currentIndex()
        if not idx.isValid():
            return None, ""

        project_id = self.project_model.project_id_at(idx.row())
        if project_id is None:
            return None, ""

        project_title = ""
        if 0 <= idx.row() < len(getattr(self.project_model, 'rows', [])):
            project_title = self.project_model.rows[idx.row()].get('title', '') or ""

        if not project_title:
            try:
                row = self.db.conn.execute("SELECT title FROM projects WHERE id=?", (project_id,)).fetchone()
                project_title = row['title'] if row else ""
            except Exception:
                project_title = ""

        return project_id, project_title

    def open_bulk_task_import_dialog(self):
        dialog = BulkTaskImportDialog(self, self._settings)
        dialog.exec()

    def _select_task_if_visible(self, task_id: int):
        try:
            for row in range(self.task_model.rowCount()):
                if self.task_model.task_id_at(row) == task_id:
                    return self._select_task_row(row)
        except Exception as e:
            print(f"Error selecting imported task: {e}")
        return False

    def _select_task_row(self, row: int) -> bool:
        """Select a visible task row and keep it in view."""
        if not (0 <= row < self.task_model.rowCount()):
            return False
        task_index = self.task_model.index(row, TaskTableModel.COL_TITLE)
        if not task_index.isValid():
            return False
        self.task_view.selectRow(row)
        self.task_view.setCurrentIndex(task_index)
        self.task_view.scrollTo(task_index, QTableView.ScrollHint.EnsureVisible)
        return True

    def _clear_current_task_context(self):
        """Clear task selection and refresh dependent panes and status."""
        self.task_view.clearSelection()
        self.task_view.setCurrentIndex(QModelIndex())
        self.on_task_selected()

    def _restore_task_selection_after_delete(self, preferred_project_id: Optional[int], anchor_row: int):
        """Refresh views after deletion and keep the user anchored to nearby tasks."""
        self._refresh_lists(anchor_row=anchor_row)

    def _refresh_after_task_metadata_change(self, preferred_task_id: Optional[int] = None):
        """Refresh task/project views after a due-date change."""
        if preferred_task_id is None:
            preferred_task_id = self.task_view.current_task_id()
        self._refresh_lists(select_task_id=preferred_task_id)

    def _perform_bulk_task_import(self, task_titles: List[str]) -> bool:
        normalized_titles = [title.strip() for title in task_titles if (title or "").strip()]
        if not normalized_titles:
            QMessageBox.warning(self, "Bulk Import Tasks", "Enter at least one task to import.")
            return False

        project_id, project_title = self._selected_project_details()
        if project_id is None:
            QMessageBox.warning(self, "Bulk Import Tasks", "Select a project before importing tasks.")
            return False

        if self._adding_task:
            return False

        self._adding_task = True
        try:
            self._close_active_editors()
            created_task_ids = self.db.add_tasks_bulk(project_id, normalized_titles)
            if not created_task_ids:
                QMessageBox.information(self, "Bulk Import Tasks", "No tasks were imported.")
                return False

            last_task_id = created_task_ids[-1]

            if self._is_searching():
                self.apply_search_filter()
                QTimer.singleShot(75, lambda tid=last_task_id: self._select_task_if_visible(tid))
            else:
                self.task_model.set_context(project_id, self.include_done, self.pinned_only)
                QTimer.singleShot(25, lambda tid=last_task_id: self._select_task_if_visible(tid))

            self._update_comprehensive_status()
            QMessageBox.information(
                self,
                "Bulk Import Tasks",
                f"Imported {len(created_task_ids)} task(s) into '{project_title or 'Selected Project'}'."
            )
            return True
        except Exception as e:
            print(f"Error importing tasks in bulk: {e}")
            QMessageBox.critical(self, "Bulk Import Tasks", f"Failed to import tasks:\n{e}")
            return False
        finally:
            QTimer.singleShot(100, lambda: setattr(self, '_adding_task', False))
    
    def _select_task_after_search_refresh(self, task_id: int):
        """Helper to select a task after search filter has been refreshed."""
        try:
            # First ensure the correct project is selected for this task
            self._ensure_project_selected_for_task(task_id)
            
            # Then find and select the task
            for row in range(self.task_model.rowCount()):
                if self.task_model.task_id_at(row) == task_id:
                    self._select_task_row(row)
                    
                    # Trigger editing with minimal delay
                    QTimer.singleShot(25, lambda: self._trigger_task_edit_safe(row))
                    break
        except Exception as e:
            print(f"Error selecting task after search refresh: {e}")

    def _add_task_to_model_optimized(self, new_task_id: int, previous_task_id: Optional[int]):
        """Optimized task model update that preserves selection state and enables rapid task addition."""
        try:
            # Temporarily disable defer flag to allow immediate reload
            old_defer_flag = getattr(self.task_model, '_defer_reload_flag', False)
            self.task_model._defer_reload_flag = False
            
            # Store current scroll position
            scrollbar = self.task_view.verticalScrollBar()
            scroll_position = scrollbar.value()
            
            # Perform immediate reload
            self.task_model._perform_reload()
            
            # Restore defer flag
            self.task_model._defer_reload_flag = old_defer_flag
            
            # Find and select the new task immediately
            self._find_and_select_new_task(new_task_id, scroll_position)
                
        except Exception as e:
            print(f"Error in optimized task addition: {e}")
            # Fallback to simple reload
            self.task_model.reload()
            # Try to find new task with fallback method
            QTimer.singleShot(100, lambda: self._find_and_select_new_task(new_task_id, 0))
    
    def _find_and_select_new_task(self, new_task_id: int, fallback_scroll_position: int):
        """Find and select the newly created task, with fallback scroll position."""
        new_task_found = False
        try:
            for i in range(self.task_model.rowCount()):
                if self.task_model.task_id_at(i) == new_task_id:
                    self._select_task_row(i)
                    
                    # Trigger editing with minimal delay
                    QTimer.singleShot(10, lambda row=i: self._trigger_task_edit_safe(row))
                    new_task_found = True
                    break
            
            if not new_task_found:
                # Restore scroll position if task not found
                scrollbar = self.task_view.verticalScrollBar()
                scrollbar.setValue(fallback_scroll_position)
                print(f"Warning: Could not find newly created task with ID {new_task_id}")
                
        except Exception as e:
            print(f"Error finding and selecting new task: {e}")
    
    def _trigger_task_edit_safe(self, row: int):
        """Safely trigger editing of the task at the specified row with validation."""
        try:
            # Validate row is still valid
            if not (0 <= row < self.task_model.rowCount()):
                return
                
            # Get the index for the title column
            index = self.task_model.index(row, TaskTableModel.COL_TITLE)
            if not index.isValid():
                return
            
            # Ensure the view is still focused on this row
            current_index = self.task_view.currentIndex()
            if current_index.row() != row:
                # Re-select the correct row
                self.task_view.setCurrentIndex(index)
                self.task_view.selectRow(row)
            
            # Start editing
            self.task_view.edit(index)
            
        except Exception as e:
            print(f"Error triggering safe task edit: {e}")

    def edit_selected_task(self):
        idx = self.task_view.currentIndex()
        if idx.isValid():
            self.task_view.edit(self.task_model.index(idx.row(), TaskTableModel.COL_TITLE))

    def remove_selected_task(self):
        if not self.notes.flush_pending():
            return
        selected_rows = []
        selection_model = self.task_view.selectionModel()
        if selection_model:
            selected_indexes = selection_model.selectedRows()
            selected_rows = sorted({idx.row() for idx in selected_indexes if idx.isValid()})
        
        if not selected_rows:
            QMessageBox.information(self, "Remove Task(s)", "No tasks selected.")
            return
        
        # Several tasks at once still ask first; a single task is removed straight away (Undo is offered)
        if len(selected_rows) > 1:
            message = f"Delete {len(selected_rows)} selected tasks?"
            if QMessageBox.question(self, "Remove Task(s)", message) != QMessageBox.StandardButton.Yes:
                return

        anchor_row = selected_rows[0]
        task_ids = [tid for tid in (self.task_model.task_id_at(row) for row in selected_rows) if tid is not None]
        if not task_ids:
            return
        self._close_active_editors()
        if self.notes.task_id in task_ids:
            # Flush pending note edits before the snapshot, then detach the editor from the task
            if self.notes.set_task(None) is False:
                return
        snapshot = self.db.snapshot_tasks(task_ids)
        restore_files = self._backup_attachments_for_undo(task_ids)
        for tid in task_ids:
            self.db.remove_task(tid)

        self._restore_task_selection_after_delete(self.task_model.project_id, anchor_row)
        message = "Task deleted" if len(task_ids) == 1 else f"{len(task_ids)} tasks deleted"
        self._push_undo(message, snapshot, select_task_id=task_ids[0], after_restore=restore_files)

    def _selected_task_rows(self) -> List[int]:
        selection_model = self.task_view.selectionModel()
        rows = sorted({idx.row() for idx in selection_model.selectedRows() if idx.isValid()}) if selection_model else []
        if not rows:
            current = self.task_view.currentIndex()
            if current.isValid():
                rows = [current.row()]
        return [row for row in rows if 0 <= row < len(self.task_model.rows)]

    def set_tasks_done(self, task_ids: List[int], done: bool) -> List[int]:
        """Mark tasks done / not done and refresh the lists. Returns the ids that changed."""
        changed = []
        if not self._save_open_note_for(task_ids):
            return []
        for task_id in task_ids:
            row = self.db.conn.execute("SELECT done FROM tasks WHERE id=?", (task_id,)).fetchone()
            if row is None or bool(row['done']) == done:
                continue
            try:
                self.db.update_task(task_id, done=done)
                changed.append(task_id)
            except Exception as e:
                print(f"Error updating task {task_id}: {e}")
        if changed:
            self._reload_open_note_for(changed)
            self._refresh_lists(select_task_id=self.task_view.current_task_id())
        return changed

    def mark_task_complete(self):
        """Complete the selected tasks; when they are all complete already, reopen them."""
        rows = self._selected_task_rows()
        if not rows:
            return
        tasks = [self.task_model.rows[row] for row in rows]
        make_done = any(not bool(task['done']) for task in tasks)
        task_ids = [task['id'] for task in tasks]
        if not self._save_open_note_for(task_ids):
            return []
        snapshot = self.db.snapshot_tasks(task_ids) if make_done else None
        current_id = self.task_view.current_task_id()
        if make_done:
            self._keep_completed_in_search(task_ids)
        changed = self.set_tasks_done(task_ids, make_done)
        if changed and make_done:
            self._push_undo(self._completion_message(changed), snapshot, select_task_id=current_id, scope='completion')

    def toggle_mark_task_as_mom(self):
        """Toggle the MoM (Minutes of Meeting) status for the selected task."""
        selected_rows = []
        selection_model = self.task_view.selectionModel()
        if selection_model:
            selected_indexes = selection_model.selectedRows()
            selected_rows = [idx.row() for idx in selected_indexes if idx.isValid()]
        
        # Only allow this action for exactly one selected task
        if len(selected_rows) != 1:
            return
        
        row = selected_rows[0]
        tid = self.task_model.task_id_at(row)
        if tid is None or row >= len(self.task_model.rows):
            return
        
        task_row = self.task_model.rows[row]
        # Handle sqlite3.Row which doesn't have .get() method
        try:
            current_mom_status = bool(task_row['marked_mom'])
        except (KeyError, IndexError):
            current_mom_status = False
        new_mom_status = not current_mom_status
        
        try:
            # Update the MoM status in database
            self.db.update_task(tid, marked_mom=new_mom_status)
            
            # Reload the task model to reflect the change (ordering will change)
            self.task_model.reload()
            
            # Find and select the task at its new position (MoM tasks move to top)
            for new_row in range(self.task_model.rowCount()):
                if self.task_model.task_id_at(new_row) == tid:
                    self.task_view.selectRow(new_row)
                    self.task_view.scrollTo(self.task_model.index(new_row, 0))
                    break
            
            # Update the action text
            self._update_mom_action_text()
            
        except Exception as e:
            QMessageBox.critical(self, "Error", f"Failed to update MoM status:\n{str(e)}")
    
    def _update_mom_action_text(self):
        """Update the MoM action text based on current selection."""
        selected_rows = []
        selection_model = self.task_view.selectionModel()
        if selection_model:
            selected_indexes = selection_model.selectedRows()
            selected_rows = [idx.row() for idx in selected_indexes if idx.isValid()]
        
        # Enable/disable based on selection count
        if len(selected_rows) == 1:
            self.act_mark_mom.setEnabled(True)
            row = selected_rows[0]
            if row < len(self.task_model.rows):
                task_row = self.task_model.rows[row]
                # Handle sqlite3.Row which doesn't have .get() method
                try:
                    current_mom_status = bool(task_row['marked_mom'])
                except (KeyError, IndexError):
                    current_mom_status = False
                if current_mom_status:
                    self.act_mark_mom.setText("Unmark as Minutes of Meeting")
                else:
                    self.act_mark_mom.setText("Mark as Minutes of Meeting")
        else:
            # Disable if no selection or multiple selections
            self.act_mark_mom.setEnabled(False)
            self.act_mark_mom.setText("Mark as Minutes of Meeting")
    
    def on_sort_order_changed(self, index: int):
        """Handle sort order dropdown selection change."""
        # Get current project
        idx = self.project_view.currentIndex()
        if not idx.isValid():
            return
        
        project_id = self.project_model.project_id_at(idx.row())
        if project_id is None:
            return
        
        # index 0 = Auto order, index 1 = Manual order
        is_manual = (index == 1)
        
        # Update database
        self.db.set_manual_sort_enabled(project_id, is_manual)
        
        # Update drag-and-drop behavior
        self.task_view.set_manual_reorder(bool(is_manual))
        
        # Update menu action to stay in sync
        self.act_manual_sort.blockSignals(True)
        self.act_manual_sort.setChecked(is_manual)
        self.act_manual_sort.blockSignals(False)
        
        # Reload tasks to apply new sort order
        self.task_model.reload()

    def toggle_manual_sort(self, checked: bool):
        """Toggle manual sort override for the currently selected project."""
        # Get current project
        idx = self.project_view.currentIndex()
        if not idx.isValid():
            QMessageBox.information(self, "Manual Sort", "Please select a project first.")
            self.act_manual_sort.setChecked(False)
            return
        
        project_id = self.project_model.project_id_at(idx.row())
        if project_id is None:
            return
        
        # Update database
        self.db.set_manual_sort_enabled(project_id, checked)
        
        # Update drag-and-drop behavior
        self.task_view.set_manual_reorder(bool(checked))
        
        # Update combo box to stay in sync
        self.sort_order_combo.blockSignals(True)
        self.sort_order_combo.setCurrentIndex(1 if checked else 0)
        self.sort_order_combo.blockSignals(False)
        
        # Reload tasks to apply new sort order
        self.task_model.reload()
        
        # Update status
        status = "enabled" if checked else "disabled"
        QMessageBox.information(self, "Manual Sort", f"Manual sort override {status} for this project.")
    
    def toggle_project_visibility(self, project_id: Optional[int] = None):
        """Hide or unhide a project (default: the open one)."""
        if isinstance(project_id, bool):
            project_id = None  # QAction.triggered passes its checked state
        if project_id is None:
            project_id = self._current_project_id()
        if project_id is None:
            return
        is_open = self.current_view == ('project', project_id)
        current_row = self.project_model.row_for_project(project_id)
        now_hidden = self.db.toggle_project_hidden(project_id)
        if now_hidden and self.project_model.keep_visible_id == project_id:
            # Hiding is explicit: don't keep the project listed just because it is open
            self.project_model.keep_visible_id = None
        self.project_model.reload()
        title = self._project_title(project_id)
        self.toast.show_message(f"Hid {title}" if now_hidden else f"Unhid {title}")

        if not is_open or self.project_model.row_for_project(project_id) >= 0:
            # Another project changed, or the open one is still listed: keep the current view
            if self.current_view[0] == 'project':
                self._select_project_silently(self.current_view[1])
        elif self.project_model.rowCount() > 0:
            # The project moved into the collapsed HIDDEN section: open its neighbour
            new_row = min(current_row, max(0, self.project_model.hidden_start - 1))
            self.set_current_view(('project', self.project_model.project_id_at(new_row)))
        else:
            self.set_current_view(('smart', 'today'))
        self._update_comprehensive_status()


    def update_manual_sort_action_state(self):
        """Update the manual sort action state and combo box based on current project."""
        idx = self.project_view.currentIndex()
        if not idx.isValid():
            self.act_manual_sort.setEnabled(False)
            self.act_manual_sort.setChecked(False)
            self.sort_order_combo.setEnabled(False)
            self.sort_order_combo.blockSignals(True)
            self.sort_order_combo.setCurrentIndex(0)  # Default to Auto order
            self.sort_order_combo.blockSignals(False)
            return
        
        project_id = self.project_model.project_id_at(idx.row())
        if project_id is None:
            self.act_manual_sort.setEnabled(False)
            self.act_manual_sort.setChecked(False)
            self.sort_order_combo.setEnabled(False)
            self.sort_order_combo.blockSignals(True)
            self.sort_order_combo.setCurrentIndex(0)  # Default to Auto order
            self.sort_order_combo.blockSignals(False)
            return
        
        # Enable the action and set its checked state
        self.act_manual_sort.setEnabled(True)
        is_manual = self.db.is_manual_sort_enabled(project_id)
        self.act_manual_sort.blockSignals(True)
        self.act_manual_sort.setChecked(is_manual)
        self.act_manual_sort.blockSignals(False)
        
        # Update combo box to match
        self.sort_order_combo.setEnabled(True)
        self.sort_order_combo.blockSignals(True)
        self.sort_order_combo.setCurrentIndex(1 if is_manual else 0)
        self.sort_order_combo.blockSignals(False)
        
        # Update drag-and-drop mode
        self.task_view.set_manual_reorder(bool(is_manual))

    def toggle_show_completed(self, checked: bool):
        """View > Completed Tasks: expand the Completed group and list projects that are fully done.
        Projects hidden by hand stay in HIDDEN."""
        self.include_done = checked
        if hasattr(self, 'act_toggle_done'):
            self.act_toggle_done.blockSignals(True)
            self.act_toggle_done.setChecked(checked)
            self.act_toggle_done.blockSignals(False)
        if checked:
            self.task_model.collapsed.discard('done')
        else:
            self.task_model.collapsed.add('done')
        self.task_model.groupsChanged.emit()
        # Update projects visibility too
        self.project_model.set_include_done(self.include_done)
        if self.current_view[0] == 'project' and not self._is_searching():
            self._select_project_silently(self.current_view[1])
        self._update_comprehensive_status()

    def toggle_pinned_only(self, checked: bool):
        """Legacy "Focus Tasks" filter: opens (or leaves) the Focus smart view."""
        self._route_filter_to_view('pinned', bool(checked))

    def toggle_show_no_due_dates(self, checked: bool):
        """Legacy "Missing Due Dates" filter: opens (or leaves) the No due date smart view."""
        self._route_filter_to_view('triage', bool(checked))

    def _apply_always_on_top(self, enable: bool):
        # Helper to set window flag without recursion
        flags = self.windowFlags()
        if enable:
            if not (flags & Qt.WindowType.WindowStaysOnTopHint):
                self.setWindowFlag(Qt.WindowType.WindowStaysOnTopHint, True)
                self.show()
        else:
            if (flags & Qt.WindowType.WindowStaysOnTopHint):
                self.setWindowFlag(Qt.WindowType.WindowStaysOnTopHint, False)
                self.show()

    def _update_right_pane_state(self, has_task_selected: bool):
        """Show the task detail or its empty state."""
        self.notes.setEnabled(has_task_selected)
        self.detail.set_has_task(has_task_selected)
        if hasattr(self, 'act_recurrence'):
            self.act_recurrence.setEnabled(has_task_selected)
            self.act_pick_due.setEnabled(has_task_selected)
        if not has_task_selected:
            self.detail_state.task_id = None
            self.detail_state.recurrence = None
            self._sync_detail_recurrence()
            self.detail_state.people = []
            self.detail.set_stakeholders([])

    def toggle_always_on_top(self, checked: bool):
        self._apply_always_on_top(checked)
        try:
            self._settings.setValue('MainWindow/AlwaysOnTop', 'true' if checked else 'false')
            self._settings.sync()
        except Exception:
            pass

    # ----- current view: a project or a sidebar smart view -----
    def _is_searching(self) -> bool:
        return self.search_query is not None and self.search_query.active

    def _current_project_id(self) -> Optional[int]:
        idx = self.project_view.currentIndex()
        return self.project_model.project_id_at(idx.row()) if idx.isValid() else None

    def _load_saved_view(self) -> Optional[Tuple[str, Any]]:
        raw = str(self._settings.value('MainWindow/CurrentView', '') or '')
        kind, _, value = raw.partition(':')
        if kind == 'smart' and value in SMART_VIEW_TITLES:
            return ('smart', value)
        if kind == 'project':
            try:
                return ('project', int(value))
            except ValueError:
                return None
        # Older settings only knew the "pinned only" toggle
        if self._settings.value('MainWindow/PinnedOnly', 'false') in ('true', '1', 'True'):
            return ('smart', 'pinned')
        return None

    def _save_current_view(self):
        """Remember the open view. Written 2 seconds after the last switch, and on close."""
        timer = getattr(self, '_view_save_timer', None)
        if timer is None:
            timer = self._view_save_timer = QTimer(self)
            timer.setSingleShot(True)
            timer.setInterval(2000)
            timer.timeout.connect(self._save_current_view_now)
        timer.start()

    def _save_current_view_now(self):
        timer = getattr(self, '_view_save_timer', None)
        if timer is not None:
            timer.stop()
        try:
            kind, value = self.current_view
            self._settings.setValue('MainWindow/CurrentView', f"{kind}:{value}")
            self._settings.setValue('MainWindow/PinnedOnly', 'true' if self.current_view == ('smart', 'pinned') else 'false')
            # No sync() here: Qt writes the file itself, and closeEvent syncs
        except Exception:
            pass

    def _clear_search_silently(self) -> bool:
        """Leave search mode without re-running the search handlers. Returns True if a search was active."""
        if not self._is_searching() and not self.search_edit.text():
            return False
        self.search_edit.blockSignals(True)
        self.search_edit.clear()
        self.search_edit.blockSignals(False)
        self.search_terms = []
        self.search_stakeholder_terms = []
        self.search_query = None
        self._note_search_keep_id = None
        self._note_search_query = None
        self._search_debounce.stop()
        self._search_results_by_project.clear()
        self._preserve_project_id = None
        self._preserve_task_id = None
        self.project_model.set_search_matches(None)
        return True

    def _select_project_silently(self, project_id: Optional[int]) -> bool:
        """Make project_id the current sidebar row without firing on_project_selected."""
        selection = self.project_view.selectionModel()
        row = self.project_model.row_for_project(project_id) if project_id is not None else -1
        selection.blockSignals(True)
        try:
            if row >= 0:
                selection.setCurrentIndex(self.project_model.index(row, 0), QItemSelectionModel.SelectionFlag.ClearAndSelect)
            else:
                selection.clear()
        finally:
            selection.blockSignals(False)
        self.project_view.viewport().update()
        self.hidden_project_view.viewport().update()
        return row >= 0

    def set_current_view(self, view: Tuple[str, Any]):
        """Switch to a project ('project', id) or a smart view ('smart', key). Leaves search mode."""
        if not self.notes.flush_pending():
            return
        self._clear_search_silently()
        self.current_view = view
        self._apply_current_view()
        self._save_current_view()

    def _apply_current_view(self):
        """Load the task list for current_view and sync the sidebar, menus and status bar."""
        self._leave_review_visit(self.current_view)
        kind, value = self.current_view
        if kind == 'project':
            row = self.project_model.row_for_project(value)
            if row < 0 and any(r['id'] == value for r in self.project_model.hidden_rows):
                # The project sits in the collapsed HIDDEN section: open it
                self.sidebar.set_hidden_expanded(True)
                self._on_hidden_section_toggled(True, reapply=False)
                row = self.project_model.row_for_project(value)
            if row < 0:
                if self.project_model.rowCount() > 0:
                    value = self.project_model.project_id_at(0)
                    self.current_view = ('project', value)
                else:
                    self.current_view = ('smart', 'today')
                    kind, value = self.current_view
        self.task_model.collapsed = self._collapsed_groups.setdefault((kind, value), {'done'})
        # While the list reloads for the new view, the handlers that react to a reload stand
        # down: the selection, the detail pane and the header are settled once, right after.
        self._switching_view = True
        try:
            if kind == 'project':
                self._last_project_id = value
                self._select_project_silently(value)
                self._keep_project_listed(value)
                self.task_model.show_project(value, self.include_done)
                project_id = value
            else:
                self._select_project_silently(None)
                self._keep_project_listed(None)
                self.task_model.set_smart_view(value)
                project_id = None
        finally:
            self._switching_view = False
        # The list keeps a task selected when it also exists in the new view
        self._select_first_task_if_needed()
        self._sync_view_chrome(project_id)
        self._note_review_visit()

    def _select_first_task_if_needed(self):
        """Keep the current task when it is visible in the list, otherwise select the first visible one."""
        current = self.task_view.currentIndex()
        if current.isValid() and self.task_view.is_row_selectable(current.row()):
            self.on_task_selected()
            return
        rows = self.task_view.visible_rows()
        if rows:
            # Selecting the row runs on_task_selected through the selection signal
            self._select_task_row(rows[0])
            if self.notes.task_id != self.task_view.current_task_id():
                self.on_task_selected()
        else:
            self._on_task_selection_lost()

    def _keep_project_listed(self, project_id: Optional[int]):
        """Keep the open project in the sidebar even if completing its tasks would hide it."""
        model = self.project_model
        if project_id is not None:
            row = model.row_for_project(project_id)
            if row < 0 or model.is_hidden_row(row):
                project_id = None
        if model.keep_visible_id == project_id:
            return
        was_kept = model.kept_by_pin
        model.keep_visible_id = project_id
        if was_kept:
            # The previously open project is only listed because it was open: let it drop out
            current = self.current_view[1] if self.current_view[0] == 'project' else None
            model.reload()
            self._select_project_silently(current)

    def _reset_task_detail(self):
        """No task is selected after the view changes: clear the right pane."""
        if self.notes.set_task(None) is False:
            return
        self._update_right_pane_state(False)
        self.detail_state.due_date = None
        self._sync_detail_due()

    def _current_view_title(self) -> str:
        kind, value = self.current_view
        if self._is_searching():
            return "Search"
        if kind == 'smart':
            return SMART_VIEW_TITLES.get(value, "Tasks")
        row = self.project_model.row_for_project(value)
        return self.project_model.rows[row]['title'] if row >= 0 else "Tasks"

    def _sync_view_chrome(self, project_id: Optional[int]):
        """Reflect current_view in the sidebar highlight, the legacy filter actions and the status bar."""
        kind, value = self.current_view
        searching = self._is_searching()
        self.sidebar.set_active_view(value if kind == 'smart' and not searching else None)
        for action, keys in (
            (getattr(self, 'act_show_pinned_filter', None), ('pinned',)),
            (getattr(self, 'act_show_no_due_dates', None), ('triage',)),
        ):
            if action is not None:
                action.blockSignals(True)
                action.setChecked(kind == 'smart' and value in keys)
                action.blockSignals(False)
        self._update_list_header()
        self.sidebar.set_hidden_count(len(self.project_model.hidden_rows))
        self.sidebar.set_project_count(self.project_model.hidden_start)
        if hasattr(self, 'notes'):
            self.notes.set_search_terms(self.search_terms if searching else [])
        self.project_view.viewport().update()
        self.hidden_project_view.viewport().update()
        self._update_comprehensive_status()
        self.update_manual_sort_action_state()
        self._update_feature_link_for_project(project_id)

    EMPTY_STATE_TEXT = {
        'today': "Nothing due today. Tasks with a due date of today or earlier show here.",
        'pinned': "Add a task to Focus to keep it here across projects.",
        'waiting': "Tasks you are waiting on others for.",
        'mom': "Tasks marked as minutes of meeting.",
        'triage': "Every open task has a due date.",
    }

    def _update_list_header(self):
        """Title, subtitle, project tools, quick-add target and the empty state for the task list."""
        if not hasattr(self, 'list_header') or getattr(self, '_switching_view', False):
            return
        kind, value = self.current_view
        searching = self._is_searching()
        title = self._current_view_title()
        row_count = self.task_model.rowCount()
        if searching:
            project_count = len(self.task_model.groups)
            subtitle = (f"{row_count} result{'' if row_count == 1 else 's'} in "
                        f"{project_count} project{'' if project_count == 1 else 's'}")
            hidden_done = getattr(self, '_search_hidden_done_count', 0)
            if hidden_done:
                subtitle += f" · {hidden_done} completed hidden (Alt+C)"
                empty = ("No open matches",
                         f"{hidden_done} completed task{'' if hidden_done == 1 else 's'} match. "
                         "Turn on Completed in the search box (Alt+C) to see them.")
            else:
                empty = ("No matches", "Try fewer words, or @name to search stakeholders.")
        elif kind == 'smart':
            subtitle = f"{row_count} across projects"
            empty = ("Nothing here", self.EMPTY_STATE_TEXT.get(value, ""))
        else:
            subtitle = f"{self.task_model.open_count()} open"
            empty = ("Nothing here", "Add the first task above.")
        self.list_header.set_texts(title, subtitle)
        self.list_header.set_project_tools_visible(kind == 'project' and not searching)
        self.quick_add.setVisible(not searching)
        self._sync_quick_add_target()
        self.task_view.set_empty_text(*empty)
        self.task_view._sync_empty_state()

    def _populate_project_menu(self):
        """Project actions behind the list header's menu button."""
        menu = self.list_header.project_menu
        menu.clear()
        project_id = self._current_project_id()
        if project_id is not None:
            self._build_project_menu(menu, project_id)

    def _populate_menubar_project_menu(self):
        """Menu bar → Project: list actions, the open project's actions, and periodic review."""
        menu = self.menu_project
        menu.clear()
        menu.addAction(self.act_add_project)
        menu.addAction(self.act_go_to_project)
        menu.addSeparator()
        open_id = self.current_view[1] if self.current_view[0] == 'project' else None
        self._build_project_menu(menu, open_id, label=True, title_case=True)
        menu.addSeparator()
        active = self.project_model.is_review_active()
        self.act_start_periodic_review.setText("Continue Review" if active else "Start Review…")
        menu.addAction(self.act_start_periodic_review)
        menu.addAction(self.act_next_review_project)
        self.act_next_review_project.setEnabled(active)
        end_action = menu.addAction("End Review", self.end_review)
        end_action.setEnabled(active)

    def _on_task_selection_lost(self):
        """The selected task left the list (completed in a smart view, deleted, moved)."""
        if getattr(self, '_switching_view', False):
            return  # the view switch picks the selection itself
        self.task_view.clearSelection()
        self.task_view.setCurrentIndex(QModelIndex())
        self.on_task_selected()

    # ----- undo -----
    def _push_undo(self, message: str, snapshot: Optional[Dict[str, Any]], *, select_task_id: Optional[int] = None,
                   view: Optional[Tuple[str, Any]] = None, after_restore: Optional[Callable[[], None]] = None,
                   scope: str = 'full'):
        """Show the toast for an action; with a snapshot it gets an Undo that restores those rows."""
        if snapshot is None:
            self.toast.show_message(message)
            return

        window = self

        class _SnapshotCommand(QUndoCommand):
            def __init__(self):
                super().__init__(message)
                self._applied = False

            def redo(self):
                self._applied = True  # the action already happened when the command is pushed

            def undo(self):
                window._restore_undo_snapshot(snapshot, select_task_id, view, after_restore, scope)

        self.undo_stack.push(_SnapshotCommand())
        self.toast.show_message(message, self.undo_last_action)

    def undo_last_action(self):
        if self.undo_stack.canUndo():
            self.undo_stack.undo()
            # Row snapshots are not replayed, so an undone step cannot be redone
            self.undo_stack.setIndex(self.undo_stack.index())
            self.toast.hide()

    def _restore_undo_snapshot(self, snapshot: Dict[str, Any], select_task_id: Optional[int],
                               view: Optional[Tuple[str, Any]], after_restore: Optional[Callable[[], None]],
                               scope: str = 'full'):
        task_ids = [task['id'] for task in snapshot.get('tasks', [])]
        try:
            # What is typed in the open note goes to the database first, so the undo sees it
            if not self._save_open_note_for(task_ids):
                return
            if after_restore is not None:
                after_restore()
            self.db.restore_snapshot(snapshot, scope)
        except Exception as e:
            QMessageBox.critical(self, "Undo", f"Could not undo: {e}")
            return
        self._reload_open_note_for(task_ids)
        self.project_model.reload()
        if view is not None:
            self.set_current_view(view)
        self._refresh_lists(select_task_id=select_task_id)
        if select_task_id is not None:
            self.on_task_selected()

    def _clear_undo_history(self):
        """Undo snapshots hold row ids of the database they were taken from."""
        self.undo_stack.clear()
        self._pending_complete_snapshots.clear()
        self._discard_undo_backups()
        self.toast.hide()

    def _backup_attachments_for_undo(self, task_ids: List[int]) -> Optional[Callable[[], None]]:
        """Copy the attachment files of tasks that are about to be deleted; returns a restore function."""
        try:
            marks = ",".join("?" * len(task_ids))
            names = [r['filename'] for r in self.db.conn.execute(
                f"SELECT DISTINCT filename FROM attachments WHERE task_id IN ({marks})", list(task_ids))]
        except Exception:
            names = []
        copies = []
        if names:
            backup_dir = tempfile.mkdtemp(prefix="project-notes-undo-")
            self._undo_attachment_dirs.append(backup_dir)
            for name in names:
                source = self.db._attachment_abs_path(name)
                if source and os.path.isfile(source):
                    target = os.path.join(backup_dir, uuid.uuid4().hex + os.path.splitext(source)[1])
                    try:
                        shutil.copy2(source, target)
                        copies.append((target, source))
                    except Exception as e:
                        print(f"Could not back up attachment {name}: {e}")
        if not copies:
            return None

        def restore():
            for backup, original in copies:
                try:
                    if os.path.isfile(backup) and not os.path.exists(original):
                        os.makedirs(os.path.dirname(original), exist_ok=True)
                        shutil.copy2(backup, original)
                except Exception as e:
                    print(f"Could not restore attachment {original}: {e}")
        return restore

    def _discard_undo_backups(self):
        for path in self._undo_attachment_dirs:
            shutil.rmtree(path, ignore_errors=True)
        self._undo_attachment_dirs = []

    def _save_open_note_for(self, task_ids: List[int]):
        """Before the database appends a completion stamp: store what is typed in the open note."""
        if self.notes.task_id is not None and self.notes.task_id in task_ids:
            return self.notes.save_all_now()
        return True

    def _reload_open_note_for(self, task_ids: List[int]):
        """After the database changed a note (completion stamp, undo): show the stored version."""
        if self.notes.task_id is not None and self.notes.task_id in task_ids:
            self.notes._autosave_timer.stop(); self.notes._html_save_timer.stop()
            self.notes.reload_html()
            self._update_note_meta()

    def _on_task_about_to_complete(self, task_id: int):
        self._keep_completed_in_search([task_id])
        self._save_open_note_for([task_id])
        self._pending_complete_snapshots[task_id] = self.db.snapshot_tasks([task_id])

    def _completion_message(self, task_ids: List[int]) -> str:
        if len(task_ids) > 1:
            return f"{len(task_ids)} tasks completed"
        row = self.db.conn.execute("SELECT recurring FROM tasks WHERE id=?", (task_ids[0],)).fetchone() if task_ids else None
        return "Completed — next one scheduled" if row and bool(row['recurring']) else "Task completed"

    def _on_task_completed_in_list(self, task_id: int):
        """Completion from the list's complete box: offer Undo."""
        snapshot = self._pending_complete_snapshots.pop(task_id, None)
        self._reload_open_note_for([task_id])
        self._push_undo(self._completion_message([task_id]), snapshot, select_task_id=task_id, scope='completion')
        self._update_comprehensive_status()

    # ----- keyboard -----
    def _is_typing(self) -> bool:
        """True while the keyboard focus is in a text field or an inline editor."""
        widget = QApplication.focusWidget()
        if widget is None:
            return False
        # People chips handle Backspace/Delete locally, never as task deletion.
        if hasattr(self, 'detail') and (widget is self.detail.chips or self.detail.chips.isAncestorOf(widget)):
            return True
        if isinstance(widget, (QLineEdit, QTextEdit, QPlainTextEdit, QAbstractSpinBox)):
            return True
        if isinstance(widget, QComboBox):
            return True
        if isinstance(widget, QAbstractItemView) and widget.state() == QAbstractItemView.State.EditingState:
            return True
        return False

    def _move_task_selection(self, step: int):
        current = self.task_view.currentIndex()
        if current.isValid() and self.task_view.is_row_selectable(current.row()):
            row = self.task_view.neighbour_row(current.row(), step)
        else:
            rows = self.task_view.visible_rows()
            row = (rows[0] if step > 0 else rows[-1]) if rows else -1
        if row >= 0:
            self._select_task_row(row)

    def _handle_global_key(self, event) -> bool:
        """Window-level shortcuts that work while not typing. Returns True when the key was used."""
        if not self.isActiveWindow() or QApplication.activeModalWidget() is not None or QApplication.activePopupWidget() is not None:
            return False
        if self._is_typing():
            return False
        mods = event.modifiers() & ~Qt.KeyboardModifier.KeypadModifier
        if mods != Qt.KeyboardModifier.NoModifier:
            return False
        key = event.key()
        focus = QApplication.focusWidget()
        in_sidebar = focus is not None and self.sidebar.isAncestorOf(focus)
        if key == Qt.Key.Key_N:
            self.add_task()
            return True
        if key == Qt.Key.Key_Slash and in_sidebar:
            self.sidebar.filter_button.setChecked(True)
            self.project_filter_edit.setFocus()
            return True
        if key in (Qt.Key.Key_J, Qt.Key.Key_K):
            self._move_task_selection(1 if key == Qt.Key.Key_J else -1)
            return True
        if key in (Qt.Key.Key_Down, Qt.Key.Key_Up) and not in_sidebar and not isinstance(focus, (QAbstractItemView, QMenu)):
            self._move_task_selection(1 if key == Qt.Key.Key_Down else -1)
            return True
        if key == Qt.Key.Key_Delete and focus in (self.project_view, self.hidden_project_view):
            index = focus.currentIndex()
            project_id = self.project_model.project_id_at(index.row()) if index.isValid() else None
            if project_id is not None:
                self.remove_selected_project(project_id)
                return True
        if key == Qt.Key.Key_Delete and not in_sidebar and self.task_view.currentIndex().isValid():
            self.remove_selected_task()
            return True
        return False

    def _quick_add_project_id(self) -> Optional[int]:
        """Default quick-add project: the open project, else (in smart views, the same for all of
        them) the last project opened, else the first listed one."""
        kind, value = self.current_view
        if kind == 'project':
            return value
        if self._last_project_id is not None and self.db.conn.execute(
                "SELECT 1 FROM projects WHERE id=?", (self._last_project_id,)).fetchone():
            return self._last_project_id
        if self.project_model.rowCount() > 0:
            return self.project_model.project_id_at(0)
        return None

    def _quick_add_task(self, title: str):
        """Create a task from the quick-add row; smart views pass their flag on to the new task."""
        if self._is_searching():
            return
        project_id = self._quick_add_target_id()
        if project_id is None:
            QMessageBox.information(self, "Add Task", "Create a project first.")
            return
        try:
            self._close_active_editors()
            task_id = self.db.add_task(project_id, title)
            kind, value = self.current_view
            if kind == 'smart':
                if value == 'pinned':
                    self.db.update_task(task_id, pinned=True)
                elif value == 'waiting':
                    self.db.update_task(task_id, waiting=True)
                elif value == 'mom':
                    self.db.update_task(task_id, marked_mom=True)
                elif value == 'today':
                    self.db.set_task_due_date(task_id, f"{datetime.date.today().isoformat()} 00:00:00")
        except Exception as e:
            QMessageBox.critical(self, "Add Task", f"Failed to add task: {e}")
            return
        self._refresh_lists(select_task_id=task_id)
        self.quick_add.focus_input()

    def _refresh_lists(self, select_task_id: Optional[int] = None, anchor_row: Optional[int] = None):
        """Reload the sidebar and the task list for the current view or search, then fix the selection.

        select_task_id keeps (or makes) that task selected. anchor_row selects the nearest
        visible row to a row that was just removed.
        """
        self.project_model.refresh_visibility()
        if self.current_view[0] == 'project' and not self._is_searching():
            self._select_project_silently(self.current_view[1])
        if self._is_searching():
            self.apply_search_filter()
        else:
            old_defer = getattr(self.task_model, '_defer_reload_flag', False)
            self.task_model._defer_reload_flag = False
            self.task_model._perform_reload()
            self.task_model._defer_reload_flag = old_defer
        if select_task_id is not None and self._select_task_if_visible(select_task_id):
            pass
        elif select_task_id is not None and anchor_row is None:
            self._on_task_selection_lost()
        elif anchor_row is not None:
            row = anchor_row if self.task_view.is_row_selectable(anchor_row) else self.task_view.neighbour_row(anchor_row, 1)
            if row < 0:
                row = self.task_view.neighbour_row(anchor_row, -1)
            if row >= 0:
                self._select_task_row(row)
            else:
                self._on_task_selection_lost()
        self._update_comprehensive_status()

    def _route_filter_to_view(self, key: str, checked: bool):
        """The old filter toggles (menu actions, addons) now open or leave a smart view."""
        if checked:
            self.set_current_view(('smart', key))
        elif self.current_view == ('smart', key):
            if self._last_project_id is not None:
                self.set_current_view(('project', self._last_project_id))
            elif self.project_model.rowCount() > 0:
                self.set_current_view(('project', self.project_model.project_id_at(0)))
        else:
            self._sync_view_chrome(self._current_project_id())

    def _counts_key(self) -> Tuple[Any, ...]:
        """Changes whenever the counts can: total_changes counts every write on the connection
        (undo restores included); the date moves "due today" / "overdue"."""
        return (id(self.db.conn), self.db.conn.total_changes, datetime.date.today())

    def _refresh_sidebar_counts(self):
        try:
            self.sidebar.set_view_counts(self.db.smart_view_counts())
            self.project_model.refresh_counts()
        except Exception as e:
            print(f"Error refreshing sidebar counts: {e}")

    def _on_project_model_reset(self):
        self._sync_quick_add_target()
        if hasattr(self, 'review_panel'):
            # A reload re-reads the review from the database (startup, database switch)
            self._on_review_changed()
        self.sidebar.set_hidden_count(len(self.project_model.hidden_rows))
        self.sidebar.set_project_count(self.project_model.hidden_start)
        self.sidebar.sync_filter_visibility()
        if not self.project_model.project_name_filter and self.project_model.include_hidden_rows != (
                self._settings.value('MainWindow/HiddenExpanded', 'false') in ('true', '1', 'True')):
            # The filter was cleared without a textChanged signal: close the HIDDEN section it opened
            QTimer.singleShot(0, self._sync_filter_hidden_expansion)
        # A reset drops the current index; put the highlight back on the active project
        if self.current_view[0] == 'project' and not self._is_searching() and not self.project_view.currentIndex().isValid():
            QTimer.singleShot(0, self._restore_project_highlight)

    def _restore_project_highlight(self):
        if self.current_view[0] == 'project' and not self._is_searching() and not self.project_view.currentIndex().isValid():
            self._select_project_silently(self.current_view[1])

    def _on_hidden_section_toggled(self, expanded: bool, reapply: bool = True, persist: bool = True):
        if persist:
            try:
                self._settings.setValue('MainWindow/HiddenExpanded', 'true' if expanded else 'false')
            except Exception:
                pass
        if self.project_model.include_hidden_rows == expanded:
            return
        self.project_model.include_hidden_rows = expanded
        self.project_model.reload()
        if not reapply or self._is_searching():
            return
        if self.current_view[0] == 'project' and self.project_model.row_for_project(self.current_view[1]) < 0:
            # The open project was in the section that just collapsed: move to a visible one
            if self.project_model.rowCount() > 0:
                self.current_view = ('project', self.project_model.project_id_at(0))
            else:
                self.current_view = ('smart', 'today')
            self._apply_current_view()
            self._save_current_view()
        else:
            self._restore_project_highlight()

    def _on_hidden_project_context_menu(self, position):
        index = self.hidden_project_view.indexAt(position)
        if not index.isValid():
            self._exec_project_list_menu(self.hidden_project_view.viewport().mapToGlobal(position))
            return
        project_id = self.project_model.project_id_at(index.row())
        if project_id is not None:
            self._exec_project_context_menu(self.hidden_project_view, index.row(), project_id,
                                            self.hidden_project_view.viewport().mapToGlobal(position))

    def _create_project_from_sidebar(self, title: str):
        self._clear_search_silently()
        row = self.project_model.add_project(title)
        project_id = self.project_model.project_id_at(row) if row >= 0 else None
        if project_id is None:
            return
        default_manual_sort = self._settings.value('Preferences/DefaultManualSort', 'false') in ('true', '1', 'True')
        self.db.set_manual_sort_enabled(project_id, default_manual_sort)
        self.set_current_view(('project', project_id))
        self.task_view.setFocus()

    def on_project_selected(self):
        if not self.notes.flush_pending():
            self._select_project_silently(self.current_view[1] if self.current_view[0] == 'project' else None)
            return
        pid = self._current_project_id()
        if pid is None:
            # Selection cleared by a model reset or a smart view; current_view decides what is shown
            return
        if self._clear_search_silently():
            # Picking a project leaves search mode; the reload dropped the selection
            self._select_project_silently(pid)
        self._leave_review_visit(('project', pid))
        self.current_view = ('project', pid)
        self._last_project_id = pid
        self.task_model.collapsed = self._collapsed_groups.setdefault(('project', pid), {'done'})
        self._switching_view = True
        try:
            self._keep_project_listed(pid)
            self.task_model.show_project(pid, self.include_done)
        finally:
            self._switching_view = False
        self._select_first_task_if_needed()
        self._sync_view_chrome(pid)
        self._save_current_view()
        self._note_review_visit()

    def _restore_task_selection(self, task_id: int):
        """Restore task selection after model changes."""
        try:
            # First ensure the correct project is selected for this task
            self._ensure_project_selected_for_task(task_id)
            
            # Then find and select the task
            for row in range(self.task_model.rowCount()):
                if self.task_model.task_id_at(row) == task_id:
                    self.task_view.setCurrentIndex(self.task_model.index(row, 0))
                    self.task_view.selectRow(row)
                    self.task_view.scrollTo(self.task_model.index(row, 0), QTableView.ScrollHint.EnsureVisible)
                    break
        except Exception as e:
            print(f"Error restoring task selection: {e}")

    def on_task_selected(self):
        self.detail.chips._commit_input()
        if not self.notes.flush_pending():
            if self.notes.task_id is not None:
                self._select_task_if_visible(self.notes.task_id)
            return
        idx = self.task_view.currentIndex()
        tid = self.task_model.task_id_at(idx.row()) if idx.isValid() else None
        has_task = tid is not None
        
        # Update right pane state based on task selection
        self._update_right_pane_state(has_task)
        # Ensure the project containing this task is selected
        if tid is not None:
            self._ensure_project_selected_for_task(tid)
        
        # Update MoM action text based on selection
        self._update_mom_action_text()
        if hasattr(self, 'act_recurrence'):
            self.act_recurrence.setEnabled(has_task)
            self.act_pick_due.setEnabled(has_task)
        
        # Update status bar with task creation date
        self._update_task_status(tid)
        
        if tid != self.notes.task_id:
            if getattr(self, '_note_search_keep_id', None) == self.notes.task_id:
                self._note_search_keep_id = None
                if self._is_searching() and hasattr(self, '_note_search_timer'):
                    self._note_search_timer.start(100)
            self.detail.close_find()
            self.notes.set_task(tid)
        self.notes.set_search_terms(self.search_terms if self._is_searching() else [])
        self._update_note_meta()
        
        self.detail_state.task_id = tid
        self._refresh_people_suggestions()
        # Load due date
        if tid is None:
            self.detail_state.due_date = None
        else:
            due_date_str = self.db.get_task_due_date(tid)
            if due_date_str:
                # Parse ISO format date (YYYY-MM-DD or YYYY-MM-DD HH:MM:SS)
                date_part = due_date_str.split()[0] if ' ' in due_date_str else due_date_str
                year, month, day = map(int, date_part.split('-'))
                self.detail_state.due_date = QDate(year, month, day)
            else:
                self.detail_state.due_date = None
        
        # Update button text
        self._sync_detail_due()
        
        # Load recurrence
        if tid is None:
            self.detail_state.recurrence = None
        else:
            self.detail_state.recurrence = self.db.get_task_recurrence(tid)
        self._sync_detail_recurrence()
        
        # Load stakeholders
        if tid is None:
            self.detail_state.people = []
            self.detail.set_stakeholders([])
        else:
            names = self.db.get_stakeholders_for_task(tid)
            self.detail_state.people = list(names)
            self.detail.set_stakeholders(list(names))
        self._sync_detail_header()
    
    def _open_due_date_picker(self):
        if self.task_view.current_task_id() is not None:
            self.detail.show_property_editor('due')

    def _sync_detail_due(self):
        """Update the due date button text based on current state."""
        if self.detail_state.due_date:
            date_str = self.detail_state.due_date.toString("yyyy-MM-dd")
            # Calculate week number and day of week
            year = self.detail_state.due_date.year()
            week_number, _ = self.detail_state.due_date.weekNumber()  # weekNumber() returns (week, year)
            day_of_week = self.detail_state.due_date.dayOfWeek()  # 1=Monday, 7=Sunday
            # Format: "2025-11-22 / wk2547.6"
            week_info = f"wk{year % 100:02d}{week_number:02d}.{day_of_week}"
            full_text = f"{date_str} / {week_info}"
            self.detail.due_edit.setToolTip(full_text)
            self.detail.set_due_date(self.detail_state.due_date)
        else:
            self.detail.due_edit.setToolTip("")
            self.detail.set_due_date(None)
    
    def _sync_detail_recurrence(self):
        """Update the recurrence button text based on current recurrence settings."""
        if not self.detail_state.recurrence or not self.detail_state.recurrence.get('recurring'):
            self.detail.set_repeat("none")
            return

        rule = _parse_recurrence_rule(self.detail_state.recurrence.get('recurrence_rule'))
        summary = _format_recurrence_summary(rule)
        tooltip = _format_recurrence_tooltip(rule, self.detail_state.recurrence.get('recurrence_next_at'))
        self.detail.set_repeat(self._recurrence_preset_key(rule), self._recurrence_combo_label(rule), tooltip or summary)

    @staticmethod
    def _recurrence_preset_key(rule: Optional[Dict[str, Any]]) -> str:
        """Which Repeat option a stored rule corresponds to; anything richer is 'custom'."""
        if not rule:
            return REPEAT_CUSTOM
        try:
            interval = int(rule.get("interval") or rule.get("interval_days") or 1)
        except (TypeError, ValueError):
            interval = 1
        freq, mode = rule.get("freq"), rule.get("mode")
        if mode == RECURRENCE_MODE_COMPLETION:
            return "completion" if freq == RECURRENCE_FREQ_EVERY_N_DAYS and interval == 7 else REPEAT_CUSTOM
        if interval == 1:
            if freq in (RECURRENCE_FREQ_DAILY, RECURRENCE_FREQ_EVERY_N_DAYS):
                return "daily"
            if freq == RECURRENCE_FREQ_WEEKLY and len(rule.get("weekdays") or [1]) <= 1:
                return "weekly"
            if freq == RECURRENCE_FREQ_MONTHLY:
                return "monthly"
        return REPEAT_CUSTOM

    def _apply_recurrence_preset(self, key: str):
        """Repeat combo: apply a preset to the selected task, or open the full recurrence dialog."""
        tid = self.task_view.current_task_id()
        if tid is None:
            return
        if key == REPEAT_EDIT:
            self._open_recurrence_dialog()
            # Put the combo back on the stored rule when the dialog was cancelled
            self.detail_state.recurrence = self.db.get_task_recurrence(tid)
            self._sync_detail_recurrence()
            return
        try:
            existing = self.db.get_task_recurrence(tid) or {}
            if key == "none":
                self.db.set_task_recurrence(tid, recurring=False, rule_text=None, start_at=None, next_at=None, last_at=None)
            else:
                now = datetime.datetime.now()
                rule: Dict[str, Any] = {"mode": RECURRENCE_MODE_FIXED, "interval": 1, "time": recurrence_default_time(self._settings)}
                if key == "daily":
                    rule.update(freq=RECURRENCE_FREQ_EVERY_N_DAYS, interval_days=1)
                elif key == "weekly":
                    rule.update(freq=RECURRENCE_FREQ_WEEKLY, weekdays=[now.isoweekday()])
                elif key == "monthly":
                    rule.update(freq=RECURRENCE_FREQ_MONTHLY, day_of_month=now.day)
                elif key == "completion":
                    rule.update(mode=RECURRENCE_MODE_COMPLETION, freq=RECURRENCE_FREQ_EVERY_N_DAYS, interval=7, interval_days=7)
                else:
                    return
                start_at = next_at = None
                if rule["mode"] == RECURRENCE_MODE_FIXED:
                    time_of_day = _parse_recurrence_time(rule["time"], now.time())
                    start_dt = now.replace(hour=time_of_day.hour, minute=time_of_day.minute, second=0, microsecond=0)
                    start_at = _format_recurrence_datetime(start_dt)
                    next_dt = _compute_next_recurrence(rule, start_dt, now)
                    next_at = _format_recurrence_datetime(next_dt) if next_dt else None
                else:
                    done_row = self.db.conn.execute("SELECT done FROM tasks WHERE id=?", (tid,)).fetchone()
                    if done_row and bool(done_row['done']):
                        start_at = _format_recurrence_datetime(now)
                        next_dt = _compute_next_recurrence(rule, now, now)
                        next_at = _format_recurrence_datetime(next_dt) if next_dt else None
                self.db.set_task_recurrence(
                    tid, recurring=True, rule_text=json.dumps(rule), start_at=start_at, next_at=next_at,
                    last_at=existing.get('recurrence_last_at')
                )
        except Exception as e:
            QMessageBox.critical(self, "Recurrence", f"Failed to update recurrence: {e}")
        self.detail_state.recurrence = self.db.get_task_recurrence(tid)
        self._sync_detail_recurrence()
        self._refresh_lists(select_task_id=tid)

    def _on_note_saved(self):
        self._update_note_meta()
        if self._is_searching():
            self._note_search_keep_id = self.notes.task_id
            # Queue until the current save/navigation call has finished.
            if not hasattr(self, '_note_search_timer'):
                self._note_search_timer = QTimer(self)
                self._note_search_timer.setSingleShot(True)
                self._note_search_timer.timeout.connect(self.apply_search_filter)
            self._note_search_timer.start(100)

    def _update_note_meta(self):
        """Notes toolbar corner: when the note was last edited, or the image hint for an empty note."""
        if not hasattr(self, 'detail'):
            return
        tid = self.notes.task_id
        if tid is None or not self.notes.has_content():
            self.detail.set_note_meta("Paste or drop images", "")
            return
        try:
            row = self.db.conn.execute("SELECT updated_at FROM notes WHERE task_id=?", (tid,)).fetchone()
        except Exception:
            row = None
        moment = notes_html.parse_db_timestamp(row['updated_at']) if row else None
        if moment is None:
            self.detail.set_note_meta("", "")
            return
        local = moment.astimezone()
        self.detail.set_note_meta(f"Edited {notes_html.relative_time(moment)}", local.strftime("Last edited %d %b %Y, %H:%M"))

    def _has_note_templates(self) -> bool:
        try:
            return self.db.conn.execute("SELECT 1 FROM note_templates LIMIT 1").fetchone() is not None
        except Exception:
            return False

    @staticmethod
    def _recurrence_combo_label(rule: Optional[Dict[str, Any]]) -> str:
        """Readable schedule for the property chip; its tooltip includes the next occurrence."""
        return (_format_recurrence_summary(rule)
                .replace(' on day ', ' · day ')
                .replace(' on ', ' · ')
                .replace(' at ', ' · '))

    # ----- task detail panel handlers -----
    def _sync_detail_header(self):
        """Push the selected task's title, project and flags into the detail panel."""
        if not hasattr(self, 'detail') or getattr(self, '_switching_view', False):
            return
        idx = self.task_view.currentIndex()
        if not idx.isValid() or not (0 <= idx.row() < len(self.task_model.rows)):
            return
        ctx = self.task_model.row_context(idx.row())
        task = self.task_model.rows[idx.row()]
        project = self.task_model._project_titles.get(task['project_id'], "")
        self.detail.set_task_header(
            title=ctx['title'], project=project, done=ctx['done'], pinned=ctx['pinned'],
            waiting=ctx['waiting'], mom=ctx['mom']
        )

    def _toggle_current_task_flag(self, flag: str):
        idx = self.task_view.currentIndex()
        if idx.isValid():
            self.task_model.toggle_task_flag(idx.row(), flag)
        self._sync_detail_header()

    def _refresh_people_suggestions(self):
        names = [row[0] for row in self.db.conn.execute('SELECT name FROM stakeholders ORDER BY name COLLATE NOCASE')]
        self.detail.chips.set_suggestions(names)

    def _open_detail_project(self):
        tid = self.task_view.current_task_id()
        if tid is None or not self.notes.flush_pending():
            return
        row = self.db.conn.execute('SELECT project_id FROM tasks WHERE id=?', (tid,)).fetchone()
        if row:
            self.set_current_view(('project', row[0]))
            self._select_task_if_visible(tid)

    def _on_detail_title_committed(self, title: str):
        tid = self.task_view.current_task_id()
        if tid is None:
            return
        self.db.update_task(tid, title=title)
        self._refresh_lists(select_task_id=tid)

    def _on_detail_due_changed(self, date):
        if self.task_view.current_task_id() is None:
            return
        if date is None:
            self._clear_due_date()
            return
        self.detail_state.due_date = date
        self._save_due_date_now()

    def _on_detail_stakeholders_changed(self, names: List[str]):
        if self.detail_state.task_id is None:
            return
        self.detail_state.people = list(names)
        self._save_stakeholders_now()
        self.detail.set_stakeholders(names)
        self._refresh_people_suggestions()

    def _populate_task_more_menu(self):
        """Detail panel menu: snooze, duplicate, move to project, recurrence, delete."""
        menu = self.detail.more_menu
        menu.clear()
        idx = self.task_view.currentIndex()
        if not idx.isValid() or not (0 <= idx.row() < len(self.task_model.rows)):
            return
        task = self.task_model.rows[idx.row()]
        for label, flag in (('Focus', 'pinned'), ('Waiting', 'waiting'), ('Meeting notes', 'marked_mom')):
            action = menu.addAction(label)
            action.setCheckable(True)
            action.setChecked(bool(task[flag]))
            action.triggered.connect(lambda _checked=False, field=flag: self.toggle_mark_task_as_mom() if field == 'marked_mom' else self._toggle_current_task_flag(field))
        menu.addSeparator()

        snooze = QMenu(menu)
        self._populate_task_snooze_menu(snooze)
        for action in snooze.actions():
            action.setParent(menu)
            action.setText(f"Snooze to {action.text().lower()}")
            action.setIcon(lucide_icon("clock", 15, T.NEUTRAL_700))
            menu.addAction(action)
        menu.addAction(lucide_icon("cal", 15, T.NEUTRAL_700), "Pick a due date…",
                       lambda _checked=False: self.pick_due_date_for_selected())
        if bool(str(task['due_date'] or '').strip()):
            menu.addAction(lucide_icon("x", 15, T.NEUTRAL_700), "Clear due date",
                           lambda _checked=False: self.snooze_selected_tasks("clear"))
        menu.addSeparator()
        menu.addAction(lucide_icon("copy", 15, T.NEUTRAL_700), "Duplicate", self.duplicate_selected_tasks)
        menu.addAction(lucide_icon("repeat", 15, T.NEUTRAL_700), "Recurrence…", self._open_recurrence_dialog)
        menu.addSeparator()
        menu_section_label(menu, "Move to")
        # Recent targets (this session), then pinned projects; everything else via "Other project…"
        quick = [pid for pid in self._recent_move_targets if pid != task['project_id']]
        quick += [r['id'] for r in self.project_model.rows[:self.project_model.hidden_start]
                  if r.get('starred') and r['id'] != task['project_id'] and r['id'] not in quick]
        for pid in quick[:8]:
            title = self._project_title(pid)
            if title is None:
                continue
            starred = bool(self.db.conn.execute("SELECT starred FROM projects WHERE id=?", (pid,)).fetchone()[0])
            menu.addAction(lucide_icon("pin" if starred else "folder", 15, T.NEUTRAL_700), title,
                           lambda _checked=False, target=pid: self.move_selected_tasks(target))
        menu.addAction(lucide_icon("arrow", 15, T.NEUTRAL_700), "Other project…", lambda _checked=False: self.move_selected_tasks())
        menu.addSeparator()
        delete = menu.addAction(lucide_icon("trash", 15, T.NEUTRAL_700), "Delete task", self.remove_selected_task)
        delete.setShortcutVisibleInContextMenu(True)
        delete.setShortcut(QKeySequence("Del"))
        delete.setShortcutContext(Qt.ShortcutContext.WidgetShortcut)

    def _open_recurrence_dialog(self):
        idx = self.task_view.currentIndex()
        tid = self.task_model.task_id_at(idx.row()) if idx.isValid() else None
        if tid is None:
            QMessageBox.information(self, "Recurrence", "Please select a task to configure recurrence.")
            return

        task_row = self.db.conn.execute(
            "SELECT title, done FROM tasks WHERE id=?",
            (tid,)
        ).fetchone()
        task_title = task_row['title'] if task_row else "Selected task"
        task_done = bool(task_row['done']) if task_row else False

        recurrence_data = self.db.get_task_recurrence(tid)
        is_recurring = bool(recurrence_data.get('recurring'))
        rule = _parse_recurrence_rule(recurrence_data.get('recurrence_rule'))

        now_dt = datetime.datetime.now()
        default_rule = {
            "mode": RECURRENCE_MODE_FIXED,
            "freq": RECURRENCE_FREQ_EVERY_N_DAYS,
            "interval": 1,
            "interval_days": 1,
            "time": recurrence_default_time(self._settings)
        }
        if not rule:
            rule = default_rule

        start_dt = _parse_recurrence_datetime(recurrence_data.get('recurrence_start_at')) or now_dt
        time_of_day = _parse_recurrence_time(rule.get("time"), start_dt.time())
        start_dt = start_dt.replace(hour=time_of_day.hour, minute=time_of_day.minute, second=0, microsecond=0)

        dialog = QDialog(self)
        dialog.setWindowTitle("Recurrence")
        dialog.setModal(True)
        dialog.setMinimumWidth(640)

        layout = QVBoxLayout(dialog)
        layout.setContentsMargins(16, 16, 16, 16)
        layout.setSpacing(12)

        title_label = QLabel(f"Configure recurrence for '{task_title}'.")
        title_label.setStyleSheet("color: #5d5d60; font-size: 12px;")
        title_label.setWordWrap(True)
        layout.addWidget(title_label)

        enable_checkbox = QCheckBox("Enable recurrence")
        enable_checkbox.setChecked(is_recurring)
        layout.addWidget(enable_checkbox)

        settings_widget = QWidget()
        settings_layout = QVBoxLayout(settings_widget)
        settings_layout.setContentsMargins(0, 0, 0, 0)
        settings_layout.setSpacing(8)

        # Mode row
        mode_row = QWidget()
        mode_layout = QHBoxLayout(mode_row)
        mode_layout.setContentsMargins(0, 0, 0, 0)
        mode_layout.setSpacing(8)
        mode_label = QLabel("Repeat")
        mode_label.setFixedWidth(100)
        mode_combo = QComboBox()
        mode_combo.addItem("On a fixed schedule", RECURRENCE_MODE_FIXED)
        mode_combo.addItem("After I complete it", RECURRENCE_MODE_COMPLETION)
        mode_combo.setItemData(0, "Comes back on set dates, done or not", Qt.ItemDataRole.ToolTipRole)
        mode_combo.setItemData(1, "Comes back a set time after you complete it", Qt.ItemDataRole.ToolTipRole)
        current_mode = rule.get("mode", RECURRENCE_MODE_FIXED)
        mode_index = mode_combo.findData(current_mode)
        mode_combo.setCurrentIndex(mode_index if mode_index >= 0 else 0)
        mode_layout.addWidget(mode_label)
        mode_layout.addWidget(mode_combo, stretch=1)
        settings_layout.addWidget(mode_row)

        # Pattern row
        pattern_row = QWidget()
        pattern_layout = QHBoxLayout(pattern_row)
        pattern_layout.setContentsMargins(0, 0, 0, 0)
        pattern_layout.setSpacing(8)
        pattern_label = QLabel("Unit")
        pattern_label.setFixedWidth(100)
        pattern_combo = QComboBox()
        # "Daily" and "Every N days" behave the same; both show as Days
        pattern_combo.addItem("days", RECURRENCE_FREQ_EVERY_N_DAYS)
        pattern_combo.addItem("weeks", RECURRENCE_FREQ_WEEKLY)
        pattern_combo.addItem("months", RECURRENCE_FREQ_MONTHLY)
        current_freq = rule.get("freq", RECURRENCE_FREQ_EVERY_N_DAYS)
        if current_freq == RECURRENCE_FREQ_DAILY:
            current_freq = RECURRENCE_FREQ_EVERY_N_DAYS
        pattern_index = pattern_combo.findData(current_freq)
        pattern_combo.setCurrentIndex(pattern_index if pattern_index >= 0 else 0)
        pattern_layout.addWidget(pattern_label)
        settings_layout.addWidget(pattern_row)
        pattern_row.hide()  # the unit sits next to the interval: "Every [2] [weeks]"

        # Interval row
        interval_row = QWidget()
        interval_layout = QHBoxLayout(interval_row)
        interval_layout.setContentsMargins(0, 0, 0, 0)
        interval_layout.setSpacing(8)
        interval_label = QLabel("Every")
        interval_label.setFixedWidth(100)
        interval_spin = QSpinBox()
        interval_spin.setRange(1, 365)
        interval_unit_label = QLabel("days")
        interval_layout.addWidget(interval_label)
        interval_layout.addWidget(interval_spin)
        interval_layout.addWidget(pattern_combo)
        pattern_combo.setMinimumWidth(110)
        interval_unit_label.hide()
        interval_layout.addStretch(1)
        settings_layout.addWidget(interval_row)

        # Weekly weekdays row
        weekdays_row = QWidget()
        weekdays_layout = QHBoxLayout(weekdays_row)
        weekdays_layout.setContentsMargins(0, 0, 0, 0)
        weekdays_layout.setSpacing(6)
        weekdays_label = QLabel("On")
        weekdays_label.setFixedWidth(100)
        weekdays_layout.addWidget(weekdays_label)
        weekday_checkboxes = []
        for day_num in range(1, 8):
            checkbox = QCheckBox(RECURRENCE_WEEKDAY_LABELS[day_num])
            weekday_checkboxes.append((day_num, checkbox))
            weekdays_layout.addWidget(checkbox)
        weekdays_layout.addStretch(1)
        settings_layout.addWidget(weekdays_row)

        # Monthly day-of-month row
        month_row = QWidget()
        month_layout = QHBoxLayout(month_row)
        month_layout.setContentsMargins(0, 0, 0, 0)
        month_layout.setSpacing(8)
        month_label = QLabel("On day")
        month_label.setFixedWidth(100)
        month_spin = QSpinBox()
        month_spin.setRange(1, 31)
        month_layout.addWidget(month_label)
        month_layout.addWidget(month_spin)
        month_layout.addStretch(1)
        settings_layout.addWidget(month_row)

        # Start date row
        start_row = QWidget()
        start_layout = QHBoxLayout(start_row)
        start_layout.setContentsMargins(0, 0, 0, 0)
        start_layout.setSpacing(8)
        start_label = QLabel("First time")
        start_label.setFixedWidth(100)
        start_date_edit = QDateEdit()
        start_date_edit.setCalendarPopup(True)
        start_date_edit.setDisplayFormat("ddd d MMM yyyy")
        start_date_edit.setDate(QDate(start_dt.year, start_dt.month, start_dt.day))
        start_date_edit.hide()  # holds the value; picked through the calendar button
        start_date_button = QPushButton()
        start_date_button.setIcon(lucide_icon("cal", 15, T.NEUTRAL_700))
        start_date_button.setStyleSheet("QPushButton { text-align: left; padding: 6px 10px; }")
        start_date_button.setCursor(Qt.CursorShape.PointingHandCursor)
        start_date_button.setToolTip("Pick the date of the first repeat")

        def _sync_start_button(date: QDate):
            start_date_button.setText(date.toString("ddd d MMM yyyy"))

        def _open_start_calendar():
            popup = QFrame(dialog, Qt.WindowType.Popup)
            popup.setFrameShape(QFrame.Shape.Box)
            box = QVBoxLayout(popup)
            box.setContentsMargins(6, 6, 6, 6)
            calendar_widget = DueCalendar(popup)
            calendar_widget.show_date(start_date_edit.date())

            def _picked(date: QDate):
                start_date_edit.setDate(date)
                popup.close()
            calendar_widget.picked.connect(_picked)
            box.addWidget(calendar_widget)
            popup.adjustSize()
            popup.move(start_date_button.mapToGlobal(QPoint(0, start_date_button.height() + 2)))
            popup.show()
            calendar_widget.setFocus()

        start_date_button.clicked.connect(_open_start_calendar)
        start_date_edit.dateChanged.connect(_sync_start_button)
        _sync_start_button(start_date_edit.date())
        start_layout.addWidget(start_label)
        start_layout.addWidget(start_date_edit)
        start_layout.addWidget(start_date_button, stretch=1)
        settings_layout.addWidget(start_row)

        # Time row
        time_row = QWidget()
        time_layout = QHBoxLayout(time_row)
        time_layout.setContentsMargins(0, 0, 0, 0)
        time_layout.setSpacing(8)
        time_label = QLabel("At")
        time_label.setFixedWidth(100)
        time_edit = QTimeEdit()
        time_edit.setDisplayFormat("HH:mm")
        time_edit.setTime(QTime(time_of_day.hour, time_of_day.minute))
        time_layout.addWidget(time_label)
        time_layout.addWidget(time_edit, stretch=1)
        default_time_checkbox = QCheckBox("Use as default for new repeats")
        default_time_checkbox.setToolTip("New repeating tasks get this time (Preferences → Planning)")
        time_layout.addWidget(default_time_checkbox)
        settings_layout.addWidget(time_row)

        preview_label = QLabel("")
        preview_label.setStyleSheet("color: #1d2d3d; background: #e5eff8; padding: 6px 8px; font-size: 12px;")
        preview_label.setWordWrap(True)
        settings_layout.addWidget(preview_label)

        layout.addWidget(settings_widget)

        button_box = QDialogButtonBox()
        clear_btn = button_box.addButton("Clear", QDialogButtonBox.ButtonRole.ActionRole)
        save_btn = button_box.addButton("Save", QDialogButtonBox.ButtonRole.AcceptRole)
        cancel_btn = button_box.addButton(QDialogButtonBox.StandardButton.Cancel)

        clear_btn.setStyleSheet("""
            QPushButton {
                background-color: #f2f2f3;
                border: 1px solid #d6d6d7;
                border-radius: 0;
                padding: 6px 12px;
                color: #1d2d3d;
            }
            QPushButton:hover {
                background-color: #e3e3e4;
            }
        """)

        save_btn.setStyleSheet("""
            QPushButton {
                background-color: #5980a6;
                border: 1px solid #5980a6;
                border-radius: 0;
                padding: 6px 16px;
                color: #f2f2f3;
                font-weight: 500;
            }
            QPushButton:hover {
                background-color: #597ea3;
            }
        """)

        cancel_btn.setStyleSheet("""
            QPushButton {
                background-color: #f2f2f3;
                border: 1px solid #d6d6d7;
                border-radius: 0;
                padding: 6px 16px;
                color: #1d1f20;
            }
            QPushButton:hover {
                background-color: #e3e3e4;
            }
        """)

        layout.addWidget(button_box)

        def _build_rule_from_ui() -> Dict[str, Any]:
            freq = pattern_combo.currentData()
            rule_data: Dict[str, Any] = {
                "mode": mode_combo.currentData(),
                "freq": freq,
                "interval": interval_spin.value(),
                "time": time_edit.time().toString("HH:mm")
            }
            if freq == RECURRENCE_FREQ_EVERY_N_DAYS:
                rule_data["interval_days"] = interval_spin.value()
            if freq == RECURRENCE_FREQ_WEEKLY:
                selected = [day for day, cb in weekday_checkboxes if cb.isChecked()]
                if selected:
                    rule_data["weekdays"] = selected
            if freq == RECURRENCE_FREQ_MONTHLY:
                rule_data["day_of_month"] = month_spin.value()
            return rule_data

        def _update_pattern_ui():
            freq = pattern_combo.currentData()
            unit = {RECURRENCE_FREQ_WEEKLY: "week", RECURRENCE_FREQ_MONTHLY: "month"}.get(freq, "day")
            interval_unit_label.setText(unit + ("s" if interval_spin.value() != 1 else ""))
            plural = "s" if interval_spin.value() != 1 else ""
            for index, name in enumerate(("day", "week", "month")):
                pattern_combo.setItemText(index, name + plural)

            # Weekdays and day of month only mean something on a fixed schedule
            fixed = mode_combo.currentData() == RECURRENCE_MODE_FIXED
            weekly = fixed and freq == RECURRENCE_FREQ_WEEKLY
            weekdays_row.setVisible(weekly)
            month_row.setVisible(fixed and freq == RECURRENCE_FREQ_MONTHLY)
            if weekly and not any(cb.isChecked() for _, cb in weekday_checkboxes):
                first_day = start_date_edit.date().dayOfWeek()
                for day_num, cb in weekday_checkboxes:
                    cb.setChecked(day_num == first_day)

            current_value = interval_spin.value()
            if freq == RECURRENCE_FREQ_EVERY_N_DAYS:
                interval_spin.setRange(1, 365)
            elif freq == RECURRENCE_FREQ_DAILY:
                interval_spin.setRange(1, 365)
            elif freq == RECURRENCE_FREQ_WEEKLY:
                interval_spin.setRange(1, 52)
            else:
                interval_spin.setRange(1, 24)
            if current_value < interval_spin.minimum():
                interval_spin.setValue(interval_spin.minimum())

        def _update_mode_ui():
            # After completion, the completion date is the start: no first date to pick
            start_row.setVisible(mode_combo.currentData() == RECURRENCE_MODE_FIXED)
            _update_pattern_ui()
            QTimer.singleShot(0, dialog.adjustSize)  # after the hidden rows leave the layout

        def _refresh_preview():
            if not enable_checkbox.isChecked():
                preview_label.setText("Recurrence is disabled.")
                return
            rule_preview = _build_rule_from_ui()
            summary = _format_recurrence_summary(rule_preview)
            if rule_preview.get("mode") == RECURRENCE_MODE_COMPLETION:
                if task_done:
                    next_dt = _compute_next_after_completion(rule_preview, datetime.datetime.now())
                    preview_label.setText(f"{summary}\nNext: {_format_recurrence_when(next_dt)}")
                else:
                    example = _compute_next_after_completion(rule_preview, datetime.datetime.now())
                    preview_label.setText(f"{summary}\nIf you complete it today, it comes back "
                                          f"{_format_recurrence_when(example)}")
                return

            start_date = start_date_edit.date()
            start_dt_local = datetime.datetime(
                start_date.year(), start_date.month(), start_date.day(),
                time_edit.time().hour(), time_edit.time().minute(), 0
            )
            next_dt = _compute_next_recurrence(rule_preview, start_dt_local, datetime.datetime.now())
            preview_label.setText(f"{summary}\nNext: {_format_recurrence_when(next_dt)}")

        def _apply_enabled_state():
            settings_widget.setEnabled(enable_checkbox.isChecked())
            _refresh_preview()

        def _apply_defaults_from_rule():
            freq = rule.get("freq", RECURRENCE_FREQ_EVERY_N_DAYS)
            if freq == RECURRENCE_FREQ_EVERY_N_DAYS:
                interval_spin.setValue(int(rule.get("interval_days", rule.get("interval", 1)) or 1))
            else:
                interval_spin.setValue(int(rule.get("interval", 1) or 1))
            if freq == RECURRENCE_FREQ_WEEKLY:
                weekdays = _normalize_recurrence_weekdays(rule.get("weekdays"), start_dt.isoweekday())
                for day_num, cb in weekday_checkboxes:
                    cb.setChecked(day_num in weekdays)
            else:
                for _, cb in weekday_checkboxes:
                    cb.setChecked(False)
            if freq == RECURRENCE_FREQ_MONTHLY:
                month_spin.setValue(int(rule.get("day_of_month", start_dt.day) or start_dt.day))
            else:
                month_spin.setValue(start_dt.day)

        _apply_defaults_from_rule()
        _update_pattern_ui()
        _update_mode_ui()
        _apply_enabled_state()

        enable_checkbox.toggled.connect(_apply_enabled_state)
        mode_combo.currentIndexChanged.connect(lambda _: (_update_mode_ui(), _refresh_preview()))
        pattern_combo.currentIndexChanged.connect(lambda _: (_update_pattern_ui(), _refresh_preview()))
        interval_spin.valueChanged.connect(lambda _: (_update_pattern_ui(), _refresh_preview()))
        month_spin.valueChanged.connect(lambda _: _refresh_preview())
        start_date_edit.dateChanged.connect(lambda _: _refresh_preview())
        enable_checkbox.toggled.connect(lambda on: default_time_checkbox.setChecked(False) if not on else None)
        time_edit.timeChanged.connect(lambda _: _refresh_preview())
        for _, cb in weekday_checkboxes:
            cb.toggled.connect(lambda _: _refresh_preview())

        def on_clear():
            enable_checkbox.setChecked(False)
            self.db.set_task_recurrence(
                tid,
                recurring=False,
                rule_text=None,
                start_at=None,
                next_at=None,
                last_at=None
            )
            dialog.accept()

        def on_save():
            if not enable_checkbox.isChecked():
                self.db.set_task_recurrence(
                    tid,
                    recurring=False,
                    rule_text=None,
                    start_at=None,
                    next_at=None,
                    last_at=None
                )
                dialog.accept()
                return

            now_local = datetime.datetime.now()
            rule_data = _build_rule_from_ui()
            freq = rule_data.get("freq")
            if freq == RECURRENCE_FREQ_WEEKLY:
                selected_days = rule_data.get("weekdays", [])
                if not selected_days:
                    mode_value = rule_data.get("mode")
                    if mode_value == RECURRENCE_MODE_FIXED:
                        start_date = start_date_edit.date()
                        fallback_day = start_date.dayOfWeek()
                    else:
                        fallback_day = now_local.isoweekday()
                    rule_data["weekdays"] = [fallback_day]

            mode_value = rule_data.get("mode")
            next_at = None
            start_at = None

            if mode_value == RECURRENCE_MODE_FIXED:
                start_date = start_date_edit.date()
                start_dt_local = datetime.datetime(
                    start_date.year(), start_date.month(), start_date.day(),
                    time_edit.time().hour(), time_edit.time().minute(), 0
                )
                start_at = _format_recurrence_datetime(start_dt_local)
                next_dt = _compute_next_recurrence(rule_data, start_dt_local, now_local)
                if not next_dt:
                    QMessageBox.warning(dialog, "Recurrence", "Unable to compute the next occurrence.")
                    return
                next_at = _format_recurrence_datetime(next_dt)
            else:
                if task_done:
                    completion_dt = now_local
                    start_at = _format_recurrence_datetime(completion_dt)
                    next_dt = _compute_next_after_completion(rule_data, completion_dt)
                    next_at = _format_recurrence_datetime(next_dt) if next_dt else None

            if default_time_checkbox.isChecked():
                self._settings.setValue(RECURRENCE_DEFAULT_TIME_KEY, time_edit.time().toString("HH:mm"))
            rule_text = json.dumps(rule_data)
            self.db.set_task_recurrence(
                tid,
                recurring=True,
                rule_text=rule_text,
                start_at=start_at,
                next_at=next_at,
                last_at=recurrence_data.get('recurrence_last_at')
            )
            dialog.accept()

        clear_btn.clicked.connect(on_clear)
        save_btn.clicked.connect(on_save)
        button_box.rejected.connect(dialog.reject)

        if dialog.exec() == QDialog.DialogCode.Accepted:
            self.detail_state.recurrence = self.db.get_task_recurrence(tid)
            self._sync_detail_recurrence()
            if self._is_searching():
                self.apply_search_filter()
            else:
                self.task_model.reload()

    def on_due_date_changed(self, _date):
        # Deprecated - keeping for compatibility but not used anymore
        pass
    
    def _save_due_date_now(self):
        idx = self.task_view.currentIndex()
        tid = self.task_model.task_id_at(idx.row()) if idx.isValid() else None
        if tid is None:
            return
        # Only save if date is actually set (not just cleared)
        if not self.detail_state.due_date:
            return
        date = self.detail_state.due_date
        # Store as ISO format date string (YYYY-MM-DD 00:00:00)
        date_str = f"{date.year():04d}-{date.month():02d}-{date.day():02d} 00:00:00"
        self.task_model.linger([tid])
        self.db.set_task_due_date(tid, date_str)
        self._refresh_after_task_metadata_change(tid)
    
    def on_stakeholders_changed(self, _text: str):
        self._save_stakeholders_now()
    
    def on_project_filter_changed(self, text: str):
        """Handle project filter text changes (applied after a short pause in typing)."""
        self._project_filter_timer.start()

    def _apply_project_filter(self):
        self.project_model.set_project_name_filter(self.project_filter_edit.text())
        self._sync_filter_hidden_expansion()

    def _move_dropped_tasks(self, row: int, task_ids: List[int]):
        """Tasks dragged from the list onto a sidebar project: move them there (with undo)."""
        project_id = self.project_model.project_id_at(row)
        if project_id is None:
            return
        wanted = set(task_ids)
        selection = self.task_view.selectionModel()
        selected = {self.task_model.task_id_at(i.row()) for i in selection.selectedRows()}
        if selected != wanted:
            selection.clearSelection()
            for model_row in range(self.task_model.rowCount()):
                if self.task_model.task_id_at(model_row) in wanted:
                    selection.select(self.task_model.index(model_row, 0),
                                     QItemSelectionModel.SelectionFlag.Select | QItemSelectionModel.SelectionFlag.Rows)
        self.move_selected_tasks(target_project_id=project_id)

    def _open_first_filtered_project(self):
        """Enter in the project filter: open the first matching project and move to its tasks."""
        if self._project_filter_timer.isActive():
            self._project_filter_timer.stop()
            self._apply_project_filter()
        project_id = self.project_model.project_id_at(0)
        if project_id is None:
            return
        self.set_current_view(('project', project_id))
        self.task_view.setFocus()

    def _sync_filter_hidden_expansion(self):
        """While the name filter has text, HIDDEN is open so matching hidden projects show too."""
        saved = self._settings.value('MainWindow/HiddenExpanded', 'false') in ('true', '1', 'True')
        want = bool(self.project_model.project_name_filter) or saved
        if self.project_model.include_hidden_rows != want:
            self.sidebar.set_hidden_expanded(want)
            self._on_hidden_section_toggled(want, reapply=False, persist=False)

    def _ensure_project_selected_for_task(self, task_id: int):
        """Kept for compatibility. The sidebar follows current_view, not the selected task."""
        return

    def _save_stakeholders_now(self):
        tid = self.detail_state.task_id
        if tid is None:
            return
        self.db.set_stakeholders_for_task(tid, self.detail_state.people)
        self.task_model.refresh_row_details()

    def _clear_due_date(self):
        """Clear the due date for the current task (kept for compatibility)."""
        idx = self.task_view.currentIndex()
        tid = self.task_model.task_id_at(idx.row()) if idx.isValid() else None
        if tid is None:
            return
        # Set due_date to None in database
        self.task_model.linger([tid])
        self.db.set_task_due_date(tid, None)
        # Update the UI state
        self.detail_state.due_date = None
        self._sync_detail_due()
        self._refresh_after_task_metadata_change(tid)
    
    def _set_next_monday(self):
        """Set the due date to the next Monday (kept for compatibility)."""
        current = QDate.currentDate()
        # Monday is 1, Sunday is 7 in Qt
        current_day = current.dayOfWeek()
        if current_day == 1:  # If today is Monday
            days_to_add = 7  # Go to next Monday
        else:
            days_to_add = 8 - current_day  # Days until next Monday
        next_monday = current.addDays(days_to_add)
        self._on_detail_due_changed(next_monday)

    def on_task_completion_stamped(self, task_id: int):
        if self.notes.task_id == task_id:
            # Reload from DB to reflect appended completion stamp
            self.notes.set_task(task_id)

    def on_tasks_changed(self):
        if self._handling_tasks_changed or self._suppress_tasks_refresh:
            return
        if getattr(self, '_switching_view', False):
            return  # a view switch reloads the list but changes no data
        self._handling_tasks_changed = True
        try:
            if self._is_searching() and not self._applying_search:
                # In search mode, reapply search only for real data changes
                QTimer.singleShot(25, self.apply_search_filter)
            # Counts and visibility in the sidebar; the list view keeps its own selection
            self.project_model.refresh_visibility()
            if self.current_view[0] == 'project' and not self._is_searching():
                self._select_project_silently(self.current_view[1])
            self._update_comprehensive_status()
        finally:
            self._handling_tasks_changed = False

    def _close_active_editors(self):
        """Close any active inline editors to prevent conflicts during model operations."""
        try:
            # Close editors in task view
            if self.task_view.state() == QTableView.State.EditingState:
                self.task_view.closePersistentEditor(self.task_view.currentIndex())
            
            # Close editors in project view
            if self.project_view.state() == QListView.State.EditingState:
                self.project_view.closePersistentEditor(self.project_view.currentIndex())
                
            # Force delegates to clear their active editor tracking
            for i in range(self.task_model.columnCount()):
                delegate = self.task_view.itemDelegateForColumn(i)
                if hasattr(delegate, '_active_editors'):
                    delegate._active_editors.clear()
                    delegate._update_defer_flag()
            
            project_delegate = self.project_view.itemDelegate()
            if hasattr(project_delegate, '_active_editors'):
                project_delegate._active_editors.clear()
                project_delegate._update_defer_flag()
                
        except Exception as e:
            print(f"Error closing active editors: {e}")

    def clear_all_filters(self):
        """Clear all search terms and reset filters to default state."""
        current_project_idx = self.project_view.currentIndex()
        current_project_id = self.project_model.project_id_at(current_project_idx.row()) if current_project_idx.isValid() else None
        current_task_idx = self.task_view.currentIndex()
        current_task_id = self.task_model.task_id_at(current_task_idx.row()) if current_task_idx.isValid() else None

        self.db.clear_all_force_visibility()

        self.search_edit.blockSignals(True)
        self.search_edit.clear()
        self.search_edit.blockSignals(False)
        self.search_terms = []
        self.search_stakeholder_terms = []
        self.search_query = None
        self._note_search_keep_id = None
        self._note_search_query = None
        self._search_debounce.stop()
        self._search_results_by_project.clear()

        if hasattr(self, 'project_filter_edit'):
            self.project_filter_edit.blockSignals(True)
            self.project_filter_edit.clear()
            self.project_filter_edit.blockSignals(False)
        self.project_model.project_name_filter = ""
        self.project_model._search_filter_ids = None

        if hasattr(self, 'act_show_pinned_filter'):
            self.act_show_pinned_filter.blockSignals(True)
            self.act_show_pinned_filter.setChecked(False)
            self.act_show_pinned_filter.blockSignals(False)
        if hasattr(self, 'act_show_no_due_dates'):
            self.act_show_no_due_dates.blockSignals(True)
            self.act_show_no_due_dates.setChecked(False)
            self.act_show_no_due_dates.blockSignals(False)
        if hasattr(self, 'act_toggle_done'):
            self.act_toggle_done.blockSignals(True)
            self.act_toggle_done.setChecked(False)
            self.act_toggle_done.blockSignals(False)

        self.include_done = False
        self.pinned_only = False
        self.show_no_due_dates = False
        self.task_model.include_done = False
        self.task_model.pinned_only = False
        self.task_model.show_no_due_dates = False
        self.project_model.include_done = False

        try:
            self._settings.setValue('MainWindow/PinnedOnly', 'false')
            self._settings.sync()
        except Exception:
            pass

        self.project_model.search_match_counts = None
        self.project_model.reload()

        # Clearing filters keeps the current project or smart view
        self._apply_current_view()
        if current_task_id is not None:
            self._select_task_if_visible(current_task_id)

    def set_search_include_done(self, checked: bool):
        """Search chip / View > Include Completed in Search (Alt+C): show completed tasks in results."""
        checked = bool(checked)
        self.search_include_done = checked
        self.search_edit.set_include_done(checked)
        self.act_search_include_done.blockSignals(True)
        self.act_search_include_done.setChecked(checked)
        self.act_search_include_done.blockSignals(False)
        try:
            self._settings.setValue('Search/IncludeCompleted', 'true' if checked else 'false')
            self._settings.sync()
        except Exception:
            pass
        if self._is_searching():
            self.apply_search_filter()
        self._update_comprehensive_status()

    def _keep_completed_in_search(self, task_ids: List[int]):
        """Tasks completed from the results stay listed (faded) until the query changes."""
        if self._is_searching():
            self._search_kept_done_ids.update(task_ids)

    def on_search_changed(self, text: str):
        if text.strip():
            self._search_debounce.start()
        else:
            # Clearing the box is instant
            self._search_debounce.stop()
            self._run_search()

    def _run_search(self):
        if not self.notes.flush_pending():
            self.search_edit.blockSignals(True)
            self.search_edit.setText(getattr(self, '_applied_search_text', ''))
            self.search_edit.blockSignals(False)
            return
        text = self.search_edit.text()
        self._applied_search_text = text
        query = search_query.parse_query(text)
        previous = self.search_query
        if previous is not None and query == previous:
            return  # only spacing changed
        self._search_kept_done_ids = set()
        # Clear force_visibility when search changes
        self.db.clear_all_force_visibility()

        # Clear project filter when main search changes
        if hasattr(self, 'project_filter_edit'):
            self.project_filter_edit.blockSignals(True)
            self.project_filter_edit.clear()
            self.project_filter_edit.blockSignals(False)
            self.project_model.set_project_name_filter("")

        # Preserve current selections before applying search
        self._preserve_current_focus()

        self.search_query = query if query.active else None
        self.search_terms = list(query.terms)
        self.search_stakeholder_terms = list(query.stakeholders)
        self.apply_search_filter()
        if self.search_query is not None and self.task_model.rowCount():
            self._remember_search(text)
        # Update status bar after search changes
        self._update_comprehensive_status()

    # ----- recent searches -----
    RECENT_SEARCHES_MAX = 8

    def _recent_searches(self) -> List[str]:
        try:
            items = json.loads(str(self._settings.value('Search/Recent', '[]') or '[]'))
            return [str(x) for x in items if isinstance(x, str) and x.strip()][:self.RECENT_SEARCHES_MAX]
        except Exception:
            return []

    def _remember_search(self, text: str):
        """Keep the query in the recent list. While typing, a longer or shorter version of the
        latest entry replaces it, so "ras", "rasp", "raspi" leave one entry."""
        text = text.strip()
        if not text:
            return
        recent = self._recent_searches()
        if recent and (text.startswith(recent[0]) or recent[0].startswith(text)):
            recent.pop(0)
        recent = [text] + [r for r in recent if r != text]
        try:
            self._settings.setValue('Search/Recent', json.dumps(recent[:self.RECENT_SEARCHES_MAX]))
        except Exception:
            pass

    def _show_recent_searches(self):
        """Recent queries under the empty search box (focus, Down arrow or a click)."""
        recent = self._recent_searches()
        if not recent:
            return
        menu = QMenu(self.search_edit)
        menu.aboutToHide.connect(menu.deleteLater)
        menu_section_label(menu, "Recent searches")
        for text in recent:
            action = menu.addAction(lucide_icon("search", 14, T.NEUTRAL_600), text)
            action.triggered.connect(lambda _=False, q=text: self._use_recent_search(q))
        menu.addSeparator()
        menu.addAction("Clear recent searches", lambda: self._settings.setValue('Search/Recent', '[]'))
        menu.addSeparator()
        hint = menu.addAction('Tip: "phrase"  -word  @name  in:project  is:done / is:open')
        hint.setEnabled(False)
        menu.popup(self.search_edit.mapToGlobal(QPoint(0, self.search_edit.height() + 2)))

    def _use_recent_search(self, text: str):
        self.search_edit.setText(text)
        self.search_edit.setFocus(Qt.FocusReason.OtherFocusReason)
        self.search_edit.end(False)

    def apply_search_filter(self):
        if self._applying_search:
            return
        self._applying_search = True
        try:
            # Check for active editors and defer if necessary
            active_editors = False
            for delegate in [self.task_view.itemDelegateForColumn(i) for i in range(self.task_model.columnCount())]:
                if hasattr(delegate, '_active_editors') and delegate._active_editors:
                    active_editors = True
                    break

            if active_editors:
                # Defer the search until editors are closed
                QTimer.singleShot(100, self.apply_search_filter)
                return

            current_task_id = None
            task_idx = self.task_view.currentIndex()
            if task_idx.isValid():
                current_task_id = self.task_model.task_id_at(task_idx.row())

            query = self.search_query
            if query is None or not query.active:
                # Clear search: back to the view that was open before searching
                self._search_results_by_project.clear()
                self._search_hidden_done_count = 0
                self.project_model.set_search_matches(None)
                self._apply_current_view()
                if current_task_id is not None:
                    self._select_task_if_visible(current_task_id)
                return

            # One query across all projects: words AND, @names OR, project names count as text
            rows = self.task_model.apply_secondary_filters(self.db.search_tasks(query, self.pinned_only))

            # Keep the open note in place while its edits remove the final match.
            no_longer_matches = None
            edited_id = self.notes.task_id
            if edited_id is not None and edited_id == getattr(self, '_note_search_keep_id', None) and edited_id in self.task_model._search_details and not any(r['id'] == edited_id for r in rows):
                kept = self.db.conn.execute('SELECT * FROM tasks WHERE id=?', (edited_id,)).fetchone()
                if kept is not None and getattr(self, '_note_search_query', None) == query:
                    rows.append(kept)
                    no_longer_matches = edited_id
            self._note_search_query = query

            # Completed tasks only when asked for (is:done or the Completed chip), or completed just now
            hidden_done = 0
            if query.status is None and not self.search_include_done:
                kept = self._search_kept_done_ids
                visible = [t for t in rows if not bool(t['done']) or t['id'] in kept]
                hidden_done = len(rows) - len(visible)
                rows = visible
            self._search_hidden_done_count = hidden_done

            # Relevance: where each word matched decides the order and what is highlighted
            projects = {r['id']: r for r in self.db.list_projects()}
            terms = query.folded_terms()
            notes_text = self.db.notes_plain_text([r['id'] for r in rows]) if terms else {}
            scores: Dict[int, int] = {}
            details: Dict[int, Dict[str, Any]] = {}
            for r in rows:
                title = str(r['title'] or '')
                folded_title = search_query.fold(title)
                project = projects.get(r['project_id'])
                folded_project = search_query.fold(str(project['title'] or '')) if project else ""
                note = notes_text.get(r['id'], "")
                folded_note = search_query.fold(note) if note else ""
                in_title = [t for t in terms if t in folded_title]
                scores[r['id']] = search_query.score_task(
                    sum(folded_title.count(t) for t in terms),
                    sum(1 for t in terms if t in folded_project),
                    sum(folded_note.count(t) for t in terms),
                    len(in_title), len(terms))
                info: Dict[str, Any] = {'title_spans': search_query.find_spans(title, terms)}
                if r['id'] == no_longer_matches:
                    info['excerpt'] = ('No longer matches — editing this note', [])
                # Words that are not in the title: show where the notes mention them
                missing = [t for t in terms if t not in folded_title and t in folded_note]
                if missing and r['id'] != no_longer_matches:
                    info['excerpt'] = search_query.excerpt(note, missing)
                details[r['id']] = info

            grouped: dict[int, List[sqlite3.Row]] = {}
            for r in rows:
                grouped.setdefault(r['project_id'], []).append(r)
            grouped = {
                project_id: sorted(self.db.order_task_rows_for_project(project_id, tasks),
                                   key=lambda t: -scores.get(t['id'], 0))
                for project_id, tasks in grouped.items()
            }

            self._search_results_by_project = grouped
            self.project_model.set_search_matches({project_id: len(tasks) for project_id, tasks in grouped.items()})

            # No sidebar item is active while searching; results from every project share one list.
            # More title matches first, then best relevance; ties keep sidebar order.
            self._select_project_silently(None)
            sidebar_order = {r['id']: i for i, r in enumerate(self.project_model.rows)}
            ordered_ids = sorted(grouped, key=lambda pid: (-sum(bool(details[t['id']]['title_spans']) for t in grouped[pid]),
                                                           -max(scores.get(t['id'], 0) for t in grouped[pid]),
                                                           sidebar_order.get(pid, len(sidebar_order))))

            normally_hidden = {
                r['id'] for r in self.project_model.rows + self.project_model.hidden_rows
                if not self.project_model._is_listed(r, self.project_model.auto_hide_finished())
            }

            def group_title(project_id: int) -> str:
                project = projects.get(project_id)
                if project is None:
                    return ""
                hidden = project_id in normally_hidden
                return f"{project['title']}  · hidden project" if hidden else str(project['title'])

            self._keep_project_listed(None)
            self.task_model.collapsed = self._collapsed_groups.setdefault(('search', None), set())
            self._suppress_tasks_refresh = True
            self.task_model.set_search_results(
                [(project_id, group_title(project_id), grouped[project_id]) for project_id in ordered_ids],
                details,
            )
            self._suppress_tasks_refresh = False
            self._sync_view_chrome(None)
            # The list view kept the selected task if it is among the results
            self._select_first_task_if_needed()

        finally:
            self._applying_search = False
            self._suppress_search_restore = False
            self._preserve_project_id = None
            self._preserve_task_id = None

    def _select_fallback_project(self) -> Optional[int]:
        """Select the first available project as fallback and return its ID."""
        if self.project_model.rowCount() > 0:
            first_idx = self.project_model.index(0, 0)
            self.project_view.setCurrentIndex(first_idx)
            self.project_view.scrollTo(first_idx, QListView.ScrollHint.EnsureVisible)
            return self.project_model.project_id_at(0)
        else:
            # No projects available
            self.project_view.clearSelection()
            return None

    def _restore_task_selection_after_search(self, task_id: int):
        """Restore task selection after search filter has been applied."""
        for row in range(self.task_model.rowCount()):
            if self.task_model.task_id_at(row) == task_id:
                self._select_task_row(row)
                break

    def _preserve_current_focus(self):
        """Store current project and task selection for restoration after search."""
        # Preserve current project selection
        project_idx = self.project_view.currentIndex()
        if project_idx.isValid():
            self._preserve_project_id = self.project_model.project_id_at(project_idx.row())
        else:
            self._preserve_project_id = None
            
        # Preserve current task selection
        task_idx = self.task_view.currentIndex()
        if task_idx.isValid():
            self._preserve_task_id = self.task_model.task_id_at(task_idx.row())
        else:
            self._preserve_task_id = None

    def _restore_preserved_focus(self):
        """Re-select the task that was selected before a refresh, if it is still listed."""
        task_id, self._preserve_task_id, self._preserve_project_id = self._preserve_task_id, None, None
        if task_id is not None:
            self._select_task_if_visible(task_id)

    def _select_project_row_preserving_slot(self, desired_row: int):
        """Select the project at the desired row slot after model changes, with safe fallback."""
        total_rows = self.project_model.rowCount(QModelIndex())
        if total_rows <= 0:
            # Nothing left; clear selection and related context
            self.project_view.clearSelection()
            self.task_model.set_context(None, self.include_done, self.pinned_only)
            if self.notes.set_task(None) is False:
                return
            self._update_right_pane_state(False)
            return
        target_row = max(0, min(desired_row, total_rows - 1))
        self.project_view.setCurrentIndex(self.project_model.index(target_row, 0))

    def _restore_task_focus(self):
        """Restore task selection within the currently selected project."""
        if self._preserve_task_id is None:
            # Clear preserved state if no task to restore
            self._preserve_project_id = None
            self._preserve_task_id = None
            return
            
        try:
            # Look for the preserved task in current task model
            for row in range(self.task_model.rowCount()):
                if self.task_model.task_id_at(row) == self._preserve_task_id:
                    # Task found, select it
                    task_index = self.task_model.index(row, 0)
                    self.task_view.setCurrentIndex(task_index)
                    self.task_view.selectRow(row)
                    # Ensure task is visible
                    self.task_view.scrollTo(task_index, QTableView.ScrollHint.EnsureVisible)
                    
                    # Clear preserved state after successful restoration
                    self._preserve_project_id = None
                    self._preserve_task_id = None
                    return
                    
            # Task not found in current view, try to find nearest task
            self._select_nearest_task()
            
        except Exception as e:
            print(f"Error restoring task focus: {e}")
        finally:
            # Always clear preserved state
            self._preserve_project_id = None
            self._preserve_task_id = None

    def _select_nearest_task(self):
        """Select the first visible task, or clear the detail pane when the list is empty."""
        try:
            rows = self.task_view.visible_rows()
            if rows:
                self._select_task_row(rows[0])
            else:
                self._clear_current_task_context()
        except Exception as e:
            print(f"Error selecting nearest task: {e}")

    def _ensure_selection_fallback(self):
        """Ensure something is selected when focus restoration fails."""
        try:
            # Try to select first available project
            if self.project_model.rowCount() > 0:
                first_project_index = self.project_model.index(0, 0)
                self.project_view.setCurrentIndex(first_project_index)
                self.project_view.scrollTo(first_project_index, QListView.ScrollHint.EnsureVisible)
                
                # Try to select first task in that project
                QTimer.singleShot(25, lambda: self._select_nearest_task())
        except Exception as e:
            print(f"Error in selection fallback: {e}")

    def _update_task_status(self, task_id: Optional[int]):
        """Update status bar to show task creation date and last modified date when a task is selected."""
        if not hasattr(self, 'status_task_info') or self.status_task_info is None:
            return
        
        if task_id is None:
            # No task selected, clear the right side
            self.status_task_info.setText("")
            self.status_task_info.setToolTip("")
            # Update left side with comprehensive status
            self._update_comprehensive_status()
            return
        
        try:
            # Get task creation date and last modified from database
            task_row = self.db.conn.execute(
                "SELECT created_at, updated_at, title FROM tasks WHERE id=?", 
                (task_id,)
            ).fetchone()
            
            if task_row:
                created_at = task_row['created_at']
                updated_at = task_row['updated_at']
                task_title = task_row['title']
                
                status_info = []
                tooltip_parts = [f"Task: {task_title}"]
                
                # Parse and format creation date
                if created_at:
                    try:
                        # Handle ISO format: 2025-10-12T14:30:45.123Z
                        if 'T' in created_at:
                            dt = datetime.datetime.fromisoformat(created_at.replace('Z', '+00:00'))
                        else:
                            dt = datetime.datetime.strptime(created_at, '%Y-%m-%d %H:%M:%S')
                        
                        formatted_date = dt.strftime('%Y-%m-%d %H:%M')
                        status_info.append(f"Created: {formatted_date}")
                        tooltip_parts.append(f"Created: {formatted_date}")
                    except (ValueError, AttributeError) as e:
                        print(f"Date parsing error for created_at: {e}")
                
                # Parse and format last modified date
                if updated_at:
                    try:
                        # Handle ISO format
                        if 'T' in updated_at:
                            dt = datetime.datetime.fromisoformat(updated_at.replace('Z', '+00:00'))
                        else:
                            dt = datetime.datetime.strptime(updated_at, '%Y-%m-%d %H:%M:%S')
                        
                        formatted_date = dt.strftime('%Y-%m-%d %H:%M')
                        status_info.append(f"Modified: {formatted_date}")
                        tooltip_parts.append(f"Last Modified: {formatted_date}")
                    except (ValueError, AttributeError) as e:
                        print(f"Date parsing error for updated_at: {e}")
                
                # Update right side with task dates
                if status_info:
                    status_text = " • ".join(status_info)
                    self.status_task_info.setText(status_text)
                    self.status_task_info.setToolTip("\n".join(tooltip_parts))
                else:
                    self.status_task_info.setText("")
                    self.status_task_info.setToolTip("")
                
                # Update left side with comprehensive status
                self._update_comprehensive_status()
            else:
                # No task data available, clear right side
                self.status_task_info.setText("")
                self.status_task_info.setToolTip("")
                self._update_comprehensive_status()
                
        except Exception as e:
            print(f"Error updating task status: {e}")
            # Clear right side on error
            self.status_task_info.setText("")
            self.status_task_info.setToolTip("")
            self._update_comprehensive_status()
    
    def _update_comprehensive_status(self, *_args):
        """Schedule one refresh of the status bar and the sidebar counts.

        Many code paths ask for this during a single action; they collapse into one refresh
        when control returns to the event loop.
        """
        timer = getattr(self, '_status_update_timer', None)
        if timer is None:
            timer = self._status_update_timer = QTimer(self)
            timer.setSingleShot(True)
            timer.setInterval(0)
            timer.timeout.connect(self._update_comprehensive_status_now)
        if not timer.isActive():
            timer.start()

    def _update_comprehensive_status_now(self):
        """Update comprehensive status bar with project information: count, filters, search results, creation date."""
        # Safety check to ensure status bar is initialized
        if not hasattr(self, 'status_comprehensive') or self.status_comprehensive is None:
            return
        
        # Clear right status bar if no task is selected
        if hasattr(self, 'status_task_info') and self.status_task_info is not None:
            # Check if task view has a valid selection
            if hasattr(self, 'task_view'):
                selected_indexes = self.task_view.selectionModel().selectedRows() if self.task_view.selectionModel() else []
                if not selected_indexes:
                    self.status_task_info.setText("")
                    self.status_task_info.setToolTip("")
            
        try:
            project_count = self.project_model.rowCount()
            project_idx = self.project_view.currentIndex()

            # Counts change only when the data does: skip the queries after a view switch
            key = self._counts_key()
            if key != getattr(self, '_counts_cache_key', None):
                total = self.db.conn.execute("SELECT COUNT(*) FROM projects").fetchone()[0]
                self._counts_cache = (self._status_counts(), int(total))
                self._refresh_sidebar_counts()
                self._counts_cache_key = key
            (open_count, overdue_count), total_projects = self._counts_cache
            self.status_comprehensive.setText(f"{open_count} open · {overdue_count} overdue")

            filter_labels = self._active_filter_labels()
            self.status_filter.setText(f"Filter: {' + '.join(filter_labels)}" if filter_labels else "")
            self.status_filter.setVisible(bool(filter_labels))

            # Update tooltip with full information
            tooltip_parts = []
            
            tooltip_parts.append("PROJECT INFORMATION:")
            tooltip_parts.append(f"  • Visible projects: {project_count}")
            tooltip_parts.append(f"  • Total projects: {total_projects}")
            if project_count < total_projects:
                tooltip_parts.append(f"  • Hidden: {total_projects - project_count}")
            
            tooltip_parts.append("\nACTIVE FILTERS:")
            tooltip_parts.append(f"  • Show completed: {'Yes' if self.include_done else 'No'}")
            tooltip_parts.append(f"  • Pinned only: {'Yes' if self.pinned_only else 'No'}")
            
            if self._is_searching():
                tooltip_parts.append("\nACTIVE SEARCH:")
                if self.search_terms:
                    tooltip_parts.append(f"  • Text terms: {', '.join(self.search_terms)}")
                if self.search_stakeholder_terms:
                    tooltip_parts.append(f"  • Stakeholders: @{', @'.join(self.search_stakeholder_terms)}")
                tooltip_parts.append(f"  • Results: {project_count}")
            
            # Add selected project info to tooltip
            if project_idx.isValid():
                project_id = self.project_model.project_id_at(project_idx.row())
                if project_id:
                    project_data = next((p for p in self.project_model.rows if p['id'] == project_id), None)
                    if project_data:
                        tooltip_parts.append("\nSELECTED PROJECT:")
                        tooltip_parts.append(f"  • Title: {project_data['title']}")
                        try:
                            project_row = self.db.conn.execute(
                                "SELECT created_at FROM projects WHERE id=?", 
                                (project_id,)
                            ).fetchone()
                            if project_row and project_row['created_at']:
                                created_at = project_row['created_at']
                                if 'T' in created_at:
                                    dt = datetime.datetime.fromisoformat(created_at.replace('Z', '+00:00'))
                                else:
                                    dt = datetime.datetime.strptime(created_at, '%Y-%m-%d %H:%M:%S')
                                formatted_date = dt.strftime('%Y-%m-%d %H:%M')
                                tooltip_parts.append(f"  • Created: {formatted_date}")
                        except Exception:
                            pass
            
            self._status_project_tooltip = "\n".join(tooltip_parts)
            
        except Exception as e:
            # Fallback to simple status on any error
            if hasattr(self, 'status_comprehensive') and self.status_comprehensive is not None:
                self.status_comprehensive.setText("")
                self._status_project_tooltip = ""
            print(f"Error updating comprehensive status: {e}")

    # -------------------------- splitter persistence --------------------------
    def _save_splitter_sizes(self):
        try:
            self._settings.setValue('MainSplitter/Sizes', self.splitter.sizes())
            self._settings.sync()
        except Exception:
            pass

    def _restore_splitter_sizes(self):
        try:
            val = self._settings.value('MainSplitter/Sizes')
            if val:
                if isinstance(val, (list, tuple)):
                    sizes = [int(x) for x in val if int(x) > 0]
                else:
                    # comma separated fallback
                    sizes = [int(x) for x in str(val).split(',') if x.strip().isdigit()]
                if len(sizes) == 3 and sum(sizes) > 0:
                    self.splitter.setSizes(sizes)
                    return
            # No saved sizes: 232 / 420 / rest, narrower panes under ~1100px
            width = max(self.width(), 600)
            side = 200 if width < 1100 else 232
            middle = int(max(300, min(420, width * 0.32))) if width < 1100 else 420
            self.splitter.setSizes([side, middle, max(200, width - side - middle)])
        except Exception:
            pass

    # -------------------------- window state persistence --------------------------
    def _save_window_state(self):
        """Save window state - only size/position when not maximized, always save maximized flag."""
        try:
            is_maximized = self.isMaximized()
            is_minimized = self.isMinimized()
            self._settings.setValue('MainWindow/Maximized', 'true' if is_maximized else 'false')
            
            # Only save size and position when not maximized or minimized
            if not is_maximized and not is_minimized:
                sz = self.size()
                pos = self.pos()
                self._settings.setValue('MainWindow/Size', f"{sz.width()},{sz.height()}")
                self._settings.setValue('MainWindow/Position', f"{pos.x()},{pos.y()}")
                
                # Save screen name for multi-monitor support
                screen = self.screen()
                if screen:
                    self._settings.setValue('MainWindow/ScreenName', screen.name())
            
            self._settings.sync()
        except Exception:
            pass

    def _restore_geometry_to_monitor(self, geometry: QRect, target_screen: QScreen = None, is_first: bool = False):
        """Restore window geometry to specific monitor (Windows 11 multi-monitor fix)."""
        # Only set pending flag on first attempt
        if is_first:
            self._restore_pending = True
        
        if not self._restore_pending:
            return
            
        try:
            # Validate the geometry is still reasonable
            if not geometry or geometry.width() < 200 or geometry.height() < 200:
                if is_first:
                    self._restore_pending = False
                return
            
            # If we have a target screen, try to restore to it
            if target_screen:
                # Check if the target screen still exists
                app = QApplication.instance()
                if app and target_screen in app.screens():
                    # Screen still exists, use it
                    screen_geometry = target_screen.availableGeometry()
                    
                    # Ensure the window fits within the screen bounds
                    x = max(screen_geometry.x(), geometry.x())
                    y = max(screen_geometry.y(), geometry.y())
                    
                    # Adjust if window would go off-screen
                    if x + geometry.width() > screen_geometry.x() + screen_geometry.width():
                        x = screen_geometry.x() + screen_geometry.width() - geometry.width()
                    if y + geometry.height() > screen_geometry.y() + screen_geometry.height():
                        y = screen_geometry.y() + screen_geometry.height() - geometry.height()
                    
                    restored_geometry = QRect(x, y, geometry.width(), geometry.height())
                    self.setGeometry(restored_geometry)
                    return
            
            # Fallback: just restore the geometry as-is
            self.setGeometry(geometry)
            
        except Exception as ex:
            print(f"Error restoring geometry to monitor: {ex}")
            if is_first:
                self._restore_pending = False
    
    def _clear_minimize_state(self):
        """Clear the saved minimize state after restore attempts are complete."""
        self._geometry_before_minimize = None
        self._screen_before_minimize = None
        self._restore_pending = False
    
    def _restore_window_state(self):
        """Restore window state - handle maximized vs normal window positioning."""
        try:
            # Check if window should be maximized
            maximized = self._settings.value('MainWindow/Maximized', 'false') in ('true','1','True')
            
            if maximized:
                # For maximized windows, just maximize - don't set size or position
                self.showMaximized()
            else:
                # For normal windows, restore size and position with multi-monitor support
                self._restore_window_size_and_position()
        except Exception:
            # Fallback: just center with default size
            QTimer.singleShot(100, self._center_window)

    def _restore_window_size_and_position(self):
        """Restore window size and position with multi-monitor awareness."""
        try:
            # Try to restore both size and position
            size_val = self._settings.value('MainWindow/Size')
            pos_val = self._settings.value('MainWindow/Position')
            screen_name = self._settings.value('MainWindow/ScreenName')
            
            if size_val and pos_val:
                # Parse size
                size_parts = [p.strip() for p in str(size_val).split(',') if p.strip().lstrip('-').isdigit()]
                pos_parts = [p.strip() for p in str(pos_val).split(',') if p.strip().lstrip('-').isdigit()]
                
                if len(size_parts) == 2 and len(pos_parts) == 2:
                    w, h = int(size_parts[0]), int(size_parts[1])
                    x, y = int(pos_parts[0]), int(pos_parts[1])
                    
                    if w > 200 and h > 200:
                        # Try to find the saved screen
                        target_screen = None
                        if screen_name:
                            app = QApplication.instance()
                            if app:
                                for screen in app.screens():
                                    if screen.name() == screen_name:
                                        target_screen = screen
                                        break
                        
                        # Validate position is within available screens
                        geometry = QRect(x, y, w, h)
                        if target_screen:
                            screen_geom = target_screen.availableGeometry()
                            # Ensure window is mostly visible on target screen
                            if geometry.intersects(screen_geom):
                                self.setGeometry(geometry)
                                return
                        
                        # Fallback: check if position is valid on any screen
                        app = QApplication.instance()
                        if app:
                            for screen in app.screens():
                                if geometry.intersects(screen.availableGeometry()):
                                    self.setGeometry(geometry)
                                    return
            
            # If we get here, couldn't restore position - fall back to size + center
            self._restore_window_size_and_center()
        except Exception:
            # Final fallback: center with current size
            QTimer.singleShot(10, self._center_window)
    
    def _restore_window_size_and_center(self):
        """Restore window size from settings, then center on active display."""
        try:
            # Restore size if available
            size_restored = False
            val = self._settings.value('MainWindow/Size')
            if val:
                parts = [p.strip() for p in str(val).split(',') if p.strip().isdigit()]
                if len(parts) == 2:
                    w, h = int(parts[0]), int(parts[1])
                    if w > 200 and h > 200:
                        self.resize(w, h)
                        size_restored = True
            
            # Always center after size is set (either restored or default)
            QTimer.singleShot(50 if size_restored else 10, self._center_window)
        except Exception:
            # Fallback: center with current size
            QTimer.singleShot(10, self._center_window)

    def resizeEvent(self, e):
        # Debounced auto-save of window state (only when not maximized)
        if not self.isMaximized():
            self._window_save_timer.start()
        super().resizeEvent(e)
        if hasattr(self, 'toast'):
            self.toast.reposition()

    def moveEvent(self, e):
        # Debounced auto-save of window state (only when not maximized)  
        if not self.isMaximized():
            self._window_save_timer.start()
        super().moveEvent(e)

    def changeEvent(self, e):
        # Handle window state changes
        if e.type() == e.Type.WindowStateChange:
            try:
                is_maximized = self.isMaximized()
                is_minimized = self.isMinimized()
                
                # Save maximized state to settings
                self._settings.setValue('MainWindow/Maximized', 'true' if is_maximized else 'false')
                
                # Handle minimize/restore for multi-monitor support (Windows 11 fix)
                if is_minimized and not self._was_minimized:
                    # Window is being minimized - save current geometry AND screen
                    if not is_maximized:
                        self._geometry_before_minimize = self.geometry()
                        self._screen_before_minimize = self.screen()
                        # Also save to settings for persistence
                        self._save_window_state()
                    self._was_minimized = True
                elif not is_minimized and self._was_minimized:
                    # Window is being restored from minimize
                    self._was_minimized = False
                    if self._geometry_before_minimize is not None and not is_maximized:
                        # Read screen name from settings to support manual changes
                        screen_name = self._settings.value('MainWindow/ScreenName')
                        target_screen = None
                        
                        # Try to find the screen by name from settings
                        if screen_name:
                            app = QApplication.instance()
                            if app:
                                for screen in app.screens():
                                    if screen.name() == screen_name:
                                        target_screen = screen
                                        break
                        
                        # Fallback to the saved screen if settings don't specify a valid screen
                        if target_screen is None:
                            target_screen = self._screen_before_minimize
                        
                        # Schedule restoration with multiple retries for Windows 11
                        saved_geometry = self._geometry_before_minimize
                        QTimer.singleShot(0, lambda: self._restore_geometry_to_monitor(saved_geometry, target_screen, is_first=True))
                        QTimer.singleShot(50, lambda: self._restore_geometry_to_monitor(saved_geometry, target_screen, is_first=False))
                        QTimer.singleShot(150, lambda: self._restore_geometry_to_monitor(saved_geometry, target_screen, is_first=False))
                        QTimer.singleShot(250, self._clear_minimize_state)
                    
            except Exception as ex:
                print(f"changeEvent error: {ex}")
                pass
        super().changeEvent(e)

    def open_activity_log(self):
        """Open the activity log file in the default text editor."""
        log_path = os.path.join(APP_DIR, 'activity.log')
        
        # Create the log file if it doesn't exist
        if not os.path.exists(log_path):
            try:
                with open(log_path, 'w', encoding='utf-8') as f:
                    f.write(f"# Activity Log - Created {datetime.datetime.now().isoformat()}\n")
                    f.write("# Format: TIMESTAMP LEVEL ACTION key=\"value\" ...\n\n")
            except Exception as e:
                QMessageBox.critical(
                    self,
                    "Open Activity Log",
                    f"Could not create activity log file:\n{str(e)}"
                )
                return
        
        try:
            # Try PyQt's cross-platform approach first
            url = QUrl.fromLocalFile(log_path)
            if not QDesktopServices.openUrl(url):
                # Fallback to platform-specific methods
                if sys.platform.startswith('win'):
                    os.startfile(log_path)
                elif sys.platform.startswith('darwin'):
                    os.system(f'open "{log_path}"')
                else:  # Linux and other Unix-like systems
                    os.system(f'xdg-open "{log_path}"')
                    
        except Exception as e:
            # Final fallback: show file location to user
            QMessageBox.information(
                self,
                "Open Activity Log",
                f"Could not open activity log automatically.\n\n"
                f"Please open this file manually:\n{log_path}\n\n"
                f"Error: {str(e)}"
            )

    def closeEvent(self, e):
        """Handle application close with proper resource cleanup."""
        self.detail.chips._commit_input()
        if not self.notes.cleanup_resources():
            e.ignore()
            return
        self._leave_review_visit()  # the review step on screen counts if auto-mark applies
        if not self._create_database_backup():
            answer = QMessageBox.warning(self, 'Backup failed',
                'Your notes are saved, but the backup could not be completed.\n'
                + getattr(self, '_last_backup_error', '') + '\n\nClose without this backup?',
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No)
            if answer != QMessageBox.StandardButton.Yes:
                e.ignore()
                return
        # Application-level event filters written in Python must be gone before the QApplication
        # object is released, or PyQt's shutdown cleanup crashes.
        ui_menus.uninstall_menu_chrome()
        self._save_current_view_now()
        for timer_name in ('_status_update_timer', '_view_save_timer', '_search_debounce', '_note_search_timer'):
            timer = getattr(self, timer_name, None)
            if timer is not None:
                timer.stop()
        self._discard_undo_backups()
        self._save_splitter_sizes()
        self._save_window_state()
        
        
        # SECOND: Close database connection (after notes are saved)
        try:
            if hasattr(self, 'db') and self.db:
                self.db.close()
        except Exception as db_error:
            print(f"Warning: Database cleanup error: {db_error}")
        
        # THIRD: Gracefully shutdown activity logger
        try:
            activity_logger.shutdown()
        except Exception as cleanup_error:
            print(f"Warning: Activity logger cleanup error: {cleanup_error}")
        
        super().closeEvent(e)

    def open_activity_log(self):
        """Open the activity log file in the default text editor."""
        log_path = os.path.join(APP_DIR, 'activity.log')
        
        # Create the log file if it doesn't exist
        if not os.path.exists(log_path):
            try:
                with open(log_path, 'w', encoding='utf-8') as f:
                    f.write(f"# Activity Log - Created {datetime.datetime.now().isoformat()}\n")
                    f.write("# Format: TIMESTAMP LEVEL ACTION key=\"value\" ...\n\n")
            except Exception as e:
                QMessageBox.critical(
                    self,
                    "Open Activity Log",
                    f"Could not create activity log file:\n{str(e)}"
                )
                return
        
        try:
            # Try PyQt's cross-platform approach first
            url = QUrl.fromLocalFile(log_path)
            if not QDesktopServices.openUrl(url):
                # Fallback to platform-specific methods
                if sys.platform.startswith('win'):
                    os.startfile(log_path)
                elif sys.platform.startswith('darwin'):
                    os.system(f'open "{log_path}"')
                else:  # Linux and other Unix-like systems
                    os.system(f'xdg-open "{log_path}"')
                    
        except Exception as e:
            # Final fallback: show file location to user
            QMessageBox.information(
                self,
                "Open Activity Log",
                f"Could not open activity log automatically.\n\n"
                f"Please open this file manually:\n{log_path}\n\n"
                f"Error: {str(e)}"
            )

    def _create_database_backup(self):
        """Publish a complete SQLite-and-images snapshot after all edits are durable."""
        if not self.db or not self.db.conn or not self.notes.flush_pending():
            return False
        try:
            filename = os.path.basename(self.db.path)
            base, ext = os.path.splitext(filename)
            ext = ext or '.db'
            stamp = datetime.datetime.now().strftime('%Y%m%d-%H%M%S-%f')
            target = os.path.join(self.backup_dir, f'{base}.backup-{stamp}{ext}')
            backup_notes(self.db.conn, self.db.path, target)
            self._cleanup_old_backups(base, ext)
            return True
        except Exception as error:
            self._last_backup_error = str(error)
            self.statusBar().showMessage(f'Backup failed: {error}', 15000)
            return False

    def _cleanup_old_backups(self, base_name: str, ext: str):
        """Remove old backups, keeping only the newest 30 for this specific database, and migrate existing backups."""
        try:
            # First, migrate any existing backups from the main project directory
            self._migrate_existing_backups(base_name, ext)
            
            # Create pattern to match only backups for this specific database
            # Note: Use shell glob pattern, not regex pattern for glob.glob()
            pattern = f"{base_name}.backup-*{ext}"
            search_pattern = os.path.join(self.backup_dir, pattern)
            
            # Find all backup files for this database
            backup_files = []
            for filepath in glob.glob(search_pattern):
                if os.path.isfile(filepath):
                    filename = os.path.basename(filepath)
                    # Double-check the pattern with regex for exact match
                    regex_pattern = rf"^{re.escape(base_name)}\.backup-\d{{8}}-\d{{6}}(?:-\d{{6}})?{re.escape(ext)}$"
                    if re.match(regex_pattern, filename):
                        backup_files.append(filepath)
            
            # Sort by modification time, newest first
            backup_files.sort(key=os.path.getmtime, reverse=True)
            
            # Remove backups beyond the newest 30
            files_to_remove = backup_files[30:]  # Keep first 30 (newest)
            
            removed_count = 0
            for old_backup in files_to_remove:
                try:
                    os.remove(old_backup)
                    shutil.rmtree(old_backup + '.assets', ignore_errors=True)
                    removed_count += 1
                except Exception as e:
                    print(f"Failed to remove old backup {os.path.basename(old_backup)}: {e}")
            
            if removed_count > 0:
                print(f"Cleaned up {removed_count} old backup(s) for {base_name}{ext}")
                
        except Exception as e:
            print(f"Error during backup cleanup: {e}")

    def _migrate_existing_backups(self, base_name: str, ext: str):
        """Migrate existing backup files from the main project directory to the backups subfolder."""
        try:
            # Look for backup files in the main project directory
            # Note: Use shell glob pattern, not regex pattern for glob.glob()
            pattern = f"{base_name}.backup-*{ext}"
            search_pattern = os.path.join(APP_DIR, pattern)
            
            migrated_count = 0
            for old_backup_path in glob.glob(search_pattern):
                if os.path.isfile(old_backup_path):
                    filename = os.path.basename(old_backup_path)
                    # Double-check the pattern with regex for exact match
                    regex_pattern = rf"^{re.escape(base_name)}\.backup-\d{{8}}-\d{{6}}(?:-\d{{6}})?{re.escape(ext)}$"
                    if re.match(regex_pattern, filename):
                        new_backup_path = os.path.join(self.backup_dir, filename)
                        
                        # Only move if destination doesn't exist
                        if not os.path.exists(new_backup_path):
                            try:
                                shutil.move(old_backup_path, new_backup_path)
                                migrated_count += 1
                            except Exception as e:
                                print(f"Failed to migrate backup {filename}: {e}")
                        else:
                            # If destination exists, remove the old file
                            try:
                                os.remove(old_backup_path)
                                print(f"Removed duplicate backup from main directory: {filename}")
                            except Exception as e:
                                print(f"Failed to remove duplicate backup {filename}: {e}")
            
            if migrated_count > 0:
                print(f"Migrated {migrated_count} existing backup(s) to backups folder")
                
            # Also look for and migrate any other backup files that might use different patterns
            # This handles legacy backups that might not follow the exact pattern
            for filename in os.listdir(APP_DIR):
                if filename.startswith(f"{base_name}.backup") and filename.endswith(ext):
                    old_backup_path = os.path.join(APP_DIR, filename)
                    new_backup_path = os.path.join(self.backup_dir, filename)
                    
                    if os.path.isfile(old_backup_path) and not os.path.exists(new_backup_path):
                        try:
                            shutil.move(old_backup_path, new_backup_path)
                            print(f"Migrated legacy backup: {filename}")
                        except Exception as e:
                            print(f"Failed to migrate legacy backup {filename}: {e}")
                            
        except Exception as e:
            print(f"Error during backup migration: {e}")

    def _center_window(self):
        """Center window on the active screen, respecting screen boundaries."""
        try:
            # Get the screen that contains the mouse cursor (active screen)
            cursor_pos = self.cursor().pos()
            screen = QApplication.screenAt(cursor_pos)
            
            # Fallback to primary screen if no screen found
            if screen is None:
                screen = QApplication.primaryScreen()
            
            if screen is None:
                return
                
            # Get available geometry (excludes taskbar, etc.)
            available_rect = screen.availableGeometry()
            
            # Get current window size
            window_size = self.size()
            
            # Calculate center position
            x = available_rect.x() + (available_rect.width() - window_size.width()) // 2
            y = available_rect.y() + (available_rect.height() - window_size.height()) // 2
            
            # Ensure window stays within screen bounds
            x = max(available_rect.x(), min(x, available_rect.x() + available_rect.width() - window_size.width()))
            y = max(available_rect.y(), min(y, available_rect.y() + available_rect.height() - window_size.height()))
            
            self.move(x, y)
        except Exception as e:
            print(f"Error centering window: {e}")

    def showEvent(self, e):
        super().showEvent(e)
        # Robust first-show handling even if actions not yet created
        if not getattr(self, '_first_show', True):
            return
        self._first_show = False
        if hasattr(self, 'act_always_on_top'):
            current = bool(self.windowFlags() & Qt.WindowType.WindowStaysOnTopHint)
            self.act_always_on_top.blockSignals(True)
            self.act_always_on_top.setChecked(current)
            self.act_always_on_top.blockSignals(False)
        # Note: centering is now handled in _restore_window_state for normal windows
        # Maximized windows don't need centering

# -------------------------- Entry Point --------------------------

def _open_database_with_lock_handling(path: str, temp_win=None) -> Optional[DB]:
    """Attempt to open database, handling lock conflicts by prompting for different database."""
    # If no path provided, start with file dialog
    if not path:
        while True:
            path, _ = QFileDialog.getOpenFileName(
                temp_win,
                "Open SQLite DB",
                APP_DIR,
                "SQLite DB (*.sqlite *.db);;All files (*.*)"
            )
            
            if not path:
                return None  # User cancelled
            
            try:
                database = DB(path)
                # Save the new path to settings
                if temp_win:
                    temp_settings = QSettings(SETTINGS_PATH, QSettings.Format.IniFormat)
                    temp_settings.setValue('Database/LastPath', path)
                    temp_settings.sync()
                return database
            except DatabaseLockException as e:
                # Show error and continue loop
                QMessageBox.warning(
                    temp_win,
                    "Database Locked",
                    f"{str(e)}\n\nPlease select a different database file."
                )
                continue
            except Exception as e:
                QMessageBox.critical(temp_win, "Error", f"Failed to open database: {e}")
                continue
    
    # Try to open the specified path
    try:
        return DB(path)
    except DatabaseLockException as e:
        # Database is locked, show error and prompt for different database
        parent = temp_win if temp_win else None
        QMessageBox.warning(
            parent,
            "Database Locked",
            f"{str(e)}\n\nPlease select a different database file."
        )
        
        while True:
            new_path, _ = QFileDialog.getOpenFileName(
                parent,
                "Select Different Database",
                APP_DIR,
                "SQLite DB (*.sqlite *.db);;All files (*.*)"
            )
            
            if not new_path:
                return None  # User cancelled
            
            try:
                database = DB(new_path)
                # Save the new path to settings
                if temp_win:
                    temp_settings = QSettings(SETTINGS_PATH, QSettings.Format.IniFormat)
                    temp_settings.setValue('Database/LastPath', new_path)
                    temp_settings.sync()
                return database
            except DatabaseLockException as e:
                # Still locked, show error and continue loop
                QMessageBox.warning(
                    parent,
                    "Database Locked",
                    f"{str(e)}\n\nPlease select a different database file."
                )
                continue
            except Exception as e:
                QMessageBox.critical(parent, "Error", f"Failed to open database: {e}")
                continue
    except Exception:
        raise  # Re-raise non-lock exceptions

def main():
    global SETTINGS_PATH
    app = QApplication(sys.argv)
    # Before anything reads settings (addon startup hooks included)
    SETTINGS_PATH = settings_store.prepare_settings(APP_DIR)
    settings_store.upgrade_settings(QSettings(SETTINGS_PATH, QSettings.Format.IniFormat))
    ui_theme.apply(app, install_menus=False)
    
    # Suppress Qt file engine warnings (harmless QSettings warnings)
    def qt_message_handler(mode, context, message):
        if "QFSFileEngine::open: No file name specified" not in message:
            # Only print non-suppressed messages
            if mode == 0:  # QtDebugMsg
                print(f"Debug: {message}")
            elif mode == 1:  # QtWarningMsg
                print(f"Warning: {message}")
            elif mode == 2:  # QtCriticalMsg
                print(f"Critical: {message}")
            elif mode == 3:  # QtFatalMsg
                print(f"Fatal: {message}")
    
    from PyQt6.QtCore import qInstallMessageHandler
    qInstallMessageHandler(qt_message_handler)

    # Create a temporary window to load database from settings
    temp_win = QMainWindow()
    temp_settings = QSettings(SETTINGS_PATH, QSettings.Format.IniFormat)
    
    # Load database from settings with proper error handling
    try:
        # Try to get last used database path from settings
        last_path = temp_settings.value('Database/LastPath', DB_PATH)
        
        database = None
        
        # If we have a saved path and it exists, try to open it
        if last_path and os.path.exists(last_path):
            database = _open_database_with_lock_handling(last_path, temp_win)
            if database:
                pass  # Successfully opened
            else:
                # User cancelled database selection, exit
                return 1
        
        # If we couldn't open the saved path, try the default path
        if database is None:
            if os.path.exists(DB_PATH):
                database = _open_database_with_lock_handling(DB_PATH, temp_win)
                if database:
                    # Update settings to reflect we're using default
                    temp_settings.setValue('Database/LastPath', DB_PATH)
                    temp_settings.sync()
                elif database is None:
                    # User cancelled, exit
                    return 1
            else:
                # Create default database if it doesn't exist
                try:
                    database = DB(DB_PATH)
                    ensure_db_seed(database)
                    temp_settings.setValue('Database/LastPath', DB_PATH)
                    temp_settings.sync()
                except DatabaseLockException as e:
                    # Shouldn't happen for new file, but handle just in case
                    QMessageBox.critical(temp_win, "Error", f"Cannot create default database: {e}")
                    return 1
                except Exception:
                    pass
        
        # If we still don't have a database, prompt user
        if database is None:
            QMessageBox.warning(
                temp_win, 
                "Database Not Found", 
                "Could not find or open the database. Please select a database file."
            )
            
            database = _open_database_with_lock_handling("", temp_win)  # Empty path will trigger file dialog
            
            if database is None:
                # User cancelled, create default as final fallback
                try:
                    database = DB(DB_PATH)
                    ensure_db_seed(database)
                    temp_settings.setValue('Database/LastPath', DB_PATH)
                    temp_settings.sync()
                except Exception as e:
                    QMessageBox.critical(temp_win, "Fatal Error", f"Could not create default database: {e}")
                    return 1
        
        # Ensure database has seed data if it's newly created or empty
        try:
            ensure_db_seed(database)
        except Exception:
            pass  # Not critical if seeding fails
        
        # Clean up temporary window
        temp_win.close()
        
        # Create main window with the database
        win = MainWindow(database)
        win.show()
        
        # Run application
        try:
            return app.exec()
        finally:
            # Ensure proper cleanup on exit
            try:
                database.close()
                activity_logger.shutdown()
            except Exception as e:
                print(f"Cleanup error: {e}")
        
    except Exception as e:
        # Clean up resources on exception
        try:
            if 'database' in locals() and database:
                database.close()
            activity_logger.shutdown()
        except Exception:
            pass
        QMessageBox.critical(None, "Fatal Error", f"Failed to initialize application: {e}")
        return 1

if __name__ == '__main__':
    sys.exit(main())
