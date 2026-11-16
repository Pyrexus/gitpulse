"""/start, the central callback dispatcher, home, help, login and logout."""

from __future__ import annotations

import asyncio

from aiogram import F, Router
from aiogram.filters import CommandObject, CommandStart
from aiogram.types import CallbackQuery, Chat, Message

from app.bot import panels
from app.bot.common import (
    PRIVILEGED,
    ROUTES,
    CallbackCtx,
    can_manage,
    clear_pending,
    home_panel,
    is_private,
    load_user,
    route,
    set_pending,
    show,
    touch_chat,
)
from app.bot.keyboards import A, ttl_for
from app.context import AppContext
from app.db import repo as db
from app.db.models import User
from app.formatting.html import esc
from app.github import api, oauth
from app.github.client import GitHubError, RateLimited
from app.log import get_logger
from app.security.signing import CallbackError, CallbackExpired

log = get_logger(__name__)
router = Router(name="core")

# Integration-scoped actions: the first callback arg is the integration id.
INTEGRATION_ACTIONS = {A.OPEN, A.TOGGLE, A.QUIET, A.BRANCH, A.PING, A.REMOVE, A.REMOVE_YES}
_device_tasks: dict[int, asyncio.Task[None]] = {}


# ── /start ────────────────────────────────────────────────────────────────────


@router.message(CommandStart())
async def cmd_start(message: Message, command: CommandObject, ctx: AppContext) -> None:
    if message.from_user is None:
        return
    await touch_chat(ctx, message.chat)
    uid = message.from_user.id
    await clear_pending(ctx, uid, message.chat.id)
    if command.args == "login" and is_private(message.chat):
        user = await load_user(ctx, uid)
        if not user.logged_in:
            msg_id = await show(ctx, message.chat.id, None, panels.login_prompt(ctx.keys))
            await set_pending(ctx, uid, message.chat.id, "pat", msg_id)
            return
    await show(ctx, message.chat.id, None, await home_panel(ctx, uid, message.chat))


# ── dispatcher ────────────────────────────────────────────────────────────────


