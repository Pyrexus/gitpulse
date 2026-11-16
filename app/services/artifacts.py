"""Deliver build outputs (APKs by default) from workflow artifacts and release assets."""

from __future__ import annotations

import asyncio
import fnmatch
import tempfile
import zipfile
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any

from aiogram.exceptions import TelegramBadRequest
from aiogram.types import FSInputFile, ReplyParameters

from app.context import AppContext
from app.db import repo as db
from app.db.models import Integration
from app.formatting.events import artifact_caption, matching_assets
from app.github.api import run_artifacts
from app.github.client import GitHubError, TooLarge
from app.log import get_logger
from app.telegram.sender import ChatUnavailable

log = get_logger(__name__)

MAX_ARTIFACTS_PER_RUN = 3
UPLOAD_TIMEOUT = 900


@dataclass(slots=True)
class ArtifactItem:
    key: str
    name: str
    size: int
    url: str


@dataclass(slots=True)
class ArtifactPlan:
    items: list[ArtifactItem] = field(default_factory=list)
    button_url: str | None = None


def _patterns(raw: str) -> list[str]:
    return [p.strip().lower() for p in raw.split(",") if p.strip()]


def _matches(name: str, pats: list[str]) -> bool:
    return any(fnmatch.fnmatch(name.lower(), p) for p in pats)


