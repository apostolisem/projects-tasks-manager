"""Editing features for the notes editor: links, checklists, type-to-format, find and line spacing.

NotesFeaturesMixin is mixed into the app's NotesEditor (a QTextEdit). It only adds methods; the
editor calls them from its own event handlers. Signals stay on the editor class itself.
"""
from __future__ import annotations
from typing import List, Optional, Tuple

from PyQt6.QtCore import Qt, QUrl, QTimer
from PyQt6.QtGui import (QColor, QDesktopServices, QFont, QSyntaxHighlighter, QTextBlockFormat, QTextCharFormat,
                         QTextCursor, QTextDocument, QTextListFormat)
from PyQt6.QtWidgets import (QDialog, QDialogButtonBox, QFormLayout, QLabel, QLineEdit, QTextEdit, QVBoxLayout)

from . import tokens as T
from .search import find_spans, fold, fold_with_map, utf16_mapper, utf16_offset
from .notes_html import (LINE_DISTANCE_PX, URL_RE, href_for, is_allowed_link, split_trailing_punctuation,
                         task_id_from_link)

INTERNAL_MIME = "application/x-project-notes-fragment"
MAX_FIND_MATCHES = 500
LINE_DISTANCE_TYPE = QTextBlockFormat.LineHeightTypes.LineDistanceHeight.value

# Typed at the start of an empty line and followed by Space
AUTOFORMAT_MARKERS = {
    "-": "bullet", "*": "bullet",
    "1.": "numbered",
    "#": "heading",
    "[]": "checklist", "[ ]": "checklist",
}


class ChecklistHighlighter(QSyntaxHighlighter):
    """Shows checked checklist items struck through and muted. Display only; nothing is stored."""

    def __init__(self, document: QTextDocument):
        super().__init__(document)
        self._checked = QTextCharFormat()
        self._checked.setFontStrikeOut(True)
        self._checked.setForeground(QColor(T.NEUTRAL_600))

    def highlightBlock(self, text: str):
        if self.currentBlock().blockFormat().marker() == QTextBlockFormat.MarkerType.Checked:
            self.setFormat(0, utf16_offset(text, len(text)), self._checked)


class LinkDialog(QDialog):
    """Insert or edit a link: display text and address."""

    def __init__(self, text: str = "", url: str = "", parent=None):
        super().__init__(parent)
        self.setWindowTitle("Link")
        self.setMinimumWidth(420)
        self.text_edit = QLineEdit(text)
        self.url_edit = QLineEdit(url)
        self.url_edit.setPlaceholderText("https://")
        form = QFormLayout()
        form.addRow("Text", self.text_edit)
        form.addRow("Address", self.url_edit)
        self.error = QLabel("")
        self.error.setStyleSheet(f"color:{T.ACCENT_800}")
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self._accept)
        buttons.rejected.connect(self.reject)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 20, 20, 14)
        layout.setSpacing(10)
        layout.addLayout(form)
        layout.addWidget(self.error)
        layout.addWidget(buttons)
        (self.url_edit if text else self.text_edit).setFocus()

    def _accept(self):
        url = self.url_edit.text().strip()
        if not url:
            self.error.setText("Enter an address.")
            return
        if not is_allowed_link(href_for(url)):
            self.error.setText("Only http, https and mailto links are supported.")
            return
        self.accept()

    def values(self) -> Tuple[str, str]:
        url = href_for(self.url_edit.text().strip())
        return (self.text_edit.text().strip() or url), url


def link_char_format(href: str) -> QTextCharFormat:
    fmt = QTextCharFormat()
    fmt.setAnchor(True)
    fmt.setAnchorHref(href)
    fmt.setFontUnderline(True)
    fmt.setForeground(QColor(T.ACCENT_700))
    return fmt


def plain_char_format_from_link() -> QTextCharFormat:
    """Format that undoes link_char_format when merged."""
    fmt = QTextCharFormat()
    fmt.setAnchor(False)
    fmt.setAnchorHref("")
    fmt.setFontUnderline(False)
    fmt.clearForeground()
    return fmt


