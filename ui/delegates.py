"""Item delegates shared by the sidebar and the task list."""
from __future__ import annotations

from PyQt6.QtWidgets import QLineEdit, QStyledItemDelegate


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
        editor.setStyleSheet("QLineEdit{padding:4px 6px; border:1px solid #5980a6; background:#f2f2f3;}")
        
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
