"""Chat lifecycle: bot added/removed, group → supergroup migration."""

from __future__ import annotations

from aiogram import F, Router
from aiogram.enums import ChatMemberStatus, ChatType
from aiogram.types import ChatMemberUpdated, Message

from app.bot.common import home_panel, show
from app.context import AppContext
from app.db import repo as db
from app.log import get_logger

log = get_logger(__name__)
router = Router(name="chats")

_GONE = {ChatMemberStatus.KICKED, ChatMemberStatus.LEFT}
_IN = {ChatMemberStatus.MEMBER, ChatMemberStatus.ADMINISTRATOR}


@router.my_chat_member()
async def on_my_member(event: ChatMemberUpdated, ctx: AppContext) -> None:
    chat = event.chat
    new, old = event.new_chat_member.status, event.old_chat_member.status
    if new in _GONE:
        async with ctx.db() as s, s.begin():
            await db.deactivate_chat(s, chat.id)
        log.info("bot_removed", chat_id=chat.id)
        return
    if new in _IN:
        ctx.sender.forget(chat.id)
        async with ctx.db() as s, s.begin():
            await db.upsert_chat(s, chat.id, str(chat.type), chat.title)
            await db.reactivate_chat(s, chat.id)
        # Fresh arrival in a group: greet with the panel (channels get nothing).
        if old in _GONE and chat.type in {ChatType.GROUP, ChatType.SUPERGROUP}:
            try:
                await show(ctx, chat.id, None, await home_panel(ctx, event.from_user.id, chat))
            except Exception:
                log.info("greet_failed", chat_id=chat.id)


async def migrate(ctx: AppContext, old_id: int, new_id: int) -> None:
    async with ctx.db() as s, s.begin():
        await db.migrate_chat(s, old_id, new_id)
    log.info("chat_migrated", old=old_id, new=new_id)


@router.message(F.migrate_to_chat_id)
async def on_migrate(message: Message, ctx: AppContext) -> None:
    if message.migrate_to_chat_id:
        await migrate(ctx, message.chat.id, message.migrate_to_chat_id)
