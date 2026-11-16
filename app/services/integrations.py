"""Create, test and remove integrations (and their GitHub webhooks)."""

from __future__ import annotations

import secrets
from dataclasses import dataclass

from app.context import AppContext
from app.db import repo as db
from app.db.models import Integration, User
from app.domain import ALL_EVENTS, GITHUB_EVENTS
from app.github import api
from app.github.client import GitHubError
from app.log import get_logger

log = get_logger(__name__)


@dataclass(slots=True)
class CreateResult:
    integration: Integration | None
    created: bool  # False → it already existed
    hook_error: str | None = None
    manual_secret: str | None = None  # only for manual setup


class IntegrationError(Exception):
    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


def webhook_url(ctx: AppContext, hook_key: str) -> str:
    return f"{ctx.settings.github_webhook_base}/{hook_key}"


def _hook_error(exc: GitHubError, kind: str) -> str:
    if exc.status in {403, 404}:
        scope = "admin:org_hook" if kind == "org" else "admin:repo_hook"
        return f"GitHub refused to create the webhook — the token needs the <code>{scope}</code> scope and admin access."
    if exc.status == 422:
        return "GitHub rejected the webhook (it may already exist with this URL, or PUBLIC_URL isn't reachable)."
    return f"GitHub error {exc.status}: {exc.message[:120]}"


async def resolve_target(
    ctx: AppContext, token: str, kind: str, target_id: int
) -> tuple[str, bool]:
    """Return (target name, user is admin)."""
    if kind == "repo":
        repo = await api.repo_by_id(ctx.gh, token, target_id)
        return repo.full_name, repo.can_admin
    org = await api.org_by_id(ctx.gh, token, target_id)
    return org.login, True  # org hook creation will tell us if not


async def create_integration(
    ctx: AppContext,
    user: User,
    chat_id: int,
    kind: str,
    target_id: int,
    manual: bool = False,
) -> CreateResult:
    token = ctx.user_token(user)
    if not token:
        raise IntegrationError("Please log in first.")
    try:
        target, is_admin = await resolve_target(ctx, token, kind, target_id)
    except GitHubError as exc:
        raise IntegrationError("I can't see that repository with your token.") from exc
    if not is_admin and not manual:
        raise IntegrationError(
            f"You need <b>admin</b> access to <code>{target}</code> to add a webhook."
        )

    async with ctx.db() as s, s.begin():
        existing = await db.find_integration(s, chat_id, kind, target)
        if existing is not None:
            existing.active = True
            return CreateResult(existing, created=False)

        hook_key = secrets.token_urlsafe(18)
        secret = secrets.token_urlsafe(32)
        integ = Integration(
            chat_id=chat_id,
            owner_id=user.id,
            kind=kind,
            target=target,
            target_id=target_id,
            hook_key=hook_key,
            secret_enc=ctx.box.encrypt(secret, f"hook:{hook_key}"),
            events=ALL_EVENTS,
            quiet=False,
            active=True,
            hook_ok=False,
        )
        s.add(integ)
        await s.flush()

    if manual:
        return CreateResult(integ, created=True, manual_secret=secret)

    try:
        hook_id = await api.create_hook(
            ctx.gh, token, kind, target, webhook_url(ctx, hook_key), secret, GITHUB_EVENTS
        )
    except GitHubError as exc:
        log.info("hook_create_failed", target=target, status=exc.status)
        async with ctx.db() as s, s.begin():
            row = await s.get(Integration, integ.id)
            if row:
                await s.delete(row)
        return CreateResult(None, created=False, hook_error=_hook_error(exc, kind))

    async with ctx.db() as s, s.begin():
        row = await s.get(Integration, integ.id)
        if row:
            row.hook_id = hook_id
            integ = row
    log.info("integration_created", integration=integ.id, kind=kind)
    return CreateResult(integ, created=True)


async def remove_integration(ctx: AppContext, integ: Integration) -> str | None:
    """Delete the webhook (best effort) and the integration. Returns a warning, if any."""
    warning = None
    if integ.hook_id:
        token = ctx.user_token(integ.owner)
        if token:
            try:
                await api.delete_hook(ctx.gh, token, integ.kind, integ.target, integ.hook_id)
            except GitHubError as exc:
                warning = f"Couldn't delete the GitHub webhook ({exc.status}); remove it manually."
        else:
            warning = "The owner is logged out, so the GitHub webhook must be removed manually."
    async with ctx.db() as s, s.begin():
        row = await s.get(Integration, integ.id)
        if row:
            await s.delete(row)
    if ctx.hook_cache:
        ctx.hook_cache.invalidate(integ.hook_key)
    return warning


async def ping(ctx: AppContext, integ: Integration) -> str:
    token = ctx.user_token(integ.owner)
    if not integ.hook_id or not token:
        return "No managed webhook to test."
    try:
        await api.ping_hook(ctx.gh, token, integ.kind, integ.target, integ.hook_id)
    except GitHubError as exc:
        return f"Ping failed: GitHub {exc.status}"
    return "Ping sent — status turns 🟢 when it arrives."
