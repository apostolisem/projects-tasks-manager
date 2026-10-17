"""Lucide icon loader: tints the SVG by replacing currentColor and caches the result."""
from __future__ import annotations
import os
import re
from typing import Dict, Optional, Tuple

from PyQt6.QtCore import QByteArray, QRectF, QSize, Qt
from PyQt6.QtGui import QIcon, QPainter, QPixmap
from PyQt6.QtSvg import QSvgRenderer

from . import tokens as T

ICONS_DIR = os.path.join(os.path.dirname(os.path.abspath(os.path.dirname(__file__))), "icons", "lucide")

_METADATA_RE = re.compile(r"<metadata>.*?</metadata>", re.DOTALL)
_SCALES = (1.0, 2.0)

_svg_cache: Dict[str, str] = {}
_pixmap_cache: Dict[Tuple, QPixmap] = {}
_icon_cache: Dict[Tuple, QIcon] = {}


def _svg_source(name: str) -> str:
    src = _svg_cache.get(name)
    if src is None:
        try:
            with open(os.path.join(ICONS_DIR, f"{name}.svg"), "r", encoding="utf-8") as fh:
                src = _METADATA_RE.sub("", fh.read())
        except OSError:
            print(f"Warning: icon '{name}' not found")
            src = ""
        _svg_cache[name] = src
    return src


def pixmap(name: str, size: int = 15, color: str = T.NEUTRAL_800, fill: Optional[str] = None,
           scale: float = 1.0, stroke: Optional[float] = None) -> QPixmap:
    """Render an icon at size x size logical pixels. fill colours the inside of the shape."""
    key = (name, size, color, fill, scale, stroke)
    cached = _pixmap_cache.get(key)
    if cached is not None:
        return cached
    px = max(1, round(size * scale))
    pm = QPixmap(px, px)
    pm.fill(Qt.GlobalColor.transparent)
    src = _svg_source(name)
    if src:
        if fill:
            src = src.replace('fill="none"', f'fill="{fill}"', 1)
        if stroke is not None:
            src = src.replace(f'stroke-width="{T.ICON_STROKE}"', f'stroke-width="{stroke}"', 1)
        src = src.replace("currentColor", color)
        renderer = QSvgRenderer(QByteArray(src.encode("utf-8")))
        painter = QPainter(pm)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        renderer.render(painter, QRectF(0, 0, px, px))
        painter.end()
    pm.setDevicePixelRatio(scale)
    _pixmap_cache[key] = pm
    return pm


def icon(name: str, size: int = 15, color: str = T.NEUTRAL_800, *, fill: Optional[str] = None,
         on_color: Optional[str] = None, on_fill: Optional[str] = None,
         disabled_color: str = T.NEUTRAL_400) -> QIcon:
    """Tinted QIcon. on_color / on_fill are used for the checked state of a checkable button."""
    key = (name, size, color, fill, on_color, on_fill, disabled_color)
    cached = _icon_cache.get(key)
    if cached is not None:
        return cached
    qicon = QIcon()
    for scale in _SCALES:
        qicon.addPixmap(pixmap(name, size, color, fill, scale), QIcon.Mode.Normal, QIcon.State.Off)
        qicon.addPixmap(pixmap(name, size, disabled_color, None, scale), QIcon.Mode.Disabled, QIcon.State.Off)
        if on_color or on_fill:
            on_pm = pixmap(name, size, on_color or color, on_fill or fill, scale)
            qicon.addPixmap(on_pm, QIcon.Mode.Normal, QIcon.State.On)
            qicon.addPixmap(on_pm, QIcon.Mode.Active, QIcon.State.On)
    _icon_cache[key] = qicon
    return qicon


def icon_size(size: int = 15) -> QSize:
    return QSize(size, size)
