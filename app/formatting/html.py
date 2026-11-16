"""Telegram-HTML primitives.

Telegram rejects a whole message if a single tag is unbalanced, unknown or
illegally nested, so everything we send passes through :func:`sanitize`,
which rebuilds the markup from scratch: only allowed tags/attributes survive,
nesting rules are enforced, and tags are always balanced. The same pass can
truncate to a visible-length budget without ever cutting a tag or entity.
"""

from __future__ import annotations

import html as _html
from html.parser import HTMLParser
from urllib.parse import urlsplit

ELLIPSIS = "…"

# tag → allowed attributes
_ALLOWED: dict[str, frozenset[str]] = {
    "b": frozenset(),
    "strong": frozenset(),
    "i": frozenset(),
    "em": frozenset(),
    "u": frozenset(),
    "ins": frozenset(),
    "s": frozenset(),
    "strike": frozenset(),
    "del": frozenset(),
    "tg-spoiler": frozenset(),
    "span": frozenset({"class"}),
    "a": frozenset({"href"}),
    "code": frozenset({"class"}),
    "pre": frozenset(),
    "blockquote": frozenset({"expandable"}),
    "tg-emoji": frozenset({"emoji-id"}),
    "tg-time": frozenset({"unix", "format"}),
}
_FORMATTING = {"b", "strong", "i", "em", "u", "ins", "s", "strike", "del", "tg-spoiler", "span"}
_SAFE_SCHEMES = {"http", "https", "tg", "mailto"}


def esc(text: object) -> str:
    """Escape text for an HTML text node."""
    return _html.escape(str(text), quote=False)


def esc_attr(text: object) -> str:
    return _html.escape(str(text), quote=True)


def safe_url(url: str | None, base: str = "https://github.com") -> str | None:
    """Return an absolute, safe URL or ``None``."""
    if not url:
        return None
    url = url.strip()
    if url.startswith("//"):
        url = "https:" + url
    elif url.startswith("/"):
        url = base.rstrip("/") + url
    elif url.startswith("#"):
        return None
    try:
        parts = urlsplit(url)
    except ValueError:
        return None
    if parts.scheme.lower() not in _SAFE_SCHEMES:
        return None
    if parts.scheme in {"http", "https"} and not parts.netloc:
        return None
    return url


def link(url: str | None, text: object) -> str:
    """``<a>`` if the URL is safe, otherwise just the escaped text."""
    href = safe_url(url)
    if href is None:
        return esc(text)
    return f'<a href="{esc_attr(href)}">{esc(text)}</a>'


def code(text: object) -> str:
    return f"<code>{esc(text)}</code>"


def bold(text: object) -> str:
    return f"<b>{esc(text)}</b>"


def italic(text: object) -> str:
    return f"<i>{esc(text)}</i>"


def utf16_len(text: str) -> int:
    return len(text.encode("utf-16-le")) // 2


def to_plain(html_text: str) -> str:
    """Visible text of a Telegram-HTML string."""
    p = _Rebuilder(limit=None)
    p.feed(html_text)
    p.close()
    return p.plain()


def visible_len(html_text: str) -> int:
    return utf16_len(to_plain(html_text))


def sanitize(html_text: str, limit: int | None = None, ellipsis: str = ELLIPSIS) -> str:
    """Rebuild Telegram-safe HTML, optionally truncated to ``limit`` visible UTF-16 units."""
    p = _Rebuilder(limit=limit, ellipsis=ellipsis)
    p.feed(html_text)
    p.close()
    return p.result()


def truncate(html_text: str, limit: int, ellipsis: str = ELLIPSIS) -> str:
    return sanitize(html_text, limit=limit, ellipsis=ellipsis)