class NotesFeaturesMixin:
    """Mixed into NotesEditor. Expects QTextEdit plus the editor's own formatting helpers."""

    def init_notes_features(self):
        self._spacing_busy = False
        self._find_text = ""
        self._find_matches: List[QTextCursor] = []
        self._find_index = -1
        self._search_terms: List[str] = []
        self._checklist_highlighter = ChecklistHighlighter(self.document())
        self.document().blockCountChanged.connect(self._on_block_count_changed)
        self._highlight_timer = QTimer(self)
        self._highlight_timer.setSingleShot(True)
        self._highlight_timer.setInterval(80)
        self._highlight_timer.timeout.connect(self._refresh_highlights_after_edit)
        self.textChanged.connect(self._highlight_timer.start)
        self._search_matches = []
        self._search_index = -1
        self.viewport().setMouseTracking(True)

    # ------------------------------------------------------------------ line spacing
    def apply_line_spacing(self, whole_document: bool = False):
        """Give text blocks the on-screen line distance. Stripped again when the note is saved."""
        if self._spacing_busy:
            return
        self._spacing_busy = True
        try:
            fmt = QTextBlockFormat()
            fmt.setLineHeight(LINE_DISTANCE_PX, LINE_DISTANCE_TYPE)
            if whole_document:
                cursor = QTextCursor(self.document())
                cursor.select(QTextCursor.SelectionType.Document)
                cursor.mergeBlockFormat(fmt)
                return
            block = self.document().begin()
            while block.isValid():
                if block.blockFormat().lineHeightType() != LINE_DISTANCE_TYPE:
                    cursor = QTextCursor(block)
                    # Part of whatever edit created the block, so undo does not stop on it
                    cursor.joinPreviousEditBlock()
                    cursor.mergeBlockFormat(fmt)
                    cursor.endEditBlock()
                block = block.next()
        finally:
            self._spacing_busy = False

    def _on_block_count_changed(self, _count: int):
        if not self._spacing_busy and not getattr(self, "_loading_note", False):
            self.apply_line_spacing()

    # ------------------------------------------------------------------ links
    def anchor_range_at_cursor(self) -> Optional[Tuple[int, int, str]]:
        """(start, end, href) of the link under the text cursor, if any."""
        cursor = self.textCursor()
        position = cursor.position()
        block = cursor.block()
        found: Optional[Tuple[int, int, str]] = None
        it = block.begin()
        runs: List[Tuple[int, int, str]] = []
        while not it.atEnd():
            fragment = it.fragment()
            if fragment.isValid():
                fmt = fragment.charFormat()
                href = fmt.anchorHref() if fmt.isAnchor() else ""
                start, end = fragment.position(), fragment.position() + fragment.length()
                if href and runs and runs[-1][2] == href and runs[-1][1] == start:
                    runs[-1] = (runs[-1][0], end, href)
                else:
                    runs.append((start, end, href))
            it += 1
        for start, end, href in runs:
            if href and start <= position <= end:
                found = (start, end, href)
                break
        return found

    def autolink_before_cursor(self) -> bool:
        """Turn a URL that was just typed (the word before the cursor) into a link."""
        cursor = self.textCursor()
        if cursor.hasSelection():
            return False
        block = cursor.block()
        column = cursor.position() - block.position()
        before = block.text().encode('utf-16-le')[:column * 2].decode('utf-16-le')
        token_start = max(before.rfind(" "), before.rfind("\t"), before.rfind(" ")) + 1
        token = split_trailing_punctuation(before[token_start:])
        if not token or not URL_RE.fullmatch(token):
            return False
        start = block.position() + utf16_offset(before, token_start)
        probe = QTextCursor(self.document())
        probe.setPosition(start + 1)
        if probe.charFormat().isAnchor():
            return False
        typing_format = self.currentCharFormat()
        work = QTextCursor(self.document())
        work.setPosition(start)
        work.setPosition(start + utf16_offset(token, len(token)), QTextCursor.MoveMode.KeepAnchor)
        work.mergeCharFormat(link_char_format(href_for(token)))
        # What is typed next is ordinary text again
        self.setCurrentCharFormat(typing_format)
        return True

    def link_at(self, viewport_pos) -> str:
        href = self.anchorAt(viewport_pos)
        return href if href and (is_allowed_link(href) or task_id_from_link(href) is not None) else ""

    @staticmethod
    def link_label(href: str) -> str:
        """What a link leads to, for its tooltip."""
        return "Merged task" if task_id_from_link(href) is not None else href

    def open_link(self, href: str) -> bool:
        task_id = task_id_from_link(href)
        if task_id is not None:
            # A link to another task (a "Merged into" entry) opens that task in the app
            self.taskLinkActivated.emit(task_id)
            return True
        if not href or not is_allowed_link(href):
            return False
        return QDesktopServices.openUrl(QUrl(href))

    def edit_link(self):
        """Ctrl+L: insert a link, or edit the one under the cursor."""
        cursor = self.textCursor()
        existing = self.anchor_range_at_cursor()
        if existing and not cursor.hasSelection():
            start, end, href = existing
            cursor.setPosition(start)
            cursor.setPosition(end, QTextCursor.MoveMode.KeepAnchor)
        else:
            href = ""
        selected = cursor.selectedText().replace(" ", " ")
        if not href and selected and URL_RE.fullmatch(selected.strip()):
            href = href_for(selected.strip())
        dialog = LinkDialog(selected, href, self)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        text, url = dialog.values()
        typing_format = self.currentCharFormat()
        if typing_format.isAnchor():
            typing_format.merge(plain_char_format_from_link())
        fmt = QTextCharFormat(cursor.charFormat())
        fmt.merge(link_char_format(url))
        cursor.beginEditBlock()
        cursor.insertText(text, fmt)
        cursor.endEditBlock()
        self.setTextCursor(cursor)
        self.setCurrentCharFormat(typing_format)
        self._schedule_autosave()

    def remove_link(self):
        existing = self.anchor_range_at_cursor()
        if not existing:
            return
        start, end, _href = existing
        cursor = QTextCursor(self.document())
        cursor.setPosition(start)
        cursor.setPosition(end, QTextCursor.MoveMode.KeepAnchor)
        cursor.mergeCharFormat(plain_char_format_from_link())
        self._schedule_autosave()

    # ------------------------------------------------------------------ checklists
    @staticmethod
    def is_checklist_block(block) -> bool:
        return block.isValid() and block.blockFormat().marker() != QTextBlockFormat.MarkerType.NoMarker

    def _selected_blocks(self):
        cursor = self.textCursor()
        block = self.document().findBlock(cursor.selectionStart())
        last = self.document().findBlock(max(cursor.selectionStart(), cursor.selectionEnd() - 1))
        blocks = []
        while block.isValid():
            blocks.append(block)
            if block == last:
                break
            block = block.next()
        return blocks

    def _set_block_marker(self, block, marker):
        cursor = QTextCursor(block)
        fmt = QTextBlockFormat()
        fmt.setMarker(marker)
        cursor.mergeBlockFormat(fmt)

    def make_checklist(self):
        """Turn the selected paragraphs into unchecked checklist items."""
        cursor = self.textCursor()
        cursor.beginEditBlock()
        blocks = self._selected_blocks()
        if not all(block.textList() is not None for block in blocks):
            list_fmt = QTextListFormat()
            list_fmt.setStyle(QTextListFormat.Style.ListDisc)
            cursor.createList(list_fmt)
            blocks = self._selected_blocks()
        for block in blocks:
            if not self.is_checklist_block(block):
                self._set_block_marker(block, QTextBlockFormat.MarkerType.Unchecked)
        cursor.endEditBlock()

    def remove_checklist(self):
        """Back to plain paragraphs."""
        cursor = self.textCursor()
        cursor.beginEditBlock()
        for block in self._selected_blocks():
            self._set_block_marker(block, QTextBlockFormat.MarkerType.NoMarker)
            text_list = block.textList()
            if text_list is not None:
                text_list.remove(block)
                work = QTextCursor(block)
                fmt = work.blockFormat()
                fmt.setIndent(0)
                work.setBlockFormat(fmt)
        cursor.endEditBlock()
        self.apply_line_spacing()

    def toggle_checklist(self):
        """Toolbar button: checklist items on or off for the selected paragraphs."""
        blocks = self._selected_blocks()
        if blocks and all(self.is_checklist_block(block) for block in blocks):
            self.remove_checklist()
        else:
            self.make_checklist()
        self._after_common_format_change()
        self._sync_format_action_state()

    def toggle_checked(self):
        """Ctrl+Shift+C: tick or untick the items; a plain paragraph becomes an item first."""
        blocks = self._selected_blocks()
        if not blocks or not all(self.is_checklist_block(block) for block in blocks):
            self.make_checklist()
        else:
            all_checked = all(block.blockFormat().marker() == QTextBlockFormat.MarkerType.Checked for block in blocks)
            marker = QTextBlockFormat.MarkerType.Unchecked if all_checked else QTextBlockFormat.MarkerType.Checked
            cursor = self.textCursor()
            cursor.beginEditBlock()
            for block in blocks:
                self._set_block_marker(block, marker)
            cursor.endEditBlock()
        self._after_common_format_change()
        self._sync_format_action_state()

    # ------------------------------------------------------------------ type-to-format
    def try_autoformat_on_space(self) -> bool:
        """Called for a plain Space key press. True when the typed marker was turned into formatting."""
        cursor = self.textCursor()
        if cursor.hasSelection():
            return False
        block = cursor.block()
        text = block.text()
        if cursor.position() - block.position() != len(text):
            return False
        kind = AUTOFORMAT_MARKERS.get(text)
        if kind is None:
            return False
        in_list = block.textList() is not None
        if in_list and kind in ("bullet", "numbered"):
            return False
        if kind == "heading" and (in_list or block.blockFormat().headingLevel() == 3):
            return False
        cursor.beginEditBlock()
        cursor.movePosition(QTextCursor.MoveOperation.StartOfBlock, QTextCursor.MoveMode.KeepAnchor)
        cursor.removeSelectedText()
        self.setTextCursor(cursor)
        if kind == "bullet":
            list_fmt = QTextListFormat()
            list_fmt.setStyle(QTextListFormat.Style.ListDisc)
            cursor.createList(list_fmt)
        elif kind == "numbered":
            list_fmt = QTextListFormat()
            list_fmt.setStyle(QTextListFormat.Style.ListDecimal)
            cursor.createList(list_fmt)
        elif kind == "heading":
            self.toggle_heading()
        elif kind == "checklist":
            self.make_checklist()
        cursor.endEditBlock()
        self._after_common_format_change()
        self._sync_format_action_state()
        return True

    # ------------------------------------------------------------------ find + search-term highlight
    def _find_all(self, text, limit=MAX_FIND_MATCHES):
        matches = []
        term = fold(text)
        if not term:
            return matches
        original = self.document().toPlainText()
        folded, mapping = fold_with_map(original)
        to_utf16 = utf16_mapper(original)
        position = folded.find(term)
        while position >= 0 and len(matches) < limit:
            start, end = mapping[position], mapping[position + len(term) - 1] + 1
            cursor = QTextCursor(self.document())
            cursor.setPosition(to_utf16(start))
            cursor.setPosition(to_utf16(end), QTextCursor.MoveMode.KeepAnchor)
            matches.append(cursor)
            position = folded.find(term, position + len(term))
        return matches

    def set_find_text(self, text: str) -> int:
        """Highlight every match of text; the first one at or after the cursor becomes current."""
        self._find_text = text or ""
        self._find_matches = self._find_all(self._find_text)
        self._find_index = -1
        if self._find_matches:
            position = self.textCursor().selectionStart()
            self._find_index = next(
                (i for i, match in enumerate(self._find_matches) if match.selectionStart() >= position), 0)
            self._reveal_current_match()
        self._refresh_extra_selections()
        self.findStateChanged.emit(*self.find_position())
        return len(self._find_matches)

    def find_step(self, step: int) -> Tuple[int, int]:
        """Move to the next (+1) or previous (-1) match. Returns (1-based index, count)."""
        count = len(self._find_matches)
        if not count:
            return 0, 0
        self._find_index = (self._find_index + step) % count
        self._reveal_current_match()
        self._refresh_extra_selections()
        return self._find_index + 1, count

    def find_position(self) -> Tuple[int, int]:
        return (self._find_index + 1 if self._find_matches else 0), len(self._find_matches)

    def clear_find(self):
        self._find_text = ""
        self._find_matches = []
        self._find_index = -1
        self._refresh_extra_selections()
        self.findStateChanged.emit(0, 0)

    def _reveal_current_match(self):
        if 0 <= self._find_index < len(self._find_matches):
            match = self._find_matches[self._find_index]
            view_cursor = QTextCursor(self.document())
            view_cursor.setPosition(match.selectionStart())
            scroll = self.verticalScrollBar().value()
            previous = self.textCursor()
            self.setTextCursor(view_cursor)
            self.ensureCursorVisible()
            # Only scroll; the user's own cursor and selection stay where they were
            new_scroll = self.verticalScrollBar().value()
            self.setTextCursor(previous)
            self.verticalScrollBar().setValue(new_scroll if new_scroll != scroll else scroll)

    def set_search_terms(self, terms: List[str]):
        """Words from the app-wide search to highlight in the open note."""
        terms = [t for t in (terms or []) if t]
        if terms == self._search_terms:
            return
        self._search_terms = terms
        self._search_index = -1
        self._refresh_extra_selections()

    def _refresh_highlights_after_edit(self):
        if self._find_text:
            current = self._find_index
            self._find_matches = self._find_all(self._find_text)
            self._find_index = max(0, min(current, len(self._find_matches) - 1)) if self._find_matches else -1
        self._refresh_extra_selections()
        self.findStateChanged.emit(*self.find_position())

    def _refresh_extra_selections(self):
        selections = []

        def add(cursor: QTextCursor, background: str, foreground: Optional[str] = None):
            selection = QTextEdit.ExtraSelection()
            selection.cursor = cursor
            selection.format.setBackground(QColor(background))
            if foreground:
                selection.format.setForeground(QColor(foreground))
            selections.append(selection)

        self._search_matches = []
        # App-wide search words: ignore case and accents, like the search itself
        if self._search_terms:
            text = self.document().toPlainText()
            to_utf16 = utf16_mapper(text)
            for start, end in find_spans(text, [fold(t) for t in self._search_terms], 200):
                cursor = QTextCursor(self.document())
                cursor.setPosition(to_utf16(start))
                cursor.setPosition(to_utf16(end), QTextCursor.MoveMode.KeepAnchor)
                self._search_matches.append(cursor)
                add(cursor, '#ffe08a' if len(self._search_matches) - 1 == self._search_index else '#fff2b3')
        self._search_index = min(self._search_index, len(self._search_matches) - 1)
        self.searchStateChanged.emit(self._search_index + 1, len(self._search_matches))
        for i, match in enumerate(self._find_matches):
            if i == self._find_index:
                add(match, T.ACCENT, T.BG)
            else:
                add(match, T.ACCENT_300)
        # Typing pauses land here too; with nothing highlighted before or now there is nothing to repaint
        if selections or self.extraSelections():
            self.setExtraSelections(selections)

    def search_step(self, step):
        if not self._search_matches:
            return
        self._search_index = ((0 if step > 0 else len(self._search_matches) - 1) if self._search_index < 0
                              else (self._search_index + step) % len(self._search_matches))
        match = self._search_matches[self._search_index]
        previous = self.textCursor()
        cursor = QTextCursor(match)
        cursor.clearSelection()
        self.setTextCursor(cursor)
        self.ensureCursorVisible()
        scroll = self.verticalScrollBar().value()
        self.setTextCursor(previous)
        self.verticalScrollBar().setValue(scroll)
        self._refresh_extra_selections()
