"""aiohttp application: GitHub webhooks, Telegram webhook, health."""

from __future__ import annotations

import json
import time
from dataclasses import dataclass

from aiohttp import web
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from app import __version__
from app.context import AppContext
from app.db import repo as db
from app.domain import GITHUB_EVENTS
from app.log import get_logger
from app.security.signing import verify_github_signature

log = get_logger(__name__)

CTX_KEY = web.AppKey("ctx", AppContext)
MAX_BODY = 26 * 1024 * 1024  # GitHub caps payloads at 25 MB
_ACCEPTED = {*GITHUB_EVENTS, "ping"}


@dataclass(slots=True)
class _Hook:
    integration_id: int
    secret: str
    active: bool
    expires: float


class HookCache:
    """hook_key → decrypted secret, so verification needs no DB round-trip."""

    def __init__(self, ctx: AppContext, ttl: float = 120.0) -> None:
        self.ctx = ctx
        self.ttl = ttl
        self._items: dict[str, _Hook] = {}

    async def get(self, key: str) -> _Hook | None:
        hit = self._items.get(key)
        if hit and hit.expires > time.monotonic():
            return hit
        async with self.ctx.db() as s:
            integ = await db.integration_by_hook_key(s, key)
        if integ is None:
            self._items.pop(key, None)
            return None
        secret = self.ctx.hook_secret(key, integ.secret_enc)
        if secret is None:
            return None
        hook = _Hook(integ.id, secret, integ.active, time.monotonic() + self.ttl)
        self._items[key] = hook
        return hook

    def invalidate(self, key: str) -> None:
        self._items.pop(key, None)


async def github_webhook(request: web.Request) -> web.Response:
    ctx = request.app[CTX_KEY]
    key = request.match_info["key"]
    if not (8 <= len(key) <= 48):
        return web.json_response({"error": "not found"}, status=404)
    hooks: HookCache = ctx.hook_cache
    hook = await hooks.get(key)
    if hook is None:
        return web.json_response({"error": "unknown hook"}, status=404)
    body = await request.read()
    if not verify_github_signature(hook.secret, body, request.headers.get("X-Hub-Signature-256")):
        log.warning("bad_signature", integration=hook.integration_id)
        return web.json_response({"error": "bad signature"}, status=401)

    event = request.headers.get("X-GitHub-Event", "")
    delivery_id = request.headers.get("X-GitHub-Delivery", "")
    if event not in _ACCEPTED or not delivery_id:
        return web.json_response({"ok": True, "ignored": event})
    try:
        payload = json.loads(body)
    except ValueError:
        return web.json_response({"error": "invalid json"}, status=400)

    try:
        async with ctx.db() as s, s.begin():
            pk = None
            if event != "ping" and hook.active:
                pk = await db.insert_delivery(s, delivery_id[:64], hook.integration_id, event, payload)
            await db.touch_integration(s, hook.integration_id)
    except IntegrityError:  # integration removed in the meantime
        hooks.invalidate(key)
        return web.json_response({"error": "unknown hook"}, status=404)

    if pk is not None and ctx.queue is not None:
        ctx.queue.enqueue(pk)
    return web.json_response({"ok": True, "queued": pk is not None})


async def health(request: web.Request) -> web.Response:
    ctx = request.app[CTX_KEY]
    db_ok = True
    try:
        async with ctx.db() as s:
            await s.execute(text("SELECT 1"))
    except Exception:
        db_ok = False
    body = {
        "status": "ok" if db_ok else "degraded",
        "version": __version__,
        "uptime_s": int(time.time() - ctx.started_at),
        "db": db_ok,
        "queue_depth": ctx.queue.depth if ctx.queue else None,
    }
    return web.json_response(body, status=200 if db_ok else 503)


async def index(_: web.Request) -> web.Response:
    return web.Response(text=f"GitPulse {__version__} ⚡\n", content_type="text/plain")


def build_web_app(ctx: AppContext) -> web.Application:
    app = web.Application(client_max_size=MAX_BODY)
    app[CTX_KEY] = ctx
    ctx.hook_cache = HookCache(ctx)
    app.router.add_get("/", index)
    app.router.add_get("/health", health)
    app.router.add_post("/github/webhook/{key}", github_webhook)
    return app
