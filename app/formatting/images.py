"""Find images in GitHub bodies (markdown, <img> tags and rendered body_html)."""

from __future__ import annotations

import re
from urllib.parse import urlsplit

from app.formatting.html import safe_url

MAX_IMAGES = 10  # Telegram media group limit

_MD_IMAGE_RE = re.compile(r"!\[[^\]]*\]\(\s*<?([^)\s>]+)>?(?:\s+[\"'][^\"']*[\"'])?\s*\)")
_REF_DEF_RE = re.compile(r"^\s*\[([^\]]+)\]:\s*<?(\S+?)>?\s*$", re.M)
_REF_IMAGE_RE = re.compile(r"!\[[^\]]*\]\[([^\]]*)\]")
_IMG_TAG_RE = re.compile(r"<img\b[^>]*?\bsrc\s*=\s*([\"'])(.*?)\1", re.I | re.S)
_CODE_FENCE_RE = re.compile(r"```.*?```|~~~.*?~~~|`[^`\n]+`", re.S)

_SKIP_HOSTS = (
    "img.shields.io",
    "shields.io",
    "badgen.net",
    "badge.fury.io",
    "codecov.io",
    "github.githubassets.com",  # emoji sprites
    "camo.githubusercontent.com/badge",
)
_IMAGE_EXT = (".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".heic")


def _is_noise(url: str) -> bool:
    parts = urlsplit(url)
    host = parts.netloc.lower()
    path = parts.path.lower()
    if any(host == h or host.endswith("." + h) for h in _SKIP_HOSTS if "/" not in h):
        return True
    if "badge" in path and (path.endswith(".svg") or "workflows" in path):
        return True
    return path.endswith(".svg")


def is_user_attachment(url: str) -> bool:
    parts = urlsplit(url)
    return (parts.netloc == "github.com" and parts.path.startswith("/user-attachments/")) or (
        parts.netloc.endswith("githubusercontent.com")
        and ("user-images" in parts.netloc or "/user-attachments/" in parts.path)
    )


def looks_like_image_url(url: str) -> bool:
    path = urlsplit(url).path.lower()
    return path.endswith(_IMAGE_EXT) or is_user_attachment(url)


def extract_images(markdown: str | None, limit: int = MAX_IMAGES) -> list[str]:
    """Ordered, de-duplicated image URLs from a markdown body (code is ignored)."""
    if not markdown:
        return []
    text = _CODE_FENCE_RE.sub("", markdown)
    refs = {m.group(1).lower(): m.group(2) for m in _REF_DEF_RE.finditer(text)}
    found: list[tuple[int, str]] = []
    for m in _MD_IMAGE_RE.finditer(text):
        found.append((m.start(), m.group(1)))
    for m in _REF_IMAGE_RE.finditer(text):
        target = refs.get(m.group(1).lower())
        if target:
            found.append((m.start(), target))
    for m in _IMG_TAG_RE.finditer(text):
        found.append((m.start(), m.group(2)))
    return _dedupe([u for _, u in sorted(found)], limit)


def extract_html_images(body_html: str | None, limit: int = MAX_IMAGES) -> list[str]:
    """Image URLs from GitHub's rendered ``body_html`` (contains signed private URLs)."""
    if not body_html:
        return []
    urls = [m.group(2).replace("&amp;", "&") for m in _IMG_TAG_RE.finditer(body_html)]
    return _dedupe(urls, limit)


def _dedupe(urls: list[str], limit: int) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for raw in urls:
        url = safe_url(raw)
        if not url or not url.startswith(("http://", "https://")) or _is_noise(url):
            continue
        if url in seen:
            continue
        seen.add(url)
        out.append(url)
        if len(out) >= limit:
            break
    return out
