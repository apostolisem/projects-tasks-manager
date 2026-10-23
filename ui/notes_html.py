"""Pure helpers for note HTML: display normalising, save-time stripping, paste sanitising.

Nothing here needs a QApplication, so the functions can be unit-tested directly.
"""
from __future__ import annotations
import datetime
import html as html_lib
import re
from html.parser import HTMLParser
from typing import List, Optional

from . import tokens as T

# Extra pixels between lines while a note is on screen (about 1.5 line height for body text).
LINE_DISTANCE_PX = 6

_FONT_FAMILY_RE = re.compile(r"font-family:[^;\"]+;")
_BLACK_COLOR_RE = re.compile(r"(?<![-\w])color:\s*#000000;\s*", re.IGNORECASE)
_LINE_HEIGHT_RE = re.compile(r"\s*line-height:[0-9.]+;\s*-qt-line-height-type:\s*line-distance;")

URL_RE = re.compile(r"(?:https?://|www\.)[^\s<>\"']+", re.IGNORECASE)
_TRAILING_PUNCTUATION = ".,;:!?)]}'\""
ALLOWED_LINK_SCHEMES = ("http", "https", "mailto")


def normalize_for_display(html: str) -> str:
    """Bring a stored note in line with the theme without touching what the user chose.

    Font families other than the app fonts become the body font, and explicit pure-black text
    falls back to the theme text colour. Sizes, weights, highlights and other colours are kept.
    """
    if not html:
        return html

    def _family(match: re.Match) -> str:
        return match.group(0) if "Barlow" in match.group(0) else f"font-family:'{T.FONT_BODY}';"

    html = _FONT_FAMILY_RE.sub(_family, html)
    return _BLACK_COLOR_RE.sub("", html)


def strip_line_height(html: str) -> str:
    """Remove the on-screen line spacing so stored and copied HTML stay as they always were."""
    return _LINE_HEIGHT_RE.sub("", html) if html else html


def split_trailing_punctuation(token: str) -> str:
    """URL part of a typed token, without sentence punctuation that follows it."""
    while token and token[-1] in _TRAILING_PUNCTUATION:
        # keep a closing bracket that belongs to the URL, e.g. .../Foo_(bar)
        if token[-1] == ")" and token.count("(") >= token.count(")"):
            break
        token = token[:-1]
    return token


def href_for(text: str) -> str:
    return text if re.match(r"^[a-z][a-z0-9+.-]*:", text, re.IGNORECASE) else "https://" + text


def is_allowed_link(href: str) -> bool:
    scheme = href.split(":", 1)[0].lower() if ":" in href else ""
    return scheme in ALLOWED_LINK_SCHEMES


# Links between tasks ("Merged into" entries): opened inside the app, never in the browser
TASK_LINK_SCHEME = "projectnotes-task"


def task_link(task_id: int) -> str:
    return f"{TASK_LINK_SCHEME}:{int(task_id)}"


def task_id_from_link(href: str) -> Optional[int]:
    """The task id of a task link, else None."""
    match = re.fullmatch(rf"{TASK_LINK_SCHEME}:(\d+)", (href or "").strip(), re.IGNORECASE)
    return int(match.group(1)) if match else None


def heading_span(inner_html: str) -> str:
    """Inline style matching the editor's Heading button (H3)."""
    return (f"<span style=\"font-family:'{T.FONT_HEADING}'; font-size:15.75pt; font-weight:600;\">"
            f"{inner_html}</span>")


