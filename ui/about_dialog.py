"""About Project Notes: version, where the data lives, and versions for bug reports."""
from __future__ import annotations
import os
import platform
import sqlite3
from typing import List, Optional, Tuple

from PyQt6.QtCore import PYQT_VERSION_STR, QT_VERSION_STR, QRect, QSize, Qt, QTimer, QUrl
from PyQt6.QtGui import QColor, QDesktopServices, QFontMetrics, QPainter
from PyQt6.QtWidgets import (QApplication, QDialog, QFrame, QGridLayout, QHBoxLayout, QLabel, QPushButton,
                             QSizePolicy, QToolButton, QVBoxLayout, QWidget)

from . import tokens as T
from .icons import icon, pixmap
from .widgets import BrandMark

REPOSITORY_URL = "https://github.com/apostolisem/projects-tasks-manager"
LICENSE_NAME = "GPL-3.0"
DESCRIPTION = ("Projects, tasks and notes in one local database file. Smart views for what is due, "
               "in Focus or waiting; rich notes with checklists and images; periodic project review.")
ELLIPSIS = "…"


def read_version(app_dir: str) -> str:
    try:
        with open(os.path.join(app_dir, "version.txt"), encoding="utf-8") as handle:
            return handle.read().strip() or "unknown"
    except OSError:
        return "unknown"


def version_info(version: str, locations: List[Tuple[str, str]]) -> str:
    """Plain text for bug reports (Copy version info)."""
    lines = [f"Project Notes {version}",
             f"OS: {platform.platform()}",
             f"Python {platform.python_version()} · PyQt {PYQT_VERSION_STR} · Qt {QT_VERSION_STR} · "
             f"SQLite {sqlite3.sqlite_version}"]
    lines += [f"{name}: {path}" for name, path in locations]
    return "\n".join(lines)


def short_home(path: str) -> str:
    home = os.path.expanduser("~")
    if path == home or path.startswith(home + os.sep):
        return "~" + path[len(home):]
    return path


def fit_path(path: str, metrics: QFontMetrics, width: int) -> str:
    """path shortened to width: ~ for the home folder, then folders dropped from the middle
    (the first folder and the file name stay), then Qt's middle elision as a last resort."""
    text = short_home(path)
    if metrics.horizontalAdvance(text) <= width:
        return text
    sep = os.sep
    parts = text.split(sep)
    if len(parts) > 3:
        head, middle, tail = parts[:2], parts[2:-1], parts[-1:]
        while middle:
            middle = middle[1:]  # drop the outermost remaining folder first
            candidate = sep.join(head + [ELLIPSIS] + middle + tail)
            if metrics.horizontalAdvance(candidate) <= width:
                return candidate
        text = sep.join(head + [ELLIPSIS] + tail)
    return metrics.elidedText(text, Qt.TextElideMode.ElideMiddle, width)


