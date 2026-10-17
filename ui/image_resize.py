"""Transient, non-destructive image resize state and a clipped image preview."""
from dataclasses import dataclass
from PyQt6.QtCore import Qt, QRect, QPoint
from PyQt6.QtGui import QPainter
from PyQt6.QtWidgets import QWidget

MAX_IMAGE_DIMENSION = 32767


def constrained_size(width, height, ratio=None):
    """Bound one scale factor when locked, preserving even very thin images."""
    if ratio:
        width = max(ratio, 1, min(float(width), MAX_IMAGE_DIMENSION,
                                  MAX_IMAGE_DIMENSION * ratio))
        height = width / ratio
    return (max(1, min(MAX_IMAGE_DIMENSION, round(width))),
            max(1, min(MAX_IMAGE_DIMENSION, round(height))))


@dataclass
class ResizeSession:
    active: bool = False
    path: str = None
    origin_size: tuple = None
    current_size: tuple = None
    cursor: object = None
    anchor: object = None
    rubber: object = None
    handle: object = None
    preview: object = None
    dragging: bool = False
    aspect: float = None
    locked: bool = True
    drag_point: object = None
    drag_size: tuple = None
    drag_scroll: tuple = None
    pointer: object = None
    shift: bool = False
    revision: int = 0


class ImageResizePreview(QWidget):
    def __init__(self, image, parent):
        super().__init__(parent)
        self.image = image
        self.target_size = image.size()
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.fillRect(self.rect(), self.parentWidget().palette().base())
        painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
        painter.drawImage(QRect(QPoint(0, 0), self.target_size), self.image)
