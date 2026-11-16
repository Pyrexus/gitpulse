"""Keep managed GitHub webhooks pointed at the current PUBLIC_URL.

Useful behind tunnels whose hostname can change between restarts: on startup,
if PUBLIC_URL differs from the last one we ran with, every managed hook's
config URL is updated in place.
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import selectinload

from app.context import AppContext
from app.db import repo as db
from app.db.models import Integration, StatsCache
from app.github.client import GitHubError
from app.log import get_logger
from app.services.integrations import webhook_url

log = get_logger(__name__)
_KEY = "meta:public_url"


async def sync_hook_urls(ctx: AppContext) -> None:
    current = ctx.settings.public_url
    async with ctx.db() as s:
        row = await s.get(StatsCache, _KEY)
        previous = (row.data or {}).get("url") if row else None
    if previous == current:
        return
    async with ctx.db() as s:
        res = await s.execute(
            select(Integration)
            .where(Integration.hook_id.is_not(None))
            .options(selectinload(Integration.owner))
        )
        integrations = list(res.scalars().all())
    updated = 0
    for integ in integrations:
        token = ctx.user_token(integ.owner)
        if not token or integ.hook_id is None:
            continue
        base = f"/repos/{integ.target}" if integ.kind == "repo" else f"/orgs/{integ.target}"
        try:
            await ctx.gh.request(
                "PATCH",
                f"{base}/hooks/{integ.hook_id}/config",
                token=token,
                json={"url": webhook_url(ctx, integ.hook_key), "content_type": "json"},
            )
            updated += 1
        except GitHubError as exc:
            log.warning("hook_sync_failed", integration=integ.id, status=exc.status)
    async with ctx.db() as s, s.begin():
        await db.cache_put(s, _KEY, {"url": current})
    if previous:
        log.info("hooks_resynced", updated=updated, total=len(integrations))
