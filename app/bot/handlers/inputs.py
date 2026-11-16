"""Free-text messages: pending button-flow input (PAT, search, branch filter)."""

from __future__ import annotations

import re

from aiogram import F, Router
from aiogram.types import Message

from app.bot import panels
from app.bot.common import chat_label, clear_pending, home_panel, is_private, safe_delete, show
from app.bot.handlers.core import LoginError, save_login
from app.bot.handlers.integrations import render_picker, set_query
from app.context import AppContext
from app.db import repo as db
from app.db.models import Integration, PendingInput
from app.formatting.html import esc
from app.log import get_logger

log = get_logger(__name__)
router = Router(name="inputs")

TOKEN_RE = re.compile(r"^(gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{40,})$")
LOOKS_LIKE_TOKEN = re.compile(r"(gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{40,})")
_BRANCH_RE = re.compile(r"^!?[\w./*?\-\[\]{}+]{1,100}$")


async def _pending_for(message: Message, ctx: AppContext) -> PendingInput | None:
    if message.from_user is None:
        return None
    async with ctx.db() as s, s.begin():
        pending = await db.get_pending(s, message.from_user.id, message.chat.id)
    if pending is None:
        return None
    if is_private(message.chat):
        return pending
    # Groups (privacy mode): input must be a reply to the panel.
    reply = message.reply_to_message
    if reply is not None and reply.message_id == pending.panel_message_id:
        return pending
    return None


async def handle_pending(message: Message, ctx: AppContext, pending: PendingInput) -> None:
    text = (message.text or "").strip()
    chat, uid = message.chat, pending.user_id
    panel = pending.panel_message_id

    if pending.kind == "pat":
        await safe_delete(ctx, chat.id, message.message_id)  # first, always
        if not TOKEN_RE.match(text):
            await show(
                ctx,
                chat.id,
                panel,
                panels.login_prompt(ctx.keys, "That doesn't look like a GitHub token."),
            )
            return
        await show(ctx, chat.id, panel, panels.loading("🔐 <i>Verifying your token…</i>"))
        try:
            banner = await save_login(ctx, uid, text, "pat")
        except LoginError as exc:
            await show(ctx, chat.id, panel, panels.login_prompt(ctx.keys, esc(exc)))
            return
        await clear_pending(ctx, uid, chat.id)
        await show(ctx, chat.id, panel, await home_panel(ctx, uid, chat, banner=banner))
        return

    if pending.kind == "search":
        await safe_delete(ctx, chat.id, message.message_id)
        scope = str(pending.data.get("scope") or "r")
        await set_query(ctx, chat.id, uid, scope, text[:40] or None)
        await clear_pending(ctx, uid, chat.id)
        await show(
            ctx, chat.id, panel, await render_picker(ctx, chat.id, uid, chat_label(chat), scope, 0)
        )
        return

    if pending.kind == "branch":
        await safe_delete(ctx, chat.id, message.message_id)
        iid = int(pending.data.get("integration_id") or 0)
        async with ctx.db() as s:
            integ = await db.get_integration(s, iid)
        if integ is None:
            await clear_pending(ctx, uid, chat.id)
            return
        raw = text.replace("\n", ",")
        parts = [p.strip() for p in raw.split(",") if p.strip()]
        if any(p.lower() in {"all", "*"} for p in parts) or not parts:
            value = None
        elif len(raw) > 255 or not all(_BRANCH_RE.match(p) for p in parts):
            await show(
                ctx,
                chat.id,
                panel,
                panels.branch_prompt(
                    ctx.keys,
                    integ,
                    not is_private(chat),
                    "Use branch names or globs like <code>release/*</code>.",
                ),
            )
            return
        else:
            value = ", ".join(parts)
        async with ctx.db() as s, s.begin():
            row = await s.get(Integration, iid)
            if row:
                row.branch_filter = value
        await clear_pending(ctx, uid, chat.id)
        async with ctx.db() as s:
            integ = await db.get_integration(s, iid)
        if integ:
            where = None if integ.chat_id == chat.id else (integ.chat.title or "another chat")
            banner = (
                f"🌿 Push filter set to <code>{esc(value)}</code>." if value else "🌿 All branches."
            )
            await show(
                ctx, chat.id, panel, panels.integration_detail(ctx.keys, integ, banner, where)
            )
        return

    await clear_pending(ctx, uid, chat.id)


@router.message(F.text & ~F.text.startswith("/"))
async def on_text(message: Message, ctx: AppContext) -> None:
    pending = await _pending_for(message, ctx)
    if pending is not None:
        await handle_pending(message, ctx, pending)
        return
    if (
        message.reply_to_message is not None
        and message.reply_to_message.from_user
        and (message.reply_to_message.from_user.id == ctx.bot.id)
    ):
        from app.bot.handlers.replies import handle_reply

        await handle_reply(message, ctx)
        return
    if LOOKS_LIKE_TOKEN.search(message.text or ""):
        deleted = await safe_delete(ctx, message.chat.id, message.message_id)
        note = "I deleted it. " if deleted else "Please delete it and revoke it on GitHub. "
        await message.answer(
            f"🛡 That looked like a GitHub token. {note}Use 🔑 <b>Login</b> in /start to connect."
        )
        return
    if is_private(message.chat):
        await show(
            ctx,
            message.chat.id,
            None,
            await home_panel(ctx, message.from_user.id if message.from_user else 0, message.chat),
        )
