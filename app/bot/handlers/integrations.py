"""Add-integration flow and per-integration settings."""

from __future__ import annotations

from datetime import timedelta

from aiogram import F, Router
from aiogram.types import (
    ChatAdministratorRights,
    KeyboardButton,
    KeyboardButtonRequestChat,
    Message,
    ReplyKeyboardMarkup,
    ReplyKeyboardRemove,
)

from app.bot import panels
from app.bot.common import (
    CallbackCtx,
    chat_label,
    clear_pending,
    is_private,
    load_user,
    route,
    safe_delete,
    set_pending,
    show,
)
from app.bot.keyboards import A
from app.context import AppContext
from app.db import repo as db
from app.db.models import Integration
from app.formatting.html import esc
from app.github import api
from app.log import get_logger
from app.services import integrations as svc

log = get_logger(__name__)
router = Router(name="integrations")

REQUEST_GROUP, REQUEST_CHANNEL = 1, 2
# "Any admin": every required right set to False (built from the model so new
# Bot API fields never break us).
_NO_RIGHTS = ChatAdministratorRights(
    **{
        name: False
        for name, f in ChatAdministratorRights.model_fields.items()
        if f.is_required()
    }
)


def _query_key(chat_id: int, user_id: int, scope: str) -> str:
    return f"q:{chat_id}:{user_id}:{scope}"


async def get_query(ctx: AppContext, chat_id: int, user_id: int, scope: str) -> str | None:
    async with ctx.db() as s:
        data = await db.cache_get(s, _query_key(chat_id, user_id, scope), timedelta(hours=2))
    return (data or {}).get("q") or None


async def set_query(ctx: AppContext, chat_id: int, user_id: int, scope: str, q: str | None) -> None:
    async with ctx.db() as s, s.begin():
        await db.cache_put(s, _query_key(chat_id, user_id, scope), {"q": q})


async def render_picker(
    ctx: AppContext, chat_id: int, user_id: int, label: str, scope: str, page: int
) -> panels.Panel:
    user = await load_user(ctx, user_id)
    token = ctx.user_token(user)
    if not token:
        return panels.need_login(ctx.keys, chat_id > 0, ctx.deep_link("login"))
    query = await get_query(ctx, chat_id, user_id, scope)
    repos = await api.list_repos(ctx.gh, token) if scope == "r" else []
    orgs = await api.list_orgs(ctx.gh, token) if scope == "o" else []
    return panels.picker(ctx.keys, repos, orgs, scope, page, query, label)


# ── picker ────────────────────────────────────────────────────────────────────


@route(A.ADD, privileged=True)
async def add(c: CallbackCtx) -> None:
    scope = c.cb.arg(1, "r")
    if c.cb.int_arg(0) == 0:
        await c.show(panels.loading("⏳ <i>Fetching your repositories…</i>"))
    await c.show(
        await render_picker(c.ctx, c.chat.id, c.user_id, chat_label(c.chat), scope, c.cb.int_arg(0))
    )


@route(A.SEARCH, privileged=True)
async def search(c: CallbackCtx) -> None:
    scope = c.cb.arg(0, "r")
    msg_id = await c.show(panels.search_prompt(c.ctx.keys, scope, not is_private(c.chat)))
    await set_pending(c.ctx, c.user_id, c.chat.id, "search", msg_id, scope=scope)


@route(A.CLEAR_SEARCH, privileged=True)
async def clear_search(c: CallbackCtx) -> None:
    scope = c.cb.arg(0, "r")
    await set_query(c.ctx, c.chat.id, c.user_id, scope, None)
    await c.show(await render_picker(c.ctx, c.chat.id, c.user_id, chat_label(c.chat), scope, 0))


@route(A.PICK_REPO, privileged=True)
async def pick_repo(c: CallbackCtx) -> None:
    token = c.ctx.user_token(await load_user(c.ctx, c.user_id))
    if not token:
        await c.show(panels.need_login(c.ctx.keys, is_private(c.chat), c.ctx.deep_link("login")))
        return
    repo = await api.repo_by_id(c.ctx.gh, token, c.cb.int_arg(0))
    await c.show(
        panels.confirm_target(
            c.ctx.keys, "repo", repo.id, repo.full_name, repo.private, repo.description,
            is_private(c.chat), chat_label(c.chat),
        )
    )


@route(A.PICK_ORG, privileged=True)
async def pick_org(c: CallbackCtx) -> None:
    token = c.ctx.user_token(await load_user(c.ctx, c.user_id))
    if not token:
        await c.show(panels.need_login(c.ctx.keys, is_private(c.chat), c.ctx.deep_link("login")))
        return
    org = await api.org_by_id(c.ctx.gh, token, c.cb.int_arg(0))
    await c.show(
        panels.confirm_target(
            c.ctx.keys, "org", org.id, org.login, None, None, is_private(c.chat), chat_label(c.chat)
        )
    )