class _Rebuilder(HTMLParser):
    def __init__(self, limit: int | None, ellipsis: str = ELLIPSIS) -> None:
        super().__init__(convert_charrefs=False)
        self.limit = limit
        self.ellipsis = ellipsis
        self.out: list[str] = []
        self.text: list[str] = []
        self.stack: list[str] = []
        self.used = 0
        self.full = False
        self.skip = 0  # inside <script>/<style>
        # Reserve room for the ellipsis so the final string never exceeds the limit.
        self.budget = None if limit is None else max(0, limit - utf16_len(ellipsis))

    # ── helpers ──────────────────────────────────────────────────────────
    def _in(self, *tags: str) -> bool:
        return any(t in self.stack for t in tags)

    def _emit_text(self, visible: str) -> None:
        if self.full or self.skip:
            return
        n = utf16_len(visible)
        if self.budget is not None and self.used + n > self.budget:
            cut = _cut_utf16(visible, self.budget - self.used)
            space = cut.rfind(" ")
            if space > len(cut) * 0.7:  # prefer a nearby word boundary
                cut = cut[:space]
            cut = cut.rstrip()
            self.out.append(esc(cut) + esc(self.ellipsis))
            self.text.append(cut + self.ellipsis)
            self.full = True
            return
        self.used += n
        self.out.append(esc(visible))
        self.text.append(visible)

    # ── HTMLParser hooks ─────────────────────────────────────────────────
    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if self.full:
            return
        tag = tag.lower()
        if tag in {"script", "style"}:
            self.skip += 1
            return
        if tag == "br":
            self._emit_text("\n")
            return
        if tag not in _ALLOWED:
            return
        if "code" in self.stack or ("pre" in self.stack and tag != "code"):
            return  # pre/code can't contain other entities (except <pre><code>)
        if tag in {"a", "code"} and self._in("a"):
            return
        if tag == "blockquote" and "blockquote" in self.stack:
            return
        if tag == "pre" and self._in("blockquote", "a", *_FORMATTING):
            return
        if tag == "code" and self._in(*_FORMATTING) and "pre" not in self.stack:
            return  # keep it simple: no code inside bold/italic
        allowed = _ALLOWED[tag]
        parts: list[str] = []
        for name, value in attrs:
            name = name.lower()
            if name not in allowed:
                continue
            if name == "href":
                href = safe_url(value)
                if href is None:
                    return
                parts.append(f' href="{esc_attr(href)}"')
            elif name == "expandable":
                parts.append(" expandable")
            elif name == "class":
                if tag == "span" and value == "tg-spoiler":
                    parts.append(' class="tg-spoiler"')
                elif tag == "code" and value and value.startswith("language-"):
                    lang = "".join(c for c in value[9:] if c.isalnum() or c in "+-#_")
                    if lang and "pre" in self.stack:
                        parts.append(f' class="language-{lang}"')
            elif value is not None:
                parts.append(f' {name}="{esc_attr(value)}"')
        if tag == "a" and not parts:
            return
        if tag == "span" and not parts:
            return
        self.stack.append(tag)
        self.out.append(f"<{tag}{''.join(parts)}>")

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag.lower() == "br":
            self._emit_text("\n")

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if tag in {"script", "style"}:
            self.skip = max(0, self.skip - 1)
            return
        if tag not in self.stack:
            return
        while self.stack:
            top = self.stack.pop()
            self.out.append(f"</{top}>")
            if top == tag:
                break

    def handle_data(self, data: str) -> None:
        self._emit_text(data)

    def handle_entityref(self, name: str) -> None:
        char = _html.unescape(f"&{name};")
        self._emit_text(char)  # unknown entities stay literal and get escaped

    def handle_charref(self, name: str) -> None:
        char = _html.unescape(f"&#{name};")
        self._emit_text(char)

    # ── results ──────────────────────────────────────────────────────────
    def result(self) -> str:
        closing = "".join(f"</{t}>" for t in reversed(self.stack))
        return "".join(self.out) + closing

    def plain(self) -> str:
        return "".join(self.text)


def _cut_utf16(text: str, units: int) -> str:
    if units <= 0:
        return ""
    out = []
    used = 0
    for ch in text:
        w = 2 if ord(ch) > 0xFFFF else 1
        if used + w > units:
            break
        out.append(ch)
        used += w
    return "".join(out)
