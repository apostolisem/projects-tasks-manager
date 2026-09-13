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
import threading, queue, gzip
from typing import Optional, List, Tuple, Callable, Dict, Any
from PyQt6.QtCore import (Qt, QAbstractTableModel, QMimeData, QTimer, QSize, QRect, QModelIndex, QItemSelectionModel, QUrl, pyqtSignal, QPoint, QDate, QTime)
from PyQt6.QtCore import QSettings
from PyQt6.QtGui import (QAction, QKeySequence, QPixmap, QImage, QPalette, QFont, QPainter, QPen, QColor, QIcon, QTextCursor, QTextDocument, QDesktopServices, QPolygon, QTextCharFormat, QTextListFormat, QScreen)
from PyQt6.QtWidgets import (QApplication, QMainWindow, QWidget, QSplitter, QVBoxLayout, QHBoxLayout, QListView, QTableView, QTextEdit, QLineEdit, QToolBar, QFileDialog, QMessageBox, QStyle, QLabel, QInputDialog, QHeaderView, QDialog, QDialogButtonBox, QRubberBand, QPushButton, QCheckBox, QMenu, QComboBox, QScrollArea, QDateEdit, QTimeEdit, QGroupBox, QSpinBox, QPlainTextEdit)
from PyQt6.QtWidgets import QStyledItemDelegate
from PIL import Image

# Precompile search split regex early
SEARCH_SPLIT_RE = re.compile(r"\s+")
IMG_SRC_RE = re.compile(r'<img[^>]+src=["\']([^"\']+)["\']', re.IGNORECASE)
CSV_METADATA_UNSET = object()

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

    if freq == RECURRENCE_FREQ_EVERY_N_DAYS:
        interval_days = max(1, int(rule.get("interval_days", interval) or 1))
        summary = f"Every {interval_days} days"
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
    if mode == RECURRENCE_MODE_COMPLETION:
        summary = f"After completion: {summary}"
    return summary


def _format_recurrence_tooltip(rule: Optional[Dict[str, Any]], next_at: Optional[str]) -> Optional[str]:
    if not rule:
        return None
    summary = _format_recurrence_summary(rule)
    if next_at:
        summary += f"\nNext: {next_at}"
    return summary


def _load_startup_addons() -> List[Any]:
    """Load addon instances early to allow startup hooks."""
    import importlib
    import pkgutil

    instances: List[Any] = []
    try:
        import addons
        addon_path = addons.__path__
    except (ImportError, AttributeError):
        return instances

    for _, modname, _ in pkgutil.iter_modules(addon_path):
        if modname.startswith('_'):
            continue
        try:
            module = importlib.import_module(f'addons.{modname}')
        except ImportError as e:
            print(f"Could not load addon {modname}: {e}")
            continue
        for attr_name in dir(module):
            obj = getattr(module, attr_name)
            if (
                not attr_name.startswith('_')
                and not isinstance(obj, type)
                and (
                    hasattr(obj, 'register_menu_items')
                    or hasattr(obj, 'create_preferences_widget')
                    or hasattr(obj, 'handle_startup')
                )
            ):
                instances.append(obj)
                break
    return instances