# ── create ────────────────────────────────────────────────────────────────────


async def finish_create(
    ctx: AppContext, user_id: int, panel_chat: int, panel_msg: int | None,
    target_chat: int, kind: str, target_id: int, manual: bool = False, where: str | None = None,
) -> Integration | None:
    user = await load_user(ctx, user_id)
    await show(ctx, panel_chat, panel_msg, panels.loading("⏳ <i>Creating the webhook…</i>"))
    try:
        result = await svc.create_integration(ctx, user, target_chat, kind, target_id, manual)
    except svc.IntegrationError as exc:
        await show(ctx, panel_chat, panel_msg, panels.notice(ctx.keys, f"⚠️ {exc.message}"))
        return None
    if result.hook_error:
        await show(
            ctx, panel_chat, panel_msg,
            panels.hook_failed(ctx.keys, result.hook_error, kind, target_id, allow_manual=panel_chat > 0),
        )
        return None
    integ = result.integration
    assert integ is not None
    async with ctx.db() as s:
        integ = await db.get_integration(s, integ.id) or integ
    if result.manual_secret:
        url = svc.webhook_url(ctx, integ.hook_key)
        await show(ctx, panel_chat, panel_msg, panels.manual_setup(ctx.keys, integ, url, result.manual_secret))
        return integ
    banner = (
        "✅ <b>Connected!</b> The webhook is live — events will appear "
        + (f"in <b>{esc(where)}</b>." if where else "here.")
        if result.created
        else "ℹ️ <b>Already connected</b> — here are its settings."
    )
    await show(ctx, panel_chat, panel_msg, panels.integration_detail(ctx.keys, integ, banner, where))
    return integ


@route(A.CREATE, privileged=True)
async def create(c: CallbackCtx) -> None:
    kind = "repo" if c.cb.arg(0) == "r" else "org"
    target_id = c.cb.int_arg(1)
    dest = c.cb.arg(2, "h")
    if dest in {"h", "m"}:
        await finish_create(
            c.ctx, c.user_id, c.chat.id, c.message_id, c.chat.id, kind, target_id,
            manual=dest == "m" and is_private(c.chat),
        )
        return
    if not is_private(c.chat):
        return
    is_channel = dest == "c"
    request = KeyboardButtonRequestChat(
        request_id=REQUEST_CHANNEL if is_channel else REQUEST_GROUP,
        chat_is_channel=is_channel,
        bot_is_member=None if is_channel else True,
        user_administrator_rights=_NO_RIGHTS,
        bot_administrator_rights=(
            _NO_RIGHTS.model_copy(update={"can_post_messages": True}) if is_channel else None
        ),
        request_title=True,
    )
    keyboard = ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text="📢 Choose channel" if is_channel else "👥 Choose group", request_chat=request)]],
        resize_keyboard=True,
        one_time_keyboard=True,
        input_field_placeholder="Tap the button below",
    )
    prompt = await c.ctx.bot.send_message(c.chat.id, panels.pick_chat_hint(dest), reply_markup=keyboard)
    await c.show(
        panels.notice(c.ctx.keys, "👇 <i>Use the button under the message box to pick the chat.</i>", A.CANCEL)
    )
    await set_pending(
        c.ctx, c.user_id, c.chat.id, "pick_chat", c.message_id,
        kind=kind, target_id=target_id, prompt_id=prompt.message_id,
    )


@router.message(F.chat_shared)
async def on_chat_shared(message: Message, ctx: AppContext) -> None:
    shared = message.chat_shared
    if shared is None or message.from_user is None:
        return
    uid, chat_id = message.from_user.id, message.chat.id
    async with ctx.db() as s, s.begin():
        pending = await db.get_pending(s, uid, chat_id)
    # Tidy up: remove the reply keyboard and the service messages.
    tmp = await ctx.bot.send_message(chat_id, "👌", reply_markup=ReplyKeyboardRemove())
    await safe_delete(ctx, chat_id, tmp.message_id)
    await safe_delete(ctx, chat_id, message.message_id)
    if pending is None or pending.kind != "pick_chat":
        return
    await safe_delete(ctx, chat_id, int(pending.data.get("prompt_id") or 0))
    await clear_pending(ctx, uid, chat_id)

    target_chat = shared.chat_id
    title = shared.title or "that chat"
    is_channel = shared.request_id == REQUEST_CHANNEL
    async with ctx.db() as s, s.begin():
        await db.upsert_chat(s, target_chat, "channel" if is_channel else "supergroup", shared.title)
    integ = await finish_create(
        ctx, uid, chat_id, pending.panel_message_id, target_chat,
        str(pending.data["kind"]), int(pending.data["target_id"]), where=title,
    )
    if integ is None:
        return
    try:
        await ctx.sender.call(
            target_chat,
            lambda: ctx.bot.send_message(
                target_chat,
                f"⚡ <b>GitPulse connected</b> · {esc(integ.target)}\n<i>GitHub events will appear here.</i>",
                disable_notification=True,
            ),
        )
    except Exception:  # no posting rights → roll back so nothing silently fails later
        await svc.remove_integration(ctx, integ)
        await show(
            ctx, chat_id, pending.panel_message_id,
            panels.notice(ctx.keys, f"⚠️ I can't post in <b>{esc(title)}</b>. Add me there (as admin for channels) and try again."),
        )


