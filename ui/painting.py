"""Shared painting helpers."""
from __future__ import annotations
from typing import Union

from PyQt6.QtCore import QRect, QRectF
from PyQt6.QtGui import QColor, QPainter, QPen

from . import tokens as T


def draw_corner_marks(painter: QPainter, rect: Union[QRect, QRectF], color: Union[QColor, str] = T.CORNER_MARK,
                      size: int = T.CORNER_MARK_SIZE) -> None:
    """Draw a "+" registration mark centred on each corner of rect.

    rect is the frame whose border pixels sit on its edges, so each mark extends
    size // 2 pixels outside it. The caller has to leave that much room.
    """
    r = QRect(rect.toRect()) if isinstance(rect, QRectF) else QRect(rect)
    arm = size // 2
    painter.save()
    painter.setRenderHint(QPainter.RenderHint.Antialiasing, False)
    pen = QPen(QColor(color))
    pen.setWidth(1)
    pen.setCosmetic(True)
    painter.setPen(pen)
    for x in (r.left(), r.right()):
        for y in (r.top(), r.bottom()):
            painter.drawLine(x - arm, y, x + arm, y)
            painter.drawLine(x, y - arm, x, y + arm)
    painter.restore()


def draw_menu_shadow(painter: QPainter, frame: QRect, layers: int = 5) -> None:
    """shadow-md (0 3px 10px rgba(43,43,45,.16)) under frame, approximated with stacked
    outlines. The caller leaves `layers` pixels of room around frame (more below it)."""
    r, g, b, _alpha = T.SHADOW_MD["color"]
    for i in range(layers):
        grow = layers - i
        painter.fillRect(frame.adjusted(-grow, -grow + 3, grow, grow + 2), QColor(r, g, b, 5 + i * 2))
