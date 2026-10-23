"""The note of a task made by merging other tasks (Task → Merge Tasks…).

Stored notes are whole Qt HTML documents, each with its own default font, so they are not
joined as text: every note is read into a QTextDocument and inserted as a document fragment,
which keeps its formatting, lists, checklists, links and images as they are.

Layout of the merged note:
    Merged from N tasks on YYYY-MM-DD (wkYYWW.D)
    ─────────────
    <title of task 1>        (heading, as the editor's Heading button makes it)
    <note of task 1>         ("(no notes)" when it has none)
    ─────────────
    <title of task 2>
    …
"""
from __future__ import annotations
from typing import Sequence, Tuple

from PyQt6.QtGui import (QFont, QTextBlockFormat, QTextCharFormat, QTextCursor, QTextDocument,
                         QTextDocumentFragment, QTextFormat, QTextLength)

from . import tokens as T
from .notes_html import strip_line_height

BODY_POINT_SIZE = 11.0       # the note editor's text (NotesEditor._apply_default_formatting)
HEADING_POINT_SIZE = 15.75   # the editor's Heading button (NOTES_HEADING_POINT_SIZE)


def _document() -> QTextDocument:
    """A document with the note editor's default font, so notes read into it look as they do there."""
    document = QTextDocument()
    font = QFont()
    font.setFamily(T.FONT_BODY)
    font.setPointSizeF(BODY_POINT_SIZE)
    document.setDefaultFont(font)
    return document


def _body_format(*, bold: bool = False, italic: bool = False) -> QTextCharFormat:
    fmt = QTextCharFormat()
    fmt.setFontFamilies([T.FONT_BODY])
    fmt.setFontPointSize(BODY_POINT_SIZE)
    fmt.setFontWeight(QFont.Weight.Bold if bold else QFont.Weight.Normal)
    fmt.setFontItalic(italic)
    return fmt


def _heading_format() -> QTextCharFormat:
    fmt = QTextCharFormat()
    fmt.setFontFamilies([T.FONT_HEADING])
    fmt.setFontPointSize(HEADING_POINT_SIZE)
    fmt.setFontWeight(QFont.Weight.DemiBold)
    return fmt


def _divider_format() -> QTextBlockFormat:
    """A paragraph drawn as a horizontal line (HTML <hr>)."""
    fmt = QTextBlockFormat()
    fmt.setProperty(QTextFormat.Property.BlockTrailingHorizontalRulerWidth,
                    QTextLength(QTextLength.Type.PercentageLength, 100))
    return fmt


def compose_merged_note(sources: Sequence[Tuple[str, str]], when: str) -> Tuple[str, str]:
    """(HTML, plain text) of the merged note. sources: (task title, stored note HTML) in the
    order the sections appear; when: the merge date as written in note entries."""
    document = _document()
    cursor = QTextCursor(document)
    cursor.setBlockFormat(QTextBlockFormat())
    noun = "task" if len(sources) == 1 else "tasks"
    cursor.insertText(f"Merged from {len(sources)} {noun}", _body_format(bold=True))
    cursor.insertText(f" on {when}", _body_format())

    for title, html in sources:
        cursor.insertBlock(_divider_format(), _body_format())
        heading = QTextBlockFormat()
        heading.setHeadingLevel(3)
        cursor.insertBlock(heading, _heading_format())
        cursor.insertText((title or "").strip() or "Untitled task", _heading_format())
        cursor.insertBlock(QTextBlockFormat(), _body_format())
        source = _document()
        source.setHtml(html or "")
        # U+FFFC (an image) is not whitespace, so a note with only images still counts
        if source.toPlainText().strip():
            cursor.insertFragment(QTextDocumentFragment(source))
        else:
            cursor.insertText("(no notes)", _body_format(italic=True))
    return strip_line_height(document.toHtml()), document.toPlainText()
