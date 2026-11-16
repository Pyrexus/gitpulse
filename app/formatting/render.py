"""Transport-agnostic message model + body composition helpers."""

from __future__ import annotations

from dataclasses import dataclass, field

from app.formatting.html import ELLIPSIS, esc, link, truncate, utf16_len, visible_len
from app.formatting.markdown import Segment

TEXT_LIMIT = 4096
CAPTION_LIMIT = 1024
EXPAND_CHARS = 320
EXPAND_LINES = 7


@dataclass(slots=True)
class Button:
    text: str
    url: str | None = None
    action: str | None = None  # signed callback action, e.g. "cm"
    style: str | None = None  # primary | success | danger
    copy: str | None = None  # copy_text button payload


@dataclass(slots=True)
class LinkMeta:
    repo: str
    kind: str  # issue | pr | comment | review | push | workflow | release | artifact
    number: int | None = None
    thread_key: str | None = None
    state: str | None = None
    html_url: str | None = None
    comment_id: int | None = None


@dataclass(slots=True)
class Rendered:
    text: str
    meta: LinkMeta
    caption: str | None = None
    buttons: list[list[Button]] = field(default_factory=list)
    images: list[str] = field(default_factory=list)
    image_api_url: str | None = None  # where to fetch body_html for signed image URLs
    edit_thread: bool = False  # update the existing thread message instead of posting
    send_if_missing: bool = True  # when edit_thread finds nothing, post a new message


# ── small display helpers ─────────────────────────────────────────────────────


def user_link(user: dict[str, object] | None, fallback: str = "someone") -> str:
    if not user:
        return esc(fallback)
    login = str(user.get("login") or fallback)
    url = str(user.get("html_url") or f"https://github.com/{login}")
    if login.endswith("[bot]"):
        return link(url, login.removesuffix("[bot]")) + " 🤖"
    return link(url, login)


def repo_link(repo: dict[str, object], short: bool = False) -> str:
    full = str(repo.get("full_name") or repo.get("name") or "repo")
    name = full.split("/")[-1] if short else full
    return link(str(repo.get("html_url") or f"https://github.com/{full}"), name)


def human_size(n: int) -> str:
    size = float(n)
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    return f"{n} B"  # pragma: no cover


def human_duration(seconds: float) -> str:
    s = int(max(0, seconds))
    if s < 60:
        return f"{s}s"
    m, s = divmod(s, 60)
    if m < 60:
        return f"{m}m {s:02d}s"
    h, m = divmod(m, 60)
    return f"{h}h {m:02d}m"


def first_line(text: str | None, limit: int = 72) -> str:
    line = (text or "").strip().split("\n", 1)[0].strip()
    return line if len(line) <= limit else line[: limit - 1].rstrip() + ELLIPSIS


# ── body composition ──────────────────────────────────────────────────────────


def _merge(segments: list[Segment]) -> list[Segment]:
    out: list[Segment] = []
    for seg in segments:
        if out and seg.kind == "text" and out[-1].kind == "text":
            out[-1] = Segment("text", out[-1].html + "\n\n" + seg.html)
        else:
            out.append(Segment(seg.kind, seg.html, seg.lang, seg.raw))
    return out


def _code_html(raw: str, lang: str | None) -> str:
    if lang:
        return f'<pre><code class="language-{lang}">{esc(raw)}</code></pre>'
    return f"<pre>{esc(raw)}</pre>"


def should_expand(segments: list[Segment]) -> bool:
    total = sum(visible_len(s.html) for s in segments)
    lines = sum(s.html.count("\n") + 1 for s in segments)
    return total > EXPAND_CHARS or lines > EXPAND_LINES


def compose_body(
    segments: list[Segment], budget: int, expandable: bool | None = None, quote: bool = True
) -> str:
    """Prose → (expandable) blockquotes, code → <pre>, all within ``budget`` visible units."""
    if not segments or budget < 12:
        return ""
    groups = _merge(segments)
    expand = should_expand(groups) if expandable is None else expandable
    open_tag = "<blockquote expandable>" if expand else "<blockquote>"
    parts: list[str] = []
    remaining = budget
    for g in groups:
        if remaining < 12:
            parts.append(ELLIPSIS)
            break
        if g.kind == "text":
            body = truncate(g.html, remaining)
            html_ = f"{open_tag}{body}</blockquote>" if quote else body
            used = visible_len(body)
        else:
            raw = g.raw or ""
            if utf16_len(raw) > remaining:
                raw = raw[: max(0, remaining - 1)].rstrip() + ELLIPSIS
            html_ = _code_html(raw, g.lang)
            used = utf16_len(raw)
        parts.append(html_)
        remaining -= used + 2
        if body_was_cut(g, used):
            break
    return "\n".join(parts)


def body_was_cut(seg: Segment, used: int) -> bool:
    full = visible_len(seg.html) if seg.kind == "text" else utf16_len(seg.raw)
    return used < full


def assemble(
    header: list[str],
    segments: list[Segment] | None = None,
    footer: list[str] | None = None,
    limit: int = TEXT_LIMIT,
    expandable: bool | None = None,
) -> str:
    """Header lines, blank line, body, blank line, footer — guaranteed to fit ``limit``."""
    head = "\n".join(line for line in header if line)
    foot = "\n".join(line for line in (footer or []) if line)
    fixed = visible_len(head) + (visible_len(foot) + 2 if foot else 0) + 2
    if fixed >= limit:
        return truncate(head, limit)
    body = compose_body(segments or [], limit - fixed - 4, expandable)
    out = head
    if body:
        out += "\n\n" + body
    if foot:
        out += "\n\n" + foot
    if visible_len(out) > limit:  # defensive: never exceed Telegram limits
        out = truncate(out, limit)
    return out
