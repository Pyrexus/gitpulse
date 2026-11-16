"""GitHub-flavoured Markdown → Telegram HTML.

The output is a list of :class:`Segment`s. Telegram can't put ``<pre>`` inside
``<blockquote>``, so prose and code blocks are kept apart: the message
formatter wraps prose segments in (expandable) quotes and places code blocks
between them.

Handles headings, emphasis, strikethrough, links, autolinked @mentions and
#refs, ordered/unordered/nested lists, task lists, fenced code, tables
(monospace grid when narrow, key/value lines otherwise), blockquotes,
``<details>``/``<summary>``, inline HTML, emoji shortcodes and GitHub issue
template noise such as empty ``_No response_`` sections.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from html.parser import HTMLParser
from typing import Literal

from markdown_it import MarkdownIt
from markdown_it.token import Token

from app.formatting.html import esc, esc_attr, safe_url, sanitize, utf16_len

SegmentKind = Literal["text", "code"]


@dataclass(slots=True)
class Segment:
    kind: SegmentKind
    html: str
    lang: str | None = None
    raw: str = ""  # code segments: raw source (for re-truncation)


@dataclass(slots=True)
class MdContext:
    repo_url: str | None = None  # https://github.com/owner/repo → enables #123 links
    base_url: str = "https://github.com"
    image_links: bool = False  # render images as "🖼 alt" links instead of dropping them
    max_code_lines: int = 25
    max_table_width: int = 44


_md = MarkdownIt("commonmark", {"html": True, "breaks": True}).enable(["table", "strikethrough"])

# ── pre-processing ────────────────────────────────────────────────────────────

_COMMENT_RE = re.compile(r"<!--.*?-->", re.S)
_NO_RESPONSE_RE = re.compile(
    r"^#{1,6}[ \t]+[^\n]+\n+[ \t]*_No response_[ \t]*(?:\n|$)", re.M | re.I
)
_BARE_NO_RESPONSE_RE = re.compile(r"^[ \t]*_No response_[ \t]*$", re.M | re.I)
_DETAILS_OPEN_RE = re.compile(r"<details[^>]*>\s*(?:<summary[^>]*>(.*?)</summary>)?", re.S | re.I)
_DETAILS_CLOSE_RE = re.compile(r"</details\s*>", re.I)
_IMG_TAG_RE = re.compile(r"<img\b[^>]*>", re.I)
_PICTURE_RE = re.compile(r"</?(?:picture|source)\b[^>]*>", re.I)
_MANY_NL_RE = re.compile(r"\n{3,}")

_MENTION_RE = re.compile(r"(?<![\w/.@`])@([A-Za-z0-9](?:[A-Za-z0-9]|-(?=[A-Za-z0-9])){0,38})\b")
_REF_RE = re.compile(r"(?<![\w/&#])#(\d{1,7})\b")
_SHORTCODE_RE = re.compile(r":([a-z0-9_+\-]+):")

EMOJI: dict[str, str] = {
    "+1": "👍", "thumbsup": "👍", "-1": "👎", "thumbsdown": "👎", "tada": "🎉",
    "rocket": "🚀", "fire": "🔥", "bug": "🐛", "sparkles": "✨", "memo": "📝",
    "warning": "⚠️", "white_check_mark": "✅", "heavy_check_mark": "✔️", "x": "❌",
    "eyes": "👀", "heart": "❤️", "smile": "😄", "laughing": "😆", "joy": "😂",
    "thinking": "🤔", "confused": "😕", "pray": "🙏", "clap": "👏", "zap": "⚡",
    "boom": "💥", "lock": "🔒", "wrench": "🔧", "hammer": "🔨", "gear": "⚙️",
    "package": "📦", "bookmark": "🔖", "construction": "🚧", "recycle": "♻️",
    "lipstick": "💄", "art": "🎨", "lady_beetle": "🐞", "arrow_up": "⬆️",
    "arrow_down": "⬇️", "arrow_right": "➡️", "information_source": "ℹ️",
    "question": "❓", "exclamation": "❗", "100": "💯", "star": "⭐", "bulb": "💡",
    "green_heart": "💚", "rotating_light": "🚨", "test_tube": "🧪", "pushpin": "📌",
    "link": "🔗", "globe_with_meridians": "🌐", "iphone": "📱", "computer": "💻",
    "hourglass": "⌛", "stopwatch": "⏱️", "label": "🏷️", "speech_balloon": "💬",
    "see_no_evil": "🙈", "ok_hand": "👌", "wave": "👋", "muscle": "💪", "trophy": "🏆",
}  # fmt: skip

# GitHub alert blocks: > [!NOTE] …
_ALERTS = {
    "NOTE": "ℹ️ Note",
    "TIP": "💡 Tip",
    "IMPORTANT": "❗ Important",
    "WARNING": "⚠️ Warning",
    "CAUTION": "🛑 Caution",
}
_ALERT_RE = re.compile(r"^\[!(NOTE|TIP|IMPORTANT|WARNING|CAUTION)\]\s*", re.I)


def clean_github_markdown(text: str) -> str:
    """Remove template noise before parsing."""
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = _COMMENT_RE.sub("", text)
    text = _NO_RESPONSE_RE.sub("", text)
    text = _BARE_NO_RESPONSE_RE.sub("", text)
    text = _DETAILS_OPEN_RE.sub(
        lambda m: f"\n\n**▸ {m.group(1).strip()}**\n\n" if m.group(1) else "\n\n", text
    )
    text = _DETAILS_CLOSE_RE.sub("\n\n", text)
    text = _IMG_TAG_RE.sub("", text)
    text = _PICTURE_RE.sub("", text)
    return _MANY_NL_RE.sub("\n\n", text).strip()


# ── inline text decoration (mentions, refs, emoji) ────────────────────────────


def _decorate(text: str, ctx: MdContext, linkable: bool) -> str:
    text = _SHORTCODE_RE.sub(lambda m: EMOJI.get(m.group(1), m.group(0)), text)
    if not linkable:
        return esc(text)
    out: list[str] = []
    pos = 0
    pattern = _MENTION_RE.pattern + "|" + _REF_RE.pattern
    for m in re.finditer(pattern, text):
        out.append(esc(text[pos : m.start()]))
        if m.group(1):
            url = f"{ctx.base_url}/{m.group(1)}"
        elif ctx.repo_url:
            url = f"{ctx.repo_url}/issues/{m.group(2)}"
        else:
            out.append(esc(m.group(0)))
            pos = m.end()
            continue
        out.append(f'<a href="{esc_attr(url)}">{esc(m.group(0))}</a>')
        pos = m.end()
    out.append(esc(text[pos:]))
    return "".join(out)


# ── inline HTML fragments ─────────────────────────────────────────────────────

_INLINE_TAG_MAP = {
    "b": "b", "strong": "b", "i": "i", "em": "i", "u": "u", "ins": "u",
    "s": "s", "del": "s", "strike": "s",
}  # fmt: skip
_TAG_RE = re.compile(r"<\s*(/?)\s*([a-zA-Z][\w-]*)([^>]*)>")
_HREF_RE = re.compile(r"""href\s*=\s*["']([^"']+)["']""", re.I)


def _inline_html(raw: str, ctx: MdContext) -> str:
    m = _TAG_RE.fullmatch(raw.strip())
    if not m:
        return ""
    closing, name, attrs = m.group(1), m.group(2).lower(), m.group(3)
    if name == "br":
        return "\n"
    if name in _INLINE_TAG_MAP:
        return f"<{closing}{_INLINE_TAG_MAP[name]}>"
    if name == "a":
        if closing:
            return "</a>"
        href = _HREF_RE.search(attrs)
        url = safe_url(href.group(1), ctx.base_url) if href else None
        return f'<a href="{esc_attr(url)}">' if url else ""
    return ""


class _HtmlBlock(HTMLParser):
    """Converts a raw HTML block (tables, divs, lists in HTML) to Telegram text."""

    _BLOCK = {"p", "div", "tr", "table", "ul", "ol", "h1", "h2", "h3", "h4", "h5", "h6", "hr"}

    def __init__(self, ctx: MdContext) -> None:
        super().__init__(convert_charrefs=True)
        self.ctx = ctx
        self.out: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in self._BLOCK:
            self.out.append("\n")
        if tag == "li":
            self.out.append("\n• ")
        elif tag in {"td", "th"}:
            self.out.append(" · " if self.out and not self.out[-1].endswith("\n") else "")
        elif tag == "br":
            self.out.append("\n")
        elif tag in _INLINE_TAG_MAP or tag in {"h1", "h2", "h3", "h4", "h5", "h6", "summary"}:
            self.out.append(f"<{_INLINE_TAG_MAP.get(tag, 'b')}>")
        elif tag == "a":
            url = safe_url(dict(attrs).get("href"), self.ctx.base_url)
            self.out.append(f'<a href="{esc_attr(url)}">' if url else "")

    def handle_endtag(self, tag: str) -> None:
        if tag in _INLINE_TAG_MAP or tag in {"h1", "h2", "h3", "h4", "h5", "h6", "summary"}:
            self.out.append(f"</{_INLINE_TAG_MAP.get(tag, 'b')}>")
        elif tag == "a":
            self.out.append("</a>")
        if tag in self._BLOCK:
            self.out.append("\n")

    def handle_data(self, data: str) -> None:
        self.out.append(esc(re.sub(r"\s+", " ", data)))


def _html_block(raw: str, ctx: MdContext) -> str:
    p = _HtmlBlock(ctx)
    p.feed(raw)
    p.close()
    text = "".join(p.out)
    text = re.sub(r"[ \t]*\n[ \t]*", "\n", text)
    return _MANY_NL_RE.sub("\n\n", text).strip()


# ── renderer ──────────────────────────────────────────────────────────────────


@dataclass
class _ListState:
    ordered: bool
    counter: int = 1
    tight: bool = True


@dataclass
class _State:
    ctx: MdContext
    segments: list[Segment] = field(default_factory=list)
    buf: list[str] = field(default_factory=list)
    lists: list[_ListState] = field(default_factory=list)
    quote_depth: int = 0
    pending_task: bool = False

    def flush(self) -> None:
        text = "".join(self.buf)
        self.buf.clear()
        text = _MANY_NL_RE.sub("\n\n", text).strip("\n")
        if text.strip():
            self.segments.append(Segment("text", text))

    def nl(self, count: int = 1) -> None:
        """Ensure the buffer ends with at least ``count`` newlines."""
        if not self.buf:
            return
        tail = "".join(self.buf[-3:])
        have = len(tail) - len(tail.rstrip("\n"))
        if have < count:
            self.buf.append("\n" * (count - have))

    def quote_prefix(self) -> str:
        return "┃ " * self.quote_depth


def _render_inline(children: list[Token], st: _State) -> str:
    ctx = st.ctx
    out: list[str] = []
    in_link = 0
    fmt_depth = 0
    for i, tok in enumerate(children):
        t = tok.type
        if t == "text":
            content = tok.content
            if i == 0 and st.pending_task:
                content = _task_marker(content)
            out.append(_decorate(content, ctx, linkable=in_link == 0))
        elif t in {"softbreak", "hardbreak"}:
            out.append("\n" + st.quote_prefix())
        elif t == "code_inline":
            # code can't live inside links or bold/italic in Telegram
            if in_link or fmt_depth:
                out.append(esc(tok.content))
            else:
                out.append(f"<code>{esc(tok.content)}</code>")
        elif t in {"strong_open", "em_open", "s_open"}:
            fmt_depth += 1
            out.append({"strong_open": "<b>", "em_open": "<i>", "s_open": "<s>"}[t])
        elif t in {"strong_close", "em_close", "s_close"}:
            fmt_depth = max(0, fmt_depth - 1)
            out.append({"strong_close": "</b>", "em_close": "</i>", "s_close": "</s>"}[t])
        elif t == "link_open":
            url = safe_url(str(tok.attrGet("href") or ""), ctx.base_url)
            if url and not in_link:
                out.append(f'<a href="{esc_attr(url)}">')
            in_link += 1
        elif t == "link_close":
            in_link = max(0, in_link - 1)
            out.append("</a>")  # unmatched closers are dropped by sanitize()
        elif t == "image":
            if ctx.image_links and not in_link:
                src = safe_url(str(tok.attrGet("src") or ""), ctx.base_url)
                alt = tok.content or "image"
                if src:
                    out.append(f'🖼 <a href="{esc_attr(src)}">{esc(alt)}</a>')
        elif t == "html_inline":
            out.append(_inline_html(tok.content, ctx))
    st.pending_task = False
    return "".join(out)


_TASK_RE = re.compile(r"^\[([ xX])\]\s+")


def _task_marker(text: str) -> str:
    m = _TASK_RE.match(text)
    if not m:
        return text
    return ("✅ " if m.group(1).lower() == "x" else "⬜ ") + text[m.end() :]


def _render_table(tokens: list[Token], start: int, st: _State) -> int:
    """Render a table starting at ``tokens[start]`` (table_open). Returns index after it."""
    rows: list[list[tuple[str, str]]] = []  # (plain, html)
    header_rows = 0
    in_head = False
    i = start + 1
    while i < len(tokens) and tokens[i].type != "table_close":
        tok = tokens[i]
        if tok.type == "thead_open":
            in_head = True
        elif tok.type == "thead_close":
            in_head = False
        elif tok.type == "tr_open":
            rows.append([])
            if in_head:
                header_rows += 1
        elif tok.type == "inline" and rows:
            children = tok.children or []
            plain = "".join(
                c.content for c in children if c.type in {"text", "code_inline"}
            ).strip()
            rows[-1].append((plain, _render_inline(children, st).strip()))
        i += 1
    if not rows:
        return i + 1

    ncols = max(len(r) for r in rows)
    widths = [max((utf16_len(r[c][0]) if c < len(r) else 0) for r in rows) for c in range(ncols)]
    total = sum(widths) + 3 * (ncols - 1)
    if total <= st.ctx.max_table_width and st.quote_depth == 0:
        lines = []
        for ri, r in enumerate(rows):
            cells = [(r[c][0] if c < len(r) else "").ljust(widths[c]) for c in range(ncols)]
            lines.append(" │ ".join(cells).rstrip())
            if ri == header_rows - 1:
                lines.append("─┼─".join("─" * w for w in widths))
        st.flush()
        raw = "\n".join(lines)
        st.segments.append(Segment("code", f"<pre>{esc(raw)}</pre>", raw=raw))
    else:
        header = [c[1] for c in rows[0]] if header_rows else []
        body = rows[header_rows:]
        st.nl(2)
        for r in body:
            cells = []
            for c, (_, cell_html) in enumerate(r):
                if not cell_html:
                    continue
                if header and c < len(header) and header[c]:
                    cells.append(f"<b>{header[c]}</b>: {cell_html}")
                else:
                    cells.append(cell_html)
            st.buf.append(st.quote_prefix() + "• " + " · ".join(cells) + "\n")
        st.nl(2)
    return i + 1


def _code_segment(content: str, lang: str | None, ctx: MdContext) -> Segment:
    lines = content.rstrip("\n").split("\n")
    if len(lines) > ctx.max_code_lines:
        hidden = len(lines) - ctx.max_code_lines
        lines = [*lines[: ctx.max_code_lines], f"… {hidden} more lines"]
    raw = "\n".join(lines)
    lang_clean = "".join(c for c in (lang or "") if c.isalnum() or c in "+-#_")
    if lang_clean:
        html_ = f'<pre><code class="language-{lang_clean}">{esc(raw)}</code></pre>'
    else:
        html_ = f"<pre>{esc(raw)}</pre>"
    return Segment("code", html_, lang=lang_clean or None, raw=raw)


def markdown_to_segments(text: str | None, ctx: MdContext | None = None) -> list[Segment]:
    ctx = ctx or MdContext()
    if not text or not text.strip():
        return []
    source = clean_github_markdown(text)
    if not source:
        return []
    tokens = _md.parse(source)
    st = _State(ctx=ctx)
    i = 0
    while i < len(tokens):
        tok = tokens[i]
        t = tok.type
        if t == "heading_open":
            st.nl(2)
            st.buf.append(st.quote_prefix() + "<b>")
        elif t == "heading_close":
            st.buf.append("</b>")
            st.nl(2)
        elif t == "paragraph_open":
            if not tok.hidden:
                st.nl(2)
            if st.quote_depth and not (st.lists and tok.hidden):
                st.buf.append(st.quote_prefix())
        elif t == "paragraph_close":
            st.nl(1 if tok.hidden else 2)
        elif t == "inline":
            children = tok.children or []
            # GitHub alerts: first paragraph of a quote starting with [!NOTE]
            if st.quote_depth and children and children[0].type == "text":
                m = _ALERT_RE.match(children[0].content)
                if m:
                    label = _ALERTS[m.group(1).upper()]
                    children[0].content = children[0].content[m.end() :]
                    st.buf.append(f"<b>{label}</b>")
                    if children[0].content.strip():
                        st.buf.append("\n" + st.quote_prefix())
                    elif len(children) > 1 and children[1].type in {"softbreak", "hardbreak"}:
                        children[0].content = ""  # the following break supplies the newline
            st.buf.append(_render_inline(children, st))
        elif t in {"bullet_list_open", "ordered_list_open"}:
            if not st.lists:
                st.nl(2)
            start = int(tok.attrGet("start") or 1) if t == "ordered_list_open" else 1
            st.lists.append(_ListState(ordered=t == "ordered_list_open", counter=start))
        elif t in {"bullet_list_close", "ordered_list_close"}:
            st.lists.pop()
            st.nl(2 if not st.lists else 1)
        elif t == "list_item_open":
            st.nl(1)
            ls = st.lists[-1]
            depth = len(st.lists) - 1
            if ls.ordered:
                marker = f"{ls.counter}."
                ls.counter += 1
            else:
                marker = ("•", "◦", "▪")[min(depth, 2)]
            st.buf.append(st.quote_prefix() + "   " * depth + marker + " ")
            st.pending_task = True
        elif t == "list_item_close":
            st.pending_task = False
            st.nl(1)
        elif t == "blockquote_open":
            st.nl(2)
            st.quote_depth += 1
        elif t == "blockquote_close":
            st.quote_depth = max(0, st.quote_depth - 1)
            st.nl(2)
        elif t in {"fence", "code_block"}:
            if st.quote_depth:
                lines = tok.content.rstrip("\n").split("\n")
                st.buf.append("\n".join(st.quote_prefix() + esc(line) for line in lines))
                st.nl(2)
            else:
                st.flush()
                st.segments.append(_code_segment(tok.content, tok.info.split(" ")[0], ctx))
        elif t == "hr":
            st.nl(2)
            st.buf.append("─────────")
            st.nl(2)
        elif t == "html_block":
            converted = _html_block(tok.content, ctx)
            if converted:
                st.nl(2)
                st.buf.append(converted)
                st.nl(2)
        elif t == "table_open":
            i = _render_table(tokens, i, st)
            continue
        i += 1
    st.flush()
    # Final safety net: every text segment is rebuilt into valid Telegram HTML.
    for seg in st.segments:
        if seg.kind == "text":
            seg.html = sanitize(seg.html).strip()
    return [s for s in st.segments if s.html]


def markdown_to_html(text: str | None, ctx: MdContext | None = None) -> str:
    """Convenience: join all segments (code blocks inline) into one HTML string."""
    return "\n\n".join(s.html for s in markdown_to_segments(text, ctx))
