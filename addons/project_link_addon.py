#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Project Link Addon for Project Notes.
Creates deep links to projects and handles incoming links.
"""
import os
import sys
import json
import hashlib
import datetime
from typing import Optional, Dict, Any, List
from urllib.parse import urlparse, parse_qs, quote, unquote

from PyQt6.QtCore import QTimer, QRegularExpression, Qt, QStringListModel, QSettings
from PyQt6.QtGui import QRegularExpressionValidator
from PyQt6.QtWidgets import (
    QApplication,
    QCompleter,
    QMessageBox,
    QDialog,
    QDialogButtonBox,
    QLabel,
    QLineEdit,
    QMenu,
    QCheckBox,
    QVBoxLayout,
    QStyle,
    QWidget,
)
from PyQt6.QtNetwork import QLocalServer, QLocalSocket


class ProjectLinkAddon:
    """Addon for creating and handling project deep links."""

    def __init__(self):
        self.name = "Project Link Addon"
        self.version = "1.0.0"
        self.description = "Create and open project deep links"
        self.preferences_category_name = "Project Link Addon"
        self._scheme = "projectnotes"
        self._pending_link: Optional[str] = None
        self._pending_navigation: Optional[Dict[str, Any]] = None
        self._nav_max_attempts = 6
        self._nav_retry_delay_ms = 200
        self._server: Optional[QLocalServer] = None
        self._server_names = self._build_server_names()
        self._server_name = self._server_names[0]
        self._menu_action_added = False
        self._main_window = None
        self._queued_links: List[str] = []
        self._debug_enabled = os.environ.get("PROJECT_LINK_DEBUG", "0").lower() in ("1", "true", "yes")
        self._debug_log_path = self._build_debug_log_path()
        self._context_menu_hooked = False
        self._db_switch_hooked = False
        self._original_project_context_menu = None
        self._para_column_ready: set[str] = set()
        self._debug_log(f"init pid={os.getpid()} server_names={self._server_names}")

    def __getattribute__(self, name):
        if name == "_edit_project_para_folder":
            is_enabled = object.__getattribute__(self, "_is_enabled")
            if not is_enabled():
                raise AttributeError(name)
        return object.__getattribute__(self, name)

    def _get_settings_path(self) -> str:
        return os.path.join(self._get_app_root(), "settings.ini")

    def _load_settings(self) -> QSettings:
        return QSettings(self._get_settings_path(), QSettings.Format.IniFormat)

    def _is_enabled(self, settings: Optional[QSettings] = None) -> bool:
        active_settings = settings if settings is not None else self._load_settings()
        value = active_settings.value("ProjectLinkAddon/Enabled", "false")
        return value in ("true", "1", "True", True)

    def create_preferences_widget(self, settings: QSettings, parent=None) -> QWidget:
        """Create addon settings widget for the preferences dialog."""
        widget = QWidget(parent)
        layout = QVBoxLayout(widget)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(16)

        title_label = QLabel("Project Link Addon")
        title_label.setStyleSheet("font-size: 18px; font-weight: 600; color: #202124;")
        layout.addWidget(title_label)

        desc_label = QLabel(
            "Enable project links, the PARA folder context action, and related startup handling."
            "\n\nChanges apply after restarting the app."
        )
        desc_label.setStyleSheet("color: #5f6368; margin-bottom: 8px;")
        desc_label.setWordWrap(True)
        layout.addWidget(desc_label)

        widget.enable_addon_checkbox = QCheckBox("Enable addon")
        widget.enable_addon_checkbox.setChecked(self._is_enabled(settings))
        widget.enable_addon_checkbox.setStyleSheet("""
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
        layout.addWidget(widget.enable_addon_checkbox)
        layout.addStretch()
        return widget

    def save_preferences(self, widget: QWidget, settings: QSettings):
        """Persist addon settings from the preferences dialog."""
        try:
            settings.setValue(
                "ProjectLinkAddon/Enabled",
                "true" if widget.enable_addon_checkbox.isChecked() else "false",
            )
            settings.sync()
        except (RuntimeError, AttributeError):
            pass

    def handle_startup(self, app, argv) -> bool:
        """Handle deep links at startup; return True to exit early."""
        if not self._is_enabled():
            self._debug_log("handle_startup skipped_disabled")
            return False
        self._debug_log(f"handle_startup argv={argv}")
        if app is not None:
            self._debug_log(f"handle_startup app.arguments={app.arguments()}")
        link = self._extract_link(argv) or self._extract_link(app.arguments() if app else [])
        self._debug_log(f"handle_startup extracted_link={link}")
        if link:
            forwarded = self._send_to_running_instance(link)
            self._debug_log(f"handle_startup forwarded={forwarded}")
            if forwarded:
                return True
            self._pending_link = link
        self._ensure_server(None, app)
        return False

    def register_menu_items(self, menu, main_window):
        """Register menu items and initialize IPC server."""
        if not self._is_enabled():
            self._debug_log("register_menu_items skipped_disabled")
            return
        self._main_window = main_window
        self._debug_log("register_menu_items")
        self._ensure_server(main_window, QApplication.instance())
        self._register_project_menu(main_window, menu)
        self._install_project_context_menu_hook(main_window)
        self._install_db_switch_hook(main_window)
        self._ensure_para_folder_column(main_window)
        if not self._pending_link:
            app = QApplication.instance()
            if app is not None:
                link = self._extract_link(app.arguments())
                if link:
                    self._pending_link = link
        if self._pending_link:
            pending_link = self._pending_link
            self._pending_link = None
            QTimer.singleShot(0, lambda: self._handle_link(main_window, pending_link))
        if self._queued_links:
            for queued in list(self._queued_links):
                QTimer.singleShot(0, lambda url=queued: self._handle_link(main_window, url))
            self._queued_links.clear()

    def _register_project_menu(self, main_window, fallback_menu):
        if not self._is_enabled():
            return
        if self._menu_action_added:
            return
        project_menu = self._find_menu(main_window, "Project")
        target_menu = project_menu if project_menu is not None else fallback_menu
        if target_menu is None:
            return
        if target_menu.actions():
            target_menu.addSeparator()
        link_icon = main_window.style().standardIcon(QStyle.StandardPixmap.SP_FileLinkIcon)
        target_menu.addAction(link_icon, "Copy Project Link", lambda: self.create_project_link(main_window))
        self._menu_action_added = True

    def _install_project_context_menu_hook(self, main_window):
        if not self._is_enabled():
            return
        if self._context_menu_hooked:
            return
        project_view = getattr(main_window, "project_view", None)
        if project_view is None:
            return
        original_handler = getattr(main_window, "show_project_context_menu", None)
        self._original_project_context_menu = original_handler
        if original_handler is not None:
            try:
                project_view.customContextMenuRequested.disconnect(original_handler)
            except Exception:
                pass
        project_view.customContextMenuRequested.connect(
            lambda pos, mw=main_window: self._show_project_context_menu(mw, pos)
        )
        try:
            main_window.show_project_context_menu = (
                lambda pos, mw=main_window: self._show_project_context_menu(mw, pos)
            )
        except Exception:
            pass
        self._context_menu_hooked = True

    def _install_db_switch_hook(self, main_window):
        if not self._is_enabled():
            return
        if self._db_switch_hooked:
            return
        original_switch = getattr(main_window, "_switch_to_database", None)
        if original_switch is None:
            return

        def switch_with_para_folder(new_db, path, _orig=original_switch, _mw=main_window):
            result = _orig(new_db, path)
            try:
                self._ensure_para_folder_column(_mw)
            except Exception:
                pass
            return result

        main_window._switch_to_database = switch_with_para_folder
        self._db_switch_hooked = True

    def _show_project_context_menu(self, main_window, position):
        if not self._is_enabled():
            return
        index = main_window.project_view.indexAt(position)
        if not index.isValid():
            return

        project_id = main_window.project_model.project_id_at(index.row())
        if project_id is None:
            return

        main_window.project_view.setCurrentIndex(index)

        menu = QMenu(main_window)
        review_action_added = False

        try:
            review_pending = main_window._project_review_enabled() and main_window.project_model.is_project_in_review(project_id)
        except Exception:
            review_pending = False

        if review_pending:
            review_action = menu.addAction("Review Completed")
            review_action.setIcon(main_window._make_green_check_icon())
            review_action.triggered.connect(
                lambda _checked=False, pid=project_id: main_window.mark_project_review_completed(pid)
            )
            review_action_added = True

        feature_links_enabled = False
        try:
            feature_links_enabled = main_window._feature_links_enabled()
        except Exception:
            feature_links_enabled = False
        current_link = main_window._project_feature_links.get(project_id) if feature_links_enabled else None

        if feature_links_enabled:
            if review_action_added:
                menu.addSeparator()
            if current_link:
                visit_action = menu.addAction("Visit feature link")
                visit_action.triggered.connect(
                    lambda _checked=False, pid=project_id: main_window._visit_project_feature_link(pid)
                )

            edit_action = menu.addAction("Edit links")
            edit_action.triggered.connect(
                lambda _checked=False, pid=project_id: main_window._edit_project_feature_link(pid)
            )

        if menu.actions():
            menu.addSeparator()
        para_action = menu.addAction("Edit PARA folder")
        para_action.triggered.connect(
            lambda _checked=False, pid=project_id: self._edit_project_para_folder(main_window, pid)
        )

        global_pos = main_window.project_view.viewport().mapToGlobal(position)
        menu.exec(global_pos)

    def _ensure_para_folder_column(self, main_window) -> bool:
        if not self._is_enabled():
            return False
        db = getattr(main_window, "db", None)
        if db is None or not hasattr(db, "conn"):
            return False
        db_path = getattr(db, "path", "") or ""
        if db_path and db_path in self._para_column_ready:
            return True
        try:
            cols = [r[1] for r in db.conn.execute("PRAGMA table_info(projects)")]
            if "para_folder" not in cols:
                db.conn.execute("ALTER TABLE projects ADD COLUMN para_folder TEXT")
                db.conn.commit()
            if db_path:
                self._para_column_ready.add(db_path)
            return True
        except Exception as e:
            self._debug_log(f"para_folder_column_error={e}")
            return False

    def _get_app_root(self) -> str:
        try:
            return os.path.abspath(os.path.join(os.path.dirname(__file__), os.pardir))
        except Exception:
            return os.getcwd()

    def _load_env_vars(self) -> Dict[str, str]:
        env_vars: Dict[str, str] = {}
        env_path = os.path.join(self._get_app_root(), ".env")
        if not os.path.exists(env_path):
            return env_vars
        try:
            with open(env_path, "r", encoding="utf-8") as handle:
                for line in handle:
                    line = line.strip()
                    if not line or line.startswith("#") or "=" not in line:
                        continue
                    key, value = line.split("=", 1)
                    env_vars[key.strip()] = value.strip().strip('"').strip("'")
        except Exception as e:
            self._debug_log(f"env_load_error={e}")
        return env_vars

    def _get_para_folder_roots(self) -> List[str]:
        raw = os.environ.get("PARA_FOLDERS")
        if not raw:
            raw = self._load_env_vars().get("PARA_FOLDERS", "")
        raw = raw.strip()
        if not raw:
            return []
        parts = [p.strip() for p in raw.replace(";", ",").split(",") if p.strip()]
        roots: List[str] = []
        app_root = self._get_app_root()
        for part in parts:
            expanded = os.path.expanduser(os.path.expandvars(part))
            if not os.path.isabs(expanded):
                expanded = os.path.normpath(os.path.join(app_root, expanded))
            roots.append(expanded)
        return roots

    def _get_para_topics_export_path(self) -> str:
        raw = os.environ.get("PARA_TOPICS_EXPORT_PATH")
        if not raw:
            raw = self._load_env_vars().get("PARA_TOPICS_EXPORT_PATH", "")
        raw = raw.strip()
        if not raw:
            return ""
        expanded = os.path.expanduser(os.path.expandvars(raw))
        if not os.path.isabs(expanded):
            expanded = os.path.normpath(os.path.join(self._get_app_root(), expanded))
        return expanded

    def _load_para_topics_from_export(self) -> List[str]:
        path = self._get_para_topics_export_path()
        if not path:
            return []
        if not os.path.isfile(path):
            self._debug_log(f"para_topics_export_missing={path}")
            return []
        try:
            with open(path, "r", encoding="utf-8-sig") as handle:
                payload = json.load(handle)
        except Exception as e:
            self._debug_log(f"para_topics_export_read_error={path} err={e}")
            return []
        sections = payload.get("sections") if isinstance(payload, dict) else None
        if not isinstance(sections, list):
            self._debug_log(f"para_topics_export_invalid={path}")
            return []
        topics: List[str] = []
        seen: set[str] = set()
        for section in sections:
            if not isinstance(section, dict):
                continue
            items = section.get("topics")
            if not isinstance(items, list):
                continue
            for topic in items:
                if not isinstance(topic, str):
                    continue
                name = topic.strip()
                if not name or name in seen:
                    continue
                seen.add(name)
                topics.append(name)
        return topics

    def _get_para_main_folders(self) -> List[str]:
        return [
            "01 - Projects",
            "02 - Areas",
            "03 - Resources",
            "04 - Archive",
        ]

    def _load_para_folder_names(self) -> List[str]:
        topics = self._load_para_topics_from_export()
        if topics:
            return topics
        roots = self._get_para_folder_roots()
        if not roots:
            return []
        names: List[str] = []
        seen: set[str] = set()
        main_folders = self._get_para_main_folders()
        for root in roots:
            for main_folder in main_folders:
                main_path = os.path.join(root, main_folder)
                if not os.path.isdir(main_path):
                    continue
                try:
                    for entry in os.scandir(main_path):
                        if entry.is_dir():
                            name = entry.name
                            if name not in seen:
                                seen.add(name)
                                names.append(name)
                except Exception as e:
                    self._debug_log(f"para_folder_scan_error={main_path} err={e}")
        names.sort(key=str.lower)
        return names

    def _get_project_para_folder(self, main_window, project_id: int) -> str:
        if not self._is_enabled():
            return ""
        if not self._ensure_para_folder_column(main_window):
            return ""
        try:
            row = main_window.db.conn.execute(
                "SELECT para_folder FROM projects WHERE id=?",
                (project_id,)
            ).fetchone()
        except Exception as e:
            self._debug_log(f"para_folder_fetch_error={e}")
            return ""
        if not row:
            return ""
        value = row[0]
        return value if value is not None else ""

    def _set_project_para_folder(self, main_window, project_id: int, folder: Optional[str]) -> bool:
        if not self._is_enabled():
            return False
        if not self._ensure_para_folder_column(main_window):
            QMessageBox.warning(main_window, "PARA Folder", "Database not ready for PARA folders.")
            return False
        try:
            main_window.db.conn.execute(
                "UPDATE projects SET para_folder=?, updated_at=CURRENT_TIMESTAMP WHERE id=?",
                (folder, project_id)
            )
            main_window.db.conn.commit()
            self._update_project_model_cache(main_window, project_id, folder)
            return True
        except Exception as e:
            self._debug_log(f"para_folder_update_error={e}")
            QMessageBox.warning(main_window, "PARA Folder", f"Failed to update PARA folder:\n{e}")
            return False

    def _update_project_model_cache(self, main_window, project_id: int, folder: Optional[str]):
        try:
            for row in main_window.project_model.rows:
                if row.get("id") == project_id:
                    row["para_folder"] = folder
                    break
        except Exception:
            pass

    def _edit_project_para_folder(self, main_window, project_id: int):
        project_title = ""
        try:
            row = main_window.project_model.row_for_project(project_id)
            if 0 <= row < len(main_window.project_model.rows):
                project_title = main_window.project_model.rows[row].get("title", "")
        except Exception:
            project_title = ""

        dialog = QDialog(main_window)
        dialog.setWindowTitle("Edit PARA Folder")
        dialog.setModal(True)
        dialog.setMinimumWidth(420)

        layout = QVBoxLayout(dialog)
        layout.setContentsMargins(14, 14, 14, 14)
        layout.setSpacing(10)

        heading = QLabel("PARA folder")
        heading.setStyleSheet("font-size: 15px; font-weight: 600; color: #202124;")
        layout.addWidget(heading)

        if project_title:
            subtitle = QLabel(f"Set a PARA folder for '{project_title}'.")
        else:
            subtitle = QLabel("Set a PARA folder for this project.")
        subtitle.setStyleSheet("color: #5f6368;")
        subtitle.setWordWrap(True)
        layout.addWidget(subtitle)

        folder_edit = QLineEdit()
        folder_edit.setPlaceholderText("e.g., areas")
        folder_edit.setText(self._get_project_para_folder(main_window, project_id))
        folder_edit.setMinimumHeight(32)
        folder_edit.setMaxLength(255)
        folder_edit.setStyleSheet("""
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
        folder_edit.setValidator(
            QRegularExpressionValidator(QRegularExpression(r"^[^/]{0,255}$"))
        )
        layout.addWidget(folder_edit)

        completions = self._load_para_folder_names()
        if completions:
            completer = QCompleter(QStringListModel(completions, folder_edit), folder_edit)
            completer.setCompletionMode(QCompleter.CompletionMode.PopupCompletion)
            completer.setCaseSensitivity(Qt.CaseSensitivity.CaseInsensitive)
            completer.setFilterMode(Qt.MatchFlag.MatchContains)
            folder_edit.setCompleter(completer)

        helper = QLabel("Allowed: Linux filename characters (no '/'), up to 255 characters.")
        helper.setStyleSheet("color: #5f6368; font-size: 12px;")
        helper.setWordWrap(True)
        layout.addWidget(helper)

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

        def save_folder():
            raw_text = folder_edit.text()
            if raw_text.strip():
                if "/" in raw_text or "\x00" in raw_text:
                    QMessageBox.warning(
                        main_window,
                        "Invalid PARA Folder",
                        "Folder names cannot contain '/' characters."
                    )
                    return
                if len(raw_text) > 255:
                    QMessageBox.warning(
                        main_window,
                        "Invalid PARA Folder",
                        "Folder names must be 255 characters or fewer."
                    )
                    return
                value = raw_text
            else:
                value = None
            if self._set_project_para_folder(main_window, project_id, value):
                dialog.accept()

        def clear_folder():
            folder_edit.clear()
            if self._set_project_para_folder(main_window, project_id, None):
                dialog.accept()

        folder_edit.returnPressed.connect(save_folder)
        save_btn.clicked.connect(save_folder)
        clear_btn.clicked.connect(clear_folder)
        button_box.rejected.connect(dialog.reject)

        dialog.exec()

    def create_project_link(self, main_window):
        """Create a deep link for the selected project and copy to clipboard."""
        if not self._is_enabled():
            return
        idx = main_window.project_view.currentIndex()
        project_id = main_window.project_model.project_id_at(idx.row()) if idx.isValid() else None
        self._debug_log(f"create_project_link project_id={project_id}")
        if project_id is None:
            QMessageBox.information(main_window, "Create Project Link", "Please select a project first.")
            return

        db_path = getattr(main_window.db, "path", "")
        if not db_path:
            QMessageBox.warning(main_window, "Create Project Link", "Database path not available.")
            return

        link = self._build_link(project_id, db_path)
        QApplication.clipboard().setText(link)
        QMessageBox.information(
            main_window,
            "Project Link Created",
            f"Link copied to clipboard:\n{link}"
        )

    def _build_link(self, project_id: int, db_path: str) -> str:
        db_abs = os.path.abspath(db_path)
        db_encoded = quote(db_abs, safe='')
        return f"{self._scheme}://open?project={project_id}&db={db_encoded}"

    def _extract_link(self, argv) -> Optional[str]:
        args = list(argv[1:]) if argv else []
        for idx, arg in enumerate(args):
            if not isinstance(arg, str):
                continue
            candidate = arg.strip().strip('\'"')
            if candidate.startswith(f"{self._scheme}://"):
                return candidate
            if f"{self._scheme}://" in candidate:
                start = candidate.find(f"{self._scheme}://")
                return candidate[start:]
            if arg.startswith("--link="):
                candidate = arg.split("=", 1)[1]
                candidate = candidate.strip().strip('\'"')
                if candidate.startswith(f"{self._scheme}://"):
                    return candidate
            if arg == "--link" and idx + 1 < len(args):
                candidate = str(args[idx + 1]).strip().strip('\'"')
                if candidate.startswith(f"{self._scheme}://"):
                    return candidate
        return None

    def _parse_link(self, url: str) -> Optional[Dict[str, Any]]:
        try:
            parsed = urlparse(url)
        except Exception:
            return None
        if parsed.scheme.lower() != self._scheme:
            return None

        target = (parsed.netloc or parsed.path.lstrip('/')).lower()
        params = parse_qs(parsed.query)

        project_id = None
        project_param = params.get("project") or params.get("id")
        if project_param:
            try:
                project_id = int(project_param[0])
            except (ValueError, TypeError):
                project_id = None
        elif target == "project":
            path_id = parsed.path.strip("/").split("/", 1)[0]
            if path_id.isdigit():
                project_id = int(path_id)

        if project_id is None:
            return None

        db_path = None
        db_param = params.get("db")
        if db_param and db_param[0]:
            db_path = os.path.normpath(unquote(db_param[0]))

        return {"project_id": project_id, "db_path": db_path}

    def _handle_link(self, main_window, url: str):
        if not self._is_enabled():
            return
        self._debug_log(f"_handle_link url={url}")
        payload = self._parse_link(url)
        self._debug_log(f"_handle_link payload={payload}")
        if not payload:
            QMessageBox.warning(main_window, "Project Link", "Unsupported or invalid project link.")
            return

        db_path = payload.get("db_path")
        if db_path:
            db_path = os.path.abspath(db_path)
            current_path = os.path.abspath(getattr(main_window.db, "path", ""))
            if not os.path.exists(db_path):
                self._debug_log(f"_handle_link db_missing={db_path}")
                QMessageBox.warning(
                    main_window,
                    "Project Link",
                    f"Database file not found:\n{db_path}"
                )
                return
            if current_path != db_path:
                self._debug_log(f"_handle_link db_switch from={current_path} to={db_path}")
                try:
                    new_db = main_window.db.__class__(db_path)
                    main_window._switch_to_database(new_db, db_path)
                except Exception as e:
                    self._debug_log(f"_handle_link db_switch_error={e}")
                    QMessageBox.warning(main_window, "Project Link", f"Failed to open database:\n{e}")
                    return
        self._queue_navigation(main_window, payload["project_id"])

    def _queue_navigation(self, main_window, project_id: int):
        if not self._is_enabled():
            return
        self._debug_log(f"_queue_navigation project_id={project_id}")
        original_filters = {
            "include_done": getattr(main_window, "include_done", False),
            "pinned_only": getattr(main_window, "pinned_only", False),
            "show_no_due_dates": getattr(main_window, "show_no_due_dates", False),
            "show_effort_missing": getattr(main_window, "show_effort_missing", False),
        }
        self._pending_navigation = {
            "project_id": project_id,
            "attempts": 0,
            "original_filters": original_filters,
        }
        delay_ms = 200 if not main_window.isVisible() else 0
        QTimer.singleShot(delay_ms, lambda: self._attempt_navigation(main_window))

    def _attempt_navigation(self, main_window):
        if not self._is_enabled():
            return
        if not self._pending_navigation:
            return
        project_id = self._pending_navigation["project_id"]
        attempts = self._pending_navigation["attempts"]
        original_filters = self._pending_navigation["original_filters"]
        self._debug_log(f"_attempt_navigation project_id={project_id} attempt={attempts}")

        if self._navigate_to_project(main_window, project_id, original_filters, restore_on_failure=False):
            self._pending_navigation = None
            return

        attempts += 1
        if attempts < self._nav_max_attempts:
            self._pending_navigation["attempts"] = attempts
            QTimer.singleShot(self._nav_retry_delay_ms, lambda: self._attempt_navigation(main_window))
            return

        self._pending_navigation = None
        self._restore_filters(main_window, original_filters)
        QMessageBox.information(main_window, "Project Link", "Project not found in this database.")

    def _navigate_to_project(self, main_window, project_id: int, original_filters: Optional[Dict[str, bool]] = None, restore_on_failure: bool = True) -> bool:
        if not self._is_enabled():
            return False
        self._clear_search_state(main_window, project_id)

        if hasattr(main_window, "project_filter_edit"):
            if main_window.project_filter_edit.text():
                main_window.project_filter_edit.blockSignals(True)
                main_window.project_filter_edit.clear()
                main_window.project_filter_edit.blockSignals(False)
                try:
                    main_window.project_model.set_project_name_filter("")
                except Exception:
                    pass

        if original_filters is None:
            original_filters = {
                "include_done": getattr(main_window, "include_done", False),
                "pinned_only": getattr(main_window, "pinned_only", False),
                "show_no_due_dates": getattr(main_window, "show_no_due_dates", False),
                "show_effort_missing": getattr(main_window, "show_effort_missing", False),
            }

        row = main_window.project_model.row_for_project(project_id)
        self._debug_log(f"_navigate_to_project project_id={project_id} row={row}")
        if row < 0:
            if not main_window.include_done:
                self._set_filter_state(main_window, "include_done", "act_toggle_done", main_window.toggle_show_completed, True)
            if main_window.pinned_only:
                self._set_filter_state(main_window, "pinned_only", "act_show_pinned_filter", main_window.toggle_pinned_only, False)
            if main_window.show_no_due_dates:
                self._set_filter_state(main_window, "show_no_due_dates", "act_show_no_due_dates", main_window.toggle_show_no_due_dates, False)
            if main_window.show_effort_missing:
                self._set_filter_state(main_window, "show_effort_missing", "act_show_effort_missing", main_window.toggle_show_effort_missing, False)
            row = main_window.project_model.row_for_project(project_id)
            self._debug_log(f"_navigate_to_project retry_row={row}")
            if row < 0:
                if restore_on_failure:
                    self._restore_filters(main_window, original_filters)
                return False

        idx = main_window.project_model.index(row, 0)
        main_window.project_view.setCurrentIndex(idx)
        main_window.project_view.scrollTo(idx)
        main_window.showNormal()
        main_window.raise_()
        main_window.activateWindow()
        QTimer.singleShot(400, lambda: self._confirm_selection(main_window, project_id))
        return True

    def _clear_search_state(self, main_window, preferred_project_id: int):
        search_edit = getattr(main_window, "search_edit", None)
        search_text = search_edit.text() if search_edit is not None else ""
        search_active = bool(
            search_text
            or getattr(main_window, "search_terms", [])
            or getattr(main_window, "search_stakeholder_terms", [])
        )
        self._debug_log(f"_clear_search_state active={search_active} text={search_text!r}")
        if not search_active:
            return
        if search_edit is not None:
            search_edit.blockSignals(True)
            search_edit.clear()
            search_edit.blockSignals(False)
        try:
            main_window.search_terms = []
            main_window.search_stakeholder_terms = []
            main_window._preserve_project_id = preferred_project_id
            main_window._preserve_task_id = None
            main_window.apply_search_filter()
        except Exception:
            pass

    def _set_filter_state(self, main_window, state_attr, action_attr, toggle_method, checked: bool):
        current_state = getattr(main_window, state_attr, None)
        if current_state == checked:
            return
        action = getattr(main_window, action_attr, None)
        if action is not None:
            action.blockSignals(True)
            action.setChecked(checked)
            action.blockSignals(False)
        toggle_method(checked)

    def _restore_filters(self, main_window, original_filters: Dict[str, bool]):
        self._set_filter_state(
            main_window,
            "include_done",
            "act_toggle_done",
            main_window.toggle_show_completed,
            original_filters["include_done"],
        )
        self._set_filter_state(
            main_window,
            "pinned_only",
            "act_show_pinned_filter",
            main_window.toggle_pinned_only,
            original_filters["pinned_only"],
        )
        self._set_filter_state(
            main_window,
            "show_no_due_dates",
            "act_show_no_due_dates",
            main_window.toggle_show_no_due_dates,
            original_filters["show_no_due_dates"],
        )
        self._set_filter_state(
            main_window,
            "show_effort_missing",
            "act_show_effort_missing",
            main_window.toggle_show_effort_missing,
            original_filters["show_effort_missing"],
        )

    def _find_menu(self, main_window, title: str):
        menu_bar = main_window.menuBar() if hasattr(main_window, "menuBar") else None
        if menu_bar is None:
            return None
        for action in menu_bar.actions():
            text = action.text().replace("&", "").strip()
            if text == title:
                return action.menu()
        return None

    def _build_server_names(self) -> list[str]:
        user = os.environ.get("USER") or os.environ.get("USERNAME") or ""
        seeds = [f"project_notes_link:{user}"]

        try:
            if getattr(sys, "frozen", False):
                app_root = os.path.abspath(sys.executable)
            else:
                app_root = os.path.abspath(sys.argv[0])
            seeds.append(f"project_notes_link:{user}:{app_root}")
        except Exception:
            pass

        try:
            script_real = os.path.realpath(sys.argv[0])
            seeds.append(f"project_notes_link:{user}:{script_real}")
        except Exception:
            pass

        names = []
        for seed in seeds:
            digest = hashlib.sha1(seed.encode("utf-8")).hexdigest()[:10]
            name = f"project_notes_link_{digest}"
            if name not in names:
                names.append(name)
        return names

    def _build_debug_log_path(self) -> str:
        app_root = self._get_app_root()
        return os.path.join(app_root, "project_link_debug.log")

    def _debug_log(self, message: str):
        if not self._debug_enabled:
            return
        timestamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        line = f"{timestamp} pid={os.getpid()} {message}\n"
        try:
            with open(self._debug_log_path, "a", encoding="utf-8") as handle:
                handle.write(line)
        except Exception:
            pass

    def _ensure_server(self, main_window, app=None):
        if not self._is_enabled():
            return
        if self._server is not None:
            if main_window is not None and self._main_window is None:
                self._main_window = main_window
            return

        parent = main_window if main_window is not None else app
        server = QLocalServer(parent)
        if not server.listen(self._server_name):
            if not self._can_connect_to_server(self._server_name):
                QLocalServer.removeServer(self._server_name)
                server.listen(self._server_name)

        if server.isListening():
            self._debug_log(f"_ensure_server listening={self._server_name}")
            server.newConnection.connect(self._handle_new_connection)
            self._server = server
        else:
            self._debug_log(f"_ensure_server failed={self._server_name}")

    def _can_connect_to_server(self, server_name: str, timeout_ms: int = 400) -> bool:
        socket = QLocalSocket()
        socket.connectToServer(server_name)
        connected = socket.waitForConnected(timeout_ms)
        if connected:
            socket.disconnectFromServer()
        return connected

    def _send_to_running_instance(self, url: str, timeout_ms: int = 400) -> bool:
        for name in self._server_names:
            if self._send_to_server(name, url, timeout_ms):
                self._debug_log(f"_send_to_running_instance success server={name}")
                return True
            self._debug_log(f"_send_to_running_instance failed server={name}")
        return False

    def _send_to_server(self, server_name: str, url: str, timeout_ms: int) -> bool:
        socket = QLocalSocket()
        socket.connectToServer(server_name)
        if not socket.waitForConnected(timeout_ms):
            return False
        socket.write(url.encode("utf-8"))
        socket.flush()
        socket.waitForBytesWritten(timeout_ms)
        socket.disconnectFromServer()
        return True

    def _handle_new_connection(self):
        if not self._is_enabled():
            return
        if self._server is None:
            return
        while self._server.hasPendingConnections():
            socket = self._server.nextPendingConnection()
            socket.readyRead.connect(lambda sock=socket: self._read_socket(sock))
            socket.disconnected.connect(socket.deleteLater)

    def _read_socket(self, socket):
        if not self._is_enabled():
            try:
                socket.disconnectFromServer()
            except Exception:
                pass
            return
        try:
            data = bytes(socket.readAll()).decode("utf-8").strip()
        except Exception:
            data = ""
        self._debug_log(f"_read_socket data={data}")
        if data:
            if self._main_window is not None:
                self._handle_link(self._main_window, data)
            else:
                self._queued_links.append(data)
        socket.disconnectFromServer()

    def _confirm_selection(self, main_window, expected_project_id: int):
        try:
            idx = main_window.project_view.currentIndex()
            current_id = main_window.project_model.project_id_at(idx.row()) if idx.isValid() else None
        except Exception:
            current_id = None
        self._debug_log(f"_confirm_selection expected={expected_project_id} current={current_id}")


project_link_addon = ProjectLinkAddon()