@router.callback_query(F.data)
async def on_callback(cq: CallbackQuery, ctx: AppContext) -> None:
    data = cq.data or ""
    action = ctx.signer.peek_action(data)
    try:
        cb = ctx.signer.unpack(data, ttl_for(action, ctx.settings.panel_ttl_hours))
    except CallbackExpired:
        await cq.answer("This panel expired — here's a fresh start.")
        if cq.message is not None:
            await show(ctx, cq.message.chat.id, cq.message.message_id, panels.expired(ctx.keys))
        return
    except CallbackError:
        await cq.answer("That button is no longer valid.")
        return

    if cb.action == A.NOOP or cq.message is None:
        await cq.answer()
        return
    if cb.action == A.COMMENT:
        await _comment_hint(cq, ctx)
        return
    handler = ROUTES.get(cb.action)
    if handler is None:
        await cq.answer()
        return

    c = CallbackCtx(ctx, cq, cb)
    if cb.action in PRIVILEGED or cb.action in INTEGRATION_ACTIONS:
        integ = None
        if cb.action in INTEGRATION_ACTIONS:
            async with ctx.db() as s:
                integ = await db.get_integration(s, cb.int_arg(0))
            if integ is None:
                await cq.answer("That integration no longer exists.")
                await c.show(await home_panel(ctx, c.user_id, c.chat))
                return
        visible = integ is None or integ.chat_id == c.chat.id or integ.owner_id == c.user_id
        allowed = visible and (
            cb.action not in PRIVILEGED or await can_manage(ctx, c.chat, c.user_id, integ)
        )
        if not allowed:
            await cq.answer(
                "🔒 Only chat admins or the integration owner can change this.", show_alert=True
            )
            return

    await cq.answer()  # instant feedback; the panel edit follows
    try:
        await handler(c)
    except RateLimited as exc:
        minutes = int(exc.retry_in // 60) + 1
        await c.show(
            panels.notice(ctx.keys, f"⏳ GitHub rate limit reached — try again in {minutes} min.")
        )
    except GitHubError as exc:
        log.info("callback_github_error", action=cb.action, status=exc.status)
        await c.show(
            panels.notice(ctx.keys, f"⚠️ GitHub said: <i>{esc(exc.message[:150] or exc.status)}</i>")
        )


async def _comment_hint(cq: CallbackQuery, ctx: AppContext) -> None:
    user = await load_user(ctx, cq.from_user.id)
    if user.logged_in:
        await cq.answer(
            f"💬 Reply to this message — I'll post it on GitHub as @{user.github_login}.",
            show_alert=True,
        )
    else:
        await cq.answer(url=ctx.deep_link("login"))


# ── home / help / cancel ──────────────────────────────────────────────────────


@route(A.HOME)
async def home(c: CallbackCtx) -> None:
    await clear_pending(c.ctx, c.user_id, c.chat.id)
    await c.show(await home_panel(c.ctx, c.user_id, c.chat))


@route(A.HELP)
async def help_(c: CallbackCtx) -> None:
    await c.show(panels.help_panel(c.ctx.keys))


@route(A.CANCEL)
async def cancel(c: CallbackCtx) -> None:
    await clear_pending(c.ctx, c.user_id, c.chat.id)
    task = _device_tasks.pop(c.user_id, None)
    if task:
        task.cancel()
    await c.show(await home_panel(c.ctx, c.user_id, c.chat))


# ── login / logout ────────────────────────────────────────────────────────────


@route(A.LOGIN)
async def login(c: CallbackCtx) -> None:
    if not is_private(c.chat):
        await c.show(
            panels.notice(
                c.ctx.keys, "🔑 Log in from our private chat — never paste tokens in groups."
            )
        )
        return
    msg_id = await c.show(panels.login_prompt(c.ctx.keys))
    await set_pending(c.ctx, c.user_id, c.chat.id, "pat", msg_id)


@route(A.LOGIN_OAUTH)
async def login_oauth(c: CallbackCtx) -> None:
    client_id = c.ctx.settings.github_oauth_client_id
    if not client_id or not is_private(c.chat):
        await c.show(panels.notice(c.ctx.keys, "GitHub login isn't enabled on this server."))
        return
    try:
        code = await oauth.start(client_id)
    except (oauth.DeviceFlowError, ValueError) as exc:
        await c.show(panels.notice(c.ctx.keys, f"⚠️ GitHub login unavailable: {esc(exc)}"))
        return
    msg_id = await c.show(panels.device_prompt(c.ctx.keys, code.user_code, code.verification_uri))
    old = _device_tasks.pop(c.user_id, None)
    if old:
        old.cancel()
    _device_tasks[c.user_id] = asyncio.create_task(
        _device_wait(c.ctx, c.user_id, c.chat, msg_id, client_id, code)
    )


async def _device_wait(
    ctx: AppContext, user_id: int, chat: Chat, msg_id: int, client_id: str, code: oauth.DeviceCode
) -> None:
    try:
        token = await oauth.poll(client_id, code)
        banner = await save_login(ctx, user_id, token, "oauth")
        await show(ctx, chat.id, msg_id, await home_panel(ctx, user_id, chat, banner=banner))
    except asyncio.CancelledError:
        return
    except (oauth.DeviceFlowError, LoginError) as exc:
        await show(
            ctx, chat.id, msg_id, panels.notice(ctx.keys, f"⚠️ GitHub login failed: {esc(exc)}")
        )
    finally:
        _device_tasks.pop(user_id, None)


@route(A.LOGOUT)
async def logout(c: CallbackCtx) -> None:
    user = await load_user(c.ctx, c.user_id)
    if not user.logged_in:
        await c.show(await home_panel(c.ctx, c.user_id, c.chat))
        return
    await c.show(panels.logout_confirm(c.ctx.keys, user))


@route(A.LOGOUT_YES)
async def logout_yes(c: CallbackCtx) -> None:
    async with c.ctx.db() as s, s.begin():
        user = await s.get(User, c.user_id)
        if user:
            user.token_enc = None
            user.token_scopes = None
            user.auth_method = None
    banner = "👋 <b>Logged out.</b> Your token was erased."
    await c.show(await home_panel(c.ctx, c.user_id, c.chat, banner=banner))


# ── login persistence (also used by the PAT input handler) ────────────────────


class LoginError(Exception):
    pass


async def save_login(ctx: AppContext, user_id: int, token: str, method: str) -> str:
    """Validate a token and store it encrypted. Returns a success banner."""
    try:
        ident = await api.whoami(ctx.gh, token)
    except GitHubError as exc:
        if exc.status == 401:
            raise LoginError("GitHub rejected that token (bad credentials).") from exc
        raise LoginError(
            f"Couldn't verify the token (GitHub {exc.status or 'unreachable'})."
        ) from exc
    async with ctx.db() as s, s.begin():
        user = await db.get_or_create_user(s, user_id)
        user.github_id = ident.id
        user.github_login = ident.login
        user.github_name = ident.name
        user.token_enc = ctx.encrypt_token(user_id, token)
        user.token_scopes = (",".join(ident.scopes) or ident.token_kind)[:500]
        user.auth_method = method
    missing = []
    if ident.token_kind == "classic":
        if "repo" not in ident.scopes and "public_repo" not in ident.scopes:
            missing.append("repo")
        if "admin:repo_hook" not in ident.scopes and "repo" not in ident.scopes:
            missing.append("admin:repo_hook")
    name = f" · {esc(ident.name)}" if ident.name else ""
    warn = f"\n⚠️ Missing scopes: <code>{', '.join(missing)}</code>" if missing else ""
    return f"✅ <b>Signed in as @{esc(ident.login)}</b>{name}{warn}"