class PathLink(QWidget):
    """A folder icon and a path; clicking opens the folder. The path is shortened to the width
    the widget really has, with the font it is really drawn in (the app style sets fonts)."""
    ICON = 14
    GAP = 6

    def __init__(self, path: str, name: str, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self._path = path
        self._folder = path if os.path.isdir(path) else os.path.dirname(path)
        self._hover = False
        self.setObjectName("PathLink")
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.setMinimumWidth(80)
        self.setAttribute(Qt.WidgetAttribute.WA_Hover, True)
        available = os.path.isdir(self._folder)
        self.setEnabled(available)
        if available:
            self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setToolTip(f"{path}\nClick to open the folder" if available else f"{path}\n(folder not found)")
        self.setAccessibleName(f"Open the folder of the {name.lower()}")

    def sizeHint(self):
        return QSize(260, self.fontMetrics().height() + 8)

    def enterEvent(self, event):
        self._hover = True
        self.update()

    def leaveEvent(self, event):
        self._hover = False
        self.update()

    def mouseReleaseEvent(self, event):
        if (event.button() == Qt.MouseButton.LeftButton and self.isEnabled()
                and self.rect().contains(event.position().toPoint())):
            QDesktopServices.openUrl(QUrl.fromLocalFile(self._folder))

    def paintEvent(self, event):
        painter = QPainter(self)
        accent = self._hover and self.isEnabled()
        icon_y = (self.height() - self.ICON) // 2
        painter.drawPixmap(0, icon_y, pixmap("folder", self.ICON, T.ACCENT_700 if accent else T.NEUTRAL_600,
                                             scale=self.devicePixelRatioF()))
        x = self.ICON + self.GAP
        font = self.font()
        font.setUnderline(accent)
        painter.setFont(font)
        painter.setPen(QColor(T.ACCENT_700 if accent else T.NEUTRAL_800))
        text = fit_path(self._path, QFontMetrics(font), self.width() - x)
        painter.drawText(QRect(x, 0, self.width() - x, self.height()),
                         Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft, text)
        painter.end()


class AboutDialog(QDialog):
    def __init__(self, app_dir: str, locations: List[Tuple[str, str]], parent: Optional[QWidget] = None):
        """locations: (label, path) pairs, e.g. ("Database", "/…/database.sqlite")."""
        super().__init__(parent)
        self.setWindowTitle("About Project Notes")
        self.setWindowFlags(Qt.WindowType.Dialog | Qt.WindowType.WindowTitleHint | Qt.WindowType.WindowCloseButtonHint)
        self.setModal(True)
        self._version = read_version(app_dir)
        self._locations = locations

        col = QVBoxLayout(self)
        col.setContentsMargins(28, 24, 28, 18)
        col.setSpacing(0)

        # --- Brand mark, name and version chip
        header = QHBoxLayout()
        header.setSpacing(14)
        header.addWidget(BrandMark(self, square=40), 0, Qt.AlignmentFlag.AlignVCenter)
        names = QVBoxLayout()
        names.setSpacing(4)
        title = QLabel("Project Notes")
        title.setObjectName("DialogTitle")
        names.addWidget(title)
        self.version_chip = QToolButton()
        self.version_chip.setObjectName("VersionChip")
        self.version_chip.setText(f"v{self._version}" if self._version[:1].isdigit() else self._version)
        self.version_chip.setToolTip("Click to copy the version info")
        self.version_chip.setCursor(Qt.CursorShape.PointingHandCursor)
        self.version_chip.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.version_chip.clicked.connect(self._copy_info)
        names.addWidget(self.version_chip, 0, Qt.AlignmentFlag.AlignLeft)
        header.addLayout(names)
        header.addStretch(1)
        col.addLayout(header)
        col.addSpacing(16)

        description = QLabel(DESCRIPTION)
        description.setObjectName("AboutText")
        description.setWordWrap(True)
        col.addWidget(description)
        col.addSpacing(20)

        # --- Files: each path opens its folder
        section = QLabel("FILES")
        section.setObjectName("AboutSection")
        col.addWidget(section)
        col.addSpacing(4)
        grid = QGridLayout()
        grid.setHorizontalSpacing(14)
        grid.setVerticalSpacing(0)
        for row, (name, path) in enumerate(locations):
            label = QLabel(name)
            label.setObjectName("AboutMuted")
            grid.addWidget(label, row, 0, Qt.AlignmentFlag.AlignVCenter)
            grid.addWidget(PathLink(path, name, self), row, 1)
        grid.setColumnStretch(1, 1)
        col.addLayout(grid)
        col.addSpacing(20)

        # --- Built with, license, repository
        built = QLabel(f"Built with Python {platform.python_version()} · PyQt {PYQT_VERSION_STR} · "
                       f"Qt {QT_VERSION_STR} · SQLite {sqlite3.sqlite_version}")
        built.setObjectName("AboutSmall")
        col.addWidget(built)
        col.addSpacing(3)
        link = QLabel(f"{LICENSE_NAME} &nbsp;·&nbsp; <a href='{REPOSITORY_URL}' style='color:{T.ACCENT_700};"
                      f"text-decoration:none'>{REPOSITORY_URL.split('://', 1)[1]}</a>")
        link.setObjectName("AboutSmall")
        link.setTextFormat(Qt.TextFormat.RichText)
        link.setOpenExternalLinks(True)
        link.setToolTip(REPOSITORY_URL)
        col.addWidget(link)
        col.addSpacing(16)

        divider = QFrame()
        divider.setFixedHeight(1)
        divider.setStyleSheet(f"background:{T.DIVIDER}")
        col.addWidget(divider)
        col.addSpacing(14)

        buttons = QHBoxLayout()
        self.copy_button = QPushButton("Copy version info")
        self.copy_button.setIcon(icon("copy", 14, T.NEUTRAL_700))
        self.copy_button.setToolTip("Version, system and file locations, for a bug report")
        self.copy_button.clicked.connect(self._copy_info)
        buttons.addWidget(self.copy_button)
        buttons.addStretch(1)
        close = QPushButton("Close")
        close.setProperty("primary", True)
        close.setDefault(True)
        close.clicked.connect(self.accept)
        buttons.addWidget(close)
        col.addLayout(buttons)

        # The app style sets the font on every widget, so sizes go through the style sheet too
        self.setStyleSheet(
            "QLabel#DialogTitle{font-size:28px}"
            f"QToolButton#VersionChip{{font-family:\"Barlow\";font-weight:500;font-size:12px;color:{T.NEUTRAL_800};"
            f"background:{T.SURFACE};border:1px solid {T.DIVIDER};padding:1px 7px}}"
            f"QToolButton#VersionChip:hover{{background:{T.HOVER};border-color:{T.ACCENT_400}}}"
            f"QLabel#AboutText{{font-size:14px;color:{T.TEXT}}}"
            f"QLabel#AboutSection{{font-family:\"Barlow Condensed\";font-weight:600;font-size:12px;color:{T.NEUTRAL_700}}}"
            f"QLabel#AboutMuted{{font-size:13px;color:{T.NEUTRAL_700}}}"
            "#PathLink{font-size:13px}"
            f"QLabel#AboutSmall{{font-size:12px;color:{T.NEUTRAL_700}}}")

        self.setFixedWidth(540)
        self.adjustSize()
        self.setFixedSize(self.size())

    def _copy_info(self):
        QApplication.clipboard().setText(version_info(self._version, self._locations))
        self.copy_button.setText("Copied")
        self.copy_button.setIcon(icon("check", 14, T.ACCENT_700))
        QTimer.singleShot(1500, self._reset_copy_button)

    def _reset_copy_button(self):
        self.copy_button.setText("Copy version info")
        self.copy_button.setIcon(icon("copy", 14, T.NEUTRAL_700))