class ArtifactService:
    def __init__(self, ctx: AppContext) -> None:
        self.ctx = ctx
        self._sem = asyncio.Semaphore(2)  # concurrent big downloads

    @property
    def limit(self) -> int:
        return self.ctx.settings.upload_limit_bytes

    # ── workflow runs ─────────────────────────────────────────────────────
    async def plan_run(
        self, integ: Integration, payload: dict[str, Any], token: str | None
    ) -> ArtifactPlan | None:
        run, repo = payload["workflow_run"], payload["repository"]
        try:
            artifacts = await run_artifacts(self.ctx.gh, token, repo["full_name"], int(run["id"]))
        except GitHubError as exc:
            log.info("artifacts_list_failed", repo=repo["full_name"], status=exc.status)
            return None
        if not artifacts:
            return None
        pats = _patterns(self.ctx.settings.artifact_pattern)
        # Prefer artifacts whose name hints at the pattern (e.g. "app-release-apk").
        hints = [p.strip("*.") for p in pats]

        def score(a: dict[str, Any]) -> int:
            name = str(a.get("name", "")).lower()
            return 0 if _matches(name, pats) or any(h and h in name for h in hints) else 1

        artifacts.sort(key=score)
        run_url = f"{run['html_url']}#artifacts"
        plan = ArtifactPlan()
        for a in artifacts[:MAX_ARTIFACTS_PER_RUN]:
            size = int(a.get("size_in_bytes") or 0)
            if token is None or size > self.limit:
                if score(a) == 0:
                    plan.button_url = run_url
                continue
            plan.items.append(
                ArtifactItem(f"artifact:{a['id']}", a["name"], size, a["archive_download_url"])
            )
        if not plan.items and not plan.button_url:
            return None
        return plan

    async def deliver_run(
        self,
        integ: Integration,
        payload: dict[str, Any],
        plan: ArtifactPlan,
        token: str | None,
        reply_to: int,
    ) -> None:
        run, repo = payload["workflow_run"], payload["repository"]
        pats = _patterns(self.ctx.settings.artifact_pattern)
        for item in plan.items:
            async with self.ctx.db() as s, s.begin():
                if not await db.claim_artifact(s, item.key, integ.chat_id):
                    continue
            try:
                sent = await self._deliver_zip(integ, item, pats, token, reply_to, repo, run)
            except Exception:
                log.exception("artifact_delivery_failed", key=item.key)
                sent = False
            if not sent:
                async with self.ctx.db() as s, s.begin():
                    await db.release_artifact_claim(s, item.key, integ.chat_id)

    async def _deliver_zip(
        self,
        integ: Integration,
        item: ArtifactItem,
        pats: list[str],
        token: str | None,
        reply_to: int,
        repo: dict[str, Any],
        run: dict[str, Any],
    ) -> bool:
        async with self._sem:
            with tempfile.TemporaryDirectory(prefix="gitpulse-") as tmp:
                zpath = Path(tmp) / "artifact.zip"
                try:
                    await self.ctx.gh.download_file(item.url, zpath, token, self.limit + 2**20)
                except TooLarge:
                    return False
                except GitHubError as exc:
                    log.info("artifact_download_failed", key=item.key, status=exc.status)
                    return False
                sent_any = False
                with zipfile.ZipFile(zpath) as zf:
                    for info in zf.infolist():
                        name = PurePosixPath(info.filename).name  # zip-slip safe
                        if info.is_dir() or not name or not _matches(name, pats):
                            continue
                        if info.file_size > self.limit:
                            continue
                        out = Path(tmp) / name
                        with zf.open(info) as src, out.open("wb") as dst:
                            while chunk := src.read(1 << 20):
                                dst.write(chunk)
                        caption = artifact_caption(repo, name, info.file_size, run=run)
                        file_key = f"{item.key}:{name}"
                        if await self._send_document(integ, file_key, out, caption, reply_to):
                            sent_any = True
                return sent_any

    # ── release assets ────────────────────────────────────────────────────
    async def deliver_release_assets(
        self, integ: Integration, payload: dict[str, Any], token: str | None, reply_to: int
    ) -> None:
        rel, repo = payload["release"], payload["repository"]
        for asset in matching_assets(rel, self.ctx.settings.artifact_pattern)[:3]:
            size = int(asset.get("size") or 0)
            if size > self.limit:
                continue  # the release message already carries a Download button
            key = f"asset:{asset['id']}"
            async with self.ctx.db() as s, s.begin():
                if not await db.claim_artifact(s, key, integ.chat_id):
                    continue
            ok = False
            try:
                async with self._sem:
                    with tempfile.TemporaryDirectory(prefix="gitpulse-") as tmp:
                        out = Path(tmp) / PurePosixPath(str(asset["name"])).name
                        await self.ctx.gh.download_file(
                            asset["url"],
                            out,
                            token,
                            self.limit + 2**20,
                            accept="application/octet-stream",
                        )
                        caption = artifact_caption(repo, out.name, size, release=rel)
                        ok = await self._send_document(integ, key, out, caption, reply_to)
            except (GitHubError, OSError) as exc:
                log.info("asset_delivery_failed", key=key, error=str(exc)[:120])
            if not ok:
                async with self.ctx.db() as s, s.begin():
                    await db.release_artifact_claim(s, key, integ.chat_id)

    # ── sending ───────────────────────────────────────────────────────────
    async def _send_document(
        self, integ: Integration, key: str, path: Path, caption: str, reply_to: int
    ) -> bool:
        bot, chat = self.ctx.bot, integ.chat_id
        async with self.ctx.db() as s:
            file_id = await db.known_file_id(s, key)
        document: Any = file_id or FSInputFile(path, filename=path.name)
        reply = ReplyParameters(message_id=reply_to, allow_sending_without_reply=True)
        try:
            msg = await self.ctx.sender.call(
                chat,
                lambda: bot.send_document(
                    chat_id=chat,
                    document=document,
                    caption=caption,
                    reply_parameters=reply,
                    disable_notification=integ.quiet,
                    request_timeout=UPLOAD_TIMEOUT,
                ),
            )
        except (TelegramBadRequest, ChatUnavailable) as exc:
            log.info("document_send_failed", chat_id=chat, error=str(exc)[:120])
            return False
        if msg.document:
            async with self.ctx.db() as s, s.begin():
                await db.claim_artifact(s, key, chat)
                await db.set_artifact_file_id(s, key, chat, msg.document.file_id)
        return True