def _run_addon_startup_hooks(app: QApplication) -> bool:
    """Run addon startup hooks; return True if the app should exit early."""
    for addon in _load_startup_addons():
        if not hasattr(addon, 'handle_startup'):
            continue
        try:
            should_exit = addon.handle_startup(app, sys.argv)
            if should_exit:
                return True
        except Exception as e:
            print(f"Startup addon error: {e}")
    return False

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
SETTINGS_PATH = os.path.join(APP_DIR, 'settings.ini')

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
                cur = self.conn.execute(
                    "INSERT INTO projects(title, force_visibility) VALUES (?, ?)", 
                    (unique_title, 1)
                )
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
            cur = self.conn.execute(
                "INSERT INTO projects(title, force_visibility) VALUES (?, ?)", 
                (unique_title, 1)
            )
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
        
        self.conn.execute(
            "UPDATE projects SET hidden=? WHERE id=?",
            (1 if new_hidden else 0, project_id)
        )
        self.conn.commit()
        
        return new_hidden

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
                "INSERT INTO tasks(project_id, title, force_visibility, effort) VALUES (?, ?, ?, ?)",
                (project_id, clean_title, 1, 0.0)
            )
        else:
            cur = self.conn.execute(
                "INSERT INTO tasks(project_id, title, force_visibility, effort, manual_order) VALUES (?, ?, ?, ?, ?)",
                (project_id, clean_title, 1, 0.0, manual_order)
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
    
    def get_task_effort(self, task_id: int) -> float:
        """Get the effort for a task. Returns effort value or 0.0 as default."""
        row = self.conn.execute("SELECT effort FROM tasks WHERE id=?", (task_id,)).fetchone()
        return float(row['effort']) if row and row['effort'] is not None else 0.0
    
    def set_task_effort(self, task_id: int, effort: float):
        """Set the effort for a task."""
        self.conn.execute(
            "UPDATE tasks SET effort=?, updated_at=CURRENT_TIMESTAMP WHERE id=?",
            (effort, task_id)
        )
        self.conn.commit()

    def set_imported_task_state(
        self,
        task_id: int,
        *,
        pinned: Optional[bool] = None,
        done: Optional[bool] = None
    ):
        """Apply imported task state without side effects like completion note stamping."""
        sets = []
        params: List[Any] = []
        if pinned is not None:
            sets.append("pinned=?")
            params.append(1 if pinned else 0)
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
        
        # If a task was marked as done, check if all tasks in the project are now done
        # and clear the project's force_visibility if so
        if done and changes.get('done', (None, None))[1]:  # Task was marked as done
            task_project_row = self.conn.execute("SELECT project_id FROM tasks WHERE id=?", (task_id,)).fetchone()
            if task_project_row:
                project_id = task_project_row['project_id']
                self._clear_project_force_visibility_if_all_tasks_done(project_id)

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
                next_dt = _compute_next_recurrence(rule, start_dt, start_dt)
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

    def _clear_project_force_visibility_if_all_tasks_done(self, project_id: int):
        """Clear the project's force_visibility flag if all its tasks are done."""
        try:
            # Get task counts for this project
            result = self.conn.execute("""
                SELECT 
                    COUNT(*) AS total_tasks,
                    COUNT(CASE WHEN done = 1 THEN 1 END) AS done_tasks
                FROM tasks 
                WHERE project_id = ?
            """, (project_id,)).fetchone()
            
            if result and result['total_tasks'] > 0 and result['total_tasks'] == result['done_tasks']:
                # All tasks are done, clear the project's force_visibility
                self.conn.execute(
                    "UPDATE projects SET force_visibility = 0 WHERE id = ? AND force_visibility = 1", 
                    (project_id,)
                )
                self.conn.commit()
        except Exception as e:
            # Don't let this functionality break other operations
            print(f"Warning: Could not clear project force_visibility: {e}")

    def remove_task(self, task_id: int):
        # Get task info for logging and project checking
        old_row = self.conn.execute("SELECT title, project_id FROM tasks WHERE id=?", (task_id,)).fetchone()
        old_title = old_row['title'] if old_row else None
        project_id = old_row['project_id'] if old_row else None

        # delete attachments files first
        seen_paths = set()
        for row in self.conn.execute("SELECT filename FROM attachments WHERE task_id=?", (task_id,)):
            fpath = self._attachment_abs_path(row['filename'])
            if not fpath or fpath in seen_paths:
                continue
            seen_paths.add(fpath)
            try:
                if os.path.isfile(fpath):
                    os.remove(fpath)
            except Exception:
                pass
        self.conn.execute("DELETE FROM tasks WHERE id=?", (task_id,))
        self.conn.commit()

        if project_id and self.is_manual_sort_enabled(project_id):
            self.ensure_manual_task_order(project_id, fallback_to_auto=True)
        
        # After removing the task, check if all remaining tasks in the project are done
        if project_id:
            self._clear_project_force_visibility_if_all_tasks_done(project_id)
        
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
                    due_date, manual_order, marked_mom, effort,
                    recurring, recurrence_rule, recurrence_start_at, recurrence_next_at, recurrence_last_at,
                    created_at, updated_at
                ) VALUES (
                    ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
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
                    original.get('effort', 0.0),
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
        names = [n.strip() for n in names if n.strip()]
        ids: List[int] = []
        for n in names:
            self.conn.execute("INSERT OR IGNORE INTO stakeholders(name) VALUES (?)", (n,))
            row = self.conn.execute("SELECT id FROM stakeholders WHERE name=?", (n,)).fetchone()
            if row:
                ids.append(row['id'])
        self.conn.execute("DELETE FROM task_stakeholders WHERE task_id=?", (task_id,))
        for sid in ids:
            self.conn.execute("INSERT INTO task_stakeholders(task_id, stakeholder_id) VALUES (?, ?)", (task_id, sid))
        self.conn.commit()

    # Search (LIKE-based partial, case-insensitive)
    def search_tasks(self, terms: List[str], stakeholder_terms: List[str], include_done: bool, project_id: Optional[int], pinned_only: bool = False) -> List[sqlite3.Row]:
        where_conditions = []
        params: List = []

        # Build LIKE conditions for general terms
        for term in terms:
            like_pattern = f"%{term.lower()}%"
            where_conditions.append("(LOWER(p.title) LIKE ? OR LOWER(t.title) LIKE ? OR LOWER(n.plain_text) LIKE ?)")
            params.extend([like_pattern, like_pattern, like_pattern])

        # Build stakeholder conditions
        if stakeholder_terms:
            stakeholder_conditions = []
            for stakeholder in stakeholder_terms:
                like_pattern = f"%{stakeholder.lower()}%"
                stakeholder_conditions.append(
                    "EXISTS (SELECT 1 FROM task_stakeholders ts JOIN stakeholders s ON s.id=ts.stakeholder_id "
                    "WHERE ts.task_id=t.id AND LOWER(s.name) LIKE ?)"
                )
                params.append(like_pattern)
            where_conditions.append(f"({' OR '.join(stakeholder_conditions)})")

        # Determine if this is a user search (any search terms present)
        is_search_mode = bool(terms or stakeholder_terms)
        # Determine if this is stakeholder-only search (only @ prefix terms, no general terms)
        is_stakeholder_only_search = bool(stakeholder_terms and not terms)

        # For force_visibility tasks, only respect project_id constraint, bypass other filters
        if where_conditions or not include_done or pinned_only:
            # Combine search/filter conditions with force_visibility logic
            search_conditions = " AND ".join(where_conditions) if where_conditions else "1=1"

            # Force visibility tasks bypass search and filter conditions but respect project constraint
            if project_id is not None:
                force_visibility_condition = f"(t.force_visibility=1 AND t.project_id={project_id})"
                # Regular task conditions with project constraint
                regular_conditions = f"(({search_conditions}) AND t.project_id={project_id}"
            else:
                force_visibility_condition = "t.force_visibility=1"
                # Regular task conditions without project constraint (global search)
                regular_conditions = f"(({search_conditions})"

            # Add regular filters for non-force_visibility tasks
            # During explicit search, include completed tasks by default even when include_done is False
            # EXCEPT for stakeholder-only searches (@ prefix only), where we respect the include_done filter
            if (not include_done) and (not is_search_mode or is_stakeholder_only_search):
                regular_conditions += " AND t.done=0"
            if pinned_only:
                regular_conditions += " AND t.pinned=1"

            regular_conditions += ")"

            # Combine: regular_conditions OR force_visibility_condition
            where_conditions = [f"{regular_conditions} OR {force_visibility_condition}"]
        else:
            # No search terms or filters, just apply project constraint if any
            if project_id is not None:
                where_conditions.append("t.project_id=?")
                params.append(project_id)

        # Build final query
        where_clause = " WHERE " + " AND ".join(where_conditions) if where_conditions else ""
        sql = (
            f"SELECT t.* FROM tasks t JOIN projects p ON p.id=t.project_id LEFT JOIN notes n ON n.task_id=t.id"
            f"{where_clause} ORDER BY t.done ASC, t.pinned DESC, t.title COLLATE NOCASE ASC"
        )
        rows = list(self.conn.execute(sql, params))
        if project_id is not None:
            return self.order_task_rows_for_project(project_id, rows)
        return rows


    def clear_all_force_visibility(self):
        """Clear force_visibility flag from all projects and tasks"""
        try:
            self.conn.execute("UPDATE projects SET force_visibility = 0 WHERE force_visibility = 1")
            self.conn.execute("UPDATE tasks SET force_visibility = 0 WHERE force_visibility = 1")
            self.conn.commit()
        except Exception as e:
            print(f"Error clearing force_visibility: {e}")

    def list_projects_with_stats(self):
        return list(self.conn.execute(
            """
            SELECT p.*, 
                   COUNT(t.id) AS task_count,
                   COUNT(CASE WHEN t.done=1 THEN 1 END) AS done_count
            FROM projects p
            LEFT JOIN tasks t ON t.project_id=p.id
            GROUP BY p.id, p.title, p.force_visibility, p.hidden, p.created_at, p.updated_at
            ORDER BY p.title COLLATE NOCASE
            """))


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

    def __init__(self, db: DB, settings: QSettings = None):
        super().__init__()
        self.db = db
        self.settings = settings
        self.rows: List[sqlite3.Row] = []
        self.include_done: bool = False
        self.pinned_only: bool = False
        self.show_no_due_dates: bool = False
        self.show_effort_missing: bool = False
        self._search_filter_ids: Optional[set[int]] = None  # when not None restrict to these project ids
        self.project_name_filter: str = ""  # Filter projects by name (case-insensitive, AND terms)
        self._review_active: bool = False
        self._review_target_ids: set[int] = set()
        self._review_completed_ids: set[int] = set()
        self.reload()

    def set_include_done(self, include_done: bool):
        if self.include_done != include_done:
            self.include_done = include_done
            self.reload()

    def set_pinned_only(self, pinned_only: bool):
        if self.pinned_only != pinned_only:
            self.pinned_only = pinned_only
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
        
        # Apply project name filter first (if set)
        if self.project_name_filter:
            filter_terms = [term.lower() for term in SEARCH_SPLIT_RE.split(self.project_name_filter.strip()) if term]
            filtered_by_name = []
            for r in all_rows:
                project_title_lower = r['title'].lower()
                # AND mode: all terms must match
                if all(term in project_title_lower for term in filter_terms):
                    filtered_by_name.append(r)
            all_rows = filtered_by_name
        
        # Get visibility mode from settings
        visibility_mode = 'task_based'  # default
        if self.settings:
            visibility_mode = self.settings.value('Preferences/ProjectVisibilityMode', 'task_based')
        task_condition_clause = self._build_task_condition_clause(visibility_mode)
        
        if self._search_filter_ids is not None:
            # In search mode, also include force_visibility projects
            visible_rows = []
            for r in all_rows:
                if r['id'] in self._search_filter_ids or r.get('force_visibility', 0):
                    visible_rows.append(r)
            self.rows = visible_rows
        else:
            # Filter projects based on active filters
            filtered_rows = []
            for r in all_rows:
                # Always include projects with force_visibility
                if r.get('force_visibility', 0):
                    filtered_rows.append(r)
                    continue

                # In task-based visibility, always show projects with no tasks
                if visibility_mode == 'task_based' and r.get('task_count', 0) == 0:
                    filtered_rows.append(r)
                    continue

                # Manual visibility mode respects hidden flag when Hide Done is enabled
                if (not self.include_done and visibility_mode == 'manual_based' and r.get('hidden', 0)):
                    continue

                include_project = True
                if task_condition_clause:
                    include_project = self._project_has_matching_tasks(r['id'], task_condition_clause)
                elif not self.include_done and visibility_mode != 'manual_based':
                    # When only Hide Done is active (task-based), fall back to aggregated stats
                    include_project = not (r['task_count'] > 0 and r['task_count'] == r['done_count'])

                if include_project:
                    filtered_rows.append(r)
            self.rows = filtered_rows
        if self._review_completed_ids:
            self._review_completed_ids.intersection_update(all_project_ids)
        if self._review_target_ids:
            self._review_target_ids.intersection_update(all_project_ids)
        self._rebuild_index()
        self.endResetModel()

    def start_review_cycle(self):
        """Begin a review cycle by bolding visible projects until individually cleared."""
        self._review_active = True
        self._review_target_ids = {r['id'] for r in self.rows}
        self._review_completed_ids.clear()
        if self.rows:
            top_left = self.index(0, 0)
            bottom_right = self.index(len(self.rows) - 1, 0)
            self.dataChanged.emit(top_left, bottom_right, [Qt.ItemDataRole.FontRole])

    def end_review_cycle(self):
        """Clear review state and restore normal project styling."""
        if not self._review_active and not self._review_completed_ids and not self._review_target_ids:
            return
        self._review_active = False
        self._review_target_ids.clear()
        self._review_completed_ids.clear()
        if self.rows:
            top_left = self.index(0, 0)
            bottom_right = self.index(len(self.rows) - 1, 0)
            self.dataChanged.emit(top_left, bottom_right, [Qt.ItemDataRole.FontRole])

    def mark_review_completed(self, project_id: int):
        """Mark a single project as reviewed within the current cycle."""
        if not self._review_active or project_id not in self._review_target_ids:
            return
        self._review_completed_ids.add(project_id)
        row = self.row_for_project(project_id)
        if row >= 0:
            idx = self.index(row, 0)
            self.dataChanged.emit(idx, idx, [Qt.ItemDataRole.FontRole])

    def is_review_active(self) -> bool:
        return self._review_active

    def is_project_in_review(self, project_id: int) -> bool:
        return (
            self._review_active
            and project_id in self._review_target_ids
            and project_id not in self._review_completed_ids
        )

    def _build_task_condition_clause(self, visibility_mode: str) -> Optional[str]:
        """Build a SQL condition that mirrors the active task-level filters."""
        conditions: List[str] = []

        if not self.include_done and visibility_mode != 'manual_based':
            conditions.append("done=0")
        if self.pinned_only:
            conditions.append("pinned=1")
        if self.show_no_due_dates:
            conditions.append("COALESCE(TRIM(due_date), '') = ''")
        if self.show_effort_missing:
            conditions.append("COALESCE(effort, 0.0) = 0.0")

        return " AND ".join(conditions) if conditions else None

    def _project_has_matching_tasks(self, project_id: int, condition_clause: str) -> bool:
        """Return True if the project has at least one task satisfying all active filters."""
        query = "SELECT 1 FROM tasks WHERE project_id=?"
        params = [project_id]
        if condition_clause:
            query += f" AND {condition_clause}"
        query += " LIMIT 1"
        return self.db.conn.execute(query, params).fetchone() is not None

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
        
        # Only trigger view updates if the change affects visibility
        # Check visibility mode for task-based hiding
        visibility_mode = 'task_based'  # default
        if self.settings:
            visibility_mode = self.settings.value('Preferences/ProjectVisibilityMode', 'task_based')
        
        should_remove = False
        if visibility_mode == 'task_based':
            # Task-based mode: hide if all tasks are done
            should_remove = (not self.include_done and r['task_count'] > 0 and r['done_count'] == r['task_count'])
        
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
            try:
                self.db.update_project_title(r['id'], new_title)
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

    HEADERS = ["Title", "Pin", "Done"]

    def __init__(self, db: DB):
        super().__init__()
        self.db = db
        self.rows: List[sqlite3.Row] = []
        self.project_id: Optional[int] = None
        self.include_done: bool = False
        self.pinned_only: bool = False
        self.show_no_due_dates: bool = False
        self.show_effort_missing: bool = False
        # Hooks injected by MainWindow to support search-mode editing
        self.is_search_active: Callable[[], bool] = lambda: False
        self.refresh_search: Callable[[], None] = lambda: None
        # Settings reference for preferences (will be set by MainWindow)
        self.settings: Optional[QSettings] = None

    tasksChanged = pyqtSignal()
    taskCompletionStamped = pyqtSignal(int)  # task_id
    doneToggled = pyqtSignal(int, bool)  # project_id, became_done

    def set_context(self, project_id: Optional[int], include_done: bool, pinned_only: bool = False):
        self.project_id = project_id
        self.include_done = include_done
        self.pinned_only = pinned_only
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
        if self.show_effort_missing:
            filtered_tasks = [
                task for task in filtered_tasks
                if self._task_value(task, 'effort') is None or float(self._task_value(task, 'effort', 0.0) or 0.0) == 0.0
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
            
        self._reloading = True
        try:
            self.beginResetModel()
            try:
                if self.project_id is None:
                    self.rows = []
                else:
                    all_tasks = self.db.list_tasks(self.project_id, self.include_done, self.pinned_only)
                    self.rows = self.apply_secondary_filters(all_tasks)
            finally:
                self.endResetModel()
            
            # Emit signals after model is fully updated
            self.tasksChanged.emit()
        finally:
            self._reloading = False
        
    def _perform_deferred_reload(self):
        """Perform reload that was deferred due to active editors"""
        if hasattr(self, '_defer_reload_flag') and not self._defer_reload_flag:
            self._perform_reload()

    def rowCount(self, parent=QModelIndex()):
        return len(self.rows)

    def columnCount(self, parent=QModelIndex()):
        return 3

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
        # Foreground color for completed tasks and due date color coding
        if role == Qt.ItemDataRole.ForegroundRole:
            # Check if task is completed and should be faded
            if bool(row['done']) and (self.include_done or self.is_search_active()):
                pal = QApplication.palette()
                c = pal.color(QPalette.ColorRole.WindowText)
                c.setAlpha(128)
                return c
            
            # Color-code based on due date (only for incomplete tasks)
            # Check if color coding is enabled in preferences
            if not bool(row['done']) and self.settings:
                color_enabled = self.settings.value('Preferences/DueDatesColorEnabled', 'true') in ('true', '1', 'True')
                
                if color_enabled:
                    due_date_str = self.db.get_task_due_date(row['id'])
                    if due_date_str:
                        try:
                            # Parse the due date
                            date_part = due_date_str.split()[0] if ' ' in due_date_str else due_date_str
                            year, month, day = map(int, date_part.split('-'))
                            due_date = QDate(year, month, day)
                            today = QDate.currentDate()
                            days_until_due = today.daysTo(due_date)
                            
                            # Color coding based on urgency
                            if days_until_due < 0:  # Overdue
                                return QColor(220, 38, 38)  # Red
                            elif days_until_due <= 3:  # Due within 3 days
                                return QColor(234, 88, 12)  # Orange
                            else:  # Future due date
                                return QColor(37, 99, 235)  # Blue
                        except (ValueError, AttributeError):
                            pass
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
                
                # Update database first
                self.db.update_task(row['id'], done=newval)
                
                # Handle view updates based on inclusion settings
                if (not self.include_done) and (not self.is_search_active()) and (not old_done_state) and newval:
                    # Task is being marked complete and we're hiding completed tasks
                    # Remove this specific task from view
                    self.beginRemoveRows(QModelIndex(), index.row(), index.row())
                    del self.rows[index.row()]
                    self.endRemoveRows()
                    
                    # Emit signals for project model updates
                    self.tasksChanged.emit()
                    if not self.is_search_active() and project_id is not None:
                        self.doneToggled.emit(project_id, newval)
                else:
                    # Task stays visible, just update its data
                    fresh = self.db.conn.execute("SELECT * FROM tasks WHERE id=?", (row['id'],)).fetchone()
                    self.rows[index.row()] = fresh
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
        
        # Add drag-drop flags only if manual sort is enabled for this project
        if self.project_id is not None and self.db.is_manual_sort_enabled(self.project_id):
            f |= Qt.ItemFlag.ItemIsDragEnabled | Qt.ItemFlag.ItemIsDropEnabled
        
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
        self._rubber.setStyleSheet("QWidget { border: 2px solid #0078d7; background: rgba(0,120,215,40); }")

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

COMMON_FORMAT_TOOLBAR_STYLESHEET = """
    QToolBar {
        border: 1px solid #d8dee4;
        border-radius: 4px;
        background: #f8f9fa;
        spacing: 2px;
        padding: 2px;
    }
    QToolButton {
        border: 1px solid transparent;
        border-radius: 3px;
        padding: 3px;
        margin: 0px;
    }
    QToolButton:hover {
        background: #e6f1fe;
        border-color: #c3dafe;
    }
    QToolButton:pressed,
    QToolButton:checked {
        background: #d0e3ff;
        border-color: #93c5fd;
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
            "Highlight (Yellow)",
            "Toggle yellow highlight (Ctrl+Shift+H)",
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
        doc_font.setFamily("Arial, Helvetica, sans-serif")
        self.document().setDefaultFont(doc_font)

        fmt = self.currentCharFormat()
        fmt.setFontPointSize(11.0)
        fmt.setForeground(QColor(0, 0, 0))
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
        self.action_bold.setIcon(self._build_text_action_icon("B", bold=True))
        self.action_italic.setIcon(self._build_text_action_icon("I", italic=True))
        self.action_underline.setIcon(self._build_text_action_icon("U", underline=True))
        self.action_strikethrough.setIcon(self._build_text_action_icon("S", strike=True))
        self.action_highlight.setIcon(self._build_text_action_icon("H", bold=True, yellow_bg=True))
        self.action_clear_formatting.setIcon(self._build_text_action_icon("Tx", slash=True))
        self.action_decrease_text.setIcon(self._build_text_action_icon("A-", bold=True))
        self.action_increase_text.setIcon(self._build_text_action_icon("A+", bold=True))
        self.action_bulleted_list.setIcon(self._build_list_action_icon(ordered=False))
        self.action_numbered_list.setIcon(self._build_list_action_icon(ordered=True))

    def _selection_has_format(self, matcher: Callable[[QTextCharFormat], bool]) -> bool:
        cursor = self.textCursor()
        if not cursor.hasSelection():
            return matcher(cursor.charFormat())

        start_pos = cursor.selectionStart()
        end_pos = cursor.selectionEnd()
        temp_cursor = QTextCursor(cursor)
        for pos in range(start_pos, end_pos):
            temp_cursor.setPosition(pos)
            if matcher(temp_cursor.charFormat()):
                return True
        return False

    def _apply_char_format(self, updater: Callable[[QTextCharFormat], None]):
        cursor = self.textCursor()

        if cursor.hasSelection():
            start_pos = cursor.selectionStart()
            end_pos = cursor.selectionEnd()
            temp_cursor = QTextCursor(cursor)

            for pos in range(start_pos, end_pos):
                temp_cursor.setPosition(pos)
                temp_cursor.setPosition(pos + 1, QTextCursor.MoveMode.KeepAnchor)
                char_format = temp_cursor.charFormat()
                updater(char_format)
                temp_cursor.mergeCharFormat(char_format)
        else:
            current_format = self.currentCharFormat()
            updater(current_format)
            self.mergeCurrentCharFormat(current_format)

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
            color = fmt.background().color()
            return (
                color.red() == 255 and
                color.green() == 255 and
                color.blue() == 0 and
                color.alpha() > 0
            )
        except Exception:
            return False

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
            lambda fmt, enabled: fmt.setBackground(QColor(255, 255, 0) if enabled else QColor(0, 0, 0, 0))
        )

    def _adjust_text_size(self, delta: int):
        cursor = self.textCursor()

        if cursor.hasSelection():
            start_pos = cursor.selectionStart()
            end_pos = cursor.selectionEnd()
            temp_cursor = QTextCursor(cursor)

            for pos in range(start_pos, end_pos):
                temp_cursor.setPosition(pos)
                temp_cursor.setPosition(pos + 1, QTextCursor.MoveMode.KeepAnchor)
                char_format = temp_cursor.charFormat()
                current_size = char_format.fontPointSize()
                if current_size <= 0:
                    current_size = self._effective_point_size()
                char_format.setFontPointSize(max(8.0, min(72.0, current_size + float(delta))))
                temp_cursor.mergeCharFormat(char_format)
        else:
            base_size = self._effective_point_size()
            fmt = self.currentCharFormat()
            fmt.setFontPointSize(max(8.0, min(72.0, base_size + float(delta))))
            self.mergeCurrentCharFormat(fmt)

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


class NotesEditor(SharedRichTextEditor):
    def __init__(self, db: DB, attach_dir: str, parent=None):
        super().__init__(parent)
        self.db = db
        self.attach_dir = attach_dir
        self.task_id: Optional[int] = None
        
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
        self._autosave_timer.setInterval(400)
        self._autosave_timer.setSingleShot(True)
        self._autosave_timer.timeout.connect(self._autosave_timeout)
        self.textChanged.connect(self._schedule_autosave)
        # Full HTML save (debounced, slower) timer
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

        # Use custom context menu for better grouping and dynamic enabling
        self.setContextMenuPolicy(Qt.ContextMenuPolicy.DefaultContextMenu)
        self.addAction(self.action_crop_visual)
        self.addAction(self.action_resize)
        self.addAction(self.action_cutimg)
        # Avoid duplicate Paste Plain in menu

        # Inline resize state
        self._resize_mode = False
        self._resize_path: Optional[str] = None
        self._resize_origin_size: Optional[Tuple[int,int]] = None
        self._resize_rubber: Optional[QRubberBand] = None
        self._resize_dragging = False
        self._resize_anchor: Optional[QRect] = None
        self._resize_img_cursor: Optional[QTextCursor] = None  # cursor pointing at image char
        self._resize_current_size: Optional[Tuple[int,int]] = None  # (w,h) during interactive drag
        self._resize_aspect: Optional[float] = None  # w/h aspect ratio of displayed image at drag start
        self._resize_left_x0_initial: Optional[int] = None  # stored left x at start
        self._resize_caret_x0: Optional[int] = None  # caret (right edge) x at start
        self._resize_caret_bottom0: Optional[int] = None  # caret baseline bottom at start
        self._resize_handle: Optional[QWidget] = None  # visible corner handle
        self._resize_img_pos: Optional[int] = None  # absolute position of image char to safely replace
        # Track last hashes
        self._last_plain_hash = ""; self._last_html_hash = ""
        # Flag to prevent autosave during resize operations
        self._in_resize_commit = False
        # Set base URL so relative image src paths resolve
        try:
            self.document().setBaseUrl(QUrl.fromLocalFile(db_dir + os.sep))
        except Exception:
            pass

        # Re-apply defaults after replacing the document.
        self._apply_default_formatting()
        self._sync_format_action_state()

    def _apply_default_formatting(self):
        """Set default formatting: 11pt, black text, no highlight."""
        # Set the document's default font to ensure consistency
        doc_font = QFont()
        doc_font.setPointSizeF(11.0)
        doc_font.setFamily("Arial, Helvetica, sans-serif")
        self.document().setDefaultFont(doc_font)
        
        # Also set the current character format for new typing
        fmt = self.currentCharFormat()
        fmt.setFontPointSize(11.0)
        fmt.setForeground(QColor(0, 0, 0))  # Black text
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
        menu.addAction(self.action_copy_image)
        menu.addAction(self.action_resize)
        menu.addAction(self.action_crop_visual)
        menu.addAction(self.action_cutimg)
        menu.addSeparator()

        # 6) Clipboard actions and formatting
        has_selection = self.textCursor().hasSelection()
        self.action_clear_formatting.setEnabled(has_selection)
        menu.addAction(self.action_clear_formatting)
        menu.addAction(self.action_paste_plain)

        menu.exec(e.globalPos())

    def _add_insert_template_submenu(self, menu: QMenu):
        """Add the Insert Template submenu populated from stored templates."""
        template_menu = menu.addMenu("Insert Template")
        if not self.db:
            template_menu.setEnabled(False)
            return

        try:
            templates = self.db.list_note_templates()
        except Exception:
            template_menu.setEnabled(False)
            return

        if not templates:
            empty_action = template_menu.addAction("No templates")
            empty_action.setEnabled(False)
            return

        for template in templates:
            title = (template['title'] or '').strip() or "Untitled template"
            html = template['content_html'] or ""
            action = template_menu.addAction(title)
            action.triggered.connect(lambda _checked=False, content=html: self.insert_template(content))

    def insert_template(self, template_html: str):
        """Insert template HTML at the current cursor position."""
        if template_html is None:
            return
        cursor = self.textCursor()
        cursor.insertHtml(template_html)
        self.setTextCursor(cursor)
        self._schedule_autosave()
        QTimer.singleShot(0, self._ensure_cursor_visibility)

    def _cursor_on_or_adjacent_to_image(self) -> bool:
        """Return True if the cursor is at an image, or immediately left/right of one."""
        cur = self.textCursor()
        if cur.charFormat().isImageFormat():
            return True
        # Check left
        if cur.position() > 0:
            tcur = QTextCursor(cur)
            tcur.movePosition(QTextCursor.MoveOperation.Left, QTextCursor.MoveMode.MoveAnchor)
            if tcur.charFormat().isImageFormat():
                return True
        # Check right
        if cur.position() < self.document().characterCount() - 1:
            tcur = QTextCursor(cur)
            tcur.movePosition(QTextCursor.MoveOperation.Right, QTextCursor.MoveMode.MoveAnchor)
            if tcur.charFormat().isImageFormat():
                return True
        return False
    # --- bold toggle ---
    def toggle_bold(self):
        cursor = self.textCursor()
        
        if cursor.hasSelection():
            # For selections, preserve existing formatting while toggling bold
            start_pos = cursor.selectionStart()
            end_pos = cursor.selectionEnd()
            
            # Check if any part of the selection is currently bold
            has_bold = False
            temp_cursor = QTextCursor(cursor)
            temp_cursor.setPosition(start_pos)
            
            for pos in range(start_pos, end_pos):
                temp_cursor.setPosition(pos)
                char_format = temp_cursor.charFormat()
                if char_format.fontWeight() >= QFont.Weight.Bold:
                    has_bold = True
                    break
            
            # Toggle bold state - if any part is bold, remove bold from all; otherwise add bold to all
            new_weight = QFont.Weight.Normal if has_bold else QFont.Weight.Bold
            
            # Apply the new weight character by character, preserving other formatting
            temp_cursor.setPosition(start_pos)
            for pos in range(start_pos, end_pos):
                temp_cursor.setPosition(pos)
                temp_cursor.setPosition(pos + 1, QTextCursor.MoveMode.KeepAnchor)
                
                # Get current character format and only change the weight
                char_format = temp_cursor.charFormat()
                char_format.setFontWeight(new_weight)
                temp_cursor.mergeCharFormat(char_format)
                
        else:
            # For cursor position, use the traditional approach
            cur_fmt = self.currentCharFormat()
            new_weight = QFont.Weight.Normal if cur_fmt.fontWeight() >= QFont.Weight.Bold else QFont.Weight.Bold
            cur_fmt.setFontWeight(new_weight)
            self.mergeCurrentCharFormat(cur_fmt)
            
        self._schedule_autosave()
        self._sync_format_action_state()

    def toggle_strikethrough(self):
        """Toggle strikethrough while preserving existing character formatting."""
        cursor = self.textCursor()

        if cursor.hasSelection():
            start_pos = cursor.selectionStart()
            end_pos = cursor.selectionEnd()

            # If any part is struck through, remove strikethrough from whole selection.
            has_strikethrough = False
            temp_cursor = QTextCursor(cursor)
            temp_cursor.setPosition(start_pos)
            for pos in range(start_pos, end_pos):
                temp_cursor.setPosition(pos)
                if temp_cursor.charFormat().fontStrikeOut():
                    has_strikethrough = True
                    break

            new_strike_state = not has_strikethrough

            temp_cursor.setPosition(start_pos)
            for pos in range(start_pos, end_pos):
                temp_cursor.setPosition(pos)
                temp_cursor.setPosition(pos + 1, QTextCursor.MoveMode.KeepAnchor)
                char_format = temp_cursor.charFormat()
                char_format.setFontStrikeOut(new_strike_state)
                temp_cursor.mergeCharFormat(char_format)
        else:
            cur_fmt = self.currentCharFormat()
            cur_fmt.setFontStrikeOut(not cur_fmt.fontStrikeOut())
            self.mergeCurrentCharFormat(cur_fmt)

        self._schedule_autosave()
        self._sync_format_action_state()

    # --- paste plain text ---
    def paste_plain_text(self):
        clipboard = QApplication.clipboard()
        mime_data = clipboard.mimeData()
        
        if mime_data.hasText():
            plain_text = mime_data.text()
            cursor = self.textCursor()
            cursor.insertText(plain_text)
            self._schedule_autosave()

    def toggle_highlight(self):
        """Toggle yellow highlight on the current selection or typing format, preserving other formatting."""
        cursor = self.textCursor()

        if cursor.hasSelection():
            # For selections, preserve existing formatting while toggling highlight
            start_pos = cursor.selectionStart()
            end_pos = cursor.selectionEnd()
            
            # Check if any part of the selection is currently highlighted
            has_highlight = False
            temp_cursor = QTextCursor(cursor)
            
            for pos in range(start_pos, end_pos):
                temp_cursor.setPosition(pos)
                char_format = temp_cursor.charFormat()
                bg = char_format.background()
                try:
                    c = bg.color()
                    if (c.red() == 255 and c.green() == 255 and c.blue() == 0 and c.alpha() > 0):
                        has_highlight = True
                        break
                except Exception:
                    continue
            
            # Toggle highlight state - if any part is highlighted, remove highlight from all; otherwise add to all
            new_background = QColor(0, 0, 0, 0) if has_highlight else QColor(255, 255, 0)
            
            # Apply the new background character by character, preserving other formatting
            temp_cursor.setPosition(start_pos)
            for pos in range(start_pos, end_pos):
                temp_cursor.setPosition(pos)
                temp_cursor.setPosition(pos + 1, QTextCursor.MoveMode.KeepAnchor)
                
                # Get current character format and only change the background
                char_format = temp_cursor.charFormat()
                char_format.setBackground(new_background)
                temp_cursor.mergeCharFormat(char_format)
                
        else:
            # For cursor position, use the traditional approach
            cur_fmt = self.currentCharFormat()

            # Determine if currently yellow
            bg = cur_fmt.background()
            is_yellow = False
            try:
                c = bg.color()
                is_yellow = (c.red() == 255 and c.green() == 255 and c.blue() == 0 and c.alpha() > 0)
            except Exception:
                is_yellow = False

            if is_yellow:
                # Clear background by setting fully transparent
                cur_fmt.setBackground(QColor(0, 0, 0, 0))
            else:
                cur_fmt.setBackground(QColor(255, 255, 0))  # yellow

            self.mergeCurrentCharFormat(cur_fmt)
            
        self._schedule_autosave()
        self._sync_format_action_state()

    # --- text size adjustments (A+/A-) ---
    def _effective_point_size(self) -> float:
        """Return the current effective point size, falling back to widget font if undefined."""
        try:
            size = self.fontPointSize()
            if size and size > 0:
                return float(size)
        except Exception:
            pass
        try:
            fs = self.font().pointSizeF()
            if fs and fs > 0:
                return float(fs)
        except Exception:
            pass
        return 12.0

    def _adjust_text_size(self, delta: int):
        """Increase/decrease font size of current selection or typing format, preserving other formatting."""
        cursor = self.textCursor()
        
        if cursor.hasSelection():
            # For selections, preserve existing formatting while adjusting size
            start_pos = cursor.selectionStart()
            end_pos = cursor.selectionEnd()
            
            # Apply the size change character by character, preserving other formatting
            temp_cursor = QTextCursor(cursor)
            temp_cursor.setPosition(start_pos)
            
            for pos in range(start_pos, end_pos):
                temp_cursor.setPosition(pos)
                temp_cursor.setPosition(pos + 1, QTextCursor.MoveMode.KeepAnchor)
                
                # Get current character format and determine its effective size
                char_format = temp_cursor.charFormat()
                current_size = char_format.fontPointSize()
                
                # If no explicit size set, get from the document/widget font
                if current_size <= 0:
                    try:
                        current_size = self.font().pointSizeF()
                        if current_size <= 0:
                            current_size = 12.0
                    except Exception:
                        current_size = 12.0
                
                # Calculate new size with bounds
                new_size = max(8.0, min(72.0, current_size + float(delta)))
                
                # Only change the font size, preserving other formatting
                char_format.setFontPointSize(new_size)
                temp_cursor.mergeCharFormat(char_format)
                
        else:
            # For cursor position, use the traditional approach
            base_size = self._effective_point_size()
            new_size = max(8.0, min(72.0, base_size + float(delta)))
            fmt = self.currentCharFormat()
            fmt.setFontPointSize(new_size)
            self.mergeCurrentCharFormat(fmt)
            
        self._schedule_autosave()
        self._sync_format_action_state()

    def _image_path_at_or_adjacent_to_cursor(self) -> Optional[str]:
        """Return the image src path at the cursor or directly adjacent, if any."""
        cur = self.textCursor()
        fmt = cur.charFormat().toImageFormat()
        if fmt.name():
            return fmt.name()
        # Try left
        if cur.position() > 0:
            tcur = QTextCursor(cur)
            tcur.movePosition(QTextCursor.MoveOperation.Left, QTextCursor.MoveMode.MoveAnchor)
            fmt = tcur.charFormat().toImageFormat()
            if fmt.name():
                return fmt.name()
        # Try right
        if cur.position() < self.document().characterCount() - 1:
            tcur = QTextCursor(cur)
            tcur.movePosition(QTextCursor.MoveOperation.Right, QTextCursor.MoveMode.MoveAnchor)
            fmt = tcur.charFormat().toImageFormat()
            if fmt.name():
                return fmt.name()
        return None

    def copy_image_at_cursor(self):
        """Copy the image at or next to the cursor to the clipboard as an image."""
        path = self._image_path_at_or_adjacent_to_cursor()
        if not path:
            QMessageBox.information(self, "Copy image", "Place the cursor on an image first.")
            return
        abs_path = self._resolve_image_path(path)
        try:
            qimg = QImage(abs_path)
            if qimg.isNull():
                QMessageBox.warning(self, "Copy image", "Could not load image from disk.")
                return
            QApplication.clipboard().setImage(qimg)
        except Exception as e:
            QMessageBox.warning(self, "Copy image", f"Failed to copy image: {e}")

    def copy_selection(self):
        """Copy the selected content (text + images) to the clipboard with multiple formats for maximum compatibility."""
        cursor = self.textCursor()
        if not cursor.hasSelection():
            return
        
        try:
            # First, let Qt do its default copy to get native formatting
            default_mime = QMimeData()
            
            # Get the selected fragment
            selection = cursor.selection()
            
            # Get plain text
            selected_text = cursor.selectedText()
            
            # Get HTML content
            selected_html = selection.toHtml()
            
            # Process HTML to embed images as data URIs
            processed_html = self._embed_images_in_html(selected_html)
            
            # Also extract any images for direct clipboard embedding
            extracted_images = self._extract_images_from_html(selected_html)
            
            # Create mime data with multiple formats for better compatibility
            mime_data = QMimeData()
            
            # 1. Plain text (fallback for any application)
            mime_data.setText(selected_text)
            
            # 2. HTML format (standard)
            mime_data.setHtml(processed_html)
            
            # 3. Rich text data using QTextDocument for better formatting preservation
            temp_doc = QTextDocument()
            temp_doc.setHtml(processed_html)
            
            # 4. Try to use Qt's native rich text clipboard format
            try:
                # Create a temporary cursor and get its rich text format
                temp_cursor = QTextCursor(temp_doc)
                temp_cursor.select(QTextCursor.SelectionType.Document)
                if temp_cursor.hasSelection():
                    # Get Qt's native rich text format
                    qt_mime = QMimeData()
                    temp_cursor.selection().toHtml()  # Ensure selection is processed
                    
                    # Copy Qt's rich text formatting
                    rich_text = temp_doc.toHtml()
                    mime_data.setData("application/x-qt-richtext", rich_text.encode('utf-8'))
            except Exception:
                pass  # Qt rich text format is optional
            
            # 5. Add standard HTML format for web compatibility
            if processed_html:
                # Set standard HTML format without special headers
                mime_data.setData("text/html", processed_html.encode('utf-8'))
            
            # 6. Add actual image data if there are images in the selection
            if extracted_images:
                # If only one image, set it as the main image
                if len(extracted_images) == 1:
                    mime_data.setImageData(extracted_images[0])
                # For multiple images, we'll rely on the HTML format with embedded data URIs
            
            # 7. Additional RTF format for Office applications
            try:
                rtf_data = self._html_to_rtf(processed_html, selected_text)
                if rtf_data:
                    mime_data.setData("application/rtf", rtf_data.encode('utf-8'))
                    mime_data.setData("text/rtf", rtf_data.encode('utf-8'))
            except Exception:
                pass  # RTF conversion is optional
            
            # 8. Also try the native Qt copy mechanism as a fallback
            try:
                # Store current clipboard
                old_clipboard = QApplication.clipboard().mimeData()
                
                # Temporarily use Qt's native copy
                super(NotesEditor, self).copy()
                
                # Get what Qt copied
                native_mime = QApplication.clipboard().mimeData()
                
                # Merge native formats into our mime data
                for fmt in native_mime.formats():
                    if not mime_data.hasFormat(fmt):
                        mime_data.setData(fmt, native_mime.data(fmt))
                
            except Exception:
                pass  # Native copy is optional
            
            # Set to clipboard
            QApplication.clipboard().setMimeData(mime_data)
            
        except Exception as e:
            QMessageBox.warning(self, "Copy", f"Failed to copy selection: {e}")

    def _html_to_rtf(self, html: str, plain_text: str) -> str:
        """Convert HTML to basic RTF format for Office compatibility."""
        try:
            # Basic RTF header
            rtf = r"{\rtf1\ansi\deff0 {\fonttbl {\f0 Times New Roman;}}"
            
            # Simple HTML to RTF conversion (basic formatting)
            import re
            
            # Remove HTML tags and convert basic formatting
            text = html
            
            # Bold
            text = re.sub(r'<b[^>]*>(.*?)</b>', r'{\\b \1\\b0}', text, flags=re.IGNORECASE | re.DOTALL)
            text = re.sub(r'<strong[^>]*>(.*?)</strong>', r'{\\b \1\\b0}', text, flags=re.IGNORECASE | re.DOTALL)
            
            # Italic  
            text = re.sub(r'<i[^>]*>(.*?)</i>', r'{\\i \1\\i0}', text, flags=re.IGNORECASE | re.DOTALL)
            text = re.sub(r'<em[^>]*>(.*?)</em>', r'{\\i \1\\i0}', text, flags=re.IGNORECASE | re.DOTALL)
            
            # Remove remaining HTML tags
            text = re.sub(r'<[^>]+>', '', text)
            
            # Escape RTF special characters
            text = text.replace('\\', '\\\\')
            text = text.replace('{', '\\{')
            text = text.replace('}', '\\}')
            
            # Add paragraph breaks
            text = text.replace('\n', '\\par ')
            
            rtf += text + "}"
            return rtf
            
        except Exception:
            return None

    def _embed_images_in_html(self, html: str) -> str:
        """Convert image src paths in HTML to data URIs with embedded image data."""
        import re
        import base64
        import mimetypes
        
        def replace_img_src(match):
            src = match.group(1)
            try:
                # Determine absolute path
                if os.path.isabs(src):
                    abs_path = src
                else:
                    abs_path = self._resolve_image_path(src)
                
                # Check if file exists
                if not os.path.exists(abs_path):
                    return match.group(0)  # Return original if file not found
                
                # Read and encode image
                with open(abs_path, 'rb') as img_file:
                    img_data = img_file.read()
                
                # Get MIME type
                mime_type, _ = mimetypes.guess_type(abs_path)
                if not mime_type or not mime_type.startswith('image/'):
                    mime_type = 'image/png'  # Default fallback
                
                # Create data URI
                encoded_data = base64.b64encode(img_data).decode('ascii')
                data_uri = f"data:{mime_type};base64,{encoded_data}"
                
                # Return the img tag with data URI
                return f'src="{data_uri}"'
                
            except Exception:
                # If anything fails, return the original src
                return match.group(0)
        
        # Replace all src attributes in img tags
        pattern = r'src="([^"]*)"'
        return re.sub(pattern, replace_img_src, html)

    def _extract_images_from_html(self, html: str) -> list:
        """Extract image data from HTML for direct clipboard embedding."""
        import re
        from PyQt6.QtGui import QPixmap
        
        images = []
        def extract_img_src(match):
            src = match.group(1)
            try:
                # Determine absolute path
                if os.path.isabs(src):
                    abs_path = src
                else:
                    abs_path = self._resolve_image_path(src)
                
                # Check if file exists
                if os.path.exists(abs_path):
                    # Load image as QPixmap for clipboard
                    pixmap = QPixmap(abs_path)
                    if not pixmap.isNull():
                        images.append(pixmap)
                        
            except Exception:
                pass  # Ignore errors
            
            return match.group(0)  # Return original match
        
        # Find all img tags and extract their images
        pattern = r'src="([^"]*)"'
        re.sub(pattern, extract_img_src, html)
        return images

    def clear_formatting(self):
        """Clear formatting from the selected text while preserving images."""
        cursor = self.textCursor()
        if not cursor.hasSelection():
            return
            
        try:
            # Get the HTML content of the selection
            fragment = cursor.selection()
            html_content = fragment.toHtml()
            
            # Process HTML to remove formatting but preserve images
            cleaned_html = self._strip_text_formatting_preserve_images(html_content)
            
            # Replace the selection with cleaned content
            cursor.insertHtml(cleaned_html)
            self._schedule_autosave()
            self._sync_format_action_state()
            
        except Exception as e:
            QMessageBox.warning(self, "Clear formatting", f"Failed to clear formatting: {e}")
    
    def _strip_text_formatting_preserve_images(self, html: str) -> str:
        """Remove text formatting from HTML while preserving images."""
        import re
        
        # Find and preserve all img tags first
        img_tags = []
        img_pattern = r'<img[^>]*>'
        
        def preserve_img(match):
            img_tags.append(match.group(0))
            return f'__IMG_PLACEHOLDER_{len(img_tags)-1}__'
        
        # Replace img tags with placeholders
        html_no_imgs = re.sub(img_pattern, preserve_img, html, flags=re.IGNORECASE)
        
        # Remove formatting tags but keep structure tags and img placeholders
        # Remove font styling attributes and tags
        html_no_imgs = re.sub(r'</?(?:b|strong|i|em|u|s|strike|sup|sub|small|big|font|span|mark)[^>]*>', '', html_no_imgs, flags=re.IGNORECASE)
        
        # Remove style attributes from remaining tags
        html_no_imgs = re.sub(r'\s+style\s*=\s*["\'][^"\']*["\']', '', html_no_imgs, flags=re.IGNORECASE)
        
        # Remove color, size, and other formatting attributes
        html_no_imgs = re.sub(r'\s+(?:color|size|face|bgcolor|align)\s*=\s*["\'][^"\']*["\']', '', html_no_imgs, flags=re.IGNORECASE)
        
        # Restore img tags
        for i, img_tag in enumerate(img_tags):
            html_no_imgs = html_no_imgs.replace(f'__IMG_PLACEHOLDER_{i}__', img_tag)
        
        return html_no_imgs
    def _handle_indent(self):
        """Handle Tab key press - insert reasonable indentation."""
        cursor = self.textCursor()
        
        if cursor.hasSelection():
            # If text is selected, indent all selected lines
            self._indent_selected_lines(cursor, indent=True)
        else:
            # Insert 4 spaces for reasonable indentation
            cursor.insertText("    ")
        
        self._schedule_autosave()
    
    def _handle_outdent(self):
        """Handle Shift+Tab key press - reduce indentation."""
        cursor = self.textCursor()
        
        if cursor.hasSelection():
            # If text is selected, outdent all selected lines
            self._indent_selected_lines(cursor, indent=False)
        else:
            # Remove indentation at cursor position
            self._remove_indentation_at_cursor(cursor)
        
        self._schedule_autosave()
    
    def _indent_selected_lines(self, cursor, indent=True):
        """Indent or outdent selected lines."""
        # Get the selection boundaries
        start = cursor.selectionStart()
        end = cursor.selectionEnd()
        
        # Move to start of first selected line
        cursor.setPosition(start)
        cursor.movePosition(QTextCursor.MoveOperation.StartOfLine)
        start_of_first_line = cursor.position()
        
        # Move to end of last selected line
        cursor.setPosition(end)
        cursor.movePosition(QTextCursor.MoveOperation.EndOfLine)
        end_of_last_line = cursor.position()
        
        # Select from start of first line to end of last line
        cursor.setPosition(start_of_first_line)
        cursor.setPosition(end_of_last_line, QTextCursor.MoveMode.KeepAnchor)
        
        # Get all text in selection
        selected_text = cursor.selectedText()
        lines = selected_text.split('\u2029')  # QTextEdit uses Unicode paragraph separator
        
        # Process each line
        modified_lines = []
        for line in lines:
            if indent:
                # Add 4 spaces to the beginning of each line
                modified_lines.append("    " + line)
            else:
                # Remove one level of indentation from the beginning of each line
                if line.startswith("    "):  # Remove 4 spaces first
                    modified_lines.append(line[4:])
                elif line.startswith("\t"):  # Remove tab if present
                    modified_lines.append(line[1:])
                elif line.startswith("  "):    # Remove 2 spaces if present
                    modified_lines.append(line[2:])
                elif line.startswith(" "):    # Remove single space if present
                    modified_lines.append(line[1:])
                else:
                    modified_lines.append(line)
        
        # Replace selection with modified text
        modified_text = '\u2029'.join(modified_lines)
        cursor.insertText(modified_text)
        
        # Restore selection to cover the modified text
        cursor.setPosition(start_of_first_line)
        cursor.setPosition(start_of_first_line + len(modified_text), QTextCursor.MoveMode.KeepAnchor)
        self.setTextCursor(cursor)
    
    def _remove_indentation_at_cursor(self, cursor):
        """Remove indentation from the beginning of the current line."""
        # Save current position
        current_pos = cursor.position()
        
        # Move to start of current line
        cursor.movePosition(QTextCursor.MoveOperation.StartOfLine)
        line_start = cursor.position()
        
        # Move to end of current line to get the full line
        cursor.movePosition(QTextCursor.MoveOperation.EndOfLine, QTextCursor.MoveMode.KeepAnchor)
        line_text = cursor.selectedText()
        
        # Determine what indentation to remove from the beginning of the line
        removed_chars = 0
        if line_text.startswith("    "):
            # Remove 4 spaces (our standard indent)
            removed_chars = 4
        elif line_text.startswith("\t"):
            # Remove 1 tab character
            removed_chars = 1
        elif line_text.startswith("  "):
            # Remove 2 spaces
            removed_chars = 2
        elif line_text.startswith(" "):
            # Remove 1 space
            removed_chars = 1
        
        if removed_chars > 0:
            # Select and remove the indentation from the start of the line
            cursor.setPosition(line_start)
            cursor.setPosition(line_start + removed_chars, QTextCursor.MoveMode.KeepAnchor)
            cursor.removeSelectedText()
            
            # Adjust cursor position relative to the removal
            new_cursor_pos = max(line_start, current_pos - removed_chars)
            cursor.setPosition(new_cursor_pos)
            
            # Update the editor's cursor
            self.setTextCursor(cursor)

    # --- autosave machinery ---
    def _schedule_autosave(self):
        if not self._in_resize_commit:
            self._autosave_timer.start()
            self._html_save_timer.start()

    def _after_common_format_change(self):
        self._schedule_autosave()

    def _autosave_timeout(self):
        if not self._in_resize_commit:
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

    def set_task(self, task_id: Optional[int]):
        self.task_id = task_id
        self._autosave_timer.stop(); self._html_save_timer.stop()
        if task_id is None:
            self.setHtml("")
            self._last_plain_hash = self._last_html_hash = ""
            # Clear cursor positioning after clearing content
            QTimer.singleShot(0, self._ensure_cursor_visibility)
            return
        html, _plain = self.db.get_note(task_id)
        self.blockSignals(True)
        self.setHtml(html)
        # Register images from HTML as resources
        self._register_images_from_html(html)
        self.blockSignals(False)
        self._last_plain_hash = hashlib.md5((_plain or "").encode('utf-8','ignore')).hexdigest()
        self._last_html_hash = hashlib.md5((html or "").encode('utf-8','ignore')).hexdigest()
        # Ensure cursor is properly positioned after loading new content
        QTimer.singleShot(50, self._ensure_cursor_visibility)

    def rebind_context(self, db: DB, attach_dir: str, reload_current_task: bool = False):
        """Rebind the editor to a new database and attachments directory."""
        self.db = db
        self.attach_dir = attach_dir
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
        if self.task_id is None:
            return
        try:
            # Check if database connection is still valid
            if not (self.db and hasattr(self.db, 'conn') and self.db.conn):
                return
            plain = self.toPlainText()
            ph = hashlib.md5(plain.encode('utf-8','ignore')).hexdigest()
            if ph != self._last_plain_hash:
                self.db.conn.execute("UPDATE notes SET plain_text=?, updated_at=CURRENT_TIMESTAMP WHERE task_id=?", (plain, self.task_id))
                self.db.conn.commit()
                self._last_plain_hash = ph
        except Exception:
            # Silently ignore database errors during shutdown
            pass

    def _save_full_html(self):
        if self.task_id is None or self._in_resize_commit:
            return
        try:
            # Check if database connection is still valid
            if not (self.db and hasattr(self.db, 'conn') and self.db.conn):
                return
            html = self.toHtml(); plain = self.toPlainText()
            hh = hashlib.md5(html.encode('utf-8','ignore')).hexdigest()
            if hh != self._last_html_hash:
                self.db.save_note(self.task_id, html, plain, self.attach_dir)
                self._last_html_hash = hh
                self._last_plain_hash = hashlib.md5(plain.encode('utf-8','ignore')).hexdigest()
        except Exception:
            # Silently ignore database errors during shutdown
            pass
    
    def cleanup_resources(self):
        """Clean up timers and save any pending changes."""
        try:
            # Stop timers to prevent further callbacks
            if hasattr(self, '_autosave_timer'):
                self._autosave_timer.stop()
            if hasattr(self, '_html_save_timer'):
                self._html_save_timer.stop()
            
            # Save any pending changes (only if database is still open)
            try:
                if self.db and hasattr(self.db, 'conn') and self.db.conn:
                    self.save_now()
                    self._save_full_html()
            except Exception as db_error:
                # Database might be closed already, which is fine during shutdown
                if "closed database" not in str(db_error).lower():
                    print(f"NotesEditor save error during cleanup: {db_error}")
            
        except Exception as e:
            print(f"NotesEditor cleanup error: {e}")

    def reload_html(self):
        """Reload current task's HTML from DB (used after external image edits)."""
        if self.task_id is None:
            return
        html, plain = self.db.get_note(self.task_id)
        self.blockSignals(True)
        self.setHtml(html)
        self.blockSignals(False)
        self._last_html_hash = hashlib.md5((html or "").encode('utf-8','ignore')).hexdigest()
        self._last_plain_hash = hashlib.md5((plain or "").encode('utf-8','ignore')).hexdigest()

    def _current_image_path(self) -> Optional[str]:
        cur = self.textCursor()
        fmt = cur.charFormat().toImageFormat()
        src = fmt.name()
        return src if src else None

    # --- Image insertion helpers & overrides (restored) ---
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

    def _resolve_image_path(self, rel_path: str) -> str:
        """Convert relative image path from HTML to absolute file path."""
        if os.path.isabs(rel_path):
            return rel_path
        else:
            # Resolve relative path relative to the database directory (parent of attach_dir)
            db_dir = os.path.dirname(self.attach_dir)
            return os.path.join(db_dir, rel_path)

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
        max_w = 1400
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

    def insertFromMimeData(self, source: QMimeData):  # override
        # Apply default formatting if inserting into empty editor
        if (self.toPlainText().strip() == "" or self.document().isEmpty()):
            self._apply_default_formatting()
            
        inserted = False
        try:
            # Direct QImage
            if source.hasImage():
                qimg = source.imageData()
                if isinstance(qimg, QImage) and not qimg.isNull():
                    fname = f"{uuid.uuid4().hex}.png"
                    folder_name = os.path.basename(self.attach_dir)
                    rel = f"{folder_name}/{fname}"
                    abs_p = os.path.join(self.attach_dir, fname)
                    qimg.save(abs_p, 'PNG')
                    inserted = self._insert_image_file(abs_p, rel)
            # Raw bytes (clipboard without QImage)
            if not inserted:
                for fmt, ext in (('image/png','.png'), ('image/jpeg','.jpg')):
                    if source.hasFormat(fmt):
                        try:
                            ba = source.data(fmt)
                            if ba:
                                fname = f"{uuid.uuid4().hex}{ext}"
                                folder_name = os.path.basename(self.attach_dir)
                                rel = f"{folder_name}/{fname}"
                                abs_p = os.path.join(self.attach_dir, fname)
                                with open(abs_p,'wb') as f: 
                                    f.write(bytes(ba))
                                inserted = self._insert_image_file(abs_p, rel)
                                break
                        except Exception:
                            pass
            # File URLs (drag/drop)
            if source.hasUrls():
                for u in source.urls():
                    if not u.isLocalFile():
                        continue
                    ext = os.path.splitext(u.toLocalFile())[1].lower()
                    if ext in ('.png','.jpg','.jpeg','.gif','.bmp','.webp'):
                        new_name = f"{uuid.uuid4().hex}{ext}"
                        folder_name = os.path.basename(self.attach_dir)
                        rel = f"{folder_name}/{new_name}"
                        dest = os.path.join(self.attach_dir, new_name)
                        try:
                            with open(u.toLocalFile(),'rb') as r, open(dest,'wb') as w: 
                                w.write(r.read())
                            if self._insert_image_file(dest, rel): 
                                inserted = True
                        except Exception:
                            pass
        finally:
            if inserted:
                self._schedule_autosave()
                # Ensure cursor is properly positioned after image insertion
                QTimer.singleShot(10, self._ensure_cursor_visibility)
        if not inserted:
            super().insertFromMimeData(source)
            # Ensure cursor is properly positioned after text insertion
            if source.hasText():
                QTimer.singleShot(10, self._ensure_cursor_visibility)

    def resize_image_at_cursor(self):
        path = self._current_image_path()
        if not path:
            QMessageBox.information(self, "Resize Image", "Place the cursor on an image first.")
            return
        self._start_inline_resize(path)

    def crop_image_visual(self):
        path = self._current_image_path()
        if not path:
            QMessageBox.information(self, "Crop Image", "Place the cursor on an image first.")
            return
        abs_path = self._resolve_image_path(path)
        if not os.path.isfile(abs_path):
            QMessageBox.warning(self, "Crop Image", "Image file not found on disk.")
            return
        dlg = CropDialog(abs_path, self)
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return
        rect = dlg.selected_rect()
        if not rect or rect.width() < 1 or rect.height() < 1:
            QMessageBox.information(self, "Crop Image", "No crop area selected.")
            return
        try:
            with Image.open(abs_path) as im:
                l = max(0, rect.x()); t = max(0, rect.y())
                r = min(im.width, rect.x() + rect.width())
                b = min(im.height, rect.y() + rect.height())
                if r - l >= 1 and b - t >= 1:
                    im = im.crop((l, t, r, b)); im.save(abs_path)
        except Exception as e:
            QMessageBox.warning(self, "Crop Image", f"Failed to crop: {e}")
            return
        self.reload_html()

    def cut_image_at_cursor(self):
        path = self._current_image_path()
        if not path:
            QMessageBox.information(self, "Cut Image", "Place the cursor on an image first.")
            return
        abs_path = self._resolve_image_path(path)
        cur = self.textCursor(); cur.deleteChar(); self.setTextCursor(cur)
        try:
            if os.path.isfile(abs_path):
                os.remove(abs_path)
        except Exception:
            pass
        self._schedule_autosave()

    # --- Inline resize helpers (improved) ---
    def _start_inline_resize(self, path: str):
        if self._resize_mode:
            self._end_inline_resize(commit=False)
            
        abs_path = self._resolve_image_path(path)
        if not os.path.isfile(abs_path):
            return
            
        try:
            with Image.open(abs_path) as im:
                img_w, img_h = im.width, im.height
        except Exception:
            return

        cur = self.textCursor()
        
        # Ensure cursor is positioned on an image character
        if not cur.charFormat().isImageFormat():
            if cur.position() > 0:
                tcur = QTextCursor(cur)
                tcur.movePosition(QTextCursor.MoveOperation.Left, QTextCursor.MoveMode.MoveAnchor)
                if tcur.charFormat().isImageFormat():
                    cur = tcur
                    self.setTextCursor(cur)
            # Try moving right if left didn't work
            if not cur.charFormat().isImageFormat() and cur.position() < self.document().characterCount() - 1:
                tcur = QTextCursor(cur)
                tcur.movePosition(QTextCursor.MoveOperation.Right, QTextCursor.MoveMode.MoveAnchor)
                if tcur.charFormat().isImageFormat():
                    cur = tcur
                    self.setTextCursor(cur)
                    
        if not cur.charFormat().isImageFormat():
            QMessageBox.information(self, "Resize Image", "Could not find image at cursor position.")
            return

        self.ensureCursorVisible()
        
        # Get the image format to determine display size
        fmt = cur.charFormat().toImageFormat()
        w_disp = int(fmt.width()) if fmt.width() > 0 else img_w
        h_disp = int(fmt.height()) if fmt.height() > 0 else img_h
        w_disp = max(10, w_disp)  # minimum 10px
        h_disp = max(10, h_disp)  # minimum 10px

        # Get precise image position by selecting the character and measuring
        sel = QTextCursor(cur)
        sel.movePosition(QTextCursor.MoveOperation.Right, QTextCursor.MoveMode.KeepAnchor)
        r_img = self.cursorRect(sel)
        
        # Position overlay correctly aligned with the actual image
        if r_img.width() > 1 and r_img.height() > 1:
            # Use the actual selection rectangle for positioning
            left_x, top_y = r_img.left(), r_img.top()
            # For display size, prefer format dimensions if available, otherwise use measured
            if fmt.width() > 0 and fmt.height() > 0:
                # Format has explicit dimensions, use those for consistency
                pass  # keep w_disp, h_disp as calculated from format
            else:
                # No format dimensions, use measured size
                w_disp = r_img.width()
                h_disp = r_img.height()
        else:
            # Fallback: calculate from cursor position
            caret_rect = self.cursorRect(cur)
            # For inline images, they typically appear at the cursor position
            left_x = caret_rect.x()
            top_y = caret_rect.y()
            
            # Adjust positioning based on text layout
            if cur.position() > 0:
                # Check if we need to position the overlay differently
                test_cursor = QTextCursor(cur)
                test_cursor.movePosition(QTextCursor.MoveOperation.Left, QTextCursor.MoveMode.MoveAnchor)
                prev_rect = self.cursorRect(test_cursor)
                
                # If the previous character is on the same line, image is inline
                if abs(prev_rect.y() - caret_rect.y()) < 5:  # same line
                    # Position overlay to cover where the image should be
                    left_x = prev_rect.right()
                    top_y = prev_rect.top()

        # Create resize overlay
        self._resize_rubber = QRubberBand(QRubberBand.Shape.Rectangle, self.viewport())
        self._resize_rubber.setStyleSheet("""
            QRubberBand { 
                border: 2px solid #2564cf; 
                background: rgba(37, 100, 207, 30); 
            }
        """)
        self._resize_rubber.setGeometry(left_x, top_y, w_disp, h_disp)
        self._resize_rubber.show()

        # Initialize state
        self._resize_mode = True
        self._resize_path = path
        self._resize_origin_size = (img_w, img_h)
        self._resize_img_cursor = QTextCursor(cur)
        self._resize_img_pos = cur.position()
        self._resize_current_size = (w_disp, h_disp)
        self._resize_aspect = (w_disp / h_disp) if h_disp > 0 else 1.0
        self._resize_anchor = QRect(left_x, top_y, w_disp, h_disp)
        self._resize_dragging = False

        # Create resize handle
        if self._resize_handle:
            self._resize_handle.deleteLater()
            
        self._resize_handle = QWidget(self.viewport())
        self._resize_handle.setCursor(Qt.CursorShape.SizeFDiagCursor)
        self._resize_handle.setStyleSheet("""
            QWidget { 
                background: #2564cf; 
                border: 2px solid #1e55b1; 
                border-radius: 4px; 
            }
        """)
        self._resize_handle.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, False)
        self._position_resize_handle()
        self._resize_handle.show()
        
        # Set cursor to indicate resize mode
        self.viewport().setCursor(Qt.CursorShape.CrossCursor)

    def _position_resize_handle(self):
        if not (self._resize_handle and self._resize_rubber):
            return
        g = self._resize_rubber.geometry()
        sz = 12  # handle size
        # Position handle at bottom-right corner with slight overlap
        self._resize_handle.setGeometry(
            g.right() - sz + 2, 
            g.bottom() - sz + 2, 
            sz, 
            sz
        )
        # Ensure handle is visible and on top
        self._resize_handle.raise_()
        self._resize_handle.show()

    def _update_resize_rubber(self):
        # Recompute image rect from stored position (handles scroll / reflow)
        if not (self._resize_mode and self._resize_rubber and self._resize_img_pos is not None and self._resize_current_size):
            return
            
        # Find the image cursor position
        temp = QTextCursor(self.document())
        temp.setPosition(self._resize_img_pos)
        
        # Ensure we're on the image character
        if not temp.charFormat().isImageFormat() and self._resize_img_pos > 0:
            temp.setPosition(self._resize_img_pos - 1)
            
        if not temp.charFormat().isImageFormat():
            # Image may have been deleted or moved, end resize mode
            self._end_inline_resize(commit=False)
            return
            
        # Get the current image rectangle by selecting it
        sel = QTextCursor(temp)
        sel.movePosition(QTextCursor.MoveOperation.Right, QTextCursor.MoveMode.KeepAnchor)
        r_img = self.cursorRect(sel)
        
        # Use current size for overlay
        w, h = self._resize_current_size
        
        # Position overlay at the image location
        if r_img.width() > 1 and r_img.height() > 1:
            left_x = r_img.left()
            top_y = r_img.top()
        else:
            # Fallback positioning
            caret_rect = self.cursorRect(temp)
            left_x = caret_rect.x()
            top_y = caret_rect.y()
        
        # Update rubber band and anchor
        self._resize_rubber.setGeometry(left_x, top_y, w, h)
        self._resize_anchor = QRect(left_x, top_y, w, h)
        self._position_resize_handle()

    def mousePressEvent(self, e):
        if self._resize_mode and e.button() == Qt.MouseButton.LeftButton:
            # Check if clicking on resize handle
            if self._resize_handle and self._resize_handle.isVisible():
                handle_rect = self._resize_handle.geometry()
                click_pos = e.position().toPoint()
                if handle_rect.contains(click_pos):
                    self._resize_dragging = True
                    e.accept()
                    return
            # If not on handle but in resize mode, end resize and allow normal cursor placement
            self._end_inline_resize(commit=False)
            # Don't return here - let the normal mouse handling proceed
        
        # Always call parent to ensure proper cursor placement
        super().mousePressEvent(e)
        
        # Force cursor visibility and position update after click
        if e.button() == Qt.MouseButton.LeftButton and not self._resize_mode:
            # Ensure cursor is visible and properly positioned
            QTimer.singleShot(0, self._ensure_cursor_visibility)

    def mouseMoveEvent(self, e):
        if self._resize_mode and self._resize_dragging and self._resize_anchor and self._resize_rubber:
            # Auto-scroll when near edges
            margin = 30
            vp = self.viewport().rect()
            pos = e.position().toPoint()
            
            # Trigger auto-scroll if near edges
            vsb = self.verticalScrollBar()
            hsb = self.horizontalScrollBar()
            if pos.y() > vp.bottom() - margin and vsb.value() < vsb.maximum():
                vsb.setValue(vsb.value() + 15)
                QTimer.singleShot(5, self._update_resize_rubber)
            elif pos.y() < vp.top() + margin and vsb.value() > vsb.minimum():
                vsb.setValue(vsb.value() - 15)
                QTimer.singleShot(5, self._update_resize_rubber)
            
            if pos.x() > vp.right() - margin and hsb.value() < hsb.maximum():
                hsb.setValue(hsb.value() + 15)
                QTimer.singleShot(5, self._update_resize_rubber)
            elif pos.x() < vp.left() + margin and hsb.value() > hsb.minimum():
                hsb.setValue(hsb.value() - 15)
                QTimer.singleShot(5, self._update_resize_rubber)

            # Calculate new size from mouse position
            anchor_left = self._resize_anchor.left()
            anchor_top = self._resize_anchor.top()
            new_w = max(10, pos.x() - anchor_left)  # minimum 10px width
            new_h = max(10, pos.y() - anchor_top)   # minimum 10px height
            
            # Apply aspect ratio constraint unless Shift is held
            if self._resize_aspect and self._resize_aspect > 0:
                shift_held = bool(e.modifiers() & Qt.KeyboardModifier.ShiftModifier)
                if not shift_held:
                    # Lock aspect ratio - choose the dimension that's closest to mouse
                    aspect_h = int(round(new_w / self._resize_aspect))
                    aspect_w = int(round(new_h * self._resize_aspect))
                    
                    # Use whichever gives a result closer to the mouse position
                    if abs(aspect_h - new_h) < abs(aspect_w - new_w):
                        new_h = max(10, aspect_h)
                    else:
                        new_w = max(10, aspect_w)
            
            # Update current size and rubber band geometry
            self._resize_current_size = (new_w, new_h)
            self._resize_rubber.setGeometry(anchor_left, anchor_top, new_w, new_h)
            self._position_resize_handle()
            
            e.accept()
            return
            
        super().mouseMoveEvent(e)

    def mouseReleaseEvent(self, e):
        if self._resize_mode and self._resize_dragging and e.button() == Qt.MouseButton.LeftButton:
            self._resize_dragging = False
            e.accept()
            return
        
        # Handle normal mouse release first
        super().mouseReleaseEvent(e)
        
        # Ensure cursor positioning is correct after mouse release
        if e.button() == Qt.MouseButton.LeftButton and not self._resize_mode:
            # Small delay to allow Qt's mouse handling to complete
            QTimer.singleShot(5, self._ensure_cursor_visibility)

    def _end_inline_resize(self, commit: bool):
        if not self._resize_mode:
            return
        if commit and self._resize_rubber and self._resize_path and self._resize_origin_size and self._resize_img_pos is not None:
            # Determine final size
            if self._resize_current_size:
                new_w, new_h = self._resize_current_size
            else:
                gr = self._resize_rubber.geometry(); new_w, new_h = gr.width(), gr.height()
            new_w = max(1, new_w); new_h = max(1, new_h)
            ow, oh = self._resize_origin_size
            if (new_w, new_h) != (ow, oh):
                abs_path = self._resolve_image_path(self._resize_path)
                try:
                    # Step 1: Stop all autosave mechanisms completely
                    self._in_resize_commit = True
                    self._autosave_timer.stop()
                    self._html_save_timer.stop()
                    
                    # Step 2: Resize the physical image file
                    with Image.open(abs_path) as im:
                        im = im.resize((new_w, new_h), Image.Resampling.LANCZOS)
                        im.save(abs_path)
                    
                    # Step 3: Get current content and modify HTML directly
                    current_html = self.toHtml()
                    current_plain = self.toPlainText()
                    
                    # Find and replace the image tag dimensions in HTML
                    import re
                    img_src_escaped = re.escape(self._resize_path)
                    
                    # Pattern to match the image tag and capture all attributes
                    img_pattern = rf'<img\s+([^>]*src=["\']?{img_src_escaped}["\'][^>]*)/?\s*>'
                    
                    def update_dimensions(match):
                        attrs = match.group(1)
                        # Remove existing width and height attributes
                        attrs = re.sub(r'\s*(?:width|height)\s*=\s*["\']?\d+["\']?', '', attrs, flags=re.IGNORECASE)
                        # Clean up extra spaces and add new dimensions
                        attrs = ' '.join(attrs.split())
                        return f'<img {attrs} width="{new_w}" height="{new_h}" />'
                    
                    updated_html = re.sub(img_pattern, update_dimensions, current_html)
                    
                    # Step 4: Use setHtml for complete content replacement (cleanest approach)
                    self.blockSignals(True)
                    try:
                        # Store scroll position to restore later
                        scrollbar = self.verticalScrollBar()
                        scroll_pos = scrollbar.value()
                        
                        # Complete replacement with updated HTML
                        self.setHtml(updated_html)
                        
                        # Update image resource with new file data
                        qimg = QImage(abs_path)
                        if not qimg.isNull():
                            self.document().addResource(
                                QTextDocument.ResourceType.ImageResource, 
                                QUrl(self._resize_path), 
                                qimg
                            )
                        
                        # Restore scroll position
                        scrollbar.setValue(scroll_pos)
                        
                    finally:
                        # Always restore signal connections
                        self.blockSignals(False)
                    
                    # Step 5: Force display refresh
                    self.viewport().update()
                    self.update()
                    
                    # Step 6: Save to database immediately with proper hashes
                    final_html = self.toHtml()
                    final_plain = self.toPlainText()
                    self.db.save_note(self.task_id, final_html, final_plain, self.attach_dir)
                    self._last_html_hash = hashlib.md5(final_html.encode('utf-8','ignore')).hexdigest()
                    self._last_plain_hash = hashlib.md5(final_plain.encode('utf-8','ignore')).hexdigest()
                        
                except Exception as e:
                    # If anything fails, reload from database
                    print(f"Resize error: {e}")
                    self.reload_html()
                finally:
                    # Always clear the commit flag
                    self._in_resize_commit = False
        # Tear down state
        if self._resize_rubber: self._resize_rubber.hide(); self._resize_rubber.deleteLater()
        if self._resize_handle: self._resize_handle.hide(); self._resize_handle.deleteLater(); self._resize_handle = None
        self._resize_rubber = None; self._resize_mode = False; self._resize_path = None; self._resize_origin_size = None
        self._resize_anchor = None; self._resize_dragging = False; self._resize_img_cursor = None
        self._resize_current_size = None; self._resize_aspect = None; self._resize_left_x0_initial = None
        self._resize_caret_x0 = None; self._resize_caret_bottom0 = None; self._resize_img_pos = None; self.viewport().unsetCursor()

    def _ensure_cursor_visibility(self):
        """Ensure the text cursor is properly positioned and visible after user interaction."""
        if self._resize_mode or self._in_resize_commit:
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
            
        # Handle tab key events for better indentation
        if e.key() == Qt.Key.Key_Tab and not (e.modifiers() & (Qt.KeyboardModifier.ControlModifier | Qt.KeyboardModifier.AltModifier)):
            if e.modifiers() & Qt.KeyboardModifier.ShiftModifier:
                # Shift+Tab: outdent
                self._handle_outdent()
            else:
                # Tab: indent
                self._handle_indent()
            e.accept()
            return
            
        if self._resize_mode:
            if e.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
                self._end_inline_resize(commit=True); e.accept(); return
            if e.key() == Qt.Key.Key_Escape:
                self._end_inline_resize(commit=False); e.accept(); return
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
        if not self._resize_mode:
            # Apply default formatting if focusing on empty editor
            if (self.toPlainText().strip() == "" or self.document().isEmpty()):
                self._apply_default_formatting()
            # Ensure cursor is visible when gaining focus
            QTimer.singleShot(10, self._ensure_cursor_visibility)

    def focusOutEvent(self, e):
        if self._resize_mode:
            self._end_inline_resize(commit=False)
        self._save_full_html()
        super().focusOutEvent(e)
        self.save_now()

    def dragEnterEvent(self, e):
        if self.canInsertFromMimeData(e.mimeData()):
            e.acceptProposedAction()
        else:
            super().dragEnterEvent(e)

    def dropEvent(self, e):
        md = e.mimeData()
        if self.canInsertFromMimeData(md):
            self.insertFromMimeData(md)
            e.acceptProposedAction()
            # Ensure cursor is positioned properly after drop
            QTimer.singleShot(10, self._ensure_cursor_visibility)
        else:
            super().dropEvent(e)

    def scrollContentsBy(self, dx, dy):
        # Update resize overlay when scrolling
        if self._resize_mode:
            QTimer.singleShot(10, self._update_resize_rubber)
        super().scrollContentsBy(dx, dy)

# -------------------------- Pinned Tasks Overview --------------------------

class PinnedTaskItemDelegate(QStyledItemDelegate):
    """Custom delegate to improve vertical spacing, full-width rounded selection, and text elision."""
    def __init__(self, parent=None):
        super().__init__(parent)
        self.vpad = 6  # vertical padding
        self.hpad = 10 # horizontal padding

    def sizeHint(self, option, index):
        fm = option.fontMetrics
        h = fm.height() + self.vpad * 2
        # Minimum comfortable height
        if h < 30:
            h = 30
        return QSize(option.rect.width(), h)

    def paint(self, painter, option, index):
        painter.save()
        text = index.data(Qt.ItemDataRole.DisplayRole) or ""
        rect = option.rect.adjusted(0, 0, -1, -1)
        is_selected = option.state & QStyle.StateFlag.State_Selected
        is_hover = option.state & QStyle.StateFlag.State_MouseOver

        # Colors (align with theme variables used in stylesheet)
        sel_bg = QColor('#cfe4ff')
        hover_bg = QColor('#e6f1fe')
        data_color = index.data(Qt.ItemDataRole.ForegroundRole)
        text_col = data_color if isinstance(data_color, QColor) else QColor('#202124')
        normal_bg = QColor(255, 255, 255, 0)
        radius = 9

        # Background fill
        if is_selected:
            painter.setBrush(sel_bg)
            painter.setPen(Qt.PenStyle.NoPen)
            painter.drawRoundedRect(rect, radius, radius)
        elif is_hover:
            painter.setBrush(hover_bg)
            painter.setPen(Qt.PenStyle.NoPen)
            painter.drawRoundedRect(rect, radius, radius)
        else:
            # keep transparent
            pass

        # Text
        painter.setPen(text_col)
        avail = rect.adjusted(self.hpad, 0, -self.hpad, 0)
        elided = option.fontMetrics.elidedText(text, Qt.TextElideMode.ElideRight, avail.width())
        painter.drawText(avail, Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft, elided)
        painter.restore()

class PinnedTasksOverview(QDialog):
    task_selected = pyqtSignal(int, int)  # project_id, task_id
    
    def __init__(self, db: DB, parent=None):
        super().__init__(parent)
        self.db = db
        self.setWindowTitle("Pinned Tasks Overview")
        self.setModal(False)
        self.resize(640, 420)
        
        # Load shared settings file (same as main window/preferences)
        from PyQt6.QtCore import QSettings
        self.settings = QSettings(SETTINGS_PATH, QSettings.Format.IniFormat)
        
        # Create layout
        layout = QVBoxLayout(self)
        
        # Create list widget
        self.task_list = QListView(self)
        self.task_list.setSpacing(4)
        self.task_list.setUniformItemSizes(False)  # allow delegate-controlled height
        self.task_list.setItemDelegate(PinnedTaskItemDelegate(self.task_list))
        self.task_list.setAlternatingRowColors(False)
        self.task_list.setSelectionMode(QListView.SelectionMode.SingleSelection)
        self.task_list.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.task_list.customContextMenuRequested.connect(self.on_task_context_menu)
        layout.addWidget(self.task_list)
        
        # Create checkbox row for filtering options
        from PyQt6.QtWidgets import QCheckBox, QWidget, QHBoxLayout
        checkbox_container = QWidget(self)
        checkbox_layout = QHBoxLayout(checkbox_container)
        checkbox_layout.setContentsMargins(0, 0, 0, 0)
        checkbox_layout.setSpacing(12)

        common_checkbox_style = """
            QCheckBox {
                font-weight: 500;
                color: #202124;
                spacing: 8px;
                padding: 8px;
            }
            QCheckBox::indicator {
                width: 18px;
                height: 18px;
            }
        """

        self.hide_future_checkbox = QCheckBox("Hide tasks with future due date")
        self.hide_future_checkbox.setStyleSheet(common_checkbox_style)
        hide_future = self.settings.value('PinnedTasksOverview/HideFutureDueDates', 'true') in ('true', '1', 'True')
        self.hide_future_checkbox.setChecked(hide_future)
        self.hide_future_checkbox.stateChanged.connect(self.on_hide_future_changed)
        checkbox_layout.addWidget(self.hide_future_checkbox)

        self.hide_unscheduled_checkbox = QCheckBox("Hide unscheduled tasks (no due date)")
        self.hide_unscheduled_checkbox.setStyleSheet(common_checkbox_style)
        hide_unscheduled = self.settings.value('PinnedTasksOverview/HideUnscheduledTasks', 'false') in ('true', '1', 'True')
        self.hide_unscheduled_checkbox.setChecked(hide_unscheduled)
        self.hide_unscheduled_checkbox.stateChanged.connect(self.on_hide_unscheduled_changed)
        checkbox_layout.addWidget(self.hide_unscheduled_checkbox)
        checkbox_layout.addStretch(1)
        layout.addWidget(checkbox_container)
        
        # Create menu bar
        self.menu_bar = self.create_menu_bar()
        layout.setMenuBar(self.menu_bar)
        
        # Load pinned tasks
        self.load_pinned_tasks()
        
        # Connect double-click signal
        self.task_list.doubleClicked.connect(self.on_task_double_clicked)
        
        # Apply theme to match main window
        self.apply_overview_theme()
    
    def create_menu_bar(self):
        menu_bar = self.menuBar() if hasattr(self, 'menuBar') else None
        if menu_bar is None:
            from PyQt6.QtWidgets import QMenuBar
            menu_bar = QMenuBar(self)
        
        export_menu = menu_bar.addMenu("Export")
        export_menu.addAction("Copy to Clipboard", self.copy_to_clipboard)
        export_menu.addAction("Export to Text", self.export_to_text)
        
        # Add Snooze menu (share actions with context menu)
        from PyQt6.QtGui import QAction
        self.act_snooze_today = QAction("Today", self)
        self.act_snooze_today.triggered.connect(self.snooze_today)
        self.act_snooze_later_today = QAction("Later Today", self)
        self.act_snooze_later_today.triggered.connect(self.snooze_later_today)
        self.act_snooze_tomorrow = QAction("Tomorrow", self)
        self.act_snooze_tomorrow.triggered.connect(self.snooze_tomorrow)
        self.act_snooze_next_week = QAction("Next Week", self)
        self.act_snooze_next_week.triggered.connect(self.snooze_next_week)
        self.act_snooze_weekend = QAction("This Weekend", self)
        self.act_snooze_weekend.triggered.connect(self.snooze_weekend)
        self.act_snooze_clear = QAction("Clear Snooze", self)
        self.act_snooze_clear.triggered.connect(self.snooze_clear)

        snooze_menu = menu_bar.addMenu("Snooze")
        self._prepare_snooze_actions()
        self._populate_snooze_menu(snooze_menu)
        snooze_menu.aboutToShow.connect(lambda m=snooze_menu: self._populate_snooze_menu(m))
        
        return menu_bar
    
    def _prepare_snooze_actions(self):
        self.snooze_prefs = load_snooze_preferences(self.settings)
        later_hours = self.snooze_prefs.get('later_today_hours', 2)
        self.act_snooze_later_today.setText(format_later_today_label(later_hours))
        self.act_snooze_weekend.setText(get_weekend_option_label())
    
    def _get_enabled_snooze_actions(self) -> List[QAction]:
        prefs = getattr(self, 'snooze_prefs', load_snooze_preferences(self.settings))
        action_map = {
            'today': self.act_snooze_today,
            'later_today': self.act_snooze_later_today,
            'tomorrow': self.act_snooze_tomorrow,
            'next_week': self.act_snooze_next_week,
            'weekend': self.act_snooze_weekend
        }
        enabled_map = {
            'today': prefs.get('today_enabled', True),
            'later_today': prefs.get('later_today_enabled', True),
            'tomorrow': prefs.get('tomorrow_enabled', True),
            'next_week': prefs.get('next_week_enabled', True),
            'weekend': prefs.get('weekend_enabled', False)
        }
        actions = []
        for key in SNOOZE_OPTION_ORDER:
            if enabled_map.get(key) and action_map.get(key):
                actions.append(action_map[key])
        return actions
    
    def _populate_snooze_menu(self, menu: QMenu):
        self._prepare_snooze_actions()
        menu.clear()
        actions = self._get_enabled_snooze_actions()
        for action in actions:
            menu.addAction(action)
        if actions:
            menu.addSeparator()
        menu.addAction(self.act_snooze_clear)
    
    def load_pinned_tasks(self):
        """Load all pinned tasks from database (excluding completed tasks)"""
        try:
            # Query for all pinned tasks with project info and due dates, excluding completed tasks
            sql = """
                SELECT t.id as task_id, t.title as task_title, t.project_id,
                       p.title as project_title, t.due_date, t.recurring
                FROM tasks t 
                JOIN projects p ON p.id = t.project_id 
                WHERE t.pinned = 1 AND t.done = 0
                ORDER BY p.title COLLATE NOCASE, t.title COLLATE NOCASE
            """
            
            all_tasks = [dict(row) for row in self.db.conn.execute(sql)]
            
            filtered_tasks = list(all_tasks)

            if self.hide_future_checkbox.isChecked():
                filtered_tasks = self._filter_future_due_tasks(filtered_tasks)

            if self.hide_unscheduled_checkbox.isChecked():
                filtered_tasks = [task for task in filtered_tasks if task.get('due_date')]

            self.pinned_tasks = filtered_tasks

            self._sort_pinned_tasks()
            
            self.update_task_list()
            
        except Exception as e:
            print(f"Error loading pinned tasks: {e}")
            self.pinned_tasks = []
    
    def on_hide_future_changed(self, state):
        """Handle checkbox state change"""
        # Save the setting
        self.settings.setValue('PinnedTasksOverview/HideFutureDueDates', 'true' if state else 'false')
        self.settings.sync()
        
        # Reload tasks with new filter
        self.load_pinned_tasks()
    
    def on_hide_unscheduled_changed(self, state):
        """Handle hide-unscheduled checkbox state change."""
        self.settings.setValue('PinnedTasksOverview/HideUnscheduledTasks', 'true' if state else 'false')
        self.settings.sync()
        self.load_pinned_tasks()
    
    def update_task_list(self):
        """Update the task list display"""
        from PyQt6.QtCore import QDateTime, QDate
        from PyQt6.QtGui import QStandardItemModel, QStandardItem
        
        model = QStandardItemModel(self.task_list)
        now = QDateTime.currentDateTime()
        today = QDate.currentDate()
        color_enabled = self._is_due_date_coloring_enabled()
        
        for task in self.pinned_tasks:
            recurring_prefix = "[R] " if bool(task.get('recurring')) else ""
            base_text = f"{task['project_title']} - {recurring_prefix}{task['task_title']}"
            
            due_date_str = task['due_date'] if 'due_date' in task.keys() else None
            if due_date_str:
                relative_label = self._get_relative_due_date_label(due_date_str, now, today)
                if relative_label:
                    base_text += f" ({relative_label})"
            
            item = QStandardItem(base_text)
            item.setEditable(False)
            
            if color_enabled and due_date_str:
                color = self._get_due_date_color(due_date_str)
                if color:
                    item.setData(color, Qt.ItemDataRole.ForegroundRole)
            
            model.appendRow(item)
        
        self.task_list.setModel(model)

    def on_task_context_menu(self, pos):
        """Show snooze options when right-clicking a task."""
        index = self.task_list.indexAt(pos)
        if not index.isValid():
            return

        # Ensure the clicked item becomes the active selection
        self.task_list.setCurrentIndex(index)

        from PyQt6.QtWidgets import QMenu

        menu = QMenu(self)
        self._prepare_snooze_actions()
        actions = self._get_enabled_snooze_actions()
        for action in actions:
            menu.addAction(action)
        if actions:
            menu.addSeparator()
        menu.addAction(self.act_snooze_clear)
        menu.exec(self.task_list.viewport().mapToGlobal(pos))
    
    def _filter_future_due_tasks(self, tasks: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """Return only tasks whose due dates are not in the future."""
        from PyQt6.QtCore import QDateTime, QDate

        now = QDateTime.currentDateTime()
        today = QDate.currentDate()
        filtered = []

        for task in tasks:
            due_date_str = task.get('due_date')
            if not due_date_str:
                filtered.append(task)
                continue

            try:
                due_datetime = QDateTime.fromString(due_date_str, "yyyy-MM-dd HH:mm:ss")
                if due_datetime.isValid():
                    if due_datetime <= now:
                        filtered.append(task)
                    continue

                date_part = due_date_str.split()[0] if ' ' in due_date_str else due_date_str
                year, month, day = map(int, date_part.split('-'))
                due_date = QDate(year, month, day)
                if due_date <= today:
                    filtered.append(task)
            except (ValueError, AttributeError):
                filtered.append(task)

        return filtered
    
    def _get_relative_due_date_label(self, due_date_str: str, now, today) -> str:
        """
        Get relative due date label for a task.
        
        Args:
            due_date_str: Due date string from database
            now: Current QDateTime
            today: Current QDate
        
        Returns:
            String label like "overdue", "later today", "today", "tomorrow", "this week", 
            "next week", "more than a week" or empty string if no label should be shown
        """
        from PyQt6.QtCore import QDateTime, QDate
        
        try:
            # Try parsing as datetime first (with time component)
            due_datetime = QDateTime.fromString(due_date_str, "yyyy-MM-dd HH:mm:ss")
            
            if due_datetime.isValid():
                # Has time component
                due_date = due_datetime.date()
                
                # Check if today
                if due_date == today:
                    # If due today with future time
                    if due_datetime > now:
                        return "later today"
                    else:
                        # Due today, time has passed or is now - still "today", never "overdue"
                        return "today"
                
                # Check if overdue (only for dates in the past, not today)
                if due_date < today:
                    return "overdue"
                
            else:
                # Try parsing as date only (backward compatibility)
                date_part = due_date_str.split()[0] if ' ' in due_date_str else due_date_str
                year, month, day = map(int, date_part.split('-'))
                due_date = QDate(year, month, day)
                
                # Check if today
                if due_date == today:
                    return "today"
                
                # Check if overdue (only for dates in the past)
                if due_date < today:
                    return "overdue"
            
            # Check if tomorrow
            tomorrow = today.addDays(1)
            if due_date == tomorrow:
                return "tomorrow"
            
            # Check weekend-specific labels (week starts Monday=1)
            current_day = today.dayOfWeek()  # Monday is 1, Sunday is 7
            if 1 <= current_day <= 5:  # Mon–Fri
                # Upcoming weekend (this weekend)
                this_saturday = today.addDays(6 - current_day)
                this_sunday = this_saturday.addDays(1)
                if due_date == this_saturday or due_date == this_sunday:
                    return "this weekend"
            else:  # Sat or Sun
                # Next weekend (skip current weekend)
                days_to_next_saturday = 13 - current_day  # 7 if Sat, 6 if Sun
                next_saturday = today.addDays(days_to_next_saturday)
                next_sunday = next_saturday.addDays(1)
                if due_date == next_saturday or due_date == next_sunday:
                    return "next weekend"
            
            # Check if this week (after tomorrow until this Sunday)
            days_until_sunday = 7 - current_day  # Days remaining until this Sunday
            this_sunday = today.addDays(days_until_sunday)
            
            # If there are days between tomorrow and this Sunday
            if due_date > tomorrow and due_date <= this_sunday:
                return "this week"
            
            # Check if next week (next Monday through next Sunday)
            if current_day == 1:  # If today is Monday
                days_to_next_monday = 7
            else:
                days_to_next_monday = 8 - current_day
            
            next_monday = today.addDays(days_to_next_monday)
            next_sunday = next_monday.addDays(6)  # End of next week
            
            if next_monday <= due_date <= next_sunday:
                return "next week"
            
            # Check if more than a week (beyond next Sunday)
            if due_date > next_sunday:
                return "more than a week"
            
            # Shouldn't reach here, but return empty string just in case
            return ""
            
        except (ValueError, AttributeError) as e:
            # If parsing fails, return empty string
            return ""

    def _is_due_date_coloring_enabled(self) -> bool:
        """Return preference flag for due-date coloring (default: enabled)."""
        try:
            self.settings.sync()
        except Exception:
            pass
        value = self.settings.value('Preferences/DueDatesColorEnabled', 'true')
        if isinstance(value, str):
            return value.lower() in ('true', '1', 'yes')
        if isinstance(value, (bool, int)):
            return bool(value)
        return True

    def _parse_due_datetime(self, due_date_str: Optional[str]) -> Optional[datetime.datetime]:
        """Parse a due date string into a datetime if possible."""
        if not due_date_str:
            return None
        try:
            return datetime.datetime.strptime(due_date_str, "%Y-%m-%d %H:%M:%S")
        except ValueError:
            try:
                date_part = due_date_str.split()[0]
                return datetime.datetime.strptime(date_part, "%Y-%m-%d")
            except (ValueError, IndexError):
                return None

    def _sort_pinned_tasks(self):
        """Sort pinned tasks: earliest due dates first, then alphabetical."""
        if not getattr(self, 'pinned_tasks', None):
            return

        def sort_key(task):
            if 'task_title' in task.keys():
                title = task['task_title'] or ""
            elif 'title' in task.keys():
                title = task['title'] or ""
            else:
                title = ""

            project = task['project_title'] if 'project_title' in task.keys() else ""
            project = project or ""
            due_str = task['due_date'] if 'due_date' in task.keys() else None
            if due_str:
                due_dt = self._parse_due_datetime(due_str)
                due_value = due_dt if due_dt else datetime.datetime.max
                due_group = 0
            else:
                due_value = datetime.datetime.max
                due_group = 1
            return (
                due_group,
                due_value,
                project.lower(),
                title.lower()
            )

        self.pinned_tasks.sort(key=sort_key)

    def _get_due_date_color(self, due_date_str: Optional[str]):
        """Mirror main window due-date coloring if enabled."""
        if not due_date_str:
            return None
        try:
            date_part = due_date_str.split()[0] if ' ' in due_date_str else due_date_str
            year, month, day = map(int, date_part.split('-'))
            due_date = QDate(year, month, day)
            today = QDate.currentDate()
            days_until_due = today.daysTo(due_date)
            if days_until_due < 0:
                return QColor(220, 38, 38)
            if days_until_due <= 3:
                return QColor(234, 88, 12)
            return QColor(37, 99, 235)
        except (ValueError, AttributeError):
            return None
    
    def on_task_double_clicked(self, index):
        """Handle double-click on task"""
        if not index.isValid() or index.row() >= len(self.pinned_tasks):
            return
        
        task = self.pinned_tasks[index.row()]
        project_id = task['project_id']
        task_id = task['task_id']
        
        # Emit signal to navigate to task
        self.task_selected.emit(project_id, task_id)
        
        # Close overview window
        self.close()
    
    def copy_to_clipboard(self):
        """Copy pinned tasks to clipboard"""
        if not self.pinned_tasks:
            QMessageBox.information(self, "Copy to Clipboard", "No pinned tasks to copy.")
            return
        
        # Format tasks for clipboard
        clipboard_text = "Pinned Tasks Overview\n"
        clipboard_text += "=====================\n\n"
        
        last_project = None
        for task in self.pinned_tasks:
            # Add extra newline between different projects
            if last_project is not None and last_project != task['project_title']:
                clipboard_text += "\n"
            
            # Format task with due date if available
            recurring_prefix = "[R] " if bool(task.get('recurring')) else ""
            task_line = f"{task['project_title']} - {recurring_prefix}{task['task_title']}"
            due_date_str = task['due_date'] if 'due_date' in task.keys() else None
            if due_date_str:
                try:
                    date_part = due_date_str.split()[0] if ' ' in due_date_str else due_date_str
                    task_line += f" [📅 {date_part}]"
                except Exception:
                    pass
            clipboard_text += task_line + "\n"
            last_project = task['project_title']
        
        # Add total with note if future tasks are hidden
        total_text = f"Total pinned tasks: {len(self.pinned_tasks)}"
        if self.hide_future_checkbox.isChecked():
            total_text += " (future pinned tasks are hidden)"
        clipboard_text += f"\n{total_text}\n"
        clipboard_text += f"Copied on: {datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n"
        
        # Copy to clipboard
        try:
            clipboard = QApplication.clipboard()
            clipboard.setText(clipboard_text)
            QMessageBox.information(self, "Copy to Clipboard", f"Copied {len(self.pinned_tasks)} pinned tasks to clipboard.")
        except Exception as e:
            QMessageBox.critical(self, "Copy Error", f"Failed to copy to clipboard:\n{str(e)}")
    
    def export_to_text(self):
        """Export pinned tasks to text file"""
        if not self.pinned_tasks:
            QMessageBox.information(self, "Export", "No pinned tasks to export.")
            return
        
        # Get downloads folder path
        try:
            import os
            downloads_path = os.path.join(os.path.expanduser("~"), "Downloads")
            if not os.path.exists(downloads_path):
                downloads_path = os.path.expanduser("~")  # Fallback to home directory
        except Exception:
            downloads_path = os.path.expanduser("~")
        
        # Show save dialog
        file_path, _ = QFileDialog.getSaveFileName(
            self,
            "Export Pinned Tasks",
            os.path.join(downloads_path, "pinned_tasks.txt"),
            "Text files (*.txt);;All files (*.*)"
        )
        
        if not file_path:
            return
        
        try:
            # Write tasks to file
            with open(file_path, 'w', encoding='utf-8') as f:
                f.write("Pinned Tasks Overview\n")
                f.write("=====================\n\n")
                
                last_project = None
                for task in self.pinned_tasks:
                    # Add extra newline between different projects
                    if last_project is not None and last_project != task['project_title']:
                        f.write("\n")
                    
                    # Format task with due date if available
                    recurring_prefix = "[R] " if bool(task.get('recurring')) else ""
                    task_line = f"{task['project_title']} - {recurring_prefix}{task['task_title']}"
                    due_date_str = task['due_date'] if 'due_date' in task.keys() else None
                    if due_date_str:
                        try:
                            date_part = due_date_str.split()[0] if ' ' in due_date_str else due_date_str
                            task_line += f" [📅 {date_part}]"
                        except Exception:
                            pass
                    f.write(task_line + "\n")
                    last_project = task['project_title']
                
                # Add total with note if future tasks are hidden
                total_text = f"Total pinned tasks: {len(self.pinned_tasks)}"
                if self.hide_future_checkbox.isChecked():
                    total_text += " (future pinned tasks are hidden)"
                f.write(f"\n{total_text}\n")
                f.write(f"Exported on: {datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n")
            
            QMessageBox.information(self, "Export Complete", f"Pinned tasks exported to:\n{file_path}")
            
        except Exception as e:
            QMessageBox.critical(self, "Export Error", f"Failed to export tasks:\n{str(e)}")
    
    def _apply_snooze_to_current_task(self, snooze_type: str):
        index = self.task_list.currentIndex()
        if not index.isValid() or index.row() >= len(self.pinned_tasks):
            QMessageBox.information(self, "Snooze", "Please select a task to snooze.")
            return
        
        task = self.pinned_tasks[index.row()]
        task_id = task['task_id']
        self._prepare_snooze_actions()
        if snooze_type == 'clear':
            date_str = None
        else:
            hours = self.snooze_prefs.get('later_today_hours', 2)
            date_str = compute_snooze_due_datetime(snooze_type, hours)
            if date_str is None:
                QMessageBox.warning(self, "Snooze", "Unable to calculate the requested snooze time.")
                return
        self.db.set_task_due_date(task_id, date_str)
        self.load_pinned_tasks()
    
    def snooze_later_today(self, _checked=False):
        self._apply_snooze_to_current_task('later_today')
    
    def snooze_today(self, _checked=False):
        self._apply_snooze_to_current_task('today')
    
    def snooze_tomorrow(self, _checked=False):
        self._apply_snooze_to_current_task('tomorrow')
    
    def snooze_next_week(self, _checked=False):
        self._apply_snooze_to_current_task('next_week')
    
    def snooze_weekend(self, _checked=False):
        self._apply_snooze_to_current_task('weekend')
    
    def snooze_clear(self, _checked=False):
        self._apply_snooze_to_current_task('clear')
    
    def apply_overview_theme(self):
        """Apply consistent theme with main window (refined for better row height)."""
        accent = '#2564cf'
        base_bg = '#ffffff'
        border_col = '#d0d7de'
        text_col = '#202124'
        sel_bg = '#cfe4ff'
        font_stack = '"Segoe UI", "Helvetica Neue", Arial, sans-serif'
        self.setStyleSheet(f"""
            QDialog {{
                background: {base_bg};
                color: {text_col};
                font: 12px {font_stack};
            }}
            QListView {{
                background: {base_bg};
                border: 1px solid {border_col};
                border-radius: 12px;
                padding: 6px 8px 8px 8px;
                outline: none;
            }}
            /* Delegate handles item painting; keep item background transparent */
            QListView::item {{
                background: transparent;
                margin: 2px 0px;
            }}
            QMenuBar {{
                background: transparent;
                padding: 4px 6px;
                border: none;
                font-weight: 500;
            }}
            QMenuBar::item {{
                background: transparent;
                padding: 6px 14px;
                border-radius: 6px;
                margin: 0 2px;
            }}
            QMenuBar::item:selected {{
                background: #e6eef8;
            }}
            QMenu {{
                background: {base_bg};
                border: 1px solid {border_col};
                border-radius: 10px;
                padding: 6px 0;
            }}
            QMenu::item {{
                padding: 8px 18px 8px 30px;
                border-radius: 6px;
                margin: 2px 6px;
            }}
            QMenu::item:selected {{
                background: {sel_bg};
            }}
        """)

# -------------------------- Move Task Dialog --------------------------

class MoveTaskProjectDelegate(QStyledItemDelegate):
    """Custom delegate for project items in the move task dialog to ensure proper spacing and rendering."""
    
    def __init__(self, parent=None):
        super().__init__(parent)
        self.item_height = 28  # Reduced height for more compact display
        self.padding = 6       # Reduced internal padding
    
    def sizeHint(self, option, index):
        """Provide consistent size hint for all items."""
        return QSize(option.rect.width(), self.item_height)
    
    def paint(self, painter, option, index):
        """Custom paint method to ensure proper item rendering."""
        painter.save()
        
        # Get item data
        text = index.data(Qt.ItemDataRole.DisplayRole) or ""
        rect = option.rect
        
        # Determine colors based on state
        if option.state & QStyle.StateFlag.State_Selected:
            bg_color = QColor('#cfe4ff')
            border_color = QColor('#7eb6f5')
            text_color = QColor('#202124')
        elif option.state & QStyle.StateFlag.State_MouseOver:
            bg_color = QColor('#e6f1fe')
            border_color = QColor('#cfe4ff')
            text_color = QColor('#202124')
        else:
            bg_color = QColor('#ffffff')
            border_color = QColor('transparent')
            text_color = QColor('#202124')
        
        # Draw background with proper margins
        item_rect = rect.adjusted(2, 1, -2, -1)  # Reduced margins for compactness
        
        # Draw background
        painter.setBrush(bg_color)
        painter.setPen(QPen(border_color, 1))
        painter.drawRoundedRect(item_rect, 6, 6)
        
        # Draw text
        text_rect = item_rect.adjusted(self.padding, 0, -self.padding, 0)
        painter.setPen(text_color)
        painter.setFont(option.font)
        
        # Use elided text to handle long project names
        elided_text = option.fontMetrics.elidedText(
            text, 
            Qt.TextElideMode.ElideRight, 
            text_rect.width()
        )
        
        painter.drawText(
            text_rect, 
            Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft, 
            elided_text
        )
        
        painter.restore()

class MoveTaskDialog(QDialog):
    """Dialog for selecting a destination project when moving task(s)."""
    
    def __init__(self, db: DB, current_project_id: int, task_titles: List[str], parent=None):
        super().__init__(parent)
        self.db = db
        self.current_project_id = current_project_id
        self.task_titles = task_titles
        self.selected_project_id = None
        
        # Robust project mapping system
        self.available_projects = []     # All projects except current: [{'id': int, 'title': str}, ...]
        self.filtered_projects = []      # Projects matching current filter: [{'id': int, 'title': str}, ...]
        self.project_id_to_index = {}    # Maps project_id -> list_index in filtered_projects
        self.list_index_to_project_id = {}  # Maps list_index -> project_id for quick lookup
        
        # Create appropriate title based on single or multiple tasks
        if len(task_titles) == 1:
            title_text = f"Move Task: {task_titles[0][:50]}{'...' if len(task_titles[0]) > 50 else ''}"
        else:
            title_text = f"Move {len(task_titles)} Tasks"
        
        self.setWindowTitle(title_text)
        self.setModal(True)
        self.resize(450, 380)  # Reduced height for more compact display
        
        # Create layout
        layout = QVBoxLayout(self)
        layout.setSpacing(12)
        
        # Title label with task count
        if len(task_titles) == 1:
            title_label = QLabel(f"Move '{task_titles[0][:40]}{'...' if len(task_titles[0]) > 40 else ''}' to:")
        else:
            title_label = QLabel(f"Move {len(task_titles)} selected tasks to:")
            
        title_label.setStyleSheet("font-weight: 600; color: #202124; margin-bottom: 4px; font-size: 13px;")
        layout.addWidget(title_label)
        
        # Show task preview for multiple tasks
        if len(task_titles) > 1:
            preview_text = "Tasks to move:\n"
            for i, title in enumerate(task_titles[:5]):  # Show first 5 tasks
                preview_text += f"  • {title[:45]}{'...' if len(title) > 45 else ''}\n"
            if len(task_titles) > 5:
                preview_text += f"  • ... and {len(task_titles) - 5} more tasks"
                
            preview_label = QLabel(preview_text)
            preview_label.setStyleSheet("""
                background: #f8f9fa; 
                border: 1px solid #e0e0e0; 
                border-radius: 6px; 
                padding: 8px; 
                color: #5f6368; 
                font-size: 11px;
                font-family: 'Consolas', 'Monaco', monospace;
                max-height: 80px;
            """)
            preview_label.setWordWrap(True)
            layout.addWidget(preview_label)
        
        # Filter/search box
        self.filter_edit = QLineEdit()
        self.filter_edit.setPlaceholderText("Type to filter projects...")
        self.filter_edit.textChanged.connect(self.filter_projects)
        layout.addWidget(self.filter_edit)
        
        # Project list
        self.project_list = QListView()
        self.project_list.setSpacing(2)  # Reduced spacing for compactness
        self.project_list.setUniformItemSizes(True)  # Ensure consistent item heights
        self.project_list.setAlternatingRowColors(False)  # Remove alternating colors for cleaner look
        self.project_list.doubleClicked.connect(self.accept)
        
        # Set custom delegate for better rendering
        self.project_delegate = MoveTaskProjectDelegate(self.project_list)
        self.project_list.setItemDelegate(self.project_delegate)
        
        layout.addWidget(self.project_list)
        
        # Debug label to show selected project ID (hidden by default)
        self.debug_label = QLabel("")
        self.debug_label.setStyleSheet("color: #999; font-size: 10px; font-family: monospace;")
        layout.addWidget(self.debug_label)
        self.debug_label.hide()  # Hidden by default
        
        # Buttons
        button_layout = QHBoxLayout()
        button_layout.addStretch()
        
        self.cancel_btn = QPushButton("Cancel")
        self.cancel_btn.clicked.connect(self.reject)
        button_layout.addWidget(self.cancel_btn)
        
        move_text = "Move Task" if len(task_titles) == 1 else f"Move {len(task_titles)} Tasks"
        self.ok_btn = QPushButton(move_text)
        self.ok_btn.clicked.connect(self.accept)
        self.ok_btn.setDefault(True)
        self.ok_btn.setEnabled(False)  # Disabled until selection
        button_layout.addWidget(self.ok_btn)
        
        layout.addLayout(button_layout)
        # Load and setup projects
        self.load_projects()
        
        # Apply consistent styling first
        self.apply_dialog_styling()
        
        # Connect selection change AFTER loading projects and applying styles
        if self.project_list.selectionModel():
            self.project_list.selectionModel().selectionChanged.connect(self.on_selection_changed)
        
        # Focus the filter box for immediate typing
        self.filter_edit.setFocus()
    
    def load_projects(self):
        """Load all projects except the current one into the list."""
        try:
            all_projects = self.db.list_projects()
            # Filter out current project
            self.available_projects = [
                proj for proj in all_projects 
                if proj['id'] != self.current_project_id
            ]
            
            # Initialize filtered projects to all available projects
            self.filtered_projects = self.available_projects.copy()
            self.update_project_list()
            
        except Exception as e:
            print(f"Error loading projects: {e}")
            self.available_projects = []
            self.filtered_projects = []
    
    def apply_dialog_styling(self):
        """Apply consistent styling matching the main window theme."""
        style = """
            QDialog {
                background: #ffffff;
                color: #202124;
                font-family: "Segoe UI", "Helvetica Neue", Arial, sans-serif;
            }
            QLabel {
                color: #202124;
                font-size: 13px;
                font-weight: 500;
            }
            QLineEdit {
                background: #ffffff;
                border: 1px solid #d0d7de;
                border-radius: 6px;
                padding: 8px 12px;
                font-size: 13px;
                min-height: 20px;
            }
            QLineEdit:focus {
                border: 1px solid #2564cf;
                outline: none;
            }
            QListView {
                background: #ffffff;
                border: 1px solid #d0d7de;
                border-radius: 8px;
                outline: none;
                font-size: 13px;
                padding: 4px;
            }
            QListView::item {
                background: transparent;
                padding: 6px 8px;
                margin: 1px 2px;
                border-radius: 4px;
                border: 1px solid transparent;
                min-height: 16px;
            }
            QListView::item:hover {
                background: #e6f1fe;
                border: 1px solid #cfe4ff;
            }
            QListView::item:selected {
                background: #cfe4ff;
                color: #202124;
                border: 1px solid #7eb6f5;
            }
            QListView::item:selected:hover {
                background: #b8dcff;
                border: 1px solid #5a9df7;
            }
            QPushButton {
                background: #2564cf;
                color: white;
                border: none;
                border-radius: 6px;
                font-size: 13px;
                font-weight: 500;
                padding: 10px 20px;
                min-width: 90px;
                min-height: 16px;
            }
            QPushButton:hover {
                background: #1e55b1;
            }
            QPushButton:pressed {
                background: #133d7a;
            }
            QPushButton:disabled {
                background: #e0e0e0;
                color: #9e9e9e;
            }
            QPushButton[flat="true"] {
                background: transparent;
                color: #5f6368;
                border: 1px solid #d0d7de;
            }
            QPushButton[flat="true"]:hover {
                background: #f8f9fa;
                border-color: #2564cf;
                color: #2564cf;
            }
        """
    
    def update_project_list(self):
        """Update the project list model with current filtered projects."""
        try:
            from PyQt6.QtCore import QStringListModel
            
            # Create display names (just the project titles)
            project_names = [proj['title'] for proj in self.filtered_projects]
            
            model = QStringListModel(project_names)
            self.project_list.setModel(model)
            
            # Need to reconnect selection model signals after model reset
            selection_model = self.project_list.selectionModel()
            if selection_model:
                try:
                    selection_model.selectionChanged.disconnect()
                except TypeError:
                    pass  # No connections exist yet
                selection_model.selectionChanged.connect(self.on_selection_changed)
            
            # Auto-select first item if available
            if self.filtered_projects:
                first_index = model.index(0, 0)
                self.project_list.setCurrentIndex(first_index)
                # Set the selected project ID to the first filtered project
                self.selected_project_id = self.filtered_projects[0]['id']
                self.ok_btn.setEnabled(True)
                self._update_debug_info()
            else:
                self.selected_project_id = None
                self.ok_btn.setEnabled(False)
                self._update_debug_info()
                
        except Exception as e:
            print(f"Error updating project list: {e}")
            self.selected_project_id = None
            self.ok_btn.setEnabled(False)
    
    def _update_debug_info(self):
        """Update debug information display."""
        if self.selected_project_id is not None:
            # Find the selected project in filtered list
            selected_project = None
            for proj in self.filtered_projects:
                if proj['id'] == self.selected_project_id:
                    selected_project = proj
                    break
            
            if selected_project:
                debug_text = f"Selected: ID={self.selected_project_id}, Title='{selected_project['title']}'"
            else:
                debug_text = f"Selected: ID={self.selected_project_id} (NOT FOUND IN FILTERED LIST!)"
        else:
            debug_text = "Selected: None"
        
        self.debug_label.setText(debug_text)
    
    def on_selection_changed(self):
        """Handle selection changes in the project list."""
        selection_model = self.project_list.selectionModel()
        if selection_model and selection_model.hasSelection():
            selected_indexes = selection_model.selectedIndexes()
            if selected_indexes:
                selected_row = selected_indexes[0].row()
                # CRITICAL FIX: Map the row index to the actual project ID
                if 0 <= selected_row < len(self.filtered_projects):
                    self.selected_project_id = self.filtered_projects[selected_row]['id']
                    self.ok_btn.setEnabled(True)
                    self._update_debug_info()
                    return
        
        # No valid selection
        self.selected_project_id = None
        self.ok_btn.setEnabled(False)
        self._update_debug_info()
    
    def filter_projects(self, filter_text: str):
        """Filter projects based on the search text."""
        filter_text = filter_text.lower().strip()
        
        # Store currently selected project ID to restore after filtering
        previously_selected_id = self.selected_project_id
        
        if not filter_text:
            # Show all projects if no filter
            self.filtered_projects = self.available_projects.copy()
        else:
            # Filter projects that contain the search text
            self.filtered_projects = [
                proj for proj in self.available_projects
                if filter_text in proj['title'].lower()
            ]
        
        # Update the list display
        self.update_project_list()
        
        # Try to restore previous selection if the project is still in filtered results
        if previously_selected_id is not None:
            for i, proj in enumerate(self.filtered_projects):
                if proj['id'] == previously_selected_id:
                    # Select the previously selected project
                    index = self.project_list.model().index(i, 0)
                    self.project_list.setCurrentIndex(index)
                    self.selected_project_id = previously_selected_id
                    self.ok_btn.setEnabled(True)
                    self._update_debug_info()
                    return
        
        # If previous selection couldn't be restored, update_project_list() 
        # already handled selecting the first item
    
class ExpandingLineEditDelegate(QStyledItemDelegate):
    """Delegate giving a comfortably padded full-width line edit for inline editing."""
    
    def __init__(self, parent=None):
        super().__init__(parent)
        self._active_editors = set()
        self._view_ref = None  # Store weak reference to view
    
    def createEditor(self, parent, option, index):
        editor = QLineEdit(parent)
        editor.setMinimumHeight(30)
        editor.setContentsMargins(0, 0, 0, 0)
        editor.setStyleSheet("QLineEdit{padding:4px 6px; border:1px solid #7eb6f5; border-radius:3px; background:#ffffff;} QLineEdit:focus{border:1px solid #2564cf;}")
        
        # Store view reference if we don't have it yet
        if self._view_ref is None:
            view = parent
            while view and not hasattr(view, 'model'):
                view = view.parent()
            if view:
                import weakref
                self._view_ref = weakref.ref(view)
        
        # Track active editors with proper cleanup
        self._active_editors.add(editor)
        
        # Connect destroyed signal with proper cleanup
        def cleanup_editor():
            self._active_editors.discard(editor)
            self._update_defer_flag()
        
        editor.destroyed.connect(cleanup_editor)
        
        # Set defer reload flag on model
        self._update_defer_flag()
        
        return editor

    def destroyEditor(self, editor, index):
        # Clean up editor tracking first
        self._active_editors.discard(editor)
        
        # Update defer flag before calling super()
        self._update_defer_flag()
        
        # Call super() to properly clean up Qt's editor management
        try:
            super().destroyEditor(editor, index)
        except RuntimeError:
            # Handle case where editor was already destroyed
            pass

    def _update_defer_flag(self):
        """Update the defer reload flag based on active editors"""
        try:
            if self._view_ref:
                view = self._view_ref()
                if view and hasattr(view, 'model') and view.model():
                    model = view.model()
                    if hasattr(model, '_defer_reload_flag'):
                        model._defer_reload_flag = len(self._active_editors) > 0
        except Exception:
            # Ignore errors in flag updating
            pass

    def updateEditorGeometry(self, editor, option, index):
        # Provide internal margins so text is not cramped
        r = option.rect.adjusted(2, 2, -2, -2)
        editor.setGeometry(r)

    def sizeHint(self, option, index):
        sz = super().sizeHint(option, index)
        sz.setHeight(max(sz.height(), 34))
        return sz

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
                accent_color = QColor(37, 100, 207)  # #2564cf
                accent_hover = QColor(30, 85, 177)   # #1e55b1
                border_color = QColor(37, 100, 207)
                bg_color = QColor(255, 255, 255) if not checked else accent_color
                hover_bg = QColor(230, 241, 254)  # #e6f1fe
                
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
        title_label.setStyleSheet("font-weight: 600; color: #202124;")
        layout.addWidget(title_label)

        self.title_edit = QLineEdit()
        self.title_edit.setPlaceholderText("Template title")
        self.title_edit.setText(title_text or "")
        layout.addWidget(self.title_edit)

        content_label = QLabel("Template content")
        content_label.setStyleSheet("font-weight: 600; color: #202124; margin-top: 6px;")
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
        heading.setStyleSheet("font-size: 16px; font-weight: 600; color: #202124;")
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
                background: #ffffff;
                border: 1px solid #d8dee4;
                border-radius: 8px;
                padding: 10px 12px;
                color: #202124;
                selection-background-color: #cfe4ff;
            }
            QPlainTextEdit:focus {
                border: 1px solid #7eb6f5;
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
        self.instruction_label.setStyleSheet("color: #6b7280; font-size: 11px;")
        helper_layout.addWidget(self.instruction_label)

        self.draft_status_label = QLabel(
            "Draft text autosaves while you type and stays available until you clear it or complete an import."
        )
        self.draft_status_label.setWordWrap(True)
        self.draft_status_label.setStyleSheet("color: #6b7280; font-size: 11px;")
        helper_layout.addWidget(self.draft_status_label)

        layout.addWidget(helper_block)

        button_row = QHBoxLayout()
        button_row.setContentsMargins(0, 0, 0, 0)
        button_row.setSpacing(8)

        self.clear_button = QPushButton("Clear")
        self.clear_button.setStyleSheet("""
            QPushButton {
                background-color: #fff1f0;
                border: 1px solid #ffccc7;
                border-radius: 4px;
                padding: 6px 14px;
                color: #cf1322;
            }
            QPushButton:hover {
                background-color: #ffe7e5;
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
                background-color: #2da44e;
                border: 1px solid #2da44e;
                border-radius: 4px;
                padding: 6px 16px;
                color: white;
                font-weight: 500;
            }
            QPushButton:hover {
                background-color: #2c974b;
            }
            QPushButton:disabled {
                background-color: #94d3a2;
                border-color: #94d3a2;
                color: #f3f4f6;
            }
        """)
        self.import_button.clicked.connect(self._import_tasks)
        button_row.addWidget(self.import_button)

        self.exit_button = QPushButton("Close")
        self.exit_button.setStyleSheet("""
            QPushButton {
                background-color: #f6f8fa;
                border: 1px solid #d0d7de;
                border-radius: 4px;
                padding: 6px 16px;
                color: #24292f;
            }
            QPushButton:hover {
                background-color: #f3f4f6;
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
            self.project_label.setStyleSheet("color: #374151; font-size: 12px; font-weight: 600;")
        else:
            self.project_label.setText("Import target: No project selected")
            self.project_label.setStyleSheet("color: #b3261e; font-size: 12px; font-weight: 600;")

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
        self.resize(700, 500)
        
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
                background: #f5f7fa;
                border: none;
                border-right: 1px solid #d0d7de;
                outline: none;
            }
            QListView::item {
                padding: 12px 16px;
                border-radius: 6px;
                margin: 4px 8px;
                color: #202124;
            }
            QListView::item:hover {
                background: #e6eef8;
            }
            QListView::item:selected {
                background: #2564cf;
                color: white;
                font-weight: 500;
            }
        """)
        
        # Category model
        from PyQt6.QtCore import QStringListModel
        categories = ["Task Sorting", "Project Visibility", "Planning", "Templates"]
        
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
        
        # Add widgets to main layout
        main_layout.addWidget(self.category_list)
        main_layout.addWidget(self.settings_scroll, 1)
        
        # Button box at bottom
        button_layout = QHBoxLayout()
        button_layout.setContentsMargins(24, 0, 24, 24)
        
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
        
        # Add button box to settings layout
        self.settings_layout.addStretch()
        self.settings_layout.addLayout(button_layout)
        
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
            self.current_settings_widget.deleteLater()
            self.current_settings_widget = None
        
        # Create new settings widget based on category
        if category == "Task Sorting":
            self.current_settings_widget = self._create_items_sort_settings()
        elif category == "Project Visibility":
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
            
            # Save Project Visibility preference if button group exists and is valid
            if hasattr(self, 'visibility_button_group') and self.visibility_button_group is not None:
                try:
                    checked_id = self.visibility_button_group.checkedId()
                    visibility_mode = 'manual_based' if checked_id == 1 else 'task_based'
                    self.settings.setValue('Preferences/ProjectVisibilityMode', visibility_mode)
                    
                    # Refresh the project list in the main window
                    if hasattr(self, 'main_window') and self.main_window:
                        self.main_window.project_model.reload()
                except RuntimeError:
                    # Widget has been deleted, skip
                    pass
            
            # Save Due Dates Color preference if checkbox exists and is valid
            if hasattr(self, 'due_dates_color_checkbox') and self.due_dates_color_checkbox is not None:
                try:
                    enabled = self.due_dates_color_checkbox.isChecked()
                    self.settings.setValue('Preferences/DueDatesColorEnabled', 'true' if enabled else 'false')
                    
                    # Refresh the task list to apply color changes
                    if hasattr(self, 'main_window') and self.main_window:
                        self.main_window.task_model.layoutChanged.emit()
                except RuntimeError:
                    # Widget has been deleted, skip
                    pass

            self._save_planning_settings()
            if hasattr(self, 'main_window') and self.main_window:
                try:
                    self.main_window._refresh_periodic_review_state()
                except Exception:
                    pass
            
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
        try:
            if hasattr(self, 'feature_links_checkbox') and self.feature_links_checkbox is not None:
                enabled = self.feature_links_checkbox.isChecked()
                self.settings.setValue('Preferences/ProjectFeatureLinksEnabled', 'true' if enabled else 'false')
            if hasattr(self, 'periodic_review_checkbox') and self.periodic_review_checkbox is not None:
                periodic_enabled = self.periodic_review_checkbox.isChecked()
                self.settings.setValue('Preferences/PeriodicReviewEnabled', 'true' if periodic_enabled else 'false')
            if hasattr(self, 'periodic_review_auto_checkbox') and self.periodic_review_auto_checkbox is not None:
                periodic_auto_enabled = self.periodic_review_auto_checkbox.isChecked()
                self.settings.setValue('Preferences/PeriodicReviewAutoMark', 'true' if periodic_auto_enabled else 'false')
        except RuntimeError:
            pass

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
    
    def _create_items_sort_settings(self) -> QWidget:
        """Create settings widget for Task Sorting preferences."""
        widget = QWidget()
        layout = QVBoxLayout(widget)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(16)
        
        # Title
        title_label = QLabel("Task Sorting")
        title_label.setStyleSheet("font-size: 18px; font-weight: 600; color: #202124;")
        layout.addWidget(title_label)
        
        # Description
        desc_label = QLabel("Set the default task sorting mode for new projects.")
        desc_label.setStyleSheet("color: #5f6368; margin-bottom: 8px;")
        desc_label.setWordWrap(True)
        layout.addWidget(desc_label)
        
        # Sort mode selection
        sort_group = QWidget()
        sort_layout = QVBoxLayout(sort_group)
        sort_layout.setContentsMargins(0, 0, 0, 0)
        sort_layout.setSpacing(8)
        
        sort_label = QLabel("Default task sort mode for new projects:")
        sort_label.setStyleSheet("font-weight: 500; color: #202124; margin-top: 8px;")
        sort_layout.addWidget(sort_label)
        
        # Dropdown (ComboBox) for sort mode
        self.sort_mode_combo = QComboBox()
        self.sort_mode_combo.addItem("A->Z (pinned first)", "auto")
        self.sort_mode_combo.addItem("Drag to Reorder", "manual")
        self.sort_mode_combo.setItemData(0, "Automatic sorting by priority: MoM tasks, pinned tasks, then alphabetically", Qt.ItemDataRole.ToolTipRole)
        self.sort_mode_combo.setItemData(1, "Manual ordering - drag and drop tasks to arrange them", Qt.ItemDataRole.ToolTipRole)
        
        sort_layout.addWidget(self.sort_mode_combo)
        
        # Apply to all projects button
        apply_all_btn = QPushButton("Apply to All Projects")
        apply_all_btn.setStyleSheet("""
            QPushButton {
                background: #f8f9fa;
                color: #202124;
                border: 1px solid #d0d7de;
                padding: 8px 16px;
                border-radius: 6px;
                font-weight: 500;
                margin-top: 8px;
            }
            QPushButton:hover {
                background: #e8eaed;
                border-color: #2564cf;
            }
            QPushButton:pressed {
                background: #d2d4d7;
            }
        """)
        apply_all_btn.clicked.connect(self._apply_to_all_projects)
        sort_layout.addWidget(apply_all_btn)
        
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
        mode_name = "Drag to Reorder" if manual_sort else "Smart Sort"
        
        # Confirm with user
        reply = QMessageBox.question(
            self,
            "Apply to All Projects",
            f"This will change the sort mode to '{mode_name}' for ALL projects.\n\n"
            f"Are you sure you want to continue?",
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
                    if actual_manual_sort:
                        self.main_window.task_view.setDragDropMode(QTableView.DragDropMode.InternalMove)
                        self.main_window.task_view.setDefaultDropAction(Qt.DropAction.MoveAction)
                    else:
                        self.main_window.task_view.setDragDropMode(QTableView.DragDropMode.NoDragDrop)
                    
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
                "Success",
                f"Sort mode changed to '{mode_name}' for all projects.",
                QMessageBox.StandardButton.Ok
            )
    
    def _create_project_visibility_settings(self) -> QWidget:
        """Create settings widget for Project Visibility preferences."""
        widget = QWidget()
        layout = QVBoxLayout(widget)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(16)
        
        # Title
        title_label = QLabel("Project Visibility")
        title_label.setStyleSheet("font-size: 18px; font-weight: 600; color: #202124;")
        layout.addWidget(title_label)
        
        # Description
        desc_label = QLabel("Choose how projects are filtered when 'Show Completed' is disabled.")
        desc_label.setStyleSheet("color: #5f6368; margin-bottom: 8px;")
        desc_label.setWordWrap(True)
        layout.addWidget(desc_label)
        
        # Visibility mode selection
        visibility_group = QWidget()
        visibility_layout = QVBoxLayout(visibility_group)
        visibility_layout.setContentsMargins(0, 0, 0, 0)
        visibility_layout.setSpacing(12)
        
        mode_label = QLabel("Project visibility behavior:")
        mode_label.setStyleSheet("font-weight: 500; color: #202124; margin-top: 8px;")
        visibility_layout.addWidget(mode_label)
        
        # Radio buttons for visibility modes
        from PyQt6.QtWidgets import QRadioButton, QButtonGroup
        
        self.visibility_button_group = QButtonGroup(widget)
        
        # Option 1: Task Completion Based (current implementation)
        self.visibility_task_based = QRadioButton("Task Completion Based")
        self.visibility_task_based.setStyleSheet("""
            QRadioButton {
                font-weight: 500;
                color: #202124;
                spacing: 8px;
            }
            QRadioButton::indicator {
                width: 18px;
                height: 18px;
            }
        """)
        self.visibility_button_group.addButton(self.visibility_task_based, 0)
        visibility_layout.addWidget(self.visibility_task_based)
        
        # Description for Option 1
        task_based_desc = QLabel(
            "When 'Show Completed' is disabled:\n"
            "  • Projects where ALL tasks are completed → Hidden\n"
            "  • Projects with at least one incomplete task → Shown\n"
            "  • Empty projects (no tasks) → Shown\n"
            "\n"
            "When 'Show Completed' is enabled:\n"
            "  • All projects → Shown"
        )
        task_based_desc.setStyleSheet("""
            color: #5f6368;
            font-size: 12px;
            margin-left: 28px;
            margin-bottom: 12px;
        """)
        task_based_desc.setWordWrap(True)
        visibility_layout.addWidget(task_based_desc)
        
        # Option 2: Manual Visibility Control
        self.visibility_manual_based = QRadioButton("Manual Visibility Control")
        self.visibility_manual_based.setStyleSheet("""
            QRadioButton {
                font-weight: 500;
                color: #202124;
                spacing: 8px;
            }
            QRadioButton::indicator {
                width: 18px;
                height: 18px;
            }
        """)
        self.visibility_button_group.addButton(self.visibility_manual_based, 1)
        visibility_layout.addWidget(self.visibility_manual_based)
        
        # Description for Option 2
        manual_based_desc = QLabel(
            "When 'Show Completed' is disabled:\n"
            "  • Projects you marked as hidden → Hidden\n"
            "  • All other projects → Shown\n"
            "\n"
            "When 'Show Completed' is enabled:\n"
            "  • All projects → Shown"
        )
        manual_based_desc.setStyleSheet("""
            color: #5f6368;
            font-size: 12px;
            margin-left: 28px;
            margin-bottom: 12px;
        """)
        manual_based_desc.setWordWrap(True)
        visibility_layout.addWidget(manual_based_desc)
        
        layout.addWidget(visibility_group)
        
        # Load current setting
        visibility_mode = self.settings.value('Preferences/ProjectVisibilityMode', 'task_based')
        if visibility_mode == 'manual_based':
            self.visibility_manual_based.setChecked(True)
        else:
            self.visibility_task_based.setChecked(True)
        
        layout.addStretch()
        
        return widget
    
    def _create_due_dates_settings(self) -> QWidget:
        """Create settings widget for Due Dates preferences."""
        widget = QWidget()
        layout = QVBoxLayout(widget)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(16)
        
        # Title
        title_label = QLabel("Planning")
        title_label.setStyleSheet("font-size: 18px; font-weight: 600; color: #202124;")
        layout.addWidget(title_label)
        
        # Description
        desc_label = QLabel("Configure how tasks with due dates are displayed in the task list and other settings related to planning.")
        desc_label.setStyleSheet("color: #5f6368; margin-bottom: 8px;")
        desc_label.setWordWrap(True)
        layout.addWidget(desc_label)

        from PyQt6.QtWidgets import QCheckBox

        self.feature_links_checkbox = QCheckBox("Enable feature links for Projects")
        self.feature_links_checkbox.setStyleSheet("""
            QCheckBox {
                font-weight: 500;
                color: #202124;
                spacing: 8px;
            }
            QCheckBox::indicator {
                width: 18px;
                height: 18px;
            }
        """)
        layout.addWidget(self.feature_links_checkbox)

        feature_links_desc = QLabel("Show 'Visit feature link' and 'Edit links' in the Projects context menu.")
        feature_links_desc.setStyleSheet("color: #5f6368; font-size: 12px; margin-left: 28px; margin-bottom: 8px;")
        feature_links_desc.setWordWrap(True)
        layout.addWidget(feature_links_desc)

        self.periodic_review_checkbox = QCheckBox("Enable periodic projects review")
        self.periodic_review_checkbox.setStyleSheet("""
            QCheckBox {
                font-weight: 500;
                color: #202124;
                spacing: 8px;
            }
            QCheckBox::indicator {
                width: 18px;
                height: 18px;
            }
        """)
        layout.addWidget(self.periodic_review_checkbox)

        periodic_review_desc = QLabel("Allow starting a periodic review from Tools and mark projects as reviewed from their context menu.")
        periodic_review_desc.setStyleSheet("color: #5f6368; font-size: 12px; margin-left: 28px; margin-bottom: 8px;")
        periodic_review_desc.setWordWrap(True)
        layout.addWidget(periodic_review_desc)

        self.periodic_review_auto_checkbox = QCheckBox("Automatically mark projects as reviewed")
        self.periodic_review_auto_checkbox.setStyleSheet("""
            QCheckBox {
                font-weight: 500;
                color: #202124;
                spacing: 8px;
            }
            QCheckBox::indicator {
                width: 18px;
                height: 18px;
            }
        """)
        layout.addWidget(self.periodic_review_auto_checkbox)

        periodic_review_auto_desc = QLabel(
            "When a periodic review is active, projects are marked as reviewed as soon as you open them; "
            "otherwise mark them manually from the context menu."
        )
        periodic_review_auto_desc.setStyleSheet("color: #5f6368; font-size: 12px; margin-left: 28px; margin-bottom: 8px;")
        periodic_review_auto_desc.setWordWrap(True)
        layout.addWidget(periodic_review_auto_desc)
        self.periodic_review_checkbox.toggled.connect(self.periodic_review_auto_checkbox.setEnabled)
        
        # Color indication checkbox
        color_group = QWidget()
        color_layout = QVBoxLayout(color_group)
        color_layout.setContentsMargins(0, 0, 0, 0)
        color_layout.setSpacing(12)

        self.due_dates_color_checkbox = QCheckBox("Enable color indication for due dates")
        self.due_dates_color_checkbox.setStyleSheet("""
            QCheckBox {
                font-weight: 500;
                color: #202124;
                spacing: 8px;
            }
            QCheckBox::indicator {
                width: 18px;
                height: 18px;
            }
        """)
        color_layout.addWidget(self.due_dates_color_checkbox)
        
        # Description for color indication
        color_desc = QLabel(
            "When enabled, tasks with due dates will be color-coded:\n"
            "  • Red text → Overdue tasks\n"
            "  • Orange text → Tasks due within 3 days\n"
            "  • Blue text → Tasks with future due dates\n"
            "\n"
            "When disabled, all tasks use the default text color."
        )
        color_desc.setStyleSheet("""
            color: #5f6368;
            font-size: 12px;
            margin-left: 28px;
            margin-bottom: 12px;
        """)
        color_desc.setWordWrap(True)
        color_layout.addWidget(color_desc)
        
        layout.addWidget(color_group)
        
        # Snooze options group
        snooze_group = QGroupBox("Snooze options")
        snooze_group.setStyleSheet("QGroupBox { font-weight: 600; color: #202124; }")
        snooze_layout = QVBoxLayout(snooze_group)
        snooze_layout.setContentsMargins(12, 10, 12, 12)
        snooze_layout.setSpacing(10)

        snooze_desc = QLabel("Choose which snooze shortcuts appear in task menus and set the default delay for 'Later Today'.")
        snooze_desc.setStyleSheet("color: #5f6368; font-size: 12px;")
        snooze_desc.setWordWrap(True)
        snooze_layout.addWidget(snooze_desc)

        checkbox_style = """
            QCheckBox {
                font-weight: 500;
                color: #202124;
                spacing: 6px;
            }
            QCheckBox::indicator {
                width: 18px;
                height: 18px;
            }
        """

        row_container = QWidget()
        row_layout = QVBoxLayout(row_container)
        row_layout.setContentsMargins(0, 0, 0, 0)
        row_layout.setSpacing(6)

        self.snooze_today_checkbox = QCheckBox("Today")
        self.snooze_today_checkbox.setStyleSheet(checkbox_style)
        row_layout.addWidget(self.snooze_today_checkbox)

        later_today_row = QWidget()
        later_today_layout = QHBoxLayout(later_today_row)
        later_today_layout.setContentsMargins(0, 0, 0, 0)
        later_today_layout.setSpacing(8)
        self.snooze_later_today_checkbox = QCheckBox("Later Today")
        self.snooze_later_today_checkbox.setStyleSheet(checkbox_style)
        later_today_layout.addWidget(self.snooze_later_today_checkbox, 1)
        later_today_layout.addStretch()
        hours_label = QLabel("Delay (hours):")
        hours_label.setStyleSheet("color: #5f6368; font-size: 12px;")
        later_today_layout.addWidget(hours_label)
        self.snooze_later_today_hours = QSpinBox()
        self.snooze_later_today_hours.setRange(1, 12)
        self.snooze_later_today_hours.setSuffix("h")
        self.snooze_later_today_hours.setFixedWidth(70)
        later_today_layout.addWidget(self.snooze_later_today_hours)
        row_layout.addWidget(later_today_row)

        self.snooze_tomorrow_checkbox = QCheckBox("Tomorrow")
        self.snooze_tomorrow_checkbox.setStyleSheet(checkbox_style)
        row_layout.addWidget(self.snooze_tomorrow_checkbox)

        self.snooze_next_week_checkbox = QCheckBox("Next Week")
        self.snooze_next_week_checkbox.setStyleSheet(checkbox_style)
        row_layout.addWidget(self.snooze_next_week_checkbox)

        self.snooze_weekend_checkbox = QCheckBox("This/Next Weekend")
        self.snooze_weekend_checkbox.setStyleSheet(checkbox_style)
        row_layout.addWidget(self.snooze_weekend_checkbox)

        weekend_hint = QLabel("Shows 'This Weekend' Monday–Friday and 'Next Weekend' on Saturday/Sunday, aligning with upcoming Saturday.")
        weekend_hint.setStyleSheet("color: #5f6368; font-size: 12px; margin-left: 24px;")
        weekend_hint.setWordWrap(True)
        row_layout.addWidget(weekend_hint)

        snooze_layout.addWidget(row_container)
        layout.addWidget(snooze_group)
        
        # Load current setting (default: enabled)
        feature_links_enabled = self.settings.value('Preferences/ProjectFeatureLinksEnabled', 'false') in ('true', '1', 'True')
        self.feature_links_checkbox.setChecked(feature_links_enabled)
        periodic_review_enabled = self.settings.value('Preferences/PeriodicReviewEnabled', 'false') in ('true', '1', 'True')
        self.periodic_review_checkbox.setChecked(periodic_review_enabled)
        periodic_review_auto_enabled = self.settings.value('Preferences/PeriodicReviewAutoMark', 'false') in ('true', '1', 'True')
        self.periodic_review_auto_checkbox.setChecked(periodic_review_auto_enabled)
        self.periodic_review_auto_checkbox.setEnabled(periodic_review_enabled)
        color_enabled = self.settings.value('Preferences/DueDatesColorEnabled', 'true') in ('true', '1', 'True')
        self.due_dates_color_checkbox.setChecked(color_enabled)
        
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
        widget = QWidget()
        layout = QVBoxLayout(widget)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(14)

        title_label = QLabel("Templates")
        title_label.setStyleSheet("font-size: 18px; font-weight: 600; color: #202124;")
        layout.addWidget(title_label)

        desc_label = QLabel(
            "Create reusable rich-text templates and insert them from the Notes context menu."
        )
        desc_label.setStyleSheet("color: #5f6368; margin-bottom: 8px;")
        desc_label.setWordWrap(True)
        layout.addWidget(desc_label)

        from PyQt6.QtCore import QStringListModel
        self.templates_data: List[Dict[str, Any]] = []
        self.templates_list_model = QStringListModel([])
        self.templates_list = QListView()
        self.templates_list.setModel(self.templates_list_model)
        self.templates_list.setSelectionMode(QListView.SelectionMode.SingleSelection)
        self.templates_list.setEditTriggers(QTableView.EditTrigger.NoEditTriggers)
        self.templates_list.setSpacing(2)
        self.templates_list.doubleClicked.connect(lambda _index: self._edit_selected_template())
        layout.addWidget(self.templates_list, 1)

        button_row = QHBoxLayout()
        button_row.setContentsMargins(0, 0, 0, 0)
        button_row.setSpacing(8)

        self.template_add_button = QPushButton("Add")
        self.template_edit_button = QPushButton("Edit")
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
            info_label = QLabel("Templates are unavailable because no database is currently open.")
            info_label.setWordWrap(True)
            info_label.setStyleSheet("color: #b3261e;")
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
            f"{item['title'] or 'Untitled template'}{' (Default)' if item.get('is_default') else ''}"
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
                "A template with this title already exists. Please choose a different title."
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
            "Delete Template",
            f"Delete template '{template['title']}'?",
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
            
            # Save Project Visibility preference
            if hasattr(self, 'visibility_button_group') and self.visibility_button_group is not None:
                try:
                    checked_id = self.visibility_button_group.checkedId()
                    visibility_mode = 'manual_based' if checked_id == 1 else 'task_based'
                    self.settings.setValue('Preferences/ProjectVisibilityMode', visibility_mode)
                    
                    # Refresh the project list in the main window
                    if hasattr(self, 'main_window') and self.main_window:
                        self.main_window.project_model.reload()
                except RuntimeError:
                    # Widget has been deleted, skip
                    pass
            
            # Save Due Dates Color preference
            if hasattr(self, 'due_dates_color_checkbox') and self.due_dates_color_checkbox is not None:
                try:
                    enabled = self.due_dates_color_checkbox.isChecked()
                    self.settings.setValue('Preferences/DueDatesColorEnabled', 'true' if enabled else 'false')
                    
                    # Refresh the task list to apply color changes
                    if hasattr(self, 'main_window') and self.main_window:
                        self.main_window.task_model.layoutChanged.emit()
                except RuntimeError:
                    # Widget has been deleted, skip
                    pass

            self._save_planning_settings()
            if hasattr(self, 'main_window') and self.main_window:
                try:
                    self.main_window._refresh_periodic_review_state()
                except Exception:
                    pass
            
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
        """Apply consistent theme styling to the dialog."""
        self.setStyleSheet("""
            QDialog {
                background: white;
            }
            QPushButton {
                background: #2564cf;
                color: white;
                border: 1px solid #2564cf;
                padding: 8px 16px;
                border-radius: 6px;
                font-weight: 500;
                min-width: 80px;
            }
            QPushButton:hover {
                background: #1e55b1;
                border-color: #1e55b1;
            }
            QPushButton:pressed {
                background: #133d7a;
                border-color: #133d7a;
            }
            QPushButton:default {
                background: #2564cf;
                border: 2px solid #2564cf;
            }
            QLabel {
                color: #202124;
            }
        """)

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
        splitter.setSizes([250, 420, 530])
        self.splitter = splitter  # store reference
        self.setCentralWidget(splitter)
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

        # Restore pinned-only preference
        pinned_only_saved = self._settings.value('MainWindow/PinnedOnly', 'false') in ('true','1','True')
        self.pinned_only = pinned_only_saved

        # Models/Views
        self.project_model = ProjectListModel(self.db, self._settings)
        # Apply pinned-only filter to project model if setting is enabled
        if self.pinned_only:
            self.project_model.set_pinned_only(self.pinned_only)
        self.project_view = QListView(); self.project_view.setModel(self.project_model)
        self.project_view.setUniformItemSizes(True)  # uniform for performance
        self.project_view.setEditTriggers(QListView.EditTrigger.DoubleClicked | QListView.EditTrigger.EditKeyPressed)
        # Keep project highlight even when focus moves elsewhere
        self.project_view.setStyleSheet(
            "QListView::item:selected { background: palette(highlight); color: palette(highlighted-text); }"
            "QListView::item:selected:!active { background: palette(highlight); color: palette(highlighted-text); }"
            "QScrollBar:vertical { background: transparent; width: 12px; margin: 0; }"
            "QScrollBar::handle:vertical { background: #d0d7de; min-height: 30px; border-radius: 3px; }"
            "QScrollBar::handle:vertical:hover { background: #b7c3cc; }"
            "QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height: 0; }"
        )
        self.project_view.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.project_view.customContextMenuRequested.connect(self.show_project_context_menu)

        self.projects_header = QWidget()
        self.projects_header_layout = QHBoxLayout(self.projects_header)
        self.projects_header_layout.setContentsMargins(0, 0, 0, 0)
        self.projects_header_layout.setSpacing(8)
        self.projects_header_layout.setAlignment(Qt.AlignmentFlag.AlignVCenter)

        self.projects_label = QLabel("Projects")
        self.projects_header_layout.addWidget(self.projects_label)
        self.projects_header_layout.addStretch()

        self.projects_header.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.projects_header.customContextMenuRequested.connect(self.show_projects_header_context_menu)
        self.projects_label.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.projects_label.customContextMenuRequested.connect(
            lambda pos: self.show_projects_header_context_menu(self.projects_label.mapTo(self.projects_header, pos))
        )

        self.task_model = TaskTableModel(self.db)
        self.task_model.settings = self._settings  # Pass settings reference for preferences
        self.task_view = QTableView(); self.task_view.setModel(self.task_model)
        # Removed invalid setUniformRowHeights (not available on QTableView)
        # self.task_view.setUniformRowHeights(True)
        # Optimize by fixing default section size only
        self.task_view.verticalHeader().setDefaultSectionSize(34)
        
        # Initially disable drag-drop (will be enabled per-project if manual sort is active)
        self.task_view.setDragEnabled(True)
        self.task_view.setAcceptDrops(True)
        self.task_view.setDragDropMode(QTableView.DragDropMode.NoDragDrop)
        self.task_view.setSelectionBehavior(QTableView.SelectionBehavior.SelectRows)
        self.task_view.setEditTriggers(
            QTableView.EditTrigger.DoubleClicked
            | QTableView.EditTrigger.SelectedClicked
            | QTableView.EditTrigger.EditKeyPressed
        )
        # Keep task highlight even when focus moves to notes
        self.task_view.setStyleSheet(
            "QTableView::item:selected { background: palette(highlight); color: palette(highlighted-text); }"
            "QTableView::item:selected:!active { background: palette(highlight); color: palette(highlighted-text); }"
        )
        
        # Set up context menu for task view
        self.task_view.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.task_view.customContextMenuRequested.connect(self.show_task_context_menu)

        # Right panel: due date + stakeholders + notes
        # Due Date Button (shows date and opens calendar popup)
        self.due_date_button = QPushButton()
        self.due_date_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.due_date_button.clicked.connect(self._open_due_date_picker)
        self.due_date_button.setStyleSheet("""
            QPushButton {
                background-color: #f6f8fa;
                border: 1px solid #d0d7de;
                border-radius: 4px;
                padding: 4px 12px;
                text-align: center;
                font-size: 13px;
                color: #24292f;
            }
            QPushButton:hover {
                background-color: #f3f4f6;
                border-color: #a5b3c0;
            }
            QPushButton:pressed {
                background-color: #e8eaed;
            }
            QPushButton:disabled {
                background-color: #f6f8fa;
                color: #8c959f;
                border-color: #d8dee4;
            }
        """)
        # Track whether due date is actually set (vs just showing placeholder)
        self._due_date_is_set = False
        self._current_due_date = None  # Store the actual date
        
        # Hidden date picker and calendar (will be shown in popup)
        self.due_date_edit = QDateEdit()
        self.due_date_edit.setCalendarPopup(False)  # We'll handle popup manually
        self.due_date_edit.setDisplayFormat("yyyy-MM-dd")
        self.due_date_edit.setMinimumDate(QDate(2000, 1, 1))
        self.due_date_edit.setMaximumDate(QDate(2100, 12, 31))
        self.due_date_edit.setDate(QDate.currentDate())
        self.due_date_edit.hide()  # Keep hidden, only used for state tracking
        
        self.stakeholders_edit = QLineEdit(); self.stakeholders_edit.setPlaceholderText("Stakeholders: separate by space, comma, or semicolon")
        
        # Match the due date button height to the stakeholders field height
        stakeholder_height = self.stakeholders_edit.sizeHint().height()
        self.due_date_button.setFixedHeight(stakeholder_height)
        
        # Effort Button (shows effort points using Fibonacci sequence)
        self.effort_button = QPushButton()
        self.effort_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.effort_button.clicked.connect(self._open_effort_picker)
        self.effort_button.setStyleSheet("""
            QPushButton {
                background-color: #f6f8fa;
                border: 1px solid #d0d7de;
                border-radius: 4px;
                padding: 4px 12px;
                text-align: center;
                font-size: 13px;
                color: #24292f;
            }
            QPushButton:hover {
                background-color: #f3f4f6;
                border-color: #a5b3c0;
            }
            QPushButton:pressed {
                background-color: #e8eaed;
            }
            QPushButton:disabled {
                background-color: #f6f8fa;
                color: #8c959f;
                border-color: #d8dee4;
            }
        """)
        self.effort_button.setFixedHeight(stakeholder_height)
        self._current_effort = 0.0  # Default effort value

        # Recurrence Button (opens recurrence configuration dialog)
        self.recurrence_button = QPushButton()
        self.recurrence_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.recurrence_button.clicked.connect(self._open_recurrence_dialog)
        self.recurrence_button.setStyleSheet("""
            QPushButton {
                background-color: #f6f8fa;
                border: 1px solid #d0d7de;
                border-radius: 4px;
                padding: 4px 12px;
                text-align: center;
                font-size: 13px;
                color: #24292f;
            }
            QPushButton:hover {
                background-color: #f3f4f6;
                border-color: #a5b3c0;
            }
            QPushButton:pressed {
                background-color: #e8eaed;
            }
            QPushButton:disabled {
                background-color: #f6f8fa;
                color: #8c959f;
                border-color: #d8dee4;
            }
        """)
        self.recurrence_button.setFixedHeight(stakeholder_height)
        self.recurrence_button.setText("Set recurrence")
        self._current_recurrence: Optional[Dict[str, Any]] = None
        
        self.notes = NotesEditor(self.db, self.attach_dir)
        self.notes_format_toolbar = self._create_notes_format_toolbar()
        
        # Create notes header with maximize/restore button
        self.notes_header = QWidget()
        self.notes_header_layout = QHBoxLayout(self.notes_header)
        self.notes_header_layout.setContentsMargins(0, 0, 0, 0)
        self.notes_header_layout.setSpacing(8)
        
        self.notes_label = QLabel("Notes")
        self.notes_header_layout.addWidget(self.notes_label)
        self.notes_header_layout.addStretch()
        
        # Create maximize/minimize toggle button
        self.notes_maximize_btn = QPushButton()
        self.notes_maximize_btn.setFixedSize(20, 18)  # Smaller size to fit properly
        self.notes_maximize_btn.setFlat(True)
        self.notes_maximize_btn.setToolTip("Maximize notes editor")
        self.notes_maximize_btn.clicked.connect(self.toggle_notes_maximized)
        
        # Set initial maximize icon (double arrows pointing outward)
        self._update_notes_maximize_icon(False)
        
        self.notes_header_layout.addWidget(self.notes_maximize_btn)
        
        # Notes maximization state
        self._notes_maximized = False
        self._stored_splitter_sizes = None
        self._stored_left_panel_visible = True
        self._stored_mid_panel_visible = True
        
        # Initially disable right pane until a task is selected
        self._update_right_pane_state(False)

        # Create tasks header with sort order dropdown
        self.tasks_header = QWidget()
        self.tasks_header_layout = QHBoxLayout(self.tasks_header)
        self.tasks_header_layout.setContentsMargins(0, 0, 0, 0)
        self.tasks_header_layout.setSpacing(8)
        self.tasks_header_layout.setAlignment(Qt.AlignmentFlag.AlignVCenter)
        
        self.tasks_label = QLabel("Tasks")
        self.tasks_header_layout.addWidget(self.tasks_label)
        self.feature_link_button = QPushButton("Link to feature")
        self.feature_link_button.setFlat(True)
        self.feature_link_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.feature_link_button.setStyleSheet("""
            QPushButton {
                color: #2564cf;
                border: none;
                font-weight: 500;
                padding: 0;
                margin: 0;
                margin-top: 2px;
                text-decoration: none;
            }
            QPushButton:hover {
                color: #1e55b1;
                text-decoration: none;
            }
        """)
        self.feature_link_button.clicked.connect(lambda: self._visit_project_feature_link(self._active_feature_link_pid) if self._active_feature_link_pid is not None else None)
        self.feature_link_button.setVisible(False)
        self._active_feature_link_pid: Optional[int] = None
        self.feature_link_button.setFixedHeight(self.tasks_label.sizeHint().height())
        self.tasks_header_layout.addWidget(self.feature_link_button)
        self.tasks_header_layout.setAlignment(self.tasks_label, Qt.AlignmentFlag.AlignVCenter)
        self.tasks_header_layout.setAlignment(self.feature_link_button, Qt.AlignmentFlag.AlignVCenter)
        self.tasks_header_layout.addStretch()
        
        # Create sort order dropdown
        self.sort_order_combo = QComboBox()
        self.sort_order_combo.addItem("A->Z (pinned first)")
        self.sort_order_combo.addItem("Drag to Reorder")
        self.sort_order_combo.setToolTip("Select task sorting mode")
        self.sort_order_combo.setFixedWidth(140)
        self.sort_order_combo.currentIndexChanged.connect(self.on_sort_order_changed)
        self.tasks_header_layout.addWidget(self.sort_order_combo)

        # Project filter field
        self.project_filter_edit = QLineEdit()
        self.project_filter_edit.setPlaceholderText("Filter projects by name...")
        self.project_filter_edit.textChanged.connect(self.on_project_filter_changed)
        self.project_filter_edit.setStyleSheet("QLineEdit { border-radius: 4px; }")
        
        # Create horizontal layout for due date and effort buttons
        date_effort_widget = QWidget()
        date_effort_layout = QHBoxLayout(date_effort_widget)
        date_effort_layout.setContentsMargins(0, 0, 0, 0)
        date_effort_layout.setSpacing(6)
        date_effort_layout.addWidget(self.due_date_button, stretch=1)
        date_effort_layout.addWidget(self.effort_button, stretch=0)
        
        # Layouts
        l = QVBoxLayout(left_panel); l.addWidget(self.projects_header); l.addWidget(self.project_filter_edit); l.addWidget(self.project_view)
        m = QVBoxLayout(mid_panel);  m.addWidget(self.tasks_header);    m.addWidget(self.task_view)
        r = QVBoxLayout(right_panel); r.addWidget(self.notes_header); r.addWidget(self.notes_format_toolbar); r.addWidget(self.notes); r.addWidget(date_effort_widget); r.addWidget(self.recurrence_button); r.addWidget(QLabel("Stakeholders")); r.addWidget(self.stakeholders_edit)

        # Connections
        self.project_view.selectionModel().selectionChanged.connect(self.on_project_selected)
        self.task_view.selectionModel().selectionChanged.connect(self.on_task_selected)
        # Due date save timer (triggered from popup dialog)
        self._due_date_save_timer = QTimer(self); self._due_date_save_timer.setInterval(300); self._due_date_save_timer.setSingleShot(True)
        self._due_date_save_timer.timeout.connect(self._save_due_date_now)
        self.stakeholders_edit.textEdited.connect(self.on_stakeholders_changed)
        self._stakeholder_save_timer = QTimer(self); self._stakeholder_save_timer.setInterval(300); self._stakeholder_save_timer.setSingleShot(True)
        self._stakeholder_save_timer.timeout.connect(self._save_stakeholders_now)

        # UI: menu + toolbar + search
        self._create_actions()
        self._create_menu()
        self._reclaim_project_list_context_menu()
        self._create_toolbar()
        self._refresh_periodic_review_state()

        # State
        self.include_done = False
        self.pinned_only = pinned_only_saved  # Use restored value instead of False
        self.show_no_due_dates = False
        self.show_effort_missing = False
        self.search_terms: List[str] = []
        self.search_stakeholder_terms: List[str] = []
        self._search_results_by_project = {}
        self._applying_search = False
        self._suppress_tasks_refresh = False
        
        # Focus preservation state
        self._preserve_project_id: Optional[int] = None
        self._preserve_task_id: Optional[int] = None
        # One-shot flag to avoid restoring focus after intentional filter clearing
        self._suppress_search_restore = False

        # Column sizing: Title dominates, Pin/Wait/Done equal width
        header = self.task_view.horizontalHeader()
        header.setStretchLastSection(False)
        header.setSectionResizeMode(TaskTableModel.COL_TITLE,  QHeaderView.ResizeMode.Stretch)
        
        # Set Pin/Done columns to have equal width based on the largest
        header.setSectionResizeMode(TaskTableModel.COL_PINNED, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(TaskTableModel.COL_DONE,   QHeaderView.ResizeMode.ResizeToContents)
        
        # Calculate and set equal widths after initial content sizing
        QTimer.singleShot(100, self._equalize_checkbox_columns)

        # Create status bar early to ensure it's available for status updates
        self._create_status_bar()
        
        # Init selection
        if self.project_model.rowCount() > 0:
            self.project_view.setCurrentIndex(self.project_model.index(0, 0))
            # Apply the pinned-only filter to task model if enabled during startup
            if self.pinned_only:
                # Get the selected project ID and apply pinned filter
                idx = self.project_view.currentIndex()
                pid = self.project_model.project_id_at(idx.row()) if idx.isValid() else None
                if pid is not None:
                    self.task_model.set_context(pid, self.include_done, self.pinned_only)

        self.task_model.tasksChanged.connect(self.on_tasks_changed)
        self.task_model.taskCompletionStamped.connect(self.on_task_completion_stamped)
        self.task_model.doneToggled.connect(self.on_task_done_toggled)
        self.task_model.tasksChanged.connect(lambda: QTimer.singleShot(50, self._equalize_checkbox_columns))
        self._handling_tasks_changed = False  # reentrancy guard

        # Inject search hooks into task model
        self.task_model.is_search_active = lambda: bool(self.search_terms or self.search_stakeholder_terms)
        self.task_model.refresh_search = self.apply_search_filter

        self._first_show = True  # for centering on initial display
        # Apply custom delegates for better inline editor sizing and centered checkboxes
        self.project_view.setItemDelegate(ExpandingLineEditDelegate(self.project_view))
        self.task_view.setItemDelegateForColumn(TaskTableModel.COL_TITLE, ExpandingLineEditDelegate(self.task_view))
        
        # Apply checkbox delegate to center checkboxes in Pin and Done columns
        checkbox_delegate = CheckBoxDelegate(self.task_view)
        self.task_view.setItemDelegateForColumn(TaskTableModel.COL_PINNED, checkbox_delegate)
        self.task_view.setItemDelegateForColumn(TaskTableModel.COL_DONE, checkbox_delegate)
        
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

    def _project_review_enabled(self) -> bool:
        """Return whether periodic project review is enabled in preferences."""
        return _settings_bool(self._settings, 'Preferences/PeriodicReviewEnabled', False)

    def _project_review_auto_mark_enabled(self) -> bool:
        """Return whether projects should auto-mark as reviewed when visited."""
        return self._project_review_enabled() and _settings_bool(self._settings, 'Preferences/PeriodicReviewAutoMark', False)

    def _refresh_periodic_review_state(self):
        """Enable/disable review tools and clear state when turned off."""
        enabled = self._project_review_enabled()
        try:
            self.act_start_periodic_review.setEnabled(enabled)
        except AttributeError:
            pass
        if not enabled:
            self.project_model.end_review_cycle()

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
        """Apply Microsoft To Do inspired light theme."""
        # Theme constants
        theme = {
            'accent': '#2564cf',
            'accent_hover': '#1e55b1',
            'sel_bg': '#cfe4ff',
            'sel_border': '#7eb6f5',
            'base_bg': '#ffffff',
            'side_bg': '#f5f7fa',
            'border_col': '#d0d7de',
            'text_col': '#202124',
            'subtle_text': '#5f6368',
            'font_stack': '"Segoe UI", "Helvetica Neue", Arial, sans-serif'
        }
        
        qss = f"""
        QMainWindow {{ background: {theme['side_bg']}; color: {theme['text_col']}; font: 12px {theme['font_stack']}; }}
        QToolBar {{ background: qlineargradient(x1:0,y1:0,x2:0,y2:1, stop:0 #fdfefe, stop:1 #eef2f6); border:0px; padding:4px; spacing:4px; }}
        QToolBar QToolButton {{ border-radius:3px; padding:4px 10px; background: transparent; }}
        QToolBar QToolButton:hover {{ background: #e2eefc; }}
        QToolBar QToolButton:pressed {{ background: {theme['sel_bg']}; }}
        QListView, QTableView {{ background: {theme['base_bg']}; border:1px solid {theme['border_col']}; border-radius:4px; outline:0; selection-background-color:{theme['sel_bg']}; selection-color:{theme['text_col']}; alternate-background-color:#fafbfc; }}
        QListView::item {{ padding:6px 8px; margin:2px; border-radius:3px; }}
        QListView::item:selected {{ background:{theme['sel_bg']}; border:1px solid {theme['sel_border']}; }}
        QHeaderView {{ border-top-left-radius:4px; border-top-right-radius:4px; }}
        QHeaderView::section {{ background:#f0f3f6; padding:6px 8px; border:0; border-right:1px solid {theme['border_col']}; font-weight:600; }}
        QHeaderView::section:first {{ border-top-left-radius:4px; }}
        QHeaderView::section:last {{ border-right:0; border-top-right-radius:4px; }}
        QTableView {{ gridline-color: #eef1f4; selection-background-color:{theme['sel_bg']}; selection-color:{theme['text_col']}; }}
        QTableView::item {{ padding:0px 4px; }}
        QTableView::item:selected {{ background:{theme['sel_bg']}; border:0; }}
        QTableView::item:focus {{ outline:0; }}
        QTableView::indicator {{ width:18px; height:18px; }}
        QTableView::indicator:unchecked {{ border:2px solid {theme['accent']}; border-radius:3px; background:transparent; }}
        QTableView::indicator:unchecked:hover {{ background:#e6f1fe; }}
        QTableView::indicator:checked {{ border:2px solid {theme['accent']}; background:{theme['accent']}; border-radius:3px; image:url(); }}
        QTableView::indicator:checked:hover {{ background:{theme['accent_hover']}; border-color:{theme['accent_hover']}; }}
        QLineEdit {{ background:{theme['base_bg']}; border:1px solid {theme['border_col']}; border-radius:3px; padding:4px  6px; selection-background-color:{theme['accent']}; selection-color:#fff; }}
        QLineEdit:focus {{ border:1px solid {theme['accent']}; }}
        QTextEdit {{ background:{theme['base_bg']}; border:1px solid {theme['border_col']}; border-radius:3px; padding:8px; selection-background-color:{theme['accent']}; selection-color:#fff; }}
        QTextEdit:focus {{ border:1px solid {theme['accent']}; }}
        QLabel {{ color:{theme['subtle_text']}; font-weight:600; margin-top:4px; }}
        QMenuBar {{ background: transparent; font: 12px {theme['font_stack']}; padding:4px 6px; }}
        QMenuBar::item {{ background: transparent; padding:4px 12px; margin:0 2px; border-radius:3px; }}
        QMenuBar::item:selected {{ background:#e6eef8; }}
        QMenu {{ background:{theme['base_bg']}; border: 1px solid {theme['border_col']}; border-radius: 10px; padding: 6px 0; }}
        QMenu::separator {{ height:1px; background:#e3e8ed; margin:4px 8px; }}
        QMenu::icon {{ padding-left:6px; padding-right:4px; }}
        QMenu::item {{ padding:6px 14px 6px 30px; border-radius:3px; }}
        QMenu::item:selected {{ background:{theme['sel_bg']}; }}
        QScrollBar:vertical {{ background:transparent; width:12px; margin:0; }}
        QScrollBar::handle:vertical {{ background:#d0d7de; min-height:30px; border-radius:3px; }}
        QScrollBar::handle:vertical:hover {{ background:#b7c3cc; }}
        QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{ height:0; }}
        QScrollBar:horizontal {{ background:transparent; height:12px; margin:0; }}
        QScrollBar::handle:horizontal {{ background:#d0d7de; min-width:30px; border-radius:3px; }}
        QScrollBar::handle:horizontal:hover {{ background:#b7c3cc; }}
        QPushButton {{ background:{theme['accent']}; color:#fff; border:1px solid {theme['accent']}; padding:6px 14px; border-radius:3px; font-weight:500; }}
        QPushButton:hover {{ background:{theme['accent_hover']}; border-color:{theme['accent_hover']}; }}
        QPushButton:pressed {{ background:#133d7a; border-color:#133d7a; }}
        QPushButton:flat {{ background:transparent; border:1px solid transparent; }}
        QPushButton:flat:hover {{ background:#e6f1fe; border:1px solid {theme['border_col']}; }}
        QPushButton:flat:pressed {{ background:{theme['sel_bg']}; border:1px solid {theme['sel_border']}; }}
        QCheckBox {{ spacing:8px; }}
        QCheckBox::indicator {{ width:18px; height:18px; border-radius:3px; border:2px solid {theme['accent']}; background:transparent; }}
        QCheckBox::indicator:hover {{ background:#e6f1fe; }}
        QCheckBox::indicator:checked {{ background:{theme['accent']}; border-color:{theme['accent']}; image:url(); }}
        QStatusBar {{ background:#f0f3f6; border-top:1px solid {theme['border_col']}; }}
        QListView:focus, QTableView:focus {{ border:1px solid {theme['accent']}; }}
        """
        QApplication.instance().setStyleSheet(qss)
        
        # Configure view-specific styling
        self.project_view.setSpacing(2)
        self.task_view.verticalHeader().setDefaultSectionSize(34)
        self.task_view.verticalHeader().hide()
        self.task_view.setAlternatingRowColors(True)
        # Preserve any prior per-view additions
        self.task_view.setStyleSheet(self.task_view.styleSheet() + '\nQTableView { border-top-left-radius:4px; border-top-right-radius:4px; }')

    def apply_theme(self):
        # Microsoft To Do inspired light theme (refined)
        accent = '#2564cf'
        accent_hover = '#1e55b1'
        sel_bg = '#cfe4ff'
        sel_border = '#7eb6f5'
        base_bg = '#ffffff'
        side_bg = '#f5f7fa'
        border_col = '#d0d7de'
        text_col = '#202124'
        subtle_text = '#5f6368'
        font_stack = '"Segoe UI", "Helvetica Neue", Arial, sans-serif'
        qss = f"""
        QMainWindow {{ background: {side_bg}; color: {text_col}; font: 12px {font_stack}; }}
        QToolBar {{ background: qlineargradient(x1:0,y1:0,x2:0,y2:1, stop:0 #fdfefe, stop:1 #eef2f6); border:0px; padding:4px; spacing:4px; }}
        QToolBar QToolButton {{ border-radius:3px; padding:4px 10px; background: transparent; }}
        QToolBar QToolButton:hover {{ background: #e2eefc; }}
        QToolBar QToolButton:pressed {{ background: {sel_bg}; }}
        QListView, QTableView {{ background: {base_bg}; border:1px solid {border_col}; border-radius:4px; outline:0; selection-background-color:{sel_bg}; selection-color:{text_col}; alternate-background-color:#fafbfc; }}
        QListView::item {{ padding:6px 8px; margin:2px; border-radius:3px; }}
        QListView::item:selected {{ background:{sel_bg}; border:1px solid {sel_border}; }}
        QHeaderView {{ border-top-left-radius:4px; border-top-right-radius:4px; }}
        QHeaderView::section {{ background:#f0f3f6; padding:6px 8px; border:0; border-right:1px solid {border_col}; font-weight:600; }}
        QHeaderView::section:first {{ border-top-left-radius:4px; }}
        QHeaderView::section:last {{ border-right:0; border-top-right-radius:4px; }}
        QTableView {{ gridline-color: #eef1f4; selection-background-color:{sel_bg}; selection-color:{text_col}; }}
        QTableView::item {{ padding:0px 4px; }}
        QTableView::item:selected {{ background:{sel_bg}; border:0; }}
        QTableView::item:focus {{ outline:0; }}
        QTableView::indicator {{ width:18px; height:18px; }}
        QTableView::indicator:unchecked {{ border:2px solid {accent}; border-radius:3px; background:transparent; }}
        QTableView::indicator:unchecked:hover {{ background:#e6f1fe; }}
        QTableView::indicator:checked {{ border:2px solid {accent}; background:{accent}; border-radius:3px; image:url(); }}
        QTableView::indicator:checked:hover {{ background:{accent_hover}; border-color:{accent_hover}; }}
        QLineEdit {{ background:{base_bg}; border:1px solid {border_col}; border-radius:3px; padding:4px  6px; selection-background-color:{accent}; selection-color:#fff; }}
        QLineEdit:focus {{ border:1px solid {accent}; }}
        QTextEdit {{ background:{base_bg}; border:1px solid {border_col}; border-radius:3px; padding:8px; selection-background-color:{accent}; selection-color:#fff; }}
        QTextEdit:focus {{ border:1px solid {accent}; }}
        QLabel {{ color:{subtle_text}; font-weight:600; margin-top:4px; }}
        QMenuBar {{ background: transparent; font: 12px {font_stack}; padding:4px 6px; }}
        QMenuBar::item {{ background: transparent; padding:4px 12px; margin:0 2px; border-radius:3px; }}
        QMenuBar::item:selected {{ background:#e6eef8; }}
        QMenu {{ background:{base_bg}; border: 1px solid {border_col}; border-radius: 10px; padding: 6px 0; }}
        QMenu::separator {{ height:1px; background:#e3e8ed; margin:4px 8px; }}
        QMenu::icon {{ padding-left:6px; padding-right:4px; }}
        QMenu::item {{ padding:6px 14px 6px 30px; border-radius:3px; }}
        QMenu::item:selected {{ background:{sel_bg}; }}
        QScrollBar:vertical {{ background:transparent; width:12px; margin:0; }}
        QScrollBar::handle:vertical {{ background:#d0d7de; min-height:30px; border-radius:3px; }}
        QScrollBar::handle:vertical:hover {{ background:#b7c3cc; }}
        QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{ height:0; }}
        QScrollBar:horizontal {{ background:transparent; height:12px; margin:0; }}
        QScrollBar::handle:horizontal {{ background:#d0d7de; min-width:30px; border-radius:3px; }}
        QScrollBar::handle:horizontal:hover {{ background:#b7c3cc; }}
        QPushButton {{ background:{accent}; color:#fff; border:1px solid {accent}; padding:6px 14px; border-radius:3px; font-weight:500; }}
        QPushButton:hover {{ background:{accent_hover}; border-color:{accent_hover}; }}
        QPushButton:pressed {{ background:#133d7a; border-color:#133d7a; }}
        QPushButton:flat {{ background:transparent; border:1px solid transparent; }}
        QPushButton:flat:hover {{ background:#e6f1fe; border:1px solid {border_col}; }}
        QPushButton:flat:pressed {{ background:{sel_bg}; border:1px solid {sel_border}; }}
        QCheckBox {{ spacing:8px; }}
        QCheckBox::indicator {{ width:18px; height:18px; border-radius:3px; border:2px solid {accent}; background:transparent; }}
        QCheckBox::indicator:hover {{ background:#e6f1fe; }}
        QCheckBox::indicator:checked {{ background:{accent}; border-color:{accent}; image:url(); }}
        QStatusBar {{ background:#f0f3f6; border-top:1px solid {border_col}; }}
        QListView:focus, QTableView:focus {{ border:1px solid {accent}; }}
        """
        QApplication.instance().setStyleSheet(qss)
        # Row / cell sizing
        self.project_view.setSpacing(2)
        self.task_view.verticalHeader().setDefaultSectionSize(34)
        self.task_view.verticalHeader().hide()
        self.task_view.setAlternatingRowColors(True)
        # Preserve any prior per-view additions
        self.task_view.setStyleSheet(self.task_view.styleSheet() + '\nQTableView { border-top-left-radius:4px; border-top-right-radius:4px; }')

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
        
        # If icon file doesn't exist, create it programmatically
        try:
            icon = self._create_rocket_icon_qt()
            self.setWindowIcon(icon)
            QApplication.setWindowIcon(icon)
        except Exception as e:
            print(f"Could not set application icon: {e}")

    def _create_rocket_icon_qt(self) -> QIcon:
        """Create a rocket icon using Qt drawing operations"""
        size = 64
        pixmap = QPixmap(size, size)
        pixmap.fill(Qt.GlobalColor.transparent)
        
        painter = QPainter(pixmap)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        
        # Scale factor
        s = size / 64.0
        
        # Colors
        rocket_body = QColor(70, 130, 180)
        rocket_nose = QColor(255, 69, 0)
        window_color = QColor(135, 206, 235)
        flame_color = QColor(255, 140, 0)
        fin_color = QColor(47, 79, 79)
        
        # Draw flame
        flame_rect = QRect(int(24 * s), int(45 * s), int(16 * s), int(12 * s))
        painter.setBrush(flame_color)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.drawEllipse(flame_rect)
        
        # Draw fins
        painter.setBrush(fin_color)
        fin_left = [
            QRect(int(20 * s), int(40 * s), int(8 * s), int(8 * s))
        ]
        fin_right = [
            QRect(int(36 * s), int(40 * s), int(8 * s), int(8 * s))
        ]
        for fin in fin_left + fin_right:
            painter.drawRect(fin)
        
        # Draw rocket body
        body_rect = QRect(int(26 * s), int(20 * s), int(12 * s), int(25 * s))
        painter.setBrush(rocket_body)
        painter.setPen(QPen(QColor(40, 80, 120), int(1 * s)))
        painter.drawRoundedRect(body_rect, int(2 * s), int(2 * s))
        
        # Draw nose cone
        painter.setBrush(rocket_nose)
        painter.setPen(QPen(QColor(200, 50, 0), int(1 * s)))
        nose_points = [
            QPoint(int(32 * s), int(8 * s)),   # Top
            QPoint(int(26 * s), int(20 * s)),  # Left
            QPoint(int(38 * s), int(20 * s))   # Right
        ]
        from PyQt6.QtGui import QPolygon
        painter.drawPolygon(QPolygon(nose_points))
        
        # Draw window
        window_center = QPoint(int(32 * s), int(28 * s))
        window_radius = int(4 * s)
        window_rect = QRect(
            window_center.x() - window_radius,
            window_center.y() - window_radius,
            window_radius * 2,
            window_radius * 2
        )
        painter.setBrush(window_color)
        painter.setPen(QPen(QColor(30, 60, 100), int(1 * s)))
        painter.drawEllipse(window_rect)
        
        # Add small highlight to window
        highlight_rect = QRect(
            window_center.x() - int(2 * s),
            window_center.y() - int(2 * s),
            int(4 * s), int(4 * s)
        )
        painter.setBrush(QColor(255, 255, 255, 150))
        painter.setPen(Qt.PenStyle.NoPen)
        painter.drawEllipse(highlight_rect)
        
        painter.end()
        return QIcon(pixmap)

    def on_task_done_toggled(self, project_id: int, became_done: bool):
        # Remember current row so we can keep selection anchored after removal
        current_idx = self.project_view.currentIndex()
        current_row = current_idx.row() if current_idx.isValid() else -1
        
        # Incrementally update project stats without full reload
        result = self.project_model.apply_task_done_toggle(project_id, became_done)
        
        # If a project was removed, handle selection
        if result and result[0] == 'removed':
            removed_row = result[2]  # Get the row index before removal
            target_row = current_row if current_row >= 0 else removed_row
            self._select_project_row_preserving_slot(target_row)
        else:
            # If the project vanished but we didn't get a removal result (e.g., fallback reload),
            # ensure selection remains anchored on the same row.
            if self.project_model.row_for_project(project_id) == -1 and current_row >= 0:
                self._select_project_row_preserving_slot(current_row)

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
                if self.search_terms or self.search_stakeholder_terms:
                    self.apply_search_filter()
                elif self.task_model.project_id in affected_projects:
                    self.task_model.reload()
                self._update_comprehensive_status()

                current_idx = self.task_view.currentIndex()
                current_tid = self.task_model.task_id_at(current_idx.row()) if current_idx.isValid() else None
                if current_tid is not None:
                    self._current_recurrence = self.db.get_task_recurrence(current_tid)
                    self._update_recurrence_button_text()
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
        """Make Pin and Done columns have equal width based on the largest."""
        header = self.task_view.horizontalHeader()
        
        # Get current widths of the two checkbox columns
        pin_width = header.sectionSize(TaskTableModel.COL_PINNED)
        done_width = header.sectionSize(TaskTableModel.COL_DONE)
        
        # Find the maximum width
        max_width = max(pin_width, done_width)
        
        # Set both columns to use fixed width equal to the maximum
        header.setSectionResizeMode(TaskTableModel.COL_PINNED, QHeaderView.ResizeMode.Fixed)
        header.setSectionResizeMode(TaskTableModel.COL_DONE, QHeaderView.ResizeMode.Fixed)
        
        # Apply the equal width to both columns
        header.resizeSection(TaskTableModel.COL_PINNED, max_width)
        header.resizeSection(TaskTableModel.COL_DONE, max_width)

    def _create_notes_format_toolbar(self) -> QToolBar:
        """Create a toolbar bound directly to NotesEditor formatting actions."""
        return self.notes.create_format_toolbar(self)

    def _update_notes_maximize_icon(self, is_maximized: bool):
        """Update the maximize button icon based on current state."""
        if is_maximized:
            # Restore icon - arrows pointing inward
            icon_text = "><"  # Restore/minimize symbol
            tooltip = "Restore notes editor"
        else:
            # Maximize icon - arrows pointing outward  
            icon_text = "<>"  # Expand/maximize symbol
            tooltip = "Maximize notes editor"
        
        # Set text and ensure button is properly styled
        self.notes_maximize_btn.setText(icon_text)
        self.notes_maximize_btn.setToolTip(tooltip)
        
        # Ensure the button has proper styling with smaller size
        button_style = """
            QPushButton {
                background: #f8f9fa;
                border: 1px solid #d0d7de;
                border-radius: 3px;
                color: #24292f;
                font-size: 10px;
                font-weight: bold;
                padding: 1px 3px;
                margin: 0px;
            }
            QPushButton:hover {
                background: #e6f1fe;
                border-color: #2564cf;
                color: #2564cf;
            }
            QPushButton:pressed {
                background: #cfe4ff;
                border-color: #1e55b1;
                color: #1e55b1;
            }
        """
        self.notes_maximize_btn.setStyleSheet(button_style)

    def toggle_notes_maximized(self):
        """Toggle between maximized and normal notes editor view."""
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
        pm = QPixmap(16, 16)
        pm.fill(Qt.GlobalColor.transparent)
        p = QPainter(pm)
        p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        pen = QPen(QColor(30, 160, 30), 2)
        p.setPen(pen)
        # simple check mark
        p.drawLine(3, 9, 7, 13)
        p.drawLine(7, 13, 13, 3)
        p.end()
        return QIcon(pm)

    def _make_snooze_icon(self) -> QIcon:
        pm = QPixmap(16, 16)
        pm.fill(Qt.GlobalColor.transparent)
        p = QPainter(pm)
        p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        # Draw clock circle
        pen = QPen(QColor(70, 130, 180), 2)
        p.setPen(pen)
        p.drawEllipse(2, 2, 12, 12)
        # Draw clock hands
        p.drawLine(8, 8, 8, 5)  # hour hand (pointing up)
        p.drawLine(8, 8, 11, 8)  # minute hand (pointing right)
        p.end()
        return QIcon(pm)

    def _create_actions(self):
        style = self.style()
        self.act_add_project = QAction(style.standardIcon(QStyle.StandardPixmap.SP_FileDialogNewFolder), "Add Project", self)
        self.act_edit_project = QAction(style.standardIcon(QStyle.StandardPixmap.SP_FileDialogDetailedView), "Rename Project", self)
        self.act_remove_project = QAction(style.standardIcon(QStyle.StandardPixmap.SP_TrashIcon), "Remove Project", self)
        self.act_add_task = QAction(style.standardIcon(QStyle.StandardPixmap.SP_FileIcon), "Add Task", self)
        self.act_bulk_import_tasks = QAction(style.standardIcon(QStyle.StandardPixmap.SP_FileDialogListView), "Bulk Import Tasks...", self)
        self.act_remove_task = QAction(style.standardIcon(QStyle.StandardPixmap.SP_TrashIcon), "Remove Task(s)", self)
        self.act_mark_complete = QAction("Mark as Complete", self)
        self.act_mark_complete.setIcon(self._make_green_check_icon())  # add green tick icon
        
        # Add the move task action
        self.act_move_task = QAction("Move to Project...", self)
        self.act_move_task.setIcon(style.standardIcon(QStyle.StandardPixmap.SP_ArrowRight))
        self.act_move_task.triggered.connect(self.move_selected_tasks)

        # Add duplicate task action
        self.act_duplicate_tasks = QAction("Duplicate Task(s)", self)
        self.act_duplicate_tasks.setIcon(style.standardIcon(QStyle.StandardPixmap.SP_FileDialogListView))
        self.act_duplicate_tasks.triggered.connect(self.duplicate_selected_tasks)
        
        # Add the mark as MoM action
        self.act_mark_mom = QAction("Mark as Minutes of Meeting", self)
        self.act_mark_mom.setIcon(style.standardIcon(QStyle.StandardPixmap.SP_FileDialogContentsView))  # Clipboard icon
        self.act_mark_mom.triggered.connect(self.toggle_mark_task_as_mom)
        self.act_mark_mom.setEnabled(False)  # Initially disabled until a task is selected

        # Recurrence configuration action
        self.act_recurrence = QAction("Recurrence...", self)
        self.act_recurrence.setIcon(style.standardIcon(QStyle.StandardPixmap.SP_BrowserReload))
        self.act_recurrence.triggered.connect(self._open_recurrence_dialog)
        self.act_recurrence.setEnabled(False)
        
        self.act_toggle_done = QAction("Completed Tasks", self, checkable=True)
        self.act_show_pinned_filter = QAction("Pinned Tasks", self, checkable=True)
        self.act_show_no_due_dates = QAction("Missing Due Dates", self, checkable=True)
        self.act_show_effort_missing = QAction("Missing Effort Estimation", self, checkable=True)
        self.act_pinned_overview = QAction("Pinned Tasks Overview", self)
        self.act_always_on_top = QAction("Always on Top", self, checkable=True)
        self.act_always_on_top.toggled.connect(self.toggle_always_on_top)
        
        # Manual sort override action
        self.act_manual_sort = QAction("Manual Sort Override", self, checkable=True)
        self.act_manual_sort.triggered.connect(self.toggle_manual_sort)

        # Toggle visibility action
        self.act_toggle_project_visibility = QAction("Toggle Visibility", self)
        self.act_toggle_project_visibility.setIcon(style.standardIcon(QStyle.StandardPixmap.SP_BrowserReload))
        self.act_toggle_project_visibility.triggered.connect(self.toggle_project_visibility)

        # Periodic projects review action
        self.act_start_periodic_review = QAction("Start Periodic Projects Review", self)
        self.act_start_periodic_review.triggered.connect(self.start_periodic_projects_review)

        self.act_add_project.triggered.connect(self.add_project)
        self.act_edit_project.triggered.connect(self.edit_selected_project)
        self.act_remove_project.triggered.connect(self.remove_selected_project)

        self.act_add_task.triggered.connect(self.add_task)
        self.act_bulk_import_tasks.triggered.connect(self.open_bulk_task_import_dialog)
        self.act_remove_task.triggered.connect(self.remove_selected_task)
        self.act_mark_complete.triggered.connect(self.mark_task_complete)
        self.act_toggle_done.triggered.connect(self.toggle_show_completed)
        self.act_show_pinned_filter.triggered.connect(self.toggle_pinned_only)
        self.act_show_no_due_dates.triggered.connect(self.toggle_show_no_due_dates)
        self.act_show_effort_missing.triggered.connect(self.toggle_show_effort_missing)
        self.act_pinned_overview.triggered.connect(self.show_pinned_overview)

        self.act_add_task.setShortcut(QKeySequence("Ctrl+N"))
        self.act_mark_complete.setShortcut(QKeySequence("Ctrl+Enter"))

    def _create_menu(self):
        menubar = self.menuBar()
        mFile = menubar.addMenu("File")
        mFile.addAction("New Database…", self.new_database)
        mFile.addAction("Open Database…", self.open_database)
        mFile.addSeparator()
        mFile.addAction("Preferences…", self.show_preferences)
        mFile.addSeparator(); mFile.addAction("Exit", self.close)

        mProject = menubar.addMenu("Project")
        mProject.addAction(self.act_add_project)
        mProject.addAction(self.act_edit_project)
        mProject.addAction(self.act_remove_project)
        mProject.addSeparator()
        mProject.addAction(self.act_toggle_project_visibility)

        mTask = menubar.addMenu("Task")
        mTask.addAction(self.act_add_task)
        mTask.addAction(self.act_bulk_import_tasks)
        mTask.addAction(self.act_remove_task)
        mTask.addSeparator()
        mTask.addAction(self.act_mark_complete)
        mTask.addAction(self.act_move_task)  # Add move action to menu
        mTask.addAction(self.act_duplicate_tasks)
        mTask.addSeparator()
        mTask.addAction(self.act_mark_mom)  # Add mark as MoM action to menu
        mTask.addAction(self.act_recurrence)

        mView = menubar.addMenu("View")
        mView.addAction(self.act_always_on_top)
        mView.addSeparator()
        mView.addAction(self.act_toggle_done)
        mView.addAction(self.act_show_pinned_filter)
        mView.addAction(self.act_show_no_due_dates)
        mView.addAction(self.act_show_effort_missing)
        mView.addSeparator()
        mView.addAction("Pinned Tasks Overview", self.show_pinned_overview)
        
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
        mTools.addSeparator()
        mTools.addAction(self.act_start_periodic_review)
        self.act_start_periodic_review.setEnabled(self._project_review_enabled())
        
        # Add addon menu items
        addon_items_added = False
        for addon_name, addon in self.addons.items():
            if hasattr(addon, 'register_menu_items'):
                if not addon_items_added:
                    mTools.addSeparator()
                    addon_items_added = True
                addon.register_menu_items(mTools, self)
        
        mTools.addSeparator()
        mTools.addAction("Remove Pins from Incomplete Tasks", self.remove_pins_from_incomplete_tasks)
        mTools.addAction("Clear Due Dates from Incomplete Tasks", self.remove_due_dates_from_incomplete_tasks)
        mTools.addSeparator()
        mTools.addAction("Open Attachments Folder", self.open_attachments_folder)

        # Add Help menu with update functionality
        mHelp = menubar.addMenu("Help")
        mHelp.addAction("Open Activity Log", self.open_activity_log)
        mHelp.addSeparator()
        mHelp.addAction("About", self.show_about)

    def _create_toolbar(self):
        tb = QToolBar("Quick Tools")
        tb.setIconSize(QSize(20, 20))
        tb.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        self.addToolBar(Qt.ToolBarArea.TopToolBarArea, tb)
        tb.addAction(self.act_add_project)
        tb.addSeparator()
        tb.addAction(self.act_bulk_import_tasks)
        tb.addAction(self.act_add_task)
        tb.addSeparator()
        tb.addAction(self.act_mark_complete)
        tb.addAction(self.act_pinned_overview)
        tb.addSeparator()
        
        # Create search container with clear button
        search_container = QWidget()
        search_layout = QHBoxLayout(search_container)
        search_layout.setContentsMargins(0, 0, 0, 0)
        search_layout.setSpacing(2)
        
        self.search_edit = QLineEdit()
        self.search_edit.setPlaceholderText("Search… (add @ prefix for stakeholders)")
        self.search_edit.textChanged.connect(self.on_search_changed)
        search_layout.addWidget(self.search_edit)
        
        # Add clear search button
        clear_search_btn = QPushButton("✕")
        clear_search_btn.setFixedSize(20, 20)
        clear_search_btn.setToolTip("Clear search and filters")
        clear_search_btn.setStyleSheet("""
            QPushButton {
                background: #f8f9fa;
                border: 1px solid #d0d7de;
                border-radius: 3px;
                color: #656d76;
                font-size: 10px;
                font-weight: bold;
            }
            QPushButton:hover {
                background: #f3f4f6;
                border-color: #d1d9e0;
                color: #24292f;
            }
            QPushButton:pressed {
                background: #ebedf0;
                border-color: #c7d2de;
            }
        """)
        clear_search_btn.clicked.connect(self.clear_all_filters)
        search_layout.addWidget(clear_search_btn)
        
        tb.addWidget(search_container)

    def _create_status_bar(self):
        """Create and configure the status bar with comprehensive filter preview."""
        status_bar = self.statusBar()
        status_bar.clearMessage()
        
        # Create left side status label (comprehensive status)
        self.status_comprehensive = QLabel("Ready")
        
        # Style the comprehensive status label
        comprehensive_style = """
            QLabel { 
                color: #202124; 
                font-size: 11px; 
                font-weight: 500;
                padding: 2px 8px; 
                background: #f8f9fa;
                border: 1px solid #e0e0e0;
                border-radius: 4px;
                margin: 2px;
            }
        """
        
        self.status_comprehensive.setStyleSheet(comprehensive_style)
        
        # Create right side status label (task creation date)
        self.status_task_info = QLabel("")
        
        # Style the task info label (right-aligned)
        task_info_style = """
            QLabel { 
                color: #5f6368; 
                font-size: 11px; 
                font-weight: 500;
                padding: 2px 8px; 
                background: #f8f9fa;
                border: 1px solid #e0e0e0;
                border-radius: 4px;
                margin: 2px;
            }
        """
        
        self.status_task_info.setStyleSheet(task_info_style)
        self.status_task_info.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        
        # Add labels to status bar - both with equal stretch factor
        status_bar.addWidget(self.status_comprehensive, 1)  # Left side - equal stretch
        status_bar.addWidget(self.status_task_info, 1)  # Right side - equal stretch with right-aligned text
        
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
            self._refresh_periodic_review_state()

    def _switch_to_database(self, new_db: DB, path: str):
        """Atomically switch to a new database and keep the current session usable on failure."""
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
            self._search_results_by_project.clear()
            self.project_model._search_filter_ids = None
            self.project_model.project_name_filter = ""
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
            self.notes.set_task(None)
            self._update_right_pane_state(False)
            self._due_date_is_set = False
            self._current_due_date = None
            self._update_due_date_button_text()
            self._current_effort = 0.0
            self._update_effort_button_text()
            self._current_recurrence = None
            self._update_recurrence_button_text()
            self.stakeholders_edit.blockSignals(True)
            self.stakeholders_edit.clear()
            self.stakeholders_edit.blockSignals(False)

            db_name = os.path.basename(path)
            self.setWindowTitle(f"Project Notes - {db_name}")
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
                    self.notes.set_task(None)
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

    def copy_filtered_tasks_to_clipboard(self):
        """Copy currently filtered tasks to clipboard"""
        export_text = self._generate_filtered_tasks_export()
        
        if not export_text.strip():
            QMessageBox.information(self, "Copy to Clipboard", "No tasks match the current filters.")
            return
        
        try:
            clipboard = QApplication.clipboard()
            clipboard.setText(export_text)
            # QMessageBox.information(self, "Copy to Clipboard", "Filtered tasks copied to clipboard successfully.")
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
        
        if self.search_terms or self.search_stakeholder_terms:
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
                tasks = self.task_model.apply_secondary_filters(
                    self.db.list_tasks(project_id, self.include_done, self.pinned_only)
                )
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
        if self.pinned_only:
            filter_desc.append("Showing: Pinned tasks only")
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
                    indicators.append("📌 Pinned")
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
            
            # Write tasks to CSV file with pipe delimiter
            with open(file_path, 'w', newline='', encoding='utf-8') as csvfile:
                fieldnames = [
                    'project_title', 'task_title', 'pinned', 'done', 'due_date',
                    'stakeholders', 'notes_html', 'notes_plain', 'created_at', 'updated_at'
                ]
                # Use pipe delimiter to avoid conflicts with commas in content
                writer = csv.DictWriter(csvfile, fieldnames=fieldnames, delimiter='|', quoting=csv.QUOTE_MINIMAL)
                
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
            
            # Show results
            result_msg = f"Filtered tasks exported to CSV:\n{file_path}\n\nExported {len(processed_tasks_data)} tasks"
            if exported_images:
                result_msg += f" with {len(exported_images)} images.\n\nImages folder: {images_dir}"
            else:
                result_msg += "."
            
            QMessageBox.information(self, "Export Complete", result_msg)
            
        except Exception as e:
            QMessageBox.critical(self, "Export Error", f"Failed to export tasks to CSV:\n{str(e)}")

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
                # Convert relative path to absolute
                if not os.path.isabs(img_src):
                    abs_img_path = os.path.join(APP_DIR, img_src.replace('/', os.sep))
                else:
                    abs_img_path = img_src
                
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
            
            with open(file_path, 'r', newline='', encoding='utf-8') as csvfile:
                # Try to detect delimiter
                sample = csvfile.read(1024)
                csvfile.seek(0)
                sniffer = csv.Sniffer()
                try:
                    dialect = sniffer.sniff(sample, delimiters='|,;\t')
                except:
                    # Default to pipe delimiter (our preferred format)
                    dialect = csv.excel
                    dialect.delimiter = '|'
                
                reader = csv.DictReader(csvfile, dialect=dialect)
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
                        
                        # Create task
                        task_id = self.db.add_task(project_id, task_title)
                        
                        # Handle boolean fields
                        pinned = sanitized_row.get('pinned', '').strip().lower() in ('true', '1', 'yes', 'y')
                        done = sanitized_row.get('done', '').strip().lower() in ('true', '1', 'yes', 'y')
                        if pinned or done:
                            self.db.set_imported_task_state(task_id, pinned=pinned, done=done)
                        
                        # Handle stakeholders
                        stakeholders_text = (sanitized_row.get('stakeholders', '') or '').strip()
                        if stakeholders_text:
                            # Split stakeholders by common separators
                            stakeholder_names = re.split(r'[,;|]+', stakeholders_text)
                            stakeholder_names = [name.strip() for name in stakeholder_names if name.strip()]
                            if stakeholder_names:
                                self.db.set_stakeholders_for_task(task_id, stakeholder_names)
                        
                        # Handle notes with images
                        notes_html = (sanitized_row.get('notes_html', '') or '').strip()
                        notes_plain = (sanitized_row.get('notes_plain', '') or '').strip()
                        
                        if notes_html:
                            # Process HTML to import images
                            processed_html, images_count = self._process_import_images(notes_html, images_dir)
                            if images_count > 0:
                                imported_images.extend([f"Task '{task_title}': {images_count} images"])
                            self.db.save_note(task_id, processed_html, notes_plain, self.attach_dir)
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
                    if self.search_terms or self.search_stakeholder_terms:
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
        """Remove pins from all incomplete tasks across all projects."""
        try:
            # Count incomplete pinned tasks
            count_query = "SELECT COUNT(*) FROM tasks WHERE pinned=1 AND done=0"
            pinned_incomplete_count = self.db.conn.execute(count_query).fetchone()[0]
            
            if pinned_incomplete_count == 0:
                QMessageBox.information(
                    self,
                    "Remove Pins",
                    "No incomplete pinned tasks found."
                )
                return
            
            if not skip_confirmation:
                # Confirm with user
                reply = QMessageBox.question(
                    self,
                    "Remove Pins",
                    f"This will remove pins from {pinned_incomplete_count} incomplete task(s).\n\n"
                    f"Completed tasks will remain pinned.\n\n"
                    f"Do you want to continue?",
                    QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                    QMessageBox.StandardButton.No
                )
                
                if reply != QMessageBox.StandardButton.Yes:
                    return
            
            # Update all incomplete pinned tasks
            update_query = "UPDATE tasks SET pinned=0, updated_at=strftime('%Y-%m-%dT%H:%M:%fZ','now') WHERE pinned=1 AND done=0"
            self.db.conn.execute(update_query)
            self.db.conn.commit()
            
            # Log the bulk operation
            if hasattr(self, 'activity_logger'):
                self.activity_logger.log_activity(
                    'bulk_unpin',
                    {'count': pinned_incomplete_count, 'scope': 'incomplete_tasks'}
                )
            
            # Refresh the UI
            self.project_model.reload()
            self.task_model.reload()
            
            QMessageBox.information(
                self,
                "Remove Pins",
                f"Successfully removed pins from {pinned_incomplete_count} incomplete task(s)."
            )
            
        except Exception as e:
            QMessageBox.critical(
                self,
                "Error",
                f"Failed to remove pins:\n{str(e)}"
            )

    def remove_due_dates_from_incomplete_tasks(self, checked=False, *, skip_confirmation=False):
        """Clear due dates from incomplete tasks without affecting completed ones."""
        try:
            count_query = (
                "SELECT COUNT(*) FROM tasks "
                "WHERE done=0 AND due_date IS NOT NULL AND TRIM(COALESCE(due_date, '')) <> ''"
            )
            pending_count = self.db.conn.execute(count_query).fetchone()[0]

            if pending_count == 0:
                QMessageBox.information(
                    self,
                    "Remove Due Dates",
                    "No incomplete tasks with due dates were found."
                )
                return

            if not skip_confirmation:
                reply = QMessageBox.question(
                    self,
                    "Remove Due Dates",
                    f"This will remove due dates from {pending_count} incomplete task(s).\n\n"
                    "Completed tasks will remain unchanged.\n\n"
                    "Do you want to continue?",
                    QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                    QMessageBox.StandardButton.No
                )

                if reply != QMessageBox.StandardButton.Yes:
                    return

            update_query = (
                "UPDATE tasks SET due_date=NULL, "
                "updated_at=strftime('%Y-%m-%dT%H:%M:%fZ','now') "
                "WHERE done=0 AND due_date IS NOT NULL AND TRIM(COALESCE(due_date, '')) <> ''"
            )
            self.db.conn.execute(update_query)
            self.db.conn.commit()

            self.project_model.reload()
            self.task_model.reload()

            QMessageBox.information(
                self,
                "Remove Due Dates",
                f"Successfully removed due dates from {pending_count} incomplete task(s)."
            )

        except Exception as e:
            QMessageBox.critical(
                self,
                "Error",
                f"Failed to remove due dates:\n{str(e)}"
            )

    def start_periodic_projects_review(self):
        """Start a periodic review cycle and highlight projects needing review."""
        if not self._project_review_enabled():
            QMessageBox.information(
                self,
                "Periodic Review",
                "Enable periodic project review in Preferences → Planning to use this feature."
            )
            return

        if self.project_model.rowCount() == 0:
            QMessageBox.information(
                self,
                "Periodic Review",
                "No projects are available to review."
            )
            return

        cleanup_choice = QMessageBox.question(
            self,
            "Periodic Review",
            "Before starting the periodic review, do you want to remove pins and clear due dates "
            "from incomplete tasks?\n\n"
            "Yes removes pins and due dates from incomplete tasks. No keeps them unchanged. "
            "Cancel stops starting the review.\n\n"
            "Tip: Add any recent tasks before starting the review.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No | QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.No
        )

        if cleanup_choice == QMessageBox.StandardButton.Cancel:
            return

        if cleanup_choice == QMessageBox.StandardButton.Yes:
            self.remove_pins_from_incomplete_tasks(skip_confirmation=True)
            self.remove_due_dates_from_incomplete_tasks(skip_confirmation=True)

        self.project_model.start_review_cycle()
        if self._project_review_auto_mark_enabled():
            idx = self.project_view.currentIndex()
            pid = self.project_model.project_id_at(idx.row()) if idx.isValid() else None
            if pid is not None and self.project_model.is_project_in_review(pid):
                self.mark_project_review_completed(pid)

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
        
        if self.search_terms or self.search_stakeholder_terms:
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
                tasks = self.task_model.apply_secondary_filters(
                    self.db.list_tasks(project_id, self.include_done, self.pinned_only)
                )
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
            'done': 'True' if bool(safe_get(task, 'done', 0)) else 'False',
            'due_date': safe_get(task, 'due_date', ''),
            'stakeholders': stakeholders_text,
            'notes_html': notes_html or '',  # Include HTML content for image processing
            'notes_plain': (notes_plain or '').replace('\n', ' | '),  # Replace newlines for CSV
            'created_at': safe_get(task, 'created_at', ''),
            'updated_at': safe_get(task, 'updated_at', '')
        }

    def show_about(self):
        """Show a modern about dialog with app information"""
        about_dialog = QDialog(self)
        about_dialog.setWindowTitle("About Project Notes")
        about_dialog.setModal(True)
        
        # Create main layout with proper spacing
        main_layout = QVBoxLayout(about_dialog)
        main_layout.setSpacing(20)
        main_layout.setContentsMargins(40, 30, 40, 30)
        
        # Header section with icon and title
        header_widget = QWidget()
        header_layout = QHBoxLayout(header_widget)
        header_layout.setContentsMargins(0, 0, 0, 0)
        header_layout.setSpacing(20)
        
        # App icon with proper sizing and alignment
        icon_container = QWidget()
        icon_container.setFixedSize(80, 80)
        icon_layout = QVBoxLayout(icon_container)
        icon_layout.setContentsMargins(0, 0, 0, 0)
        icon_layout.setAlignment(Qt.AlignmentFlag.AlignCenter)
        
        icon_label = QLabel()
        icon_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        
        try:
            app_icon = self.windowIcon()
            if not app_icon.isNull():
                # Use app icon with proper scaling
                pixmap = app_icon.pixmap(72, 72)  # Slightly larger for better visibility
                icon_label.setPixmap(pixmap)
            else:
                # Create a more detailed fallback rocket icon
                pixmap = QPixmap(72, 72)
                pixmap.fill(Qt.GlobalColor.transparent)
                painter = QPainter(pixmap)
                painter.setRenderHint(QPainter.RenderHint.Antialiasing)
                
                # Rocket body
                painter.setBrush(QColor(37, 100, 207))
                painter.setPen(Qt.PenStyle.NoPen)
                painter.drawEllipse(20, 15, 32, 45)
                
                # Rocket tip
                painter.setBrush(QColor(220, 53, 69))
                points = [
                    (36, 15),  # tip
                    (28, 25),  # left
                    (44, 25)   # right
                ]
                polygon = QPolygon([QPoint(x, y) for x, y in points])
                painter.drawPolygon(polygon)
                
                # Flames
                painter.setBrush(QColor(255, 193, 7))
                flame_points = [
                    (30, 55), (36, 65), (42, 55)
                ]
                flame_polygon = QPolygon([QPoint(x, y) for x, y in flame_points])
                painter.drawPolygon(flame_polygon)
                
                # Window
                painter.setBrush(QColor(173, 216, 230))
                painter.drawEllipse(32, 30, 8, 8)
                
                painter.end()
                icon_label.setPixmap(pixmap)
        except Exception:
            # Simple fallback if icon creation fails
            icon_label.setText("🚀")
            icon_label.setStyleSheet("font-size: 48px;")
        
        icon_layout.addWidget(icon_label)
        header_layout.addWidget(icon_container)
        
        # Title section
        title_container = QWidget()
        title_layout = QVBoxLayout(title_container)
        title_layout.setContentsMargins(0, 0, 0, 0)
        title_layout.setSpacing(5)
        title_layout.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
        
        app_title = QLabel("Project Notes")
        app_title.setStyleSheet("""
            font-size: 28px; 
            font-weight: bold; 
            color: #2564cf; 
            margin: 0px;
            padding: 0px;
        """)
        title_layout.addWidget(app_title)
        
        header_layout.addWidget(title_container)
        header_layout.addStretch()
        
        main_layout.addWidget(header_widget)
        
        # Description section
        description_widget = QWidget()
        description_layout = QVBoxLayout(description_widget)
        description_layout.setContentsMargins(0, 0, 0, 0)
        description_layout.setSpacing(10)
        
        description = QLabel(
            "A powerful task management application with rich note-taking capabilities. "
            "Features inline editing, auto-save, image attachments, search functionality, and "
            "stakeholder management."
        )
        description.setWordWrap(True)
        description.setStyleSheet("""
            font-size: 14px; 
            color: #202124; 
            line-height: 1.5;
            padding: 0px;
            margin: 0px;
        """)
        description.setAlignment(Qt.AlignmentFlag.AlignTop)
        description_layout.addWidget(description)
        
        # Technical info
        tech_info = QLabel("Built with Python • PyQt6 • SQLite • Pillow")
        tech_info.setStyleSheet("""
            font-size: 12px; 
            color: #5f6368; 
            font-weight: 500;
            padding: 0px;
            margin: 0px;
        """)
        tech_info.setAlignment(Qt.AlignmentFlag.AlignCenter)
        description_layout.addWidget(tech_info)
        
        main_layout.addWidget(description_widget)
        
        # Add some flexible spacing
        main_layout.addSpacing(20)
        
        # Close button
        close_btn = QPushButton("Close")
        close_btn.setStyleSheet("""
            QPushButton {
                background: #2564cf;
                color: white;
                border: none;
                border-radius: 8px;
                font-size: 13px;
                font-weight: 600;
                padding: 10px 32px;
                min-width: 100px;
            }
            QPushButton:hover {
                background: #1e55b1;
            }
            QPushButton:pressed {
                background: #133d7a;
            }
        """)
        close_btn.clicked.connect(about_dialog.accept)
        main_layout.addWidget(close_btn, alignment=Qt.AlignmentFlag.AlignCenter)
        
        # Apply dialog styling with better cross-platform support
        about_dialog.setStyleSheet("""
            QDialog {
                background: #ffffff;
                border: 1px solid #e0e0e0;
            }
        """)
        
        # Set sensible size constraints and resize to content
        about_dialog.setMinimumSize(450, 380)
        about_dialog.setMaximumSize(600, 500)
        
        # Adjust size to content
        about_dialog.adjustSize()
        
        # Center the dialog properly
        try:
            parent_rect = self.geometry()
            dialog_rect = about_dialog.geometry()
            x = parent_rect.x() + (parent_rect.width() - dialog_rect.width()) // 2
            y = parent_rect.y() + (parent_rect.height() - dialog_rect.height()) // 2
            about_dialog.move(x, y)
        except Exception:
            # Fallback: center on screen
            screen = QApplication.primaryScreen().geometry()
            dialog_rect = about_dialog.geometry()
            x = (screen.width() - dialog_rect.width()) // 2
            y = (screen.height() - dialog_rect.height()) // 2
            about_dialog.move(x, y)
        
        about_dialog.exec()
    
    def show_pinned_overview(self):
        """Show the pinned tasks overview window"""
        overview = PinnedTasksOverview(self.db, self)
        overview.task_selected.connect(self.navigate_to_task)
        overview.show()

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

        # Ensure project list reflects cleared filters
        self.project_model.reload()

        # Find and select the project first
        project_row = self.project_model.row_for_project(project_id)
        if project_row >= 0:
            self.project_view.setCurrentIndex(self.project_model.index(project_row, 0))
            self.project_view.scrollTo(self.project_model.index(project_row, 0), QListView.ScrollHint.EnsureVisible)

            # Ensure the task model is updated for this project
            self.task_model.set_context(project_id, self.include_done, self.pinned_only)

            # Find and select the task after a brief delay to allow model updates
            QTimer.singleShot(100, lambda: self._select_task_after_project_change(task_id))
        else:
            # Project not found - it might be filtered out
            # Temporarily clear filters and try again
            original_include_done = self.include_done
            original_pinned_only = self.pinned_only

            # Enable all filters to make project visible
            if not self.include_done:
                self.toggle_show_completed(True)
            if self.pinned_only:
                self.toggle_pinned_only(False)

            # Try finding project again
            project_row = self.project_model.row_for_project(project_id)
            if project_row >= 0:
                self.project_view.setCurrentIndex(self.project_model.index(project_row, 0))
                self.project_view.scrollTo(self.project_model.index(project_row, 0), QListView.ScrollHint.EnsureVisible)
                self.task_model.set_context(project_id, self.include_done, self.pinned_only)
                QTimer.singleShot(100, lambda: self._select_task_after_project_change(task_id))
            else:
                # Restore original filter settings if project still not found
                if original_include_done != self.include_done:
                    self.toggle_show_completed(original_include_done)
                if original_pinned_only != self.pinned_only:
                    self.toggle_pinned_only(original_pinned_only)

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

    def mark_project_review_completed(self, project_id: int):
        """Clear the review highlight for a single project."""
        if not self.project_model.is_review_active():
            return
        self.project_model.mark_review_completed(project_id)

    def show_projects_header_context_menu(self, position):
        """Show a title-level context menu for project actions tied to the current selection."""
        menu = QMenu(self)
        remove_action = menu.addAction(self.act_remove_project.icon(), "Remove Project")
        remove_action.triggered.connect(self.remove_selected_project)
        remove_action.setEnabled(self.project_view.currentIndex().isValid())
        menu.exec(self.projects_header.mapToGlobal(position))

    def _reclaim_project_list_context_menu(self):
        """Restore app ownership of the project list context menu after addons hook into it."""
        try:
            self.project_view.customContextMenuRequested.disconnect()
        except TypeError:
            pass
        self.project_view.customContextMenuRequested.connect(self._show_combined_project_context_menu)

    def _resolve_project_context_request(self, position) -> Optional[Tuple[QModelIndex, int, QPoint]]:
        """Resolve the target project and popup position for the project list context menu."""
        index = self.project_view.indexAt(position)
        if not index.isValid():
            index = self.project_view.currentIndex()
        if not index.isValid():
            return None

        project_id = self.project_model.project_id_at(index.row())
        if project_id is None:
            return None

        self.project_view.setCurrentIndex(index)
        global_pos = self.project_view.viewport().mapToGlobal(position)
        return index, project_id, global_pos

    def _populate_project_context_core_actions(self, menu: QMenu, project_id: int):
        """Add built-in project actions before addon-provided entries."""
        review_pending = self._project_review_enabled() and self.project_model.is_project_in_review(project_id)
        if review_pending:
            review_action = menu.addAction("Review Completed")
            review_action.setIcon(self._make_green_check_icon())
            review_action.triggered.connect(
                lambda _checked=False, pid=project_id: self.mark_project_review_completed(pid)
            )

        feature_links_enabled = self._feature_links_enabled()
        current_link = self._project_feature_links.get(project_id) if feature_links_enabled else None
        if feature_links_enabled:
            if menu.actions():
                menu.addSeparator()

            if current_link:
                visit_action = menu.addAction("Visit feature link")
                visit_action.triggered.connect(
                    lambda _checked=False, pid=project_id: self._visit_project_feature_link(pid)
                )

            edit_action = menu.addAction("Edit links")
            edit_action.triggered.connect(
                lambda _checked=False, pid=project_id: self._edit_project_feature_link(pid)
            )

        visibility_mode = self._settings.value('Preferences/ProjectVisibilityMode', 'task_based')
        if visibility_mode == 'manual_based':
            is_hidden = self.db.is_project_hidden(project_id)
            if is_hidden is not None:
                if menu.actions():
                    menu.addSeparator()
                visibility_text = "Unhide" if is_hidden else "Hide"
                visibility_action = menu.addAction(self.act_toggle_project_visibility.icon(), visibility_text)
                visibility_action.triggered.connect(self.toggle_project_visibility)

    def _populate_project_context_addon_actions(self, menu: QMenu, project_id: int):
        """Append supported addon actions to the project list context menu."""
        para_addon = self.addons.get('project_link_addon')
        if para_addon is None or not hasattr(para_addon, '_edit_project_para_folder'):
            for addon in self.addons.values():
                if hasattr(addon, '_edit_project_para_folder'):
                    para_addon = addon
                    break

        if para_addon is None or not hasattr(para_addon, '_edit_project_para_folder'):
            return

        if menu.actions():
            menu.addSeparator()

        para_action = menu.addAction("Edit PARA folder")
        para_action.triggered.connect(
            lambda _checked=False, addon=para_addon, pid=project_id: addon._edit_project_para_folder(self, pid)
        )

    def _add_remove_project_context_action(self, menu: QMenu):
        """Append the destructive project-removal action at the end of the menu."""
        if menu.actions():
            menu.addSeparator()

        remove_action = menu.addAction(self.act_remove_project.icon(), "Remove Project")
        remove_action.triggered.connect(self.remove_selected_project)

    def _show_combined_project_context_menu(self, position):
        """Show the merged project list context menu with core and addon actions."""
        resolved = self._resolve_project_context_request(position)
        if resolved is None:
            return

        _index, project_id, global_pos = resolved
        menu = QMenu(self)
        self._populate_project_context_core_actions(menu, project_id)
        self._populate_project_context_addon_actions(menu, project_id)
        self._add_remove_project_context_action(menu)
        menu.exec(global_pos)

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
        heading.setStyleSheet("font-size: 15px; font-weight: 600; color: #202124;")
        layout.addWidget(heading)

        if project_title:
            subtitle = QLabel(f"Add a link for '{project_title}'.")
        else:
            subtitle = QLabel("Add a link for this project.")
        subtitle.setStyleSheet("color: #5f6368;")
        subtitle.setWordWrap(True)
        layout.addWidget(subtitle)

        link_edit = QLineEdit()
        link_edit.setPlaceholderText("https://example.com/feature")
        link_edit.setText(self._project_feature_links.get(project_id, ""))
        link_edit.setMinimumHeight(32)
        link_edit.setStyleSheet("""
            QLineEdit {
                padding: 6px 8px;
                border: 1px solid #d0d7de;
                border-radius: 4px;
                font-size: 13px;
            }
            QLineEdit:focus {
                border-color: #2564cf;
            }
        """)
        layout.addWidget(link_edit)

        button_box = QDialogButtonBox()

        clear_btn = button_box.addButton("Clear", QDialogButtonBox.ButtonRole.ActionRole)
        clear_btn.setStyleSheet("""
            QPushButton {
                background-color: #fff1f0;
                border: 1px solid #ffccc7;
                border-radius: 4px;
                padding: 6px 12px;
                color: #cf1322;
            }
            QPushButton:hover {
                background-color: #ffe7e5;
            }
        """)

        save_btn = button_box.addButton("Save", QDialogButtonBox.ButtonRole.AcceptRole)
        save_btn.setStyleSheet("""
            QPushButton {
                background-color: #2da44e;
                border: 1px solid #2da44e;
                border-radius: 4px;
                padding: 6px 16px;
                color: white;
                font-weight: 500;
            }
            QPushButton:hover {
                background-color: #2c974b;
            }
        """)

        cancel_btn = button_box.addButton(QDialogButtonBox.StandardButton.Cancel)
        cancel_btn.setStyleSheet("""
            QPushButton {
                background-color: #f6f8fa;
                border: 1px solid #d0d7de;
                border-radius: 4px;
                padding: 6px 16px;
                color: #24292f;
            }
            QPushButton:hover {
                background-color: #f3f4f6;
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
        snooze_menu = context_menu.addMenu("Snooze")
        snooze_menu.setIcon(self._make_snooze_icon())
        added = self._populate_task_snooze_menu(snooze_menu)
        if added:
            snooze_menu.addSeparator()
        # QAction.triggered passes a bool; accept it to avoid TypeError in lambda
        snooze_menu.addAction("Clear Snooze", lambda _checked=False: self.snooze_selected_tasks("clear"))
        
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

    def move_selected_tasks(self):
        """Move the selected task(s) to a different project."""
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
            
            # Show move dialog
            dialog = MoveTaskDialog(self.db, current_project_id, task_titles, self)
            if dialog.exec() != QDialog.DialogCode.Accepted:
                return
            
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
                # Refresh UI
                self.project_model.reload()  # Update project task counts
                
                # Navigate to target project after successful move
                target_project_row = self.project_model.row_for_project(target_project_id)
                if target_project_row >= 0:
                    # Select the target project
                    target_index = self.project_model.index(target_project_row, 0)
                    self.project_view.setCurrentIndex(target_index)
                    self.project_view.scrollTo(target_index, QListView.ScrollHint.EnsureVisible)
                    
                    # Update task model to show tasks from target project
                    if self.search_terms or self.search_stakeholder_terms:
                        # In search mode, reapply search to update results
                        self.apply_search_filter()
                    else:
                        # In normal mode, reload tasks for target project
                        self.task_model.set_context(target_project_id, self.include_done, self.pinned_only)
                        
                        # Try to select the first moved task if it's visible
                        if tasks_to_move:
                            first_moved_task_id = tasks_to_move[0]['id']
                            QTimer.singleShot(50, lambda: self._select_moved_task(first_moved_task_id))
                else:
                    # Fallback: clear task selection if target project not found in current view
                    self.task_view.clearSelection()
                    self.notes.set_task(None)
                    self._update_right_pane_state(False)
                
                # Update status bar
                self._update_comprehensive_status()
                
                # Show confirmation
                if moved_count == 1:
                    QMessageBox.information(
                        self, 
                        "Task Moved", 
                        f"Task '{task_titles[0]}' moved from '{current_project_name}' to '{target_project_name}'."
                    )
                else:
                    QMessageBox.information(
                        self, 
                        "Tasks Moved", 
                        f"{moved_count} tasks moved from '{current_project_name}' to '{target_project_name}'."
                    )
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
        if self.search_terms or self.search_stakeholder_terms:
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

    def snooze_selected_tasks(self, snooze_type: str):
        """Snooze selected tasks by setting their due date."""
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

            for row in selected_rows:
                task_id = self.task_model.task_id_at(row)
                if task_id:
                    self.db.set_task_due_date(task_id, date_str)
                    task_count += 1

            # Refresh the current task's due date display if selected
            if current_task_id:
                self.due_date_edit.blockSignals(True)
                if date_str:
                    date_part = date_str.split()[0]
                    year, month, day = map(int, date_part.split('-'))
                    self._current_due_date = QDate(year, month, day)
                    self.due_date_edit.setDate(self._current_due_date)
                    self._due_date_is_set = True
                else:
                    self._current_due_date = None
                    self.due_date_edit.setDate(QDate.currentDate())
                    self._due_date_is_set = False
                self.due_date_edit.blockSignals(False)

            if task_count:
                self._refresh_after_task_metadata_change(current_task_id)

        except Exception as e:
            QMessageBox.critical(self, "Snooze Tasks", f"Failed to snooze tasks: {e}")

    def add_project(self):
        idx = self.project_model.add_project("New Project")
        if idx >= 0:
            # Get the newly created project ID
            project_id = self.project_model.project_id_at(idx)
            
            # Apply default sort preference from settings
            if project_id is not None:
                default_manual_sort = self._settings.value('Preferences/DefaultManualSort', 'false') in ('true', '1', 'True')
                self.db.set_manual_sort_enabled(project_id, default_manual_sort)
            
            self.project_view.setCurrentIndex(self.project_model.index(idx, 0))
            self.project_view.edit(self.project_model.index(idx, 0))

    def edit_selected_project(self):
        idx = self.project_view.currentIndex()
        if idx.isValid():
            self.project_view.edit(idx)

    def remove_selected_project(self):
        idx = self.project_view.currentIndex()
        if idx.isValid():
            row = idx.row()
            title = self.project_model.data(idx)
            project_id = self.project_model.project_id_at(row)
            
            if project_id is None:
                return
            
            # Check if project has any tasks
            try:
                task_count = self.db.conn.execute(
                    "SELECT COUNT(*) FROM tasks WHERE project_id=?", 
                    (project_id,)
                ).fetchone()[0]
            except Exception:
                task_count = 0
            
            # First confirmation dialog
            first_msg = f"Delete project '{title}'"
            if task_count > 0:
                first_msg += f" and all its {task_count} task{'s' if task_count != 1 else ''}?"
            else:
                first_msg += "?"
            
            first_confirmation = QMessageBox.question(
                self, 
                "Remove Project", 
                first_msg
            )
            
            if first_confirmation != QMessageBox.StandardButton.Yes:
                return
            
            # Second confirmation for projects with tasks
            if task_count > 0:
                second_msg = QMessageBox(
                    QMessageBox.Icon.Warning,
                    "FINAL WARNING - PROJECT DELETION",
                    f"YOU ARE ABOUT TO PERMANENTLY DELETE:\n\n"
                    f"PROJECT: '{title}'\n"
                    f"TASKS: {task_count} task{'s' if task_count != 1 else ''} will be LOST FOREVER\n\n"
                    f"THIS ACTION CANNOT BE UNDONE!\n\n"
                    f"Are you absolutely certain you want to DELETE this project and ALL its tasks?",
                    QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                    self
                )
                
                # Make the dialog more prominent
                second_msg.setDefaultButton(QMessageBox.StandardButton.No)
                
                # Customize button text for clarity
                yes_button = second_msg.button(QMessageBox.StandardButton.Yes)
                no_button = second_msg.button(QMessageBox.StandardButton.No)
                if yes_button:
                    yes_button.setText("DELETE PERMANENTLY")
                if no_button:
                    no_button.setText("Cancel (Safe Choice)")
                
                if second_msg.exec() != QMessageBox.StandardButton.Yes:
                    return
            
            # Proceed with deletion
            self.project_model.remove_project_at(row)
            if project_id in self._project_feature_links:
                self._project_feature_links.pop(project_id, None)
                self._save_project_feature_links()
            self.task_model.set_context(None, self.include_done)
            self.notes.set_task(None)
            # Lock the right pane since no task is selected after project removal
            self._update_right_pane_state(False)

    def add_task(self):
        if self.task_model.project_id is None:
            QMessageBox.information(self, "Add Task", "Select a project first.")
            return
        
        # Prevent overlapping task additions
        if self._adding_task:
            return
            
        self._adding_task = True
        
        try:
            # Close any active editors to prevent conflicts
            self._close_active_editors()
            
            # Preserve current focus if any task is selected
            current_task_id = None
            current_idx = self.task_view.currentIndex()
            if current_idx.isValid():
                current_task_id = self.task_model.task_id_at(current_idx.row())
            
            # Create the task first (force_visibility=True is set automatically in DB)
            task_id = self.db.add_task(self.task_model.project_id, "New Task")
            
            # Initialize task based on active search/filters
            task_updates = {}
            stakeholders_to_add = []
            notes_content = ""
            
            # Handle search mode: copy search query to notes and extract stakeholders
            if self.search_terms or self.search_stakeholder_terms:
                search_parts = []
                
                # Add general search terms
                if self.search_terms:
                    search_parts.extend(self.search_terms)
                
                # Add stakeholder search terms and extract stakeholders
                if self.search_stakeholder_terms:
                    for term in self.search_stakeholder_terms:
                        search_parts.append(f"@{term}")
                        stakeholders_to_add.append(term)
            
            # Note: Removed automatic pinning when pinned_only=True
            # New tasks are created unpinned and will be visible due to force_visibility
            # Users can manually pin tasks if needed
            
            # Apply task updates if any
            if task_updates:
                self.db.update_task(task_id, **task_updates)
            
            # Set stakeholders if any were extracted from search
            if stakeholders_to_add:
                self.db.set_stakeholders_for_task(task_id, stakeholders_to_add)
            
            # Set notes content if we have any
            if notes_content:
                # Get existing notes (should be empty for new task)
                existing_html, existing_plain = self.db.get_note(task_id)
                # Combine with search info
                new_html = notes_content + (existing_html or "")
                new_plain = f"Created from search: {' '.join(search_parts)}\n" + (existing_plain or "")
                self.db.save_note(task_id, new_html, new_plain, self.attach_dir)
            
            # Handle different update scenarios
            if self.search_terms or self.search_stakeholder_terms:
                # In search mode, reapply search to include new task
                QTimer.singleShot(50, lambda: self._select_task_after_search_refresh(task_id))
                self.apply_search_filter()
            else:
                # In normal mode, use optimized reload and selection
                self._add_task_to_model_optimized(task_id, current_task_id)
                
        except Exception as e:
            print(f"Error adding task: {e}")
            QMessageBox.critical(self, "Add Task", f"Failed to add task: {e}")
        finally:
            # Always clear the flag to allow future additions
            QTimer.singleShot(100, lambda: setattr(self, '_adding_task', False))

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
        if self.search_terms or self.search_stakeholder_terms:
            self._preserve_project_id = preferred_project_id
            self._preserve_task_id = None
            self.apply_search_filter()
            QTimer.singleShot(75, lambda pid=preferred_project_id, row=anchor_row: self._select_task_row_after_delete(pid, row))
            return

        self.project_model.reload()

        selected_project_id = None
        if preferred_project_id is not None:
            project_row = self.project_model.row_for_project(preferred_project_id)
            if project_row >= 0:
                project_index = self.project_model.index(project_row, 0)
                self.project_view.setCurrentIndex(project_index)
                self.project_view.scrollTo(project_index, QListView.ScrollHint.EnsureVisible)
                selected_project_id = preferred_project_id

        if selected_project_id is None:
            selected_project_id = self._select_fallback_project()

        self.task_model.set_context(selected_project_id, self.include_done, self.pinned_only)
        QTimer.singleShot(25, lambda pid=selected_project_id, row=anchor_row: self._select_task_row_after_delete(pid, row))

    def _select_task_row_after_delete(self, preferred_project_id: Optional[int], anchor_row: int):
        """Select the nearest remaining visible task after a delete operation."""
        if preferred_project_id is not None:
            current_idx = self.project_view.currentIndex()
            current_pid = self.project_model.project_id_at(current_idx.row()) if current_idx.isValid() else None
            if current_pid != preferred_project_id:
                project_row = self.project_model.row_for_project(preferred_project_id)
                if project_row >= 0:
                    project_index = self.project_model.index(project_row, 0)
                    self.project_view.setCurrentIndex(project_index)
                    self.project_view.scrollTo(project_index, QListView.ScrollHint.EnsureVisible)

        if self.task_model.rowCount() <= 0:
            self._clear_current_task_context()
            self._update_comprehensive_status()
            return

        target_row = max(0, min(anchor_row, self.task_model.rowCount() - 1))
        if not self._select_task_row(target_row):
            self._clear_current_task_context()
        self._update_comprehensive_status()

    def _refresh_after_task_metadata_change(self, preferred_task_id: Optional[int] = None):
        """Refresh task/project views after due-date or effort changes."""
        current_project_index = self.project_view.currentIndex()
        current_project_id = self.project_model.project_id_at(current_project_index.row()) if current_project_index.isValid() else None
        current_project_row = current_project_index.row() if current_project_index.isValid() else 0

        if preferred_task_id is None:
            current_task_index = self.task_view.currentIndex()
            preferred_task_id = self.task_model.task_id_at(current_task_index.row()) if current_task_index.isValid() else None

        if self.search_terms or self.search_stakeholder_terms:
            self._preserve_current_focus()
            self._preserve_project_id = current_project_id
            if preferred_task_id is not None:
                self._preserve_task_id = preferred_task_id
            self.apply_search_filter()
            return

        if self.show_no_due_dates or self.show_effort_missing:
            self.project_model.refresh_visibility()
            if current_project_id is not None:
                row = self.project_model.row_for_project(current_project_id)
                if row >= 0:
                    project_index = self.project_model.index(row, 0)
                    self.project_view.setCurrentIndex(project_index)
                    self.project_view.scrollTo(project_index, QListView.ScrollHint.EnsureVisible)
                elif self.project_model.rowCount() > 0:
                    self._select_project_row_preserving_slot(current_project_row)
            elif self.project_model.rowCount() > 0 and not self.project_view.currentIndex().isValid():
                self._select_project_row_preserving_slot(0)

            selected_project_index = self.project_view.currentIndex()
            selected_project_id = self.project_model.project_id_at(selected_project_index.row()) if selected_project_index.isValid() else None
            self.task_model.set_context(selected_project_id, self.include_done, self.pinned_only)
        else:
            self.task_model.reload()

        if preferred_task_id is not None:
            QTimer.singleShot(50, lambda tid=preferred_task_id: self._select_task_if_visible(tid) or self._select_nearest_task())
        else:
            QTimer.singleShot(50, self._select_nearest_task)
        self._update_comprehensive_status()

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

            if self.search_terms or self.search_stakeholder_terms:
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
        selected_rows = []
        selection_model = self.task_view.selectionModel()
        if selection_model:
            selected_indexes = selection_model.selectedRows()
            selected_rows = sorted({idx.row() for idx in selected_indexes if idx.isValid()})
        
        if not selected_rows:
            QMessageBox.information(self, "Remove Task(s)", "No tasks selected.")
            return
        
        # Get task titles for confirmation message
        task_titles = []
        for row in selected_rows:
            title = self.task_model.data(self.task_model.index(row, TaskTableModel.COL_TITLE))
            task_titles.append(title)
        
        # Show confirmation dialog
        if len(selected_rows) == 1:
            message = f"Delete task '{task_titles[0]}'?"
        else:
            message = f"Delete {len(selected_rows)} selected tasks?"
        
        if QMessageBox.question(self, "Remove Task(s)", message) == QMessageBox.StandardButton.Yes:
            current_project_id = self.task_model.project_id
            anchor_row = selected_rows[0]

            # Get task IDs before removal (sort by row descending to remove from bottom up)
            task_ids = []
            for row in sorted(selected_rows, reverse=True):
                tid = self.task_model.task_id_at(row)
                if tid is not None:
                    task_ids.append(tid)
            
            # Remove tasks from database
            for tid in task_ids:
                self.db.remove_task(tid)

            self._restore_task_selection_after_delete(current_project_id, anchor_row)

    def mark_task_complete(self):
        selected_rows = []
        selection_model = self.task_view.selectionModel()
        if selection_model:
            selected_indexes = selection_model.selectedRows()
            selected_rows = [idx.row() for idx in selected_indexes if idx.isValid()]
        
        if not selected_rows:
            QMessageBox.information(self, "Mark as Complete", "No tasks selected.")
            return
        
        # Store current focus before making changes
        self._preserve_current_focus()
        
        # Get task IDs and their current completion status
        tasks_to_complete = []
        for row in selected_rows:
            tid = self.task_model.task_id_at(row)
            if tid is not None and row < len(self.task_model.rows):
                task_row = self.task_model.rows[row]
                if not bool(task_row['done']):
                    tasks_to_complete.append({
                        'id': tid,
                        'row': row,
                        'title': task_row['title'],
                        'project_id': task_row['project_id']
                    })
        
        if not tasks_to_complete:
            QMessageBox.information(self, "Mark as Complete", "Selected tasks are already completed.")
            return
        
        # Mark tasks as complete in database
        completed_count = 0
        project_ids_affected = set()
        for task_info in tasks_to_complete:
            try:
                self.db.update_task(task_info['id'], done=True)
                completed_count += 1
                project_ids_affected.add(task_info['project_id'])
            except Exception as e:
                print(f"Error marking task {task_info['id']} as complete: {e}")
        
        if completed_count > 0:
            # Update project model stats for affected projects
            removed_project_info = None
            current_project_idx = self.project_view.currentIndex()
            current_project_id = None
            current_project_row = -1
            if current_project_idx.isValid():
                current_project_id = self.project_model.project_id_at(current_project_idx.row())
                current_project_row = current_project_idx.row()
            
            # Process each project and track if any were removed
            # Sort by project_id to ensure consistent processing order
            for project_id in sorted(project_ids_affected):
                # Re-check if this project still exists in the model
                # (it might have been removed in a previous iteration)
                if project_id not in self.project_model._index_by_id:
                    continue
                
                result = self.project_model.apply_task_done_toggle(project_id, True)
                
                # Check if the currently selected project was removed
                if result and result[0] == 'removed' and result[1] == current_project_id:
                    removed_project_info = result
            
            # If the current project was removed, select the new project FIRST
            # before reloading tasks
            if removed_project_info is not None:
                removed_row = removed_project_info[2]  # Get the row index before removal
                target_row = current_project_row if current_project_row >= 0 else removed_row
                # Keep selection at the same visual slot; helper handles empty list case
                self._select_project_row_preserving_slot(target_row)
                
                # Update the preserved project ID to the newly selected project
                # so _restore_preserved_focus doesn't try to restore the removed one
                def _update_preserved():
                    new_idx = self.project_view.currentIndex()
                    self._preserve_project_id = self.project_model.project_id_at(new_idx.row()) if new_idx.isValid() else None
                    self._preserve_task_id = None  # Clear task since project changed
                    # Manually trigger the project selection logic to load tasks
                    self.on_project_selected()
                # Run after the selection helper to ensure the current index is updated
                QTimer.singleShot(10, _update_preserved)
            else:
                # No project removed, handle task model update normally
                if self.include_done:
                    # If showing completed tasks, just refresh to update styling
                    self.task_model.reload()
                else:
                    # If hiding completed tasks, they should be removed from view
                    # Use a more targeted update approach
                    if self.search_terms or self.search_stakeholder_terms:
                        # In search mode, reapply search filter
                        QTimer.singleShot(25, self.apply_search_filter)
                    else:
                        # In normal mode, reload the task model for current project
                        self.task_model.reload()
            
            # If the current project vanished but we didn't get a removal result (e.g., due to a reload),
            # keep the selection anchored at the same visual row when possible.
            if removed_project_info is None and current_project_id is not None:
                if self.project_model.row_for_project(current_project_id) == -1:
                    target_row = current_project_row if current_project_row >= 0 else 0
                    self._select_project_row_preserving_slot(target_row)
            
            # Restore focus after updates complete
            # BUT skip this if we removed the current project (we already selected a new one)
            if removed_project_info is None:
                QTimer.singleShot(50, self._restore_preserved_focus)
            
            # Ensure project-task relationship is maintained after completion
            # BUT skip this if we removed the current project (we already selected a new one)
            if completed_count > 0 and removed_project_info is None:
                current_idx = self.task_view.currentIndex()
                if current_idx.isValid():
                    current_task_id = self.task_model.task_id_at(current_idx.row())
                    if current_task_id:
                        QTimer.singleShot(75, lambda: self._ensure_project_selected_for_task(current_task_id))
            
            # Only show confirmation for multiple tasks
            if completed_count > 1:
                QMessageBox.information(self, "Mark as Complete", f"{completed_count} tasks marked as complete.")
        else:
            QMessageBox.information(self, "Mark as Complete", "Failed to mark any tasks as complete.")

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
        if is_manual:
            self.task_view.setDragDropMode(QTableView.DragDropMode.InternalMove)
            self.task_view.setDefaultDropAction(Qt.DropAction.MoveAction)
        else:
            self.task_view.setDragDropMode(QTableView.DragDropMode.NoDragDrop)
        
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
        if checked:
            self.task_view.setDragDropMode(QTableView.DragDropMode.InternalMove)
            self.task_view.setDefaultDropAction(Qt.DropAction.MoveAction)
        else:
            self.task_view.setDragDropMode(QTableView.DragDropMode.NoDragDrop)
        
        # Update combo box to stay in sync
        self.sort_order_combo.blockSignals(True)
        self.sort_order_combo.setCurrentIndex(1 if checked else 0)
        self.sort_order_combo.blockSignals(False)
        
        # Reload tasks to apply new sort order
        self.task_model.reload()
        
        # Update status
        status = "enabled" if checked else "disabled"
        QMessageBox.information(self, "Manual Sort", f"Manual sort override {status} for this project.")
    
    def toggle_project_visibility(self):
        """Toggle the hidden flag for the currently selected project."""
        # Get current project
        idx = self.project_view.currentIndex()
        if not idx.isValid():
            QMessageBox.information(self, "Toggle Visibility", "Please select a project first.")
            return
        
        project_id = self.project_model.project_id_at(idx.row())
        if project_id is None:
            return
        
        # Store the current row index and total rows before toggling
        current_row = idx.row()
        total_rows_before = self.project_model.rowCount(QModelIndex())
        
        # Toggle the hidden flag in database
        new_hidden = self.db.toggle_project_hidden(project_id)

        model_row = self.project_model.row_for_project(project_id)
        if 0 <= model_row < len(self.project_model.rows):
            self.project_model.rows[model_row]['hidden'] = 1 if new_hidden else 0
        
        # Show status message
        status = "hidden" if new_hidden else "visible"
        project_title = self.project_model.rows[idx.row()]['title']
        
        # Refresh the project list to apply the visibility change
        # Only if we're in "Hide Done" mode and using manual visibility mode
        visibility_mode = self._settings.value('Preferences/ProjectVisibilityMode', 'task_based')
        if not self.include_done and visibility_mode == 'manual_based':
            self.project_model.reload()
            
            # If the project was hidden and the list changed, select the appropriate project
            total_rows_after = self.project_model.rowCount(QModelIndex())
            if new_hidden and total_rows_after < total_rows_before and total_rows_after > 0:
                # Project was hidden - keep the same index (project below moves up)
                # If this was the last project, select the new last project
                new_row = min(current_row, total_rows_after - 1)
                self.project_view.setCurrentIndex(self.project_model.index(new_row, 0))
        
        QMessageBox.information(
            self, 
            "Toggle Visibility", 
            f"Project '{project_title}' is now {status}.\n\n"
            f"Note: This will only affect visibility when:\n"
            f"• 'Hide Done' is enabled (View menu)\n"
            f"• 'Manual Visibility Control' mode is selected in Preferences"
        )
    
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
        if is_manual:
            self.task_view.setDragDropMode(QTableView.DragDropMode.InternalMove)
            self.task_view.setDefaultDropAction(Qt.DropAction.MoveAction)
        else:
            self.task_view.setDragDropMode(QTableView.DragDropMode.NoDragDrop)

    def toggle_show_completed(self, checked: bool):
        # Clear force_visibility when filter changes
        self.db.clear_all_force_visibility()
        
        self.include_done = checked
        if hasattr(self, 'cb_show_completed'):
            self.cb_show_completed.blockSignals(True)
            self.cb_show_completed.setChecked(checked)
            self.cb_show_completed.blockSignals(False)
        
        # Update task model context
        current_project_id = self.task_model.project_id
        self.task_model.set_context(current_project_id, self.include_done, self.pinned_only)
        
        # Update projects visibility too
        self.project_model.set_include_done(self.include_done)
        # Update status bar
        self._update_comprehensive_status()

    def toggle_pinned_only(self, checked: bool):
        # Clear force_visibility when filter changes
        self.db.clear_all_force_visibility()
        
        self.pinned_only = checked
        self.task_model.set_context(self.task_model.project_id, self.include_done, self.pinned_only)
        # Update projects visibility too
        self.project_model.set_pinned_only(self.pinned_only)
        # Save pinned-only preference to settings
        try:
            self._settings.setValue('MainWindow/PinnedOnly', 'true' if checked else 'false')
            self._settings.sync()
        except Exception:
            pass
        # Update status bar
        self._update_comprehensive_status()

    def toggle_show_no_due_dates(self, checked: bool):
        """Toggle filter to show only tasks without a due date."""
        self.db.clear_all_force_visibility()
        
        self.show_no_due_dates = checked
        self.task_model.show_no_due_dates = checked
        self.project_model.show_no_due_dates = checked
        
        # Apply filter by reloading both tasks and projects
        self.project_model.reload()
        self.task_model.reload()
        self._update_comprehensive_status()

    def toggle_show_effort_missing(self, checked: bool):
        """Toggle filter to show only tasks with 0 effort."""
        self.db.clear_all_force_visibility()
        
        self.show_effort_missing = checked
        self.task_model.show_effort_missing = checked
        self.project_model.show_effort_missing = checked
        
        # Apply filter by reloading both tasks and projects
        self.project_model.reload()
        self.task_model.reload()
        self._update_comprehensive_status()

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
        """Enable/disable the right pane based on task selection"""
        self.notes.setEnabled(has_task_selected)
        if hasattr(self, 'notes_format_toolbar') and self.notes_format_toolbar is not None:
            self.notes_format_toolbar.setEnabled(has_task_selected)
        self.due_date_button.setEnabled(has_task_selected)
        self.effort_button.setEnabled(has_task_selected)
        self.recurrence_button.setEnabled(has_task_selected)
        self.stakeholders_edit.setEnabled(has_task_selected)
        if hasattr(self, 'act_recurrence'):
            self.act_recurrence.setEnabled(has_task_selected)
        
        # Update visual styling to indicate locked state
        if has_task_selected:
            self.stakeholders_edit.setPlaceholderText("Stakeholders: separate by space, comma, or semicolon")
            # Reset to normal styling
            self.notes.setStyleSheet("")
            self.stakeholders_edit.setStyleSheet("")
        else:
            self._current_recurrence = None
            self._update_recurrence_button_text()
            self.stakeholders_edit.setPlaceholderText("Select a task to edit stakeholders and notes")
            self.stakeholders_edit.clear()
            # Apply locked styling
            locked_style = """
                QTextEdit:disabled {
                    background-color: #f8f9fa;
                    color: #6c757d;
                    border: 1px solid #dee2e6;
                }
            """
            locked_line_style = """
                QLineEdit:disabled {
                    background-color: #f8f9fa;
                    color: #6c757d;
                    border: 1px solid #dee2e6;
                }
            """
            self.notes.setStyleSheet(locked_style)
            self.stakeholders_edit.setStyleSheet(locked_line_style)

    def toggle_always_on_top(self, checked: bool):
        self._apply_always_on_top(checked)
        try:
            self._settings.setValue('MainWindow/AlwaysOnTop', 'true' if checked else 'false')
            self._settings.sync()
        except Exception:
            pass

    def on_project_selected(self):
        idx = self.project_view.currentIndex()
        pid = self.project_model.project_id_at(idx.row()) if idx.isValid() else None
        if (self.search_terms or self.search_stakeholder_terms):
            self._suppress_tasks_refresh = True
            self.task_model.beginResetModel()
            if pid is None:
                self.task_model.rows = []
            else:
                self.task_model.rows = self._search_results_by_project.get(pid, [])
                self.task_model.project_id = pid
            self.task_model.endResetModel()
            self._suppress_tasks_refresh = False
            # Don't clear notes/task selection during search mode project changes
            # This allows focus restoration to work properly
        else:
            # Normal mode: properly set context and reload
            self.task_model.set_context(pid, self.include_done, self.pinned_only)
            self.notes.set_task(None)
            # Update right pane state - no task selected when project changes in normal mode
            self._update_right_pane_state(False)
            # Reset due date state
            self._due_date_is_set = False
            self._current_due_date = None
            self._update_due_date_button_text()
            # Reset effort state
            self._current_effort = 0.0
            self._update_effort_button_text()
            self.stakeholders_edit.blockSignals(True)
            self.stakeholders_edit.setText("")
            self.stakeholders_edit.blockSignals(False)
        
        # Update status bar to reflect current project selection
        self._update_comprehensive_status()
        
        # Update manual sort action state based on selected project
        self.update_manual_sort_action_state()
        self._update_feature_link_for_project(pid)
        if pid is not None and self.project_model.is_project_in_review(pid) and self._project_review_auto_mark_enabled():
            self.mark_project_review_completed(pid)

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
        
        # Update status bar with task creation date
        self._update_task_status(tid)
        
        self.notes.set_task(tid)
        
        # Load due date
        if tid is None:
            self._due_date_is_set = False
            self._current_due_date = None
        else:
            due_date_str = self.db.get_task_due_date(tid)
            if due_date_str:
                # Parse ISO format date (YYYY-MM-DD or YYYY-MM-DD HH:MM:SS)
                date_part = due_date_str.split()[0] if ' ' in due_date_str else due_date_str
                year, month, day = map(int, date_part.split('-'))
                self._current_due_date = QDate(year, month, day)
                self.due_date_edit.setDate(self._current_due_date)  # Keep hidden edit in sync
                self._due_date_is_set = True
            else:
                self._current_due_date = None
                self._due_date_is_set = False
        
        # Update button text
        self._update_due_date_button_text()
        
        # Load effort
        if tid is None:
            self._current_effort = 0.0
        else:
            self._current_effort = self.db.get_task_effort(tid)
        self._update_effort_button_text()

        # Load recurrence
        if tid is None:
            self._current_recurrence = None
        else:
            self._current_recurrence = self.db.get_task_recurrence(tid)
        self._update_recurrence_button_text()
        
        # Load stakeholders
        self.stakeholders_edit.blockSignals(True)
        if tid is None:
            self.stakeholders_edit.setText("")
        else:
            names = self.db.get_stakeholders_for_task(tid)
            self.stakeholders_edit.setText("; ".join(names))
        self.stakeholders_edit.blockSignals(False)
    
    def _open_due_date_picker(self):
        """Open a popup dialog with calendar for selecting due date."""
        idx = self.task_view.currentIndex()
        tid = self.task_model.task_id_at(idx.row()) if idx.isValid() else None
        if tid is None:
            return
        
        # Create popup dialog
        dialog = QDialog(self)
        dialog.setWindowTitle("Select Due Date")
        dialog.setModal(True)
        dialog.setMinimumWidth(350)
        
        layout = QVBoxLayout(dialog)
        layout.setContentsMargins(12, 12, 12, 12)
        layout.setSpacing(12)
        
        # Calendar widget
        calendar = QDateEdit()
        calendar.setCalendarPopup(True)
        calendar.setDisplayFormat("yyyy-MM-dd")
        calendar.setMinimumDate(QDate(2000, 1, 1))
        calendar.setMaximumDate(QDate(2100, 12, 31))
        
        # Set current date value
        if self._due_date_is_set and self._current_due_date:
            calendar.setDate(self._current_due_date)
        else:
            calendar.setDate(QDate.currentDate())
        
        # Style the calendar
        cal_widget = calendar.calendarWidget()
        if cal_widget:
            cal_widget.setFirstDayOfWeek(Qt.DayOfWeek.Monday)
        
        layout.addWidget(calendar)
        
        # Quick date buttons
        button_widget = QWidget()
        button_layout = QHBoxLayout(button_widget)
        button_layout.setContentsMargins(0, 0, 0, 0)
        button_layout.setSpacing(6)
        
        btn_style = """
            QPushButton {
                background-color: #f6f8fa;
                border: 1px solid #d0d7de;
                border-radius: 4px;
                padding: 6px 10px;
                font-size: 12px;
                color: #24292f;
            }
            QPushButton:hover {
                background-color: #f3f4f6;
                border-color: #a5b3c0;
            }
            QPushButton:pressed {
                background-color: #e8eaed;
            }
        """
        
        # Today button
        today_btn = QPushButton("Today")
        today_btn.setStyleSheet(btn_style)
        today_btn.clicked.connect(lambda: calendar.setDate(QDate.currentDate()))
        button_layout.addWidget(today_btn)
        
        # Tomorrow button
        tomorrow_btn = QPushButton("Tomorrow")
        tomorrow_btn.setStyleSheet(btn_style)
        tomorrow_btn.clicked.connect(lambda: calendar.setDate(QDate.currentDate().addDays(1)))
        button_layout.addWidget(tomorrow_btn)
        
        # Next Monday button
        next_week_btn = QPushButton("Next Monday")
        next_week_btn.setStyleSheet(btn_style)
        def set_next_monday():
            current = QDate.currentDate()
            current_day = current.dayOfWeek()
            if current_day == 1:  # If today is Monday
                days_to_add = 7
            else:
                days_to_add = 8 - current_day
            calendar.setDate(current.addDays(days_to_add))
        next_week_btn.clicked.connect(set_next_monday)
        button_layout.addWidget(next_week_btn)
        
        layout.addWidget(button_widget)
        
        # Dialog buttons
        button_box = QDialogButtonBox()
        
        # Clear button (left-aligned)
        clear_btn = button_box.addButton("Clear", QDialogButtonBox.ButtonRole.ActionRole)
        clear_btn.setStyleSheet("""
            QPushButton {
                background-color: #fff1f0;
                border: 1px solid #ffccc7;
                border-radius: 4px;
                padding: 6px 12px;
                color: #cf1322;
            }
            QPushButton:hover {
                background-color: #ffe7e5;
            }
        """)
        
        # OK and Cancel buttons (right-aligned)
        ok_btn = button_box.addButton(QDialogButtonBox.StandardButton.Ok)
        cancel_btn = button_box.addButton(QDialogButtonBox.StandardButton.Cancel)
        
        ok_btn.setStyleSheet("""
            QPushButton {
                background-color: #2da44e;
                border: 1px solid #2da44e;
                border-radius: 4px;
                padding: 6px 16px;
                color: white;
                font-weight: 500;
            }
            QPushButton:hover {
                background-color: #2c974b;
            }
        """)
        
        cancel_btn.setStyleSheet("""
            QPushButton {
                background-color: #f6f8fa;
                border: 1px solid #d0d7de;
                border-radius: 4px;
                padding: 6px 16px;
                color: #24292f;
            }
            QPushButton:hover {
                background-color: #f3f4f6;
            }
        """)
        
        def on_clear():
            """Clear the due date."""
            self._due_date_is_set = False
            self._current_due_date = None
            self._update_due_date_button_text()
            self.db.set_task_due_date(tid, None)
            self._refresh_after_task_metadata_change(tid)
            dialog.reject()
        
        clear_btn.clicked.connect(on_clear)
        button_box.accepted.connect(dialog.accept)
        button_box.rejected.connect(dialog.reject)
        
        layout.addWidget(button_box)
        
        # Show dialog and process result
        if dialog.exec() == QDialog.DialogCode.Accepted:
            selected_date = calendar.date()
            self._due_date_is_set = True
            self._current_due_date = selected_date
            self.due_date_edit.setDate(selected_date)  # Update hidden edit for consistency
            self._update_due_date_button_text()
            self._due_date_save_timer.start()
    
    def _update_due_date_button_text(self):
        """Update the due date button text based on current state."""
        if self._due_date_is_set and self._current_due_date:
            date_str = self._current_due_date.toString("yyyy-MM-dd")
            # Calculate week number and day of week
            year = self._current_due_date.year()
            week_number, _ = self._current_due_date.weekNumber()  # weekNumber() returns (week, year)
            day_of_week = self._current_due_date.dayOfWeek()  # 1=Monday, 7=Sunday
            # Format: "2025-11-22 / wk2547.6"
            week_info = f"wk{year % 100:02d}{week_number:02d}.{day_of_week}"
            full_text = f"{date_str} / {week_info}"
            self.due_date_button.setText(full_text)
        else:
            self.due_date_button.setText("Set due date")
    
    def _update_effort_button_text(self):
        """Update the effort button text based on current effort value."""
        if self._current_effort == 0.5:
            self.effort_button.setText("0.5")
        elif self._current_effort == int(self._current_effort):
            self.effort_button.setText(str(int(self._current_effort)))
        else:
            self.effort_button.setText(str(self._current_effort))

    def _update_recurrence_button_text(self):
        """Update the recurrence button text based on current recurrence settings."""
        if not self._current_recurrence or not self._current_recurrence.get('recurring'):
            self.recurrence_button.setText("Set recurrence")
            self.recurrence_button.setToolTip("Configure task recurrence")
            return

        rule = _parse_recurrence_rule(self._current_recurrence.get('recurrence_rule'))
        summary = _format_recurrence_summary(rule)
        display_text = summary if len(summary) <= 40 else "Edit recurrence"
        self.recurrence_button.setText(display_text)
        tooltip = _format_recurrence_tooltip(rule, self._current_recurrence.get('recurrence_next_at'))
        self.recurrence_button.setToolTip(tooltip or "Edit recurrence")

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
            "time": RECURRENCE_DEFAULT_TIME
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
        title_label.setStyleSheet("color: #5f6368; font-size: 12px;")
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
        mode_label = QLabel("Mode")
        mode_label.setFixedWidth(100)
        mode_combo = QComboBox()
        mode_combo.addItem("Fixed schedule", RECURRENCE_MODE_FIXED)
        mode_combo.addItem("After completion", RECURRENCE_MODE_COMPLETION)
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
        pattern_label = QLabel("Pattern")
        pattern_label.setFixedWidth(100)
        pattern_combo = QComboBox()
        pattern_combo.addItem("Every N days", RECURRENCE_FREQ_EVERY_N_DAYS)
        pattern_combo.addItem("Daily", RECURRENCE_FREQ_DAILY)
        pattern_combo.addItem("Weekly", RECURRENCE_FREQ_WEEKLY)
        pattern_combo.addItem("Monthly", RECURRENCE_FREQ_MONTHLY)
        current_freq = rule.get("freq", RECURRENCE_FREQ_EVERY_N_DAYS)
        pattern_index = pattern_combo.findData(current_freq)
        pattern_combo.setCurrentIndex(pattern_index if pattern_index >= 0 else 0)
        pattern_layout.addWidget(pattern_label)
        pattern_layout.addWidget(pattern_combo, stretch=1)
        settings_layout.addWidget(pattern_row)

        # Interval row
        interval_row = QWidget()
        interval_layout = QHBoxLayout(interval_row)
        interval_layout.setContentsMargins(0, 0, 0, 0)
        interval_layout.setSpacing(8)
        interval_label = QLabel("Interval")
        interval_label.setFixedWidth(100)
        interval_spin = QSpinBox()
        interval_spin.setRange(1, 365)
        interval_unit_label = QLabel("days")
        interval_layout.addWidget(interval_label)
        interval_layout.addWidget(interval_spin)
        interval_layout.addWidget(interval_unit_label)
        interval_layout.addStretch(1)
        settings_layout.addWidget(interval_row)

        # Weekly weekdays row
        weekdays_row = QWidget()
        weekdays_layout = QHBoxLayout(weekdays_row)
        weekdays_layout.setContentsMargins(0, 0, 0, 0)
        weekdays_layout.setSpacing(6)
        weekdays_label = QLabel("Weekdays")
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
        month_label = QLabel("Day of month")
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
        start_label = QLabel("Starts on")
        start_label.setFixedWidth(100)
        start_date_edit = QDateEdit()
        start_date_edit.setCalendarPopup(True)
        start_date_edit.setDisplayFormat("yyyy-MM-dd")
        start_date_edit.setDate(QDate(start_dt.year, start_dt.month, start_dt.day))
        start_layout.addWidget(start_label)
        start_layout.addWidget(start_date_edit, stretch=1)
        settings_layout.addWidget(start_row)

        # Time row
        time_row = QWidget()
        time_layout = QHBoxLayout(time_row)
        time_layout.setContentsMargins(0, 0, 0, 0)
        time_layout.setSpacing(8)
        time_label = QLabel("Time")
        time_label.setFixedWidth(100)
        time_edit = QTimeEdit()
        time_edit.setDisplayFormat("HH:mm")
        time_edit.setTime(QTime(time_of_day.hour, time_of_day.minute))
        time_layout.addWidget(time_label)
        time_layout.addWidget(time_edit, stretch=1)
        settings_layout.addWidget(time_row)

        preview_label = QLabel("")
        preview_label.setStyleSheet("color: #5f6368; font-size: 11px;")
        preview_label.setWordWrap(True)
        settings_layout.addWidget(preview_label)

        layout.addWidget(settings_widget)

        button_box = QDialogButtonBox()
        clear_btn = button_box.addButton("Clear", QDialogButtonBox.ButtonRole.ActionRole)
        save_btn = button_box.addButton("Save", QDialogButtonBox.ButtonRole.AcceptRole)
        cancel_btn = button_box.addButton(QDialogButtonBox.StandardButton.Cancel)

        clear_btn.setStyleSheet("""
            QPushButton {
                background-color: #fff1f0;
                border: 1px solid #ffccc7;
                border-radius: 4px;
                padding: 6px 12px;
                color: #cf1322;
            }
            QPushButton:hover {
                background-color: #ffe7e5;
            }
        """)

        save_btn.setStyleSheet("""
            QPushButton {
                background-color: #2da44e;
                border: 1px solid #2da44e;
                border-radius: 4px;
                padding: 6px 16px;
                color: white;
                font-weight: 500;
            }
            QPushButton:hover {
                background-color: #2c974b;
            }
        """)

        cancel_btn.setStyleSheet("""
            QPushButton {
                background-color: #f6f8fa;
                border: 1px solid #d0d7de;
                border-radius: 4px;
                padding: 6px 16px;
                color: #24292f;
            }
            QPushButton:hover {
                background-color: #f3f4f6;
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
            if freq in (RECURRENCE_FREQ_EVERY_N_DAYS, RECURRENCE_FREQ_DAILY):
                interval_unit_label.setText("days")
            elif freq == RECURRENCE_FREQ_WEEKLY:
                interval_unit_label.setText("weeks")
            else:
                interval_unit_label.setText("months")

            weekdays_row.setVisible(freq == RECURRENCE_FREQ_WEEKLY)
            month_row.setVisible(freq == RECURRENCE_FREQ_MONTHLY)

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
            mode_value = mode_combo.currentData()
            start_row.setEnabled(mode_value == RECURRENCE_MODE_FIXED)

        def _refresh_preview():
            if not enable_checkbox.isChecked():
                preview_label.setText("Recurrence is disabled.")
                return
            rule_preview = _build_rule_from_ui()
            summary = _format_recurrence_summary(rule_preview)
            mode_value = rule_preview.get("mode")
            if mode_value == RECURRENCE_MODE_COMPLETION and not task_done:
                preview_label.setText(f"{summary}\nNext: after completion")
                return

            if mode_value == RECURRENCE_MODE_FIXED:
                start_date = start_date_edit.date()
                start_dt_local = datetime.datetime(
                    start_date.year(), start_date.month(), start_date.day(),
                    time_edit.time().hour(), time_edit.time().minute(), 0
                )
            else:
                start_dt_local = datetime.datetime.now()

            next_dt = _compute_next_recurrence(rule_preview, start_dt_local, datetime.datetime.now())
            next_text = _format_recurrence_datetime(next_dt) if next_dt else "Unavailable"
            preview_label.setText(f"{summary}\nNext: {next_text}")

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
        interval_spin.valueChanged.connect(lambda _: _refresh_preview())
        month_spin.valueChanged.connect(lambda _: _refresh_preview())
        start_date_edit.dateChanged.connect(lambda _: _refresh_preview())
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
                    next_dt = _compute_next_recurrence(rule_data, completion_dt, completion_dt)
                    next_at = _format_recurrence_datetime(next_dt) if next_dt else None

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
            self._current_recurrence = self.db.get_task_recurrence(tid)
            self._update_recurrence_button_text()
            if self.search_terms or self.search_stakeholder_terms:
                self.apply_search_filter()
            else:
                self.task_model.reload()

    def _open_effort_picker(self):
        """Open a popup dialog for selecting effort points from Fibonacci sequence."""
        idx = self.task_view.currentIndex()
        tid = self.task_model.task_id_at(idx.row()) if idx.isValid() else None
        if tid is None:
            return
        
        # Fibonacci sequence with 0.5 included
        fibonacci_values = [0, 0.5, 1, 2, 3, 5, 8, 13, 21, 34, 55, 89]
        
        # Create popup dialog
        dialog = QDialog(self)
        dialog.setWindowTitle("Select Effort Points")
        dialog.setModal(True)
        dialog.setMinimumWidth(380)
        
        layout = QVBoxLayout(dialog)
        layout.setContentsMargins(16, 16, 16, 16)
        layout.setSpacing(12)
        
        # Info label
        info_label = QLabel("Select effort points (Fibonacci sequence):")
        layout.addWidget(info_label)
        
        selected_value = [self._current_effort]  # Use list to allow modification in nested function
        
        btn_style_base = """
            QPushButton {
                background-color: #f6f8fa;
                border: 1px solid #d0d7de;
                border-radius: 4px;
                padding: 8px;
                font-size: 13px;
                color: #24292f;
            }
            QPushButton:hover {
                background-color: #f3f4f6;
                border-color: #a5b3c0;
            }
        """
        
        btn_style_selected = """
            QPushButton {
                background-color: #2da44e;
                border: 1px solid #2da44e;
                border-radius: 4px;
                padding: 8px;
                font-size: 13px;
                color: white;
                font-weight: 500;
            }
            QPushButton:hover {
                background-color: #2c974b;
            }
        """
        
        buttons = []
        
        def on_value_click(value):
            selected_value[0] = value
            # Update all button styles
            for btn, val in buttons:
                if val == value:
                    btn.setStyleSheet(btn_style_selected)
                else:
                    btn.setStyleSheet(btn_style_base)
        
        # Create buttons for first row (0.5 to 8)
        row1_widget = QWidget()
        row1_layout = QHBoxLayout(row1_widget)
        row1_layout.setContentsMargins(0, 0, 0, 0)
        row1_layout.setSpacing(8)
        for value in fibonacci_values[:6]:
            btn = QPushButton(str(value) if value != int(value) else str(int(value)))
            btn.setFixedWidth(50)  # Fixed width to prevent overlap
            btn.setFixedHeight(36)  # Fixed height for consistency
            btn.setStyleSheet(btn_style_selected if value == self._current_effort else btn_style_base)
            btn.clicked.connect(lambda checked, v=value: on_value_click(v))
            buttons.append((btn, value))
            row1_layout.addWidget(btn)
        
        layout.addWidget(row1_widget)
        
        # Create buttons for second row (13 to 89)
        row2_widget = QWidget()
        row2_layout = QHBoxLayout(row2_widget)
        row2_layout.setContentsMargins(0, 0, 0, 0)
        row2_layout.setSpacing(8)
        for value in fibonacci_values[6:]:
            btn = QPushButton(str(int(value)))
            btn.setFixedWidth(50)  # Fixed width to prevent overlap
            btn.setFixedHeight(36)  # Fixed height for consistency
            btn.setStyleSheet(btn_style_selected if value == self._current_effort else btn_style_base)
            btn.clicked.connect(lambda checked, v=value: on_value_click(v))
            buttons.append((btn, value))
            row2_layout.addWidget(btn)
        
        layout.addWidget(row2_widget)
        
        # Dialog buttons
        button_box = QDialogButtonBox()
        ok_btn = button_box.addButton(QDialogButtonBox.StandardButton.Ok)
        cancel_btn = button_box.addButton(QDialogButtonBox.StandardButton.Cancel)
        
        ok_btn.setStyleSheet("""
            QPushButton {
                background-color: #2da44e;
                border: 1px solid #2da44e;
                border-radius: 4px;
                padding: 6px 16px;
                color: white;
                font-weight: 500;
            }
            QPushButton:hover {
                background-color: #2c974b;
            }
        """)
        
        cancel_btn.setStyleSheet("""
            QPushButton {
                background-color: #f6f8fa;
                border: 1px solid #d0d7de;
                border-radius: 4px;
                padding: 6px 16px;
                color: #24292f;
            }
            QPushButton:hover {
                background-color: #f3f4f6;
            }
        """)
        
        button_box.accepted.connect(dialog.accept)
        button_box.rejected.connect(dialog.reject)
        
        layout.addWidget(button_box)
        
        # Show dialog and process result
        if dialog.exec() == QDialog.DialogCode.Accepted:
            self._current_effort = selected_value[0]
            self.db.set_task_effort(tid, self._current_effort)
            self._update_effort_button_text()
            self._refresh_after_task_metadata_change(tid)

    def on_due_date_changed(self, _date):
        # Deprecated - keeping for compatibility but not used anymore
        pass
    
    def _save_due_date_now(self):
        idx = self.task_view.currentIndex()
        tid = self.task_model.task_id_at(idx.row()) if idx.isValid() else None
        if tid is None:
            return
        # Only save if date is actually set (not just cleared)
        if not self._due_date_is_set or not self._current_due_date:
            return
        date = self._current_due_date
        # Store as ISO format date string (YYYY-MM-DD 00:00:00)
        date_str = f"{date.year():04d}-{date.month():02d}-{date.day():02d} 00:00:00"
        self.db.set_task_due_date(tid, date_str)
        self._refresh_after_task_metadata_change(tid)
    
    def on_stakeholders_changed(self, _text: str):
        self._stakeholder_save_timer.start()
    
    def on_project_filter_changed(self, text: str):
        """Handle project filter text changes."""
        self.project_model.set_project_name_filter(text)

    def _ensure_project_selected_for_task(self, task_id: int):
        """Ensure that the project containing the given task is selected in the project view."""
        if not task_id:
            return
            
        try:
            # Get the project ID for this task
            task_row = self.db.conn.execute("SELECT project_id FROM tasks WHERE id=?", (task_id,)).fetchone()
            if not task_row:
                return
                
            task_project_id = task_row['project_id']
            
            # Check if the correct project is already selected
            current_idx = self.project_view.currentIndex()
            if current_idx.isValid():
                current_project_id = self.project_model.project_id_at(current_idx.row())
                if current_project_id == task_project_id:
                    return  # Correct project already selected
            
            # Find and select the correct project
            project_row = self.project_model.row_for_project(task_project_id)
            if project_row >= 0:
                # Block signals to prevent cascading selection events
                selection_model = self.project_view.selectionModel()
                if selection_model:
                    selection_model.blockSignals(True)
                    project_index = self.project_model.index(project_row, 0)
                    self.project_view.setCurrentIndex(project_index)
                    self.project_view.scrollTo(project_index, QListView.ScrollHint.EnsureVisible)
                    selection_model.blockSignals(False)
                    
                    # Update the task model context if needed
                    if self.task_model.project_id != task_project_id:
                        self.task_model.project_id = task_project_id
                        
        except Exception as e:
            print(f"Error ensuring project selection for task {task_id}: {e}")

    def _save_stakeholders_now(self):
        idx = self.task_view.currentIndex()
        tid = self.task_model.task_id_at(idx.row()) if idx.isValid() else None
        if tid is None:
            return
        text = self.stakeholders_edit.text()
        names = re.split(r"[\s,;]+", text)
        self.db.set_stakeholders_for_task(tid, names)
    
    def _clear_due_date(self):
        """Clear the due date for the current task (kept for compatibility)."""
        idx = self.task_view.currentIndex()
        tid = self.task_model.task_id_at(idx.row()) if idx.isValid() else None
        if tid is None:
            return
        # Set due_date to None in database
        self.db.set_task_due_date(tid, None)
        # Update the UI state
        self._due_date_is_set = False
        self._current_due_date = None
        self._update_due_date_button_text()
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
        self.due_date_edit.setDate(next_monday)

    def on_task_completion_stamped(self, task_id: int):
        if self.notes.task_id == task_id:
            # Reload from DB to reflect appended completion stamp
            self.notes.set_task(task_id)

    def on_tasks_changed(self):
        if self._handling_tasks_changed or self._suppress_tasks_refresh:
            return
        self._handling_tasks_changed = True
        try:
            # Check if any editors are active and defer updates if needed
            active_editors = False
            for delegate in [self.task_view.itemDelegateForColumn(i) for i in range(self.task_model.columnCount())]:
                if hasattr(delegate, '_active_editors') and delegate._active_editors:
                    active_editors = True
                    break
            
            if active_editors:
                # Defer the update until editors are closed
                QTimer.singleShot(100, self.on_tasks_changed)
                return
            
            # Store current selection before any model changes
            current_project_id = None
            current_task_id = None
            
            # Get current project ID
            project_idx = self.project_view.currentIndex()
            if project_idx.isValid():
                current_project_id = self.project_model.project_id_at(project_idx.row())
            
            # Get current task ID
            task_idx = self.task_view.currentIndex()
            if task_idx.isValid():
                current_task_id = self.task_model.task_id_at(task_idx.row())
            
            # Handle search mode differently
            if (self.search_terms or self.search_stakeholder_terms) and not self._applying_search:
                # In search mode, reapply search only for real data changes
                QTimer.singleShot(25, self.apply_search_filter)
            
            # Update project model visibility
            self.project_model.refresh_visibility()
            
            # Handle project selection preservation
            if current_project_id is not None:
                project_row = self.project_model.row_for_project(current_project_id)
                if project_row == -1:
                    # Current project no longer visible; keep selection at the same
                    # index so it lands on the project that shifted into that slot.
                    current_row = project_idx.row() if project_idx.isValid() else 0
                    self._select_project_row_preserving_slot(current_row)
                else:
                    # Project still exists, ensure it's selected
                    project_model = self.project_view.selectionModel()
                    if project_model:
                        project_model.blockSignals(True)
                        self.project_view.setCurrentIndex(self.project_model.index(project_row, 0))
                        project_model.blockSignals(False)
            
            # Restore task selection after a brief delay to allow model updates
            if current_task_id is not None:
                QTimer.singleShot(50, lambda: self._restore_task_selection(current_task_id))
            
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
        if hasattr(self, 'act_show_effort_missing'):
            self.act_show_effort_missing.blockSignals(True)
            self.act_show_effort_missing.setChecked(False)
            self.act_show_effort_missing.blockSignals(False)
        if hasattr(self, 'act_toggle_done'):
            self.act_toggle_done.blockSignals(True)
            self.act_toggle_done.setChecked(False)
            self.act_toggle_done.blockSignals(False)

        self.include_done = False
        self.pinned_only = False
        self.show_no_due_dates = False
        self.show_effort_missing = False
        self.task_model.include_done = False
        self.task_model.pinned_only = False
        self.task_model.show_no_due_dates = False
        self.task_model.show_effort_missing = False
        self.project_model.include_done = False
        self.project_model.pinned_only = False
        self.project_model.show_no_due_dates = False
        self.project_model.show_effort_missing = False

        try:
            self._settings.setValue('MainWindow/PinnedOnly', 'false')
            self._settings.sync()
        except Exception:
            pass

        self.project_model.reload()

        selected_project_id = None
        if current_project_id is not None:
            project_row = self.project_model.row_for_project(current_project_id)
            if project_row >= 0:
                project_index = self.project_model.index(project_row, 0)
                self.project_view.setCurrentIndex(project_index)
                self.project_view.scrollTo(project_index, QListView.ScrollHint.EnsureVisible)
                selected_project_id = current_project_id
        if selected_project_id is None:
            selected_project_id = self._select_fallback_project()

        self.task_model.set_context(selected_project_id, self.include_done, self.pinned_only)
        if current_task_id is not None:
            QTimer.singleShot(25, lambda tid=current_task_id: self._select_task_if_visible(tid) or self._select_nearest_task())
        elif self.task_model.rowCount() == 0:
            self._clear_current_task_context()

        self._update_comprehensive_status()

    def _test_force_visibility(self):
        """Test method to verify force_visibility functionality works correctly."""
        try:
            # Check if any new items have force_visibility=True
            new_projects = self.db.conn.execute("SELECT COUNT(*) FROM projects WHERE force_visibility=1").fetchone()[0]
            new_tasks = self.db.conn.execute("SELECT COUNT(*) FROM tasks WHERE force_visibility=1").fetchone()[0]
            
            if new_projects > 0 or new_tasks > 0:
                print(f"Force visibility test: {new_projects} projects and {new_tasks} tasks have force_visibility=True")
                return True
            return False
        except Exception as e:
            print(f"Error in force visibility test: {e}")
            return False

    def on_search_changed(self, text: str):
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
        
        raw = [t for t in SEARCH_SPLIT_RE.split(text.strip()) if t]
        general = [t for t in raw if not t.startswith('@') and t]
        stakeholders = [t[1:] for t in raw if t.startswith('@') and len(t) > 1]
        self.search_terms = general
        self.search_stakeholder_terms = stakeholders
        self.apply_search_filter()
        # Update status bar after search changes
        self._update_comprehensive_status()

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
            
            if not self.search_terms and not self.search_stakeholder_terms:
                # Clear search: restore normal project/task lists
                self._search_results_by_project.clear()
                self.project_model.set_search_filter(None)
                
                # Store the currently focused project ID before model changes
                current_idx = self.project_view.currentIndex()
                current_pid = None
                if current_idx.isValid():
                    # Try to get project ID from current selection
                    try:
                        current_pid = self.project_model.project_id_at(current_idx.row())
                    except (IndexError, AttributeError):
                        current_pid = None
                
                # If no current selection or invalid, try to use preserved focus
                if current_pid is None and hasattr(self, '_preserve_project_id') and self._preserve_project_id:
                    current_pid = self._preserve_project_id
                
                # After model refresh, restore the project selection
                if current_pid is not None:
                    # Find the row for this project in the refreshed model
                    target_row = self.project_model.row_for_project(current_pid)
                    if target_row >= 0:
                        # Select the specific project
                        target_index = self.project_model.index(target_row, 0)
                        self.project_view.setCurrentIndex(target_index)
                        self.project_view.scrollTo(target_index, QListView.ScrollHint.EnsureVisible)
                        pid = current_pid
                    else:
                        # Project not found, select first available
                        pid = self._select_fallback_project()
                else:
                    # No preserved project, select first available
                    pid = self._select_fallback_project()
                
                # Update task model context
                self.task_model.set_context(pid, self.include_done, self.pinned_only)
                
                return
            
            # Find projects that match search terms (only if we have general search terms, not just stakeholders)
            matching_project_ids = set()
            if self.search_terms:
                for term in self.search_terms:
                    like = f"%{term.lower()}%"
                    project_rows = self.db.conn.execute(
                        "SELECT id FROM projects WHERE LOWER(title) LIKE ?", 
                        (like,)
                    ).fetchall()
                    for proj_row in project_rows:
                        matching_project_ids.add(proj_row['id'])
            
            # Global search across all projects for task content matches
            task_rows = self.db.search_tasks(self.search_terms, self.search_stakeholder_terms, self.include_done, None, self.pinned_only)
            grouped: dict[int, List[sqlite3.Row]] = {}
            for r in task_rows:
                grouped.setdefault(r['project_id'], []).append(r)
            
            # For projects with matching names, get ALL tasks regardless of search terms
            for project_id in matching_project_ids:
                # Get all tasks for this project; include completed by default during search
                all_project_tasks = self.db.list_tasks(project_id, True, self.pinned_only)
                grouped[project_id] = all_project_tasks

            grouped = {
                project_id: self.task_model.apply_secondary_filters(tasks)
                for project_id, tasks in grouped.items()
            }
            grouped = {
                project_id: self.db.order_task_rows_for_project(project_id, tasks)
                for project_id, tasks in grouped.items()
            }
            grouped = {project_id: tasks for project_id, tasks in grouped.items() if tasks}

            self._search_results_by_project = grouped
            project_ids = set(grouped.keys())
            self.project_model.set_search_filter(project_ids)
            
            # Store current task selection before adjusting project selection
            current_task_id = None
            task_idx = self.task_view.currentIndex()
            if task_idx.isValid():
                current_task_id = self.task_model.task_id_at(task_idx.row())
            
            # Adjust selection if current project no longer valid
            idx = self.project_view.currentIndex()
            pid = self.project_model.project_id_at(idx.row()) if idx.isValid() else None
            
            if pid not in project_ids:
                # If we have a selected task, try to find its project first
                if current_task_id is not None:
                    try:
                        task_row = self.db.conn.execute("SELECT project_id FROM tasks WHERE id=?", (current_task_id,)).fetchone()
                        if task_row and task_row['project_id'] in project_ids:
                            # Select the project containing the current task
                            task_project_id = task_row['project_id']
                            row = self.project_model.row_for_project(task_project_id)
                            if row >= 0:
                                # Block signals to prevent cascading selection events
                                self.project_view.selectionModel().blockSignals(True)
                                self.project_view.setCurrentIndex(self.project_model.index(row, 0))
                                self.project_view.selectionModel().blockSignals(False)
                                pid = task_project_id
                            else:
                                current_task_id = None  # Task project not found, clear selection
                        else:
                            current_task_id = None  # Task not in search results
                    except Exception:
                        current_task_id = None
                
                # If no task-based selection worked, choose first available project
                if pid not in project_ids and project_ids:
                    first_pid = next(iter(project_ids))
                    row = self.project_model.row_for_project(first_pid)
                    if row >= 0:
                        # Block signals to prevent cascading selection events
                        self.project_view.selectionModel().blockSignals(True)
                        self.project_view.setCurrentIndex(self.project_model.index(row, 0))
                        self.project_view.selectionModel().blockSignals(False)
                        pid = first_pid
                elif not project_ids:
                    # No matches: clear everything
                    self.project_view.clearSelection()
                    self._suppress_tasks_refresh = True
                    self.task_model.beginResetModel()
                    self.task_model.rows = []
                    self.task_model.endResetModel()
                    self._suppress_tasks_refresh = False
                    return
                    
            # Update task model with search results for current project
            self._suppress_tasks_refresh = True
            self.task_model.beginResetModel()
            self.task_model.rows = grouped.get(pid, []) if pid is not None else []
            self.task_model.project_id = pid
            # Mark include_done True for visual treatment while in search mode
            # (this doesn't change filters, it only affects ForegroundRole styling)
            prev_include_done = self.task_model.include_done
            self.task_model.include_done = prev_include_done or True
            self.task_model.endResetModel()
            # Restore include_done to previous value for future non-search reloads
            self.task_model.include_done = prev_include_done
            self._suppress_tasks_refresh = False
            
            # Restore task selection if the task is still visible
            if current_task_id is not None:
                QTimer.singleShot(50, lambda: self._restore_task_selection_after_search(current_task_id))
            
        finally:
            self._applying_search = False
            if getattr(self, '_suppress_search_restore', False):
                self._suppress_search_restore = False
            else:
                # Restore focus after search is complete
                QTimer.singleShot(75, self._restore_preserved_focus)

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
        """Restore previously selected project and task after search filter changes."""
        if not (self._preserve_project_id or self._preserve_task_id):
            return
            
        try:
            # If we have a task ID, find which project it belongs to and ensure that project is selected
            if self._preserve_task_id is not None:
                task_project_id = None
                try:
                    task_row = self.db.conn.execute("SELECT project_id FROM tasks WHERE id=?", (self._preserve_task_id,)).fetchone()
                    if task_row:
                        task_project_id = task_row['project_id']
                except Exception:
                    pass
                    
                # If task's project is different from preserved project, prioritize task's project
                if task_project_id is not None and task_project_id != self._preserve_project_id:
                    self._preserve_project_id = task_project_id
            
            # Restore project selection
            if self._preserve_project_id is not None:
                project_row = self.project_model.row_for_project(self._preserve_project_id)
                if project_row >= 0:
                    # Project still exists and is visible, select it
                    project_index = self.project_model.index(project_row, 0)
                    # Use proper selection without blocking signals
                    self.project_view.setCurrentIndex(project_index)
                    self.project_view.scrollTo(project_index, QListView.ScrollHint.EnsureVisible)
                    
                    # Give more time for task model to update, then restore task
                    QTimer.singleShot(100, self._restore_task_focus)
                    return
                    
            # If preserved project not found, try to restore task by finding its project
            if self._preserve_task_id is not None:
                # Find which project contains the preserved task
                task_project_id = None
                try:
                    task_row = self.db.conn.execute("SELECT project_id FROM tasks WHERE id=?", (self._preserve_task_id,)).fetchone()
                    if task_row:
                        task_project_id = task_row['project_id']
                except Exception:
                    pass
                    
                if task_project_id is not None:
                    project_row = self.project_model.row_for_project(task_project_id)
                    if project_row >= 0:
                        # Select the project containing the preserved task
                        project_index = self.project_model.index(project_row, 0)
                        self.project_view.setCurrentIndex(project_index)
                        self.project_view.scrollTo(project_index, QListView.ScrollHint.EnsureVisible)
                        
                        # Give more time for task model to update, then restore task
                        QTimer.singleShot(100, self._restore_task_focus)
                        return
                        
            # Fallback: ensure something is selected
            self._ensure_selection_fallback()
            
        except Exception as e:
            print(f"Error restoring focus: {e}")
            self._ensure_selection_fallback()
        finally:
            # Clear preserved state only after successful restoration
            self._preserve_project_id = None
            self._preserve_task_id = None

    def _select_project_row_preserving_slot(self, desired_row: int):
        """Select the project at the desired row slot after model changes, with safe fallback."""
        total_rows = self.project_model.rowCount(QModelIndex())
        if total_rows <= 0:
            # Nothing left; clear selection and related context
            self.project_view.clearSelection()
            self.task_model.set_context(None, self.include_done, self.pinned_only)
            self.notes.set_task(None)
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
        """Select the nearest available task when preserved task is not found."""
        try:
            if self.task_model.rowCount() > 0:
                self._select_task_row(0)
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
    
    def _update_comprehensive_status(self):
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
            status_parts = []
            
            # Projects count (visible/total)
            project_count = self.project_model.rowCount()
            total_projects = len(self.db.list_projects())
            
            if project_count < total_projects:
                count_text = f"📊 {project_count}/{total_projects} projects"
            else:
                count_text = f"📊 {project_count} projects"
            
            status_parts.append(count_text)
            
            # Active filters
            filter_parts = []
            filter_parts.append("✅ Show completed" if self.include_done else "❌ Hide completed")
            if self.pinned_only:
                filter_parts.append("📌 Pinned only")
            
            if filter_parts:
                status_parts.append(f"Filters: {' | '.join(filter_parts)}")
            
            # Search results count (if in search mode)
            if self.search_terms or self.search_stakeholder_terms:
                search_parts = []
                if self.search_terms:
                    search_parts.append(f"Text: '{' '.join(self.search_terms)}'")
                if self.search_stakeholder_terms:
                    search_parts.append(f"@{' @'.join(self.search_stakeholder_terms)}")
                
                status_parts.append(f"🔍 {' + '.join(search_parts)}")
                
                if project_count == 0:
                    status_parts.append("⚠️ No matches")
                else:
                    status_parts.append(f"✓ {project_count} results")
            
            # Project created date (for selected project)
            project_idx = self.project_view.currentIndex()
            if project_idx.isValid():
                project_id = self.project_model.project_id_at(project_idx.row())
                if project_id:
                    try:
                        # Get project creation date from database
                        project_row = self.db.conn.execute(
                            "SELECT created_at FROM projects WHERE id=?", 
                            (project_id,)
                        ).fetchone()
                        
                        if project_row and project_row['created_at']:
                            created_at = project_row['created_at']
                            
                            # Parse the ISO datetime string
                            if 'T' in created_at:
                                dt = datetime.datetime.fromisoformat(created_at.replace('Z', '+00:00'))
                            else:
                                dt = datetime.datetime.strptime(created_at, '%Y-%m-%d %H:%M:%S')
                            
                            formatted_date = dt.strftime('%Y-%m-%d')
                            status_parts.append(f"📅 Project created: {formatted_date}")
                    except Exception as e:
                        print(f"Error getting project creation date: {e}")
            
            # Combine everything into a single comprehensive status
            final_status = " • ".join(status_parts) if status_parts else "Ready"
            
            # Limit length to prevent status bar overflow
            if len(final_status) > 150:
                final_status = final_status[:147] + "..."
            
            self.status_comprehensive.setText(final_status)
            
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
            
            if self.search_terms or self.search_stakeholder_terms:
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
            
            self.status_comprehensive.setToolTip("\n".join(tooltip_parts))
            
        except Exception as e:
            # Fallback to simple status on any error
            if hasattr(self, 'status_comprehensive') and self.status_comprehensive is not None:
                self.status_comprehensive.setText(f"Ready • {self.project_model.rowCount()} projects")
                self.status_comprehensive.setToolTip("")
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
                self._settings.sync()
                
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
        self._save_splitter_sizes()
        self._save_window_state()
        
        # Create backup of current database before exit
        self._create_database_backup()
        
        # FIRST: Ensure notes editor saves any pending changes and cleans up timers
        # This must happen BEFORE closing the database connection
        try:
            if hasattr(self, 'notes') and self.notes:
                self.notes.cleanup_resources()
        except Exception as notes_error:
            print(f"Warning: Notes cleanup error: {notes_error}")
        
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
        """Create a backup of the currently open database and manage retention."""
        if not self.db or not hasattr(self.db, 'path'):
            return
            
        try:
            db_path = self.db.path
            if not os.path.exists(db_path):
                return
                
            # Parse database file path
            db_filename = os.path.basename(db_path)
            base_name, ext = os.path.splitext(db_filename)
            
            if not ext:
                ext = '.db'  # Default extension if none
                
            # Generate backup filename with timestamp
            timestamp = datetime.datetime.now().strftime('%Y%m%d-%H%M%S')
            backup_filename = f"{base_name}.backup-{timestamp}{ext}"
            backup_path = os.path.join(self.backup_dir, backup_filename)
            
            # Create backup using SQLite's online backup API
            try:
                # Try online backup first (preferred method)
                with sqlite3.connect(backup_path) as backup_conn:
                    self.db.conn.backup(backup_conn)
                print(f"Database backup created successfully: {backup_filename}")
            except Exception as online_error:
                # Fallback to file copy method
                try:
                    # Use atomic file copy as fallback
                    with tempfile.NamedTemporaryFile(dir=self.backup_dir, delete=False, suffix='.tmp') as temp_file:
                        temp_path = temp_file.name
                        
                    # Copy the database file
                    shutil.copy2(db_path, temp_path)
                    
                    # Ensure data is written to disk
                    with open(temp_path, 'r+b') as f:
                        f.flush()
                        os.fsync(f.fileno())
                    
                    # Atomically rename to final backup name
                    os.rename(temp_path, backup_path)
                    print(f"Database backup created successfully (fallback method): {backup_filename}")
                    
                except Exception as copy_error:
                    print(f"Failed to create database backup: {copy_error}")
                    # Clean up temp file if it exists
                    try:
                        if 'temp_path' in locals() and os.path.exists(temp_path):
                            os.remove(temp_path)
                    except Exception:
                        pass
                    return
            
            # Manage backup retention (keep only newest 30) and migrate existing backups
            self._cleanup_old_backups(base_name, ext)
            
        except Exception as e:
            print(f"Error during database backup process: {e}")

    def _cleanup_old_backups(self, base_name: str, ext: str):
        """Remove old backups, keeping only the newest 30 for this specific database, and migrate existing backups."""
        try:
            # First, migrate any existing backups from the main project directory
            self._migrate_existing_backups(base_name, ext)
            
            # Create pattern to match only backups for this specific database
            # Note: Use shell glob pattern, not regex pattern for glob.glob()
            pattern = f"{base_name}.backup-????????-??????{ext}"
            search_pattern = os.path.join(self.backup_dir, pattern)
            
            # Find all backup files for this database
            backup_files = []
            for filepath in glob.glob(search_pattern):
                if os.path.isfile(filepath):
                    filename = os.path.basename(filepath)
                    # Double-check the pattern with regex for exact match
                    regex_pattern = rf"^{re.escape(base_name)}\.backup-\d{{8}}-\d{{6}}{re.escape(ext)}$"
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
            pattern = f"{base_name}.backup-????????-??????{ext}"
            search_pattern = os.path.join(APP_DIR, pattern)
            
            migrated_count = 0
            for old_backup_path in glob.glob(search_pattern):
                if os.path.isfile(old_backup_path):
                    filename = os.path.basename(old_backup_path)
                    # Double-check the pattern with regex for exact match
                    regex_pattern = rf"^{re.escape(base_name)}\.backup-\d{{8}}-\d{{6}}{re.escape(ext)}$"
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

    def apply_theme(self):
        # Microsoft To Do inspired light theme (refined)
        accent = '#2564cf'
        accent_hover = '#1e55b1'
        sel_bg = '#cfe4ff'
        sel_border = '#7eb6f5'
        base_bg = '#ffffff'
        side_bg = '#f5f7fa'
        border_col = '#d0d7de'
        text_col = '#202124'
        subtle_text = '#5f6368'
        font_stack = '"Segoe UI", "Helvetica Neue", Arial, sans-serif'
        qss = f"""
        QMainWindow {{ background: {side_bg}; color: {text_col}; font: 12px {font_stack}; }}
        QToolBar {{ background: qlineargradient(x1:0,y1:0,x2:0,y2:1, stop:0 #fdfefe, stop:1 #eef2f6); border:0px; padding:4px; spacing:4px; }}
        QToolBar QToolButton {{ border-radius:3px; padding:4px 10px; background: transparent; }}
        QToolBar QToolButton:hover {{ background: #e2eefc; }}
        QToolBar QToolButton:pressed {{ background: {sel_bg}; }}
        QListView, QTableView {{ background: {base_bg}; border:1px solid {border_col}; border-radius:4px; outline:0; selection-background-color:{sel_bg}; selection-color:{text_col}; alternate-background-color:#fafbfc; }}
        QListView::item {{ padding:6px 8px; margin:2px; border-radius:3px; }}
        QListView::item:selected {{ background:{sel_bg}; border:1px solid {sel_border}; }}
        QHeaderView {{ border-top-left-radius:4px; border-top-right-radius:4px; }}
        QHeaderView::section {{ background:#f0f3f6; padding:6px 8px; border:0; border-right:1px solid {border_col}; font-weight:600; }}
        QHeaderView::section:first {{ border-top-left-radius:4px; }}
        QHeaderView::section:last {{ border-right:0; border-top-right-radius:4px; }}
        QTableView {{ gridline-color: #eef1f4; selection-background-color:{sel_bg}; selection-color:{text_col}; }}
        QTableView::item {{ padding:0px 4px; }}
        QTableView::item:selected {{ background:{sel_bg}; border:0; }}
        QTableView::item:focus {{ outline:0; }}
        QTableView::indicator {{ width:18px; height:18px; }}
        QTableView::indicator:unchecked {{ border:2px solid {accent}; border-radius:3px; background:transparent; }}
        QTableView::indicator:unchecked:hover {{ background:#e6f1fe; }}
        QTableView::indicator:checked {{ border:2px solid {accent}; background:{accent}; border-radius:3px; image:url(); }}
        QTableView::indicator:checked:hover {{ background:{accent_hover}; border-color:{accent_hover}; }}
        QLineEdit {{ background:{base_bg}; border:1px solid {border_col}; border-radius:3px; padding:4px  6px; selection-background-color:{accent}; selection-color:#fff; }}
        QLineEdit:focus {{ border:1px solid {accent}; }}
        QTextEdit {{ background:{base_bg}; border:1px solid {border_col}; border-radius:3px; padding:8px; selection-background-color:{accent}; selection-color:#fff; }}
        QTextEdit:focus {{ border:1px solid {accent}; }}
        QLabel {{ color:{subtle_text}; font-weight:600; margin-top:4px; }}
        QMenuBar {{ background: transparent; font: 12px {font_stack}; padding:4px 6px; }}
        QMenuBar::item {{ background: transparent; padding:4px 12px; margin:0 2px; border-radius:3px; }}
        QMenuBar::item:selected {{ background:#e6eef8; }}
        QMenu {{ background:{base_bg}; border: 1px solid {border_col}; border-radius: 10px; padding: 6px 0; }}
        QMenu::separator {{ height:1px; background:#e3e8ed; margin:4px 8px; }}
        QMenu::icon {{ padding-left:6px; padding-right:4px; }}
        QMenu::item {{ padding:6px 14px 6px 30px; border-radius:3px; }}
        QMenu::item:selected {{ background:{sel_bg}; }}
        QScrollBar:vertical {{ background:transparent; width:12px; margin:0; }}
        QScrollBar::handle:vertical {{ background:#d0d7de; min-height:30px; border-radius:3px; }}
        QScrollBar::handle:vertical:hover {{ background:#b7c3cc; }}
        QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{ height:0; }}
        QScrollBar:horizontal {{ background:transparent; height:12px; margin:0; }}
        QScrollBar::handle:horizontal {{ background:#d0d7de; min-width:30px; border-radius:3px; }}
        QScrollBar::handle:horizontal:hover {{ background:#b7c3cc; }}
        QPushButton {{ background:{accent}; color:#fff; border:1px solid {accent}; padding:6px 14px; border-radius:3px; font-weight:500; }}
        QPushButton:hover {{ background:{accent_hover}; border-color:{accent_hover}; }}
        QPushButton:pressed {{ background:#133d7a; border-color:#133d7a; }}
        QPushButton:flat {{ background:transparent; border:1px solid transparent; }}
        QPushButton:flat:hover {{ background:#e6f1fe; border:1px solid {border_col}; }}
        QPushButton:flat:pressed {{ background:{sel_bg}; border:1px solid {sel_border}; }}
        QCheckBox {{ spacing:8px; }}
        QCheckBox::indicator {{ width:18px; height:18px; border-radius:3px; border:2px solid {accent}; background:transparent; }}
        QCheckBox::indicator:hover {{ background:#e6f1fe; }}
        QCheckBox::indicator:checked {{ background:{accent}; border-color:{accent}; image:url(); }}
        QStatusBar {{ background:#f0f3f6; border-top:1px solid {border_col}; }}
        QListView:focus, QTableView:focus {{ border:1px solid {accent}; }}
        """
        QApplication.instance().setStyleSheet(qss)
        # Row / cell sizing
        self.project_view.setSpacing(2)
        self.task_view.verticalHeader().setDefaultSectionSize(34)
        self.task_view.verticalHeader().hide()
        self.task_view.setAlternatingRowColors(True)
        # Preserve any prior per-view additions
        self.task_view.setStyleSheet(self.task_view.styleSheet() + '\nQTableView { border-top-left-radius:4px; border-top-right-radius:4px; }')

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
    app = QApplication(sys.argv)
    
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

    if _run_addon_startup_hooks(app):
        try:
            activity_logger.shutdown()
        except Exception:
            pass
        return 0
    
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