# ── list & settings ───────────────────────────────────────────────────────────


def _where(integ: Integration, current_chat: int) -> str | None:
    if integ.chat_id == current_chat:
        return None
    return integ.chat.title or "another chat"


async def list_panel(c: CallbackCtx, banner: str | None = None) -> panels.Panel:
    async with c.ctx.db() as s:
        here = list(await db.chat_integrations(s, c.chat.id))
    items: list[tuple[Integration, str | None]] = [(i, None) for i in here]
    if is_private(c.chat):
        async with c.ctx.db() as s:
            others = await db.owned_elsewhere(s, c.user_id, c.chat.id)
        items += [(i, i.chat.title or "another chat") for i in others]
    return panels.integrations_list(c.ctx.keys, items, chat_label(c.chat), banner)


@route(A.LIST)
async def list_(c: CallbackCtx) -> None:
    await c.show(await list_panel(c))


async def _detail(c: CallbackCtx, integration_id: int, banner: str | None = None) -> None:
    async with c.ctx.db() as s:
        integ = await db.get_integration(s, integration_id)
    if integ is None:
        await c.show(await list_panel(c, "ℹ️ That integration is gone."))
        return
    await c.show(panels.integration_detail(c.ctx.keys, integ, banner, _where(integ, c.chat.id)))


@route(A.OPEN)
async def open_(c: CallbackCtx) -> None:
    await clear_pending(c.ctx, c.user_id, c.chat.id)
    await _detail(c, c.cb.int_arg(0))


@route(A.TOGGLE, privileged=True)
async def toggle(c: CallbackCtx) -> None:
    bit = c.cb.int_arg(1)
    if not 0 <= bit < 16:
        return
    async with c.ctx.db() as s, s.begin():
        integ = await s.get(Integration, c.cb.int_arg(0))
        if integ:
            integ.events ^= 1 << bit
    await _detail(c, c.cb.int_arg(0))


@route(A.QUIET, privileged=True)
async def quiet(c: CallbackCtx) -> None:
    async with c.ctx.db() as s, s.begin():
        integ = await s.get(Integration, c.cb.int_arg(0))
        if integ:
            integ.quiet = not integ.quiet
    await _detail(c, c.cb.int_arg(0))


@route(A.BRANCH, privileged=True)
async def branch(c: CallbackCtx) -> None:
    iid = c.cb.int_arg(0)
    if c.cb.arg(1) == "reset":
        async with c.ctx.db() as s, s.begin():
            integ = await s.get(Integration, iid)
            if integ:
                integ.branch_filter = None
        await clear_pending(c.ctx, c.user_id, c.chat.id)
        await _detail(c, iid, "🌿 Branch filter cleared — all branches.")
        return
    async with c.ctx.db() as s:
        integ = await db.get_integration(s, iid)
    if integ is None:
        return
    msg_id = await c.show(panels.branch_prompt(c.ctx.keys, integ, not is_private(c.chat)))
    await set_pending(c.ctx, c.user_id, c.chat.id, "branch", msg_id, integration_id=iid)


@route(A.PING, privileged=True)
async def ping(c: CallbackCtx) -> None:
    async with c.ctx.db() as s:
        integ = await db.get_integration(s, c.cb.int_arg(0))
    if integ is None:
        return
    result = await svc.ping(c.ctx, integ)
    await _detail(c, integ.id, f"🔄 {result}")


@route(A.REMOVE, privileged=True)
async def remove(c: CallbackCtx) -> None:
    async with c.ctx.db() as s:
        integ = await db.get_integration(s, c.cb.int_arg(0))
    if integ:
        await c.show(panels.remove_confirm(c.ctx.keys, integ))


@route(A.REMOVE_YES, privileged=True)
async def remove_yes(c: CallbackCtx) -> None:
    async with c.ctx.db() as s:
        integ = await db.get_integration(s, c.cb.int_arg(0))
    if integ is None:
        await c.show(await list_panel(c))
        return
    warning = await svc.remove_integration(c.ctx, integ)
    banner = f"🗑 <b>{esc(integ.target)}</b> removed." + (f"\n⚠️ {esc(warning)}" if warning else "")
    await c.show(await list_panel(c, banner))
