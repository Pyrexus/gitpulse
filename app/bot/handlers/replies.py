"""Reply to a bot event message → comment on the linked GitHub issue / PR."""

from __future__ import annotations

from aiogram import F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.types import Message, ReactionTypeEmoji

from app.bot.common import load_user
from app.context import AppContext
from app.db import repo as db
from app.formatting.html import esc, link
from app.github import api
from app.github.client import GitHubError, RateLimited
from app.log import get_logger

log = get_logger(__name__)
router = Router(name="replies")

COMMENTABLE = {"issue", "pr", "comment", "review"}


def _commentable(kind: str) -> bool:
    return kind.removesuffix("_media") in COMMENTABLE


async def handle_reply(message: Message, ctx: AppContext) -> None:
    reply = message.reply_to_message
    if reply is None or message.from_user is None:
        return
    async with ctx.db() as s:
        target = await db.get_message_link(s, message.chat.id, reply.message_id)
    if target is None or target.number is None or not _commentable(target.kind):
        return

    user = await load_user(ctx, message.from_user.id)
    token = ctx.user_token(user)
    if not token:
        kb = ctx.keys.markup(
            [ctx.keys.url("🔑 Log in to comment", ctx.deep_link("login"), style="success")]
        )
        await message.reply(
            "🔒 <b>Log in to comment</b> — your reply is posted to GitHub as you.",
            reply_markup=kb,
        )
        return

    body = (message.text or message.caption or "").strip()
    has_media = bool(message.photo or message.document or message.video)
    if not body:
        await message.reply(
            "📎 GitHub's API can't take image uploads — add a caption and I'll post the text."
        )
        return

    kind_label = "PR" if target.kind.startswith(("pr", "review")) else "issue"
    try:
        if target.kind.startswith("review") and target.comment_id:
            await api.reply_review_comment(
                ctx.gh, token, target.repo, target.number, target.comment_id, body
            )
        else:
            await api.post_issue_comment(ctx.gh, token, target.repo, target.number, body)
    except RateLimited:
        await message.reply("⏳ GitHub rate limit reached — try again in a few minutes.")
        return
    except GitHubError as exc:
        if exc.status in {401}:
            why = "your token expired or was revoked — log in again."
        elif exc.status in {403, 404}:
            why = f"your token can't comment on <code>{esc(target.repo)}</code>."
        elif exc.status == 410:
            why = "that conversation is locked or deleted."
        else:
            why = f"GitHub {exc.status}: {esc(exc.message[:120])}"
        await message.reply(f"⚠️ Couldn't comment — {why}")
        return

    log.info("comment_posted", repo=target.repo, number=target.number)
    try:
        await ctx.bot.set_message_reaction(
            chat_id=message.chat.id,
            message_id=message.message_id,
            reaction=[ReactionTypeEmoji(emoji="👍")],
        )
        if has_media:
            await message.reply("✅ Posted — image skipped (GitHub's API has no uploads).")
    except TelegramBadRequest:  # reactions disabled in this chat
        url = f"https://github.com/{target.repo}/issues/{target.number}"
        await message.reply(
            f"✅ Commented on {link(url, f'{kind_label} #{target.number}')}"
            + (" · image skipped" if has_media else ""),
            disable_notification=True,
        )


@router.message(F.reply_to_message & (F.photo | F.document | F.video))
async def on_media_reply(message: Message, ctx: AppContext) -> None:
    reply = message.reply_to_message
    if reply is not None and reply.from_user is not None and reply.from_user.id == ctx.bot.id:
        await handle_reply(message, ctx)
