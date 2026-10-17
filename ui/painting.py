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
