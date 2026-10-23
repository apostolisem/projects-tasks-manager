"""Matching a typed query against a name, word by word.

Every word of the query must match a different word of the name, in any order. A query
word matches a name word when it starts it ("ba" → "backups"), appears in it, or (three
letters or more) has its letters in the same order inside it ("gent" → "management").
So "ba gent" finds both "Backups management" and "Back to Gent", the closer match first.
"""
from __future__ import annotations
import re
from typing import List, Optional, Tuple

Span = Tuple[int, int]  # (start, end) in the name, for drawing the matched letters in bold

_WORD_RE = re.compile(r"[^\W_]+", re.UNICODE)

PREFIX, INSIDE, LETTERS = 3, 2, 1  # how a query word matched, best first
LETTERS_MIN = 3  # shortest query word allowed to match by letters in order


def query_terms(query: str) -> List[str]:
    return [term for term in query.lower().split() if term]


def _match_word(term: str, word: str, offset: int) -> Optional[Tuple[int, int, List[Span]]]:
    """(kind, tightness, spans) when term matches word (word starts at offset in the name)."""
    if word.startswith(term):
        return PREFIX, 0, [(offset, offset + len(term))]
    pos = word.find(term)
    if pos >= 0:
        return INSIDE, 0, [(offset + pos, offset + pos + len(term))]
    if len(term) < LETTERS_MIN:
        return None  # one or two letters scattered through a word match almost anything
    # Letters in order, each at its first possible place
    spans: List[Span] = []
    at = -1
    for ch in term:
        at = word.find(ch, at + 1)
        if at < 0:
            return None
        if spans and spans[-1][1] == offset + at:
            spans[-1] = (spans[-1][0], offset + at + 1)
        else:
            spans.append((offset + at, offset + at + 1))
    gaps = len(spans) - 1
    return LETTERS, gaps, spans


def match(query: str, name: str) -> Optional[Tuple[int, List[Span]]]:
    """(score, spans) when every query word matches a different word of name, else None.
    An empty query matches everything with score 0."""
    terms = query_terms(query)
    if not terms:
        return 0, []
    words = [(m.start(), m.group().lower()) for m in _WORD_RE.finditer(name)]
    used: set = set()
    score = 0
    spans: List[Span] = []
    # Longer query words first: they are the most selective
    for term in sorted(terms, key=len, reverse=True):
        best = None
        for index, (offset, word) in enumerate(words):
            if index in used:
                continue
            found = _match_word(term, word, offset)
            if found is None:
                continue
            kind, gaps, term_spans = found
            # Better kind first, then fewer gaps, then earlier in the name
            rank = (kind, -gaps, -index)
            if best is None or rank > best[0]:
                best = (rank, index, term_spans)
        if best is None:
            return None
        (kind, neg_gaps, _neg_index), index, term_spans = best
        used.add(index)
        spans.extend(term_spans)
        score += {PREFIX: 40, INSIDE: 25, LETTERS: 12}[kind] * len(term) + 5 * neg_gaps
        if index == 0:
            score += 10
    return score - len(name) // 10, merge_spans(spans)


def match_score(query: str, name: str) -> Optional[int]:
    result = match(query, name)
    return None if result is None else result[0]


def merge_spans(spans: List[Span]) -> List[Span]:
    merged: List[Span] = []
    for start, end in sorted(spans):
        if merged and start <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((start, end))
    return merged
