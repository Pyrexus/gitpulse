"""Helpers shared by all handlers: panel rendering, permissions, callback routing."""

from __future__ import annotations

import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import timedelta

from aiogram.enums import ChatMemberStatus, ChatType
from aiogram.exceptions import TelegramBadRequest
from aiogram.types import CallbackQuery, Chat, LinkPreviewOptions

from app.bot import panels
from app.context import AppContext
from app.db import repo as db
from app.db.models import Integration, User
from app.log import get_logger
from app.security.signing import Callback

log = get_logger(__name__)
NO_PREVIEW = LinkPreviewOptions(is_disabled=True)


def is_private(chat: Chat) -> bool:
    return chat.type == ChatType.PRIVATE


def chat_label(chat: Chat) -> str:
    return "this chat" if is_private(chat) else (chat.title or "this group")


async def show(ctx: AppContext, chat_id: int, message_id: int | None, panel: panels.Panel) -> int:
    """Edit the panel in place (or send it if it can't be edited). Returns its message id."""
    text, markup = panel
    if message_id:
        try:
            await ctx.bot.edit_message_text(
                text=text,
                chat_id=chat_id,
                message_id=message_id,
                reply_markup=markup,
                link_preview_options=NO_PREVIEW,
            )
            return message_id
        except TelegramBadRequest as exc:
            if "not modified" in str(exc).lower():
                return message_id
            log.info("panel_edit_failed", error=str(exc)[:120])
    msg = await ctx.bot.send_message(
        chat_id, text, reply_markup=markup, link_preview_options=NO_PREVIEW
    )
    return msg.message_id


async def safe_delete(ctx: AppContext, chat_id: int, message_id: int) -> bool:
    try:
        await ctx.bot.delete_message(chat_id, message_id)
        return True
    except TelegramBadRequest:
        return False


# ── users & state ─────────────────────────────────────────────────────────────


async def load_user(ctx: AppContext, user_id: int) -> User:
    async with ctx.db() as s, s.begin():
        return await db.get_or_create_user(s, user_id)


async def touch_chat(ctx: AppContext, chat: Chat) -> None:
    async with ctx.db() as s, s.begin():
        await db.upsert_chat(s, chat.id, str(chat.type), chat.title or chat.full_name)


async def set_pending(
    ctx: AppContext, user_id: int, chat_id: int, kind: str, panel_id: int | None, **data: object
) -> None:
    ttl = timedelta(minutes=ctx.settings.input_ttl_minutes)
    async with ctx.db() as s, s.begin():
        await db.set_pending(s, user_id, chat_id, kind, dict(data), panel_id, ttl)


async def clear_pending(ctx: AppContext, user_id: int, chat_id: int) -> None:
    async with ctx.db() as s, s.begin():
        await db.clear_pending(s, user_id, chat_id)


async def home_panel(
    ctx: AppContext, user_id: int, chat: Chat, banner: str | None = None
) -> panels.Panel:
    async with ctx.db() as s, s.begin():
        user = await db.get_or_create_user(s, user_id)
        integrations = await db.chat_integrations(s, chat.id)
    admin_status = None
    if user_id in ctx.settings.admin_ids and ctx.queue is not None:
        up = int(time.time() - ctx.started_at)
        limits = ctx.gh.rate_limit_snapshot()
        lowest = min((r for r, _ in limits.values()), default=None)
        admin_status = (
            f"🛠 <b>Status</b> · queue {ctx.queue.depth} · processed {ctx.queue.processed}"
            f" · failed {ctx.queue.failed} · up {up // 3600}h{(up % 3600) // 60:02d}m"
            + (f" · GH budget {lowest}" if lowest is not None else "")
        )
    return panels.home(
        ctx.keys,
        user,
        chat.title,
        is_private(chat),
        integrations,
        oauth_enabled=bool(ctx.settings.github_oauth_client_id),
        login_deep_link=ctx.deep_link("login"),
        banner=banner,
        admin_status=admin_status,
    )


# ── permissions ───────────────────────────────────────────────────────────────

_admin_cache: dict[tuple[int, int], tuple[bool, float]] = {}


async def is_chat_admin(ctx: AppContext, chat_id: int, user_id: int) -> bool:
    key = (chat_id, user_id)
    hit = _admin_cache.get(key)
    if hit and hit[1] > time.monotonic():
        return hit[0]
    try:
        member = await ctx.bot.get_chat_member(chat_id, user_id)
        ok = member.status in {ChatMemberStatus.CREATOR, ChatMemberStatus.ADMINISTRATOR}
    except TelegramBadRequest:
        ok = False
    _admin_cache[key] = (ok, time.monotonic() + 60)
    return ok


async def can_manage(ctx: AppContext, chat: Chat, user_id: int, integ: Integration | None) -> bool:
    if integ is not None and integ.owner_id == user_id:
        return True
    if integ is not None and integ.chat_id != chat.id:
        return False
    if is_private(chat):
        return integ is None or integ.chat_id == chat.id
    return await is_chat_admin(ctx, chat.id, user_id)


# ── callback routing ──────────────────────────────────────────────────────────


@dataclass(slots=True)
class CallbackCtx:
    ctx: AppContext
    cq: CallbackQuery
    cb: Callback

    @property
    def chat(self) -> Chat:
        assert self.cq.message is not None
        return self.cq.message.chat

    @property
    def message_id(self) -> int:
        assert self.cq.message is not None
        return self.cq.message.message_id

    @property
    def user_id(self) -> int:
        return self.cq.from_user.id

    async def show(self, panel: panels.Panel) -> int:
        return await show(self.ctx, self.chat.id, self.message_id, panel)


Handler = Callable[[CallbackCtx], Awaitable[None]]
ROUTES: dict[str, Handler] = {}
# Actions that change chat-wide state require admin/owner rights in groups.
PRIVILEGED: set[str] = set()


def route(action: str, privileged: bool = False) -> Callable[[Handler], Handler]:
    def deco(fn: Handler) -> Handler:
        ROUTES[action] = fn
        if privileged:
            PRIVILEGED.add(action)
        return fn

    return deco
