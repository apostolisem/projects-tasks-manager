"""App-wide search: query syntax, accent-insensitive matching, highlight spans and note excerpts.

Query syntax (words are combined with AND):
    word          the word anywhere in the project name, task title or notes
    "two words"   the exact phrase
    -word         leave out tasks that contain the word ( -"two words" for a phrase )
    @name         tasks with a stakeholder whose name contains name ( @"first last" ); several: any of them
    in:name       only projects whose name contains name ( in:"two words" ); several: any of them
    is:done       only completed tasks; is:open only open tasks (overrides the Completed chip)

Matching ignores case and accents: "ταξιδι" finds "Ταξίδι", "cafe" finds "Café", "σ" finds "ς".
"""
from __future__ import annotations
import bisect
import re
import unicodedata
from dataclasses import dataclass, field
from typing import Callable, List, Optional, Tuple

Span = Tuple[int, int]  # (start, end) in the original text

_TOKEN_RE = re.compile(r'(-?)(@?)(?:(\w+):)?(?:"([^"]*)"?|(\S+))', re.UNICODE)
_SPACE_RE = re.compile(r"\s+")


def _fold_char(ch: str) -> str:
    decomposed = unicodedata.normalize("NFD", ch)
    return "".join(c for c in decomposed if not unicodedata.combining(c)).casefold()


def fold(text) -> Optional[str]:
    """Lower case without accents. None stays None (it is also the SQL function FOLD)."""
    if not isinstance(text, str):
        return text
    if text.isascii():
        return text.lower()
    # Same result as folding character by character (_fold_char), in one pass: NFD only
    # reorders combining marks, and those are all dropped
    decomposed = unicodedata.normalize("NFD", text)
    return "".join(c for c in decomposed if not unicodedata.combining(c)).casefold()


def fold_with_map(text: str) -> Tuple[str, List[int]]:
    """Folded text plus, for each folded character, the index of the original character."""
    if text.isascii():
        return text.lower(), list(range(len(text)))
    out: List[str] = []
    index: List[int] = []
    for i, ch in enumerate(text):
        folded = _fold_char(ch) if not ch.isascii() else ch.lower()
        out.append(folded)
        index.extend([i] * len(folded))
    return "".join(out), index


def like_pattern(term: str) -> str:
    """%term% for LIKE ... ESCAPE '\\', with %, _ and \\ in the term taken literally."""
    escaped = term.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{escaped}%"


@dataclass
class Query:
    terms: List[str] = field(default_factory=list)          # words and phrases as typed
    excluded: List[str] = field(default_factory=list)       # -word, as typed
    stakeholders: List[str] = field(default_factory=list)   # @name without the @
    projects: List[str] = field(default_factory=list)       # in:name
    status: Optional[str] = None                            # 'done', 'open' or None (follow the chip)

    @property
    def active(self) -> bool:
        return bool(self.terms or self.excluded or self.stakeholders or self.projects or self.status)

    def folded_terms(self) -> List[str]:
        return [folded for t in self.terms if (folded := fold(t))]


def parse_query(text: str) -> Query:
    query = Query()
    for match in _TOKEN_RE.finditer(text or ""):
        negate, at, prefix, quoted, bare = (match.group(i) for i in range(1, 6))
        value = quoted if quoted is not None else (bare or "")
        value = _SPACE_RE.sub(" ", value).strip()
        key = (prefix or "").lower()
        if at:
            if prefix:
                value = f"{prefix}:{value}"
            if value:
                query.stakeholders.append(value)
        elif key == "in" and value:
            query.projects.append(value)
        elif key == "is" and value.lower() in ("done", "completed", "closed"):
            query.status = "done"
        elif key == "is" and value.lower() in ("open", "todo", "active"):
            query.status = "open"
        else:
            if prefix:  # not a known keyword: search for it as typed ("re:meeting")
                value = f"{prefix}:{value}" if value else prefix
            if not value or value == "-":
                continue
            if negate:
                query.excluded.append(value)
            else:
                query.terms.append(value)
    return query


def find_spans(text: str, terms: List[str], limit: int = 50) -> List[Span]:
    """Where the (already folded) terms occur in text, as merged spans of the original text."""
    if not text or not terms:
        return []
    folded, index = fold_with_map(text)
    spans: List[Span] = []
    for term in terms:
        if not term:
            continue
        start = folded.find(term)
        while start >= 0 and len(spans) < limit:
            end = start + len(term)
            original_end = index[end - 1] + 1
            while original_end < len(text) and unicodedata.combining(text[original_end]):
                original_end += 1
            spans.append((index[start], original_end))
            start = folded.find(term, end)
    return merge_spans(spans)


def merge_spans(spans: List[Span]) -> List[Span]:
    merged: List[Span] = []
    for start, end in sorted(spans):
        if merged and start <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((start, end))
    return merged


def excerpt(text: str, terms: List[str], before: int = 40, length: int = 160) -> Optional[Tuple[str, List[Span]]]:
    """A one-line piece of text around the first match of the folded terms, with match spans
    relative to the excerpt. None when no term occurs in text."""
    if not text:
        return None
    spans = find_spans(text, terms)
    if not spans:
        return None
    first = spans[0][0]
    start = max(0, first - before)
    if start:
        # Start on a word boundary
        space = text.rfind(" ", 0, start + 1)
        start = space + 1 if space >= 0 and first - space <= before + 15 else start
    end = min(len(text), start + length)
    raw = text[start:end]
    # One line: whitespace runs (newlines included) become single spaces, spans follow along
    out: List[str] = []
    pos_map: List[int] = []
    prev_space = False
    for i, ch in enumerate(raw):
        if ch.isspace():
            if prev_space or not out:
                pos_map.append(len(out))
                continue
            ch = " "
            prev_space = True
        else:
            prev_space = False
        pos_map.append(len(out))
        out.append(ch)
    pos_map.append(len(out))
    prefix = "…" if start > 0 else ""
    body = "".join(out).rstrip()
    suffix = "…" if end < len(text) else ""
    shift = len(prefix)
    rel = []
    for s, e in spans:
        if s >= start and e <= end:
            a, b = pos_map[s - start] + shift, pos_map[e - start] + shift
            if b > a:
                rel.append((a, min(b, shift + len(body))))
    return prefix + body + suffix, rel


def score_task(title_hits: int, project_hits: int, note_hits: int, terms_in_title: int, term_count: int) -> int:
    """Relevance of a result: every query word in the title first, then partial title matches,
    then matches through the project name, then notes only."""
    score = 0
    if term_count and terms_in_title == term_count:
        score += 1000
    score += 100 * terms_in_title + 10 * min(title_hits, 5) + 5 * min(project_hits, 3) + min(note_hits, 5)
    return score


def utf16_offset(text: str, position: int) -> int:
    """Translate a Python character index to the UTF-16 positions used by Qt."""
    return len(text[:position].encode("utf-16-le")) // 2


def utf16_mapper(text: str) -> Callable[[int], int]:
    """utf16_offset for many positions in one text, without re-encoding the text each time.
    Characters outside the BMP take two UTF-16 units; everything else one."""
    astral = [i for i, ch in enumerate(text) if ord(ch) > 0xFFFF] if not text.isascii() else []
    if not astral:
        return lambda position: position
    return lambda position: position + bisect.bisect_left(astral, position)
