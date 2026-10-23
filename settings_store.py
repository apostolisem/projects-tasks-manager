"""Where the settings file lives, and moving it there from the app folder.

Settings used to be kept in settings.ini next to app.pyw. That folder can sit on a slow
filesystem (a FUSE/NTFS mount), where every write blocked the UI for ~200 ms, so the file
now lives in the per-user config folder:

    Linux    ~/.config/ProjectNotes/settings.ini
    Windows  %LOCALAPPDATA%/ProjectNotes/settings.ini
    macOS    ~/Library/Preferences/ProjectNotes/settings.ini

The format stays INI, so the old file is moved by copying it byte for byte. The environment
variable PROJECT_NOTES_SETTINGS overrides the location (tests, portable setups).
"""
from __future__ import annotations
import datetime
import os
import shutil
from typing import Callable, Dict, List

from PyQt6.QtCore import QSettings, QStandardPaths

ENV_OVERRIDE = "PROJECT_NOTES_SETTINGS"
APP_FOLDER = "ProjectNotes"
FILE_NAME = "settings.ini"
MIGRATED_SUFFIX = ".migrated"


def default_settings_path() -> str:
    """The settings file to use. No side effects, so it is safe at import time."""
    override = os.environ.get(ENV_OVERRIDE)
    if override:
        return os.path.abspath(override)
    base = QStandardPaths.writableLocation(QStandardPaths.StandardLocation.GenericConfigLocation)
    if not base:
        base = os.path.join(os.path.expanduser("~"), ".config")
    return os.path.join(base, APP_FOLDER, FILE_NAME)


def legacy_settings_path(app_dir: str) -> str:
    return os.path.join(app_dir, FILE_NAME)


def _snapshot(path: str) -> Dict[str, object]:
    settings = QSettings(path, QSettings.Format.IniFormat)
    return {key: settings.value(key) for key in settings.allKeys()}


def prepare_settings(app_dir: str) -> str:
    """Return the settings file to use, moving the old one from app_dir there first if needed.

    The copy is checked key by key before the old file is renamed to settings.ini.migrated.
    If anything goes wrong the old file is left alone and its path is returned, so no
    setting is ever lost.
    """
    if os.environ.get(ENV_OVERRIDE):
        return default_settings_path()
    new_path = default_settings_path()
    legacy_path = legacy_settings_path(app_dir)
    if os.path.exists(new_path) or not os.path.exists(legacy_path):
        return new_path

    tmp_path = new_path + ".tmp"
    try:
        os.makedirs(os.path.dirname(new_path), exist_ok=True)
        shutil.copy2(legacy_path, tmp_path)
        if _snapshot(tmp_path) != _snapshot(legacy_path):
            raise ValueError("the copied settings differ from the original")
        os.replace(tmp_path, new_path)
    except Exception as exc:
        try:
            if os.path.exists(tmp_path):
                os.remove(tmp_path)
        except OSError:
            pass
        print(f"Warning: could not move settings to {new_path} ({exc}); still using {legacy_path}")
        return legacy_path

    settings = QSettings(new_path, QSettings.Format.IniFormat)
    settings.setValue("Meta/MigratedFrom", legacy_path)
    settings.setValue("Meta/MigratedAt", datetime.datetime.now().isoformat(timespec="seconds"))
    settings.sync()
    try:
        # Kept as a backup; it is not read any more
        os.replace(legacy_path, legacy_path + MIGRATED_SUFFIX)
    except OSError as exc:
        print(f"Note: settings moved to {new_path}; the old file {legacy_path} could not be renamed ({exc})")
    return new_path


# ----- key migrations, applied once each (Meta/SettingsVersion)
def _v2_drop_recent_projects(settings: QSettings) -> None:
    """The sidebar's Recent section was removed."""
    for key in settings.allKeys():
        if key.startswith("Sidebar/RecentProjects"):
            settings.remove(key)


def _v3_visibility_checkbox(settings: QSettings) -> None:
    """ProjectVisibilityMode (task_based / manual_based) became one checkbox."""
    mode = settings.value("Preferences/ProjectVisibilityMode")
    if mode is not None and settings.value("Preferences/AutoHideFinishedProjects") is None:
        settings.setValue("Preferences/AutoHideFinishedProjects", "false" if mode == "manual_based" else "true")
    settings.remove("Preferences/ProjectVisibilityMode")


def _v4_drop_project_link_addon(settings: QSettings) -> None:
    """The project link addon was removed."""
    settings.remove("ProjectLinkAddon")


def _v5_review_always_available(settings: QSettings) -> None:
    """Periodic review no longer needs to be switched on."""
    settings.remove("Preferences/PeriodicReviewEnabled")


UPGRADES: List[Callable[[QSettings], None]] = [
    _v2_drop_recent_projects,   # -> version 2
    _v3_visibility_checkbox,    # -> version 3
    _v4_drop_project_link_addon,  # -> version 4
    _v5_review_always_available,  # -> version 5
]
CURRENT_VERSION = 1 + len(UPGRADES)


def upgrade_settings(settings: QSettings) -> None:
    """Apply the key migrations this file has not had yet."""
    try:
        version = int(settings.value("Meta/SettingsVersion", 1))
    except (TypeError, ValueError):
        version = 1
    if version >= CURRENT_VERSION:
        return
    for target, upgrade in enumerate(UPGRADES, start=2):
        if version < target:
            upgrade(settings)
    settings.setValue("Meta/SettingsVersion", CURRENT_VERSION)
    settings.sync()
