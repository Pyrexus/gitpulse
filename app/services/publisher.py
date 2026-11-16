"""Deliver a :class:`Rendered` message to a chat (text, photos, media groups, edits)."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass

from aiogram.exceptions import TelegramBadRequest
from aiogram.types import (
    BufferedInputFile,
    InlineKeyboardMarkup,
    InputMediaPhoto,
    LinkPreviewOptions,
    Message,
    ReplyParameters,
)

from app.context import AppContext
from app.db import repo as db
from app.formatting.html import visible_len
from app.formatting.images import extract_html_images
from app.formatting.render import CAPTION_LIMIT, Rendered
from app.github.api import body_html
from app.log import get_logger
from app.telegram.sender import ChatUnavailable

log = get_logger(__name__)

PHOTO_MAX = 10 * 1024 * 1024
NO_PREVIEW = LinkPreviewOptions(is_disabled=True)
_MAGIC = (b"\x89PNG", b"\xff\xd8\xff", b"GIF87a", b"GIF89a", b"BM")


def _is_image(data: bytes) -> bool:
    if data.startswith(_MAGIC):
        return True
    return data[:4] == b"RIFF" and data[8:12] == b"WEBP"


def _ext(data: bytes) -> str:
    if data.startswith(b"\x89PNG"):
        return "png"
    if data.startswith((b"GIF87a", b"GIF89a")):
        return "gif"
    if data[:4] == b"RIFF":
        return "webp"
    return "jpg"


@dataclass(slots=True)
class Target:
    chat_id: int
    integration_id: int | None
    quiet: bool = False


class Publisher:
    def __init__(self, ctx: AppContext) -> None:
        self.ctx = ctx

    # ── images ────────────────────────────────────────────────────────────
    async def fetch_images(self, r: Rendered, token: str | None) -> list[bytes]:
        if not r.images:
            return []
        gh = self.ctx.gh
        results = list(await asyncio.gather(*(gh.fetch_public(u, PHOTO_MAX) for u in r.images)))
        ok = [bool(b and _is_image(b)) for b in results]
        if not all(ok) and r.image_api_url and (token or gh.fallback_token):
            # Private attachments: GitHub's rendered HTML carries short-lived signed URLs.
            signed = extract_html_images(await body_html(gh, token, r.image_api_url))
            if signed:
                candidates = (
                    [signed[i] for i, good in enumerate(ok) if not good]
                    if len(signed) == len(r.images)
                    else signed
                )
                retry = await asyncio.gather(
                    *(gh.fetch_public(u, PHOTO_MAX) for u in candidates[: 10 - sum(ok)])
                )
                extra = [b for b in retry if b and _is_image(b)]
                if len(signed) == len(r.images):
                    it = iter(retry)
                    for i, good in enumerate(ok):
                        if not good:
                            nxt = next(it, None)
                            results[i] = nxt if nxt and _is_image(nxt) else None
                    return [b for b in results if b and _is_image(b)]
                return [b for b, good in zip(results, ok, strict=True) if good and b] + extra
        return [b for b, good in zip(results, ok, strict=True) if good and b]

    # ── public entry point ────────────────────────────────────────────────
    async def publish(
        self,
        target: Target,
        r: Rendered,
        token: str | None,
        reply_to: int | None = None,
    ) -> int | None:
        keys = self.ctx.keys
        markup = keys.from_rendered(r.buttons)
        try:
            if r.edit_thread and r.meta.thread_key:
                edited = await self._edit_thread(target, r, markup)
                if edited or not r.send_if_missing:
                    return edited
            images = await self.fetch_images(r, token)
            return await self._send(target, r, markup, images, reply_to)
        except ChatUnavailable:
            return None

    # ── internals ─────────────────────────────────────────────────────────
    async def _edit_thread(
        self, target: Target, r: Rendered, markup: InlineKeyboardMarkup | None
    ) -> int | None:
        async with self.ctx.db() as s:
            links = await db.thread_messages(s, target.chat_id, r.meta.thread_key or "")
        root = next((ln for ln in links if ln.kind == r.meta.kind), None)
        if root is None:
            return None
        bot = self.ctx.bot
        try:
            if root.is_caption:
                await self.ctx.sender.call(
                    target.chat_id,
                    lambda: bot.edit_message_caption(
                        chat_id=target.chat_id,
                        message_id=root.message_id,
                        caption=r.caption or r.text[:CAPTION_LIMIT],
                        reply_markup=markup,
                    ),
                )
            else:
                await self.ctx.sender.call(
                    target.chat_id,
                    lambda: bot.edit_message_text(
                        chat_id=target.chat_id,
                        message_id=root.message_id,
                        text=r.text,
                        link_preview_options=NO_PREVIEW,
                        reply_markup=markup,
                    ),
                )
        except TelegramBadRequest as exc:
            msg = str(exc).lower()
            if "not modified" not in msg:
                log.info("edit_failed", chat_id=target.chat_id, error=msg[:120])
                return None
        async with self.ctx.db() as s, s.begin():
            await db.save_message_link(
                s,
                chat_id=target.chat_id,
                message_id=root.message_id,
                integration_id=target.integration_id,
                repo=r.meta.repo,
                number=r.meta.number,
                kind=r.meta.kind,
                thread_key=r.meta.thread_key,
                state=r.meta.state,
                is_caption=root.is_caption,
                html_url=r.meta.html_url,
                comment_id=r.meta.comment_id,
            )
        return root.message_id

    async def _send(
        self,
        target: Target,
        r: Rendered,
        markup: InlineKeyboardMarkup | None,
        images: list[bytes],
        reply_to: int | None,
    ) -> int | None:
        bot, sender, chat = self.ctx.bot, self.ctx.sender, target.chat_id
        silent = target.quiet
        reply = ReplyParameters(message_id=reply_to, allow_sending_without_reply=True) if reply_to else None
        sent_ids: list[int] = []
        main_id: int | None = None
        is_caption = False

        if len(images) == 1 and visible_len(r.text) <= CAPTION_LIMIT:
            data = images[0]
            try:
                msg: Message = await sender.call(
                    chat,
                    lambda: bot.send_photo(
                        chat_id=chat,
                        photo=BufferedInputFile(data, f"image.{_ext(data)}"),
                        caption=r.text,
                        reply_markup=markup,
                        disable_notification=silent,
                        reply_parameters=reply,
                    ),
                )
                main_id, is_caption = msg.message_id, True
                images = []
            except TelegramBadRequest as exc:  # bad dimensions etc. → fall back to text
                log.info("photo_failed", chat_id=chat, error=str(exc)[:120])
                images = []

        if main_id is None:
            msg = await sender.call(
                chat,
                lambda: bot.send_message(
                    chat_id=chat,
                    text=r.text,
                    reply_markup=markup,
                    link_preview_options=NO_PREVIEW,
                    disable_notification=silent,
                    reply_parameters=reply,
                ),
            )
            main_id = msg.message_id

        if images:
            media = [
                InputMediaPhoto(media=BufferedInputFile(b, f"image{i}.{_ext(b)}"))
                for i, b in enumerate(images[:10])
            ]
            try:
                group = await sender.call(
                    chat,
                    lambda: bot.send_media_group(
                        chat_id=chat,
                        media=media,  # type: ignore[arg-type]
                        disable_notification=True,
                        reply_parameters=ReplyParameters(
                            message_id=main_id, allow_sending_without_reply=True
                        ),
                    ),
                )
                sent_ids.extend(m.message_id for m in group)
            except TelegramBadRequest as exc:
                log.info("media_group_failed", chat_id=chat, error=str(exc)[:120])

        async with self.ctx.db() as s, s.begin():
            for mid, caption in [(main_id, is_caption), *((i, True) for i in sent_ids)]:
                await db.save_message_link(
                    s,
                    chat_id=chat,
                    message_id=mid,
                    integration_id=target.integration_id,
                    repo=r.meta.repo,
                    number=r.meta.number,
                    kind=r.meta.kind if mid == main_id else f"{r.meta.kind}_media",
                    thread_key=r.meta.thread_key,
                    state=r.meta.state,
                    is_caption=caption,
                    html_url=r.meta.html_url,
                    comment_id=r.meta.comment_id,
                )
        return main_id