class _PasteSanitizer(HTMLParser):
    """Emit balanced supported markup; void elements never alter the element stack."""
    INLINE = {"b": "b", "strong": "b", "i": "i", "em": "i", "u": "u",
              "s": "s", "strike": "s", "del": "s"}
    BLOCKS = {"p", "div", "blockquote", "pre", "section", "article", "header", "footer"}
    SKIP = {"script", "style", "head", "title", "noscript", "svg", "iframe", "object"}
    VOID = {"meta", "link", "input", "hr", "area", "base", "col", "embed", "param", "source", "track", "wbr"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.out = []
        self.stack = []
        self.skip = []
        self.pre_depth = 0

    def handle_starttag(self, tag, attrs):
        if tag in self.VOID:
            return
        if tag in self.SKIP:
            self.skip.append(tag)
            return
        if self.skip:
            return
        attrs = dict(attrs)
        if tag == 'br':
            self.out.append('<br/>')
            return
        if tag == 'img':
            src = attrs.get('src', '')
            if src:
                self.out.append('<img src="' + html_lib.escape(src, quote=True) + '"/>')
            return
        opens, closes = [], []
        if tag in self.BLOCKS:
            mapped = tag if tag in ('p', 'pre') else 'div'
            opens, closes = [f'<{mapped}>'], [f'</{mapped}>']
            if tag == 'pre':
                self.pre_depth += 1
        elif tag in ('ul', 'ol', 'li'):
            opens, closes = [f'<{tag}>'], [f'</{tag}>']
        elif tag in ('h1', 'h2', 'h3', 'h4', 'h5', 'h6'):
            opens, closes = ['<h3>' + heading_span('')[:-7]], ['</span></h3>']
        elif tag == 'tr':
            opens, closes = ['<p>'], ['</p>']
        elif tag in ('td', 'th'):
            opens = [' ']
        elif tag == 'a' and is_allowed_link(attrs.get('href', '')):
            opens = ['<a href="' + html_lib.escape(attrs['href'], quote=True) + '">']
            closes = ['</a>']
        else:
            styles = []
            if tag in self.INLINE:
                styles.append(self.INLINE[tag])
            style = (attrs.get('style') or '').lower()
            weight = re.search(r'font-weight:\s*(bold|bolder|[0-9]+)', style)
            if weight and (not weight[1].isdigit() or int(weight[1]) >= 600):
                styles.append('b')
            if re.search(r'font-style:\s*italic', style):
                styles.append('i')
            if 'underline' in style:
                styles.append('u')
            if 'line-through' in style:
                styles.append('s')
            styles = list(dict.fromkeys(styles))
            opens = [f'<{name}>' for name in styles]
            closes = [f'</{name}>' for name in reversed(styles)]
        self.out.extend(opens)
        self.stack.append((tag, closes))

    def handle_endtag(self, tag):
        if self.skip:
            if tag in self.skip:
                self.skip = self.skip[:self.skip.index(tag)]
            return
        if tag in self.VOID or tag in ('br', 'img'):
            return
        found = next((i for i in range(len(self.stack) - 1, -1, -1) if self.stack[i][0] == tag), None)
        if found is not None:
            while len(self.stack) > found:
                source, closes = self.stack.pop()
                self.out.extend(closes)
                if source == 'pre':
                    self.pre_depth = max(0, self.pre_depth - 1)

    def handle_data(self, data):
        if not self.skip:
            text = data if self.pre_depth else re.sub(r'\s+', ' ', data)
            self.out.append(html_lib.escape(text, quote=False))

    def result(self):
        while self.stack:
            self.out.extend(self.stack.pop()[1])
        return ''.join(self.out).strip()


def sanitize_pasted_html(html: str) -> str:
    """Pasted HTML reduced to bold, italic, underline, strikethrough, links, lists and headings."""
    if not html:
        return ""
    parser = _PasteSanitizer()
    try:
        parser.feed(html)
        parser.close()
    except Exception:
        return ""
    return parser.result()


def parse_db_timestamp(value: Optional[str]) -> Optional[datetime.datetime]:
    """SQLite CURRENT_TIMESTAMP (UTC, "YYYY-MM-DD HH:MM:SS") or ISO text as an aware UTC datetime."""
    if not value:
        return None
    text = str(value).strip().replace("T", " ").rstrip("Z")
    for fmt in ("%Y-%m-%d %H:%M:%S.%f", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
        try:
            return datetime.datetime.strptime(text, fmt).replace(tzinfo=datetime.timezone.utc)
        except ValueError:
            continue
    return None


def relative_time(moment: datetime.datetime, now: Optional[datetime.datetime] = None) -> str:
    """"just now", "5 minutes ago", "2 hours ago", "yesterday", "3 days ago", else a date."""
    now = now or datetime.datetime.now(datetime.timezone.utc)
    seconds = (now - moment).total_seconds()
    if seconds < 60:
        return "just now"
    minutes = int(seconds // 60)
    if minutes < 60:
        return f"{minutes} minute{'' if minutes == 1 else 's'} ago"
    hours = minutes // 60
    if hours < 24:
        return f"{hours} hour{'' if hours == 1 else 's'} ago"
    days = hours // 24
    if days == 1:
        return "yesterday"
    if days < 7:
        return f"{days} days ago"
    local = moment.astimezone()
    return f"on {local.day} {local.strftime('%b %Y')}"
