"""Turns stored deliveries into Telegram messages."""

from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import selectinload

from app.context import AppContext
from app.db import repo as db
from app.db.models import Delivery, Integration
from app.domain import EventFlag, branch_allowed, event_enabled
from app.formatting import events as fmt
from app.log import get_logger
from app.services.publisher import Publisher, Target
from app.workers.debounce import Batch, Debouncer
from app.workers.queue import Outcome

log = get_logger(__name__)

PushKey = tuple[int, str]  # integration id, ref
ReviewKey = tuple[int, int, int]  # integration id, PR number, review id (0 = loose comments)


class Processor:
    def __init__(self, ctx: AppContext) -> None:
        self.ctx = ctx
        self.publisher = Publisher(ctx)
        s = ctx.settings
        self.pushes: Debouncer[PushKey] = Debouncer(
            s.push_debounce_seconds, s.push_debounce_seconds * 4, self._flush_push
        )
        self.reviews: Debouncer[ReviewKey] = Debouncer(
            s.review_debounce_seconds, s.review_debounce_seconds * 5, self._flush_review
        )
        self._artifacts: Any = None  # set by attach_artifacts()

    def attach_artifacts(self, service: Any) -> None:
        self._artifacts = service

    async def shutdown(self) -> None:
        await self.pushes.flush_all()
        await self.reviews.flush_all()

    # ── entry point (called by EventQueue workers) ────────────────────────
    async def handle(self, pk: int) -> Outcome:
        async with self.ctx.db() as s:
            delivery = await s.get(Delivery, pk)
            if delivery is None or delivery.integration_id is None:
                return Outcome.SKIPPED
            res = await s.execute(
                select(Integration)
                .where(Integration.id == delivery.integration_id)
                .options(selectinload(Integration.owner), selectinload(Integration.chat))
            )
            integ = res.scalar_one_or_none()
        if integ is None or not integ.active or not integ.chat.active:
            return Outcome.SKIPPED

        event, payload = delivery.event, delivery.payload
        if event == "ping":
            return Outcome.DONE
        if not event_enabled(integ.events, event):
            return Outcome.SKIPPED
        log.info("processing", delivery=pk, gh_event=event, integration=integ.id)

        if event == "push":
            branch = str(payload.get("ref", "")).removeprefix("refs/heads/")
            if not branch_allowed(integ.branch_filter, branch):
                return Outcome.SKIPPED
            self.pushes.add((integ.id, str(payload.get("ref"))), payload, pk)
            return Outcome.DEFERRED

        if event == "pull_request_review":
            review = payload.get("review") or {}
            if payload.get("action") != "submitted":
                return Outcome.SKIPPED
            key = (integ.id, int(payload["pull_request"]["number"]), int(review.get("id") or 0))
            self.reviews.add(key, ("review", payload), pk)
            return Outcome.DEFERRED

        if event == "pull_request_review_comment":
            if payload.get("action") != "created":
                return Outcome.SKIPPED
            comment = payload.get("comment") or {}
            key = (
                integ.id,
                int(payload["pull_request"]["number"]),
                int(comment.get("pull_request_review_id") or 0),
            )
            self.reviews.add(key, ("comment", payload), pk)
            return Outcome.DEFERRED

        token = self.ctx.user_token(integ.owner)
        target = Target(integ.chat_id, integ.id, integ.quiet)

        if event == "workflow_run":
            return await self._workflow(integ, target, payload, token)
        if event == "release":
            rendered = fmt.render_release(payload, self.ctx.settings.artifact_pattern)
            if rendered is None:
                return Outcome.SKIPPED
            msg_id = await self.publisher.publish(target, rendered, token)
            if self._artifacts and integ.events & EventFlag.ARTIFACTS and msg_id:
                await self._artifacts.deliver_release_assets(integ, payload, token, msg_id)
            return Outcome.DONE

        renderer = fmt.SIMPLE_RENDERERS.get(event)
        if renderer is None:
            return Outcome.SKIPPED
        rendered = renderer(payload)
        if rendered is None:
            return Outcome.SKIPPED
        await self.publisher.publish(target, rendered, token)
        return Outcome.DONE

    # ── workflow runs (+ APK artifacts) ───────────────────────────────────
    async def _workflow(
        self, integ: Integration, target: Target, payload: dict[str, Any], token: str | None
    ) -> Outcome:
        run = payload.get("workflow_run") or {}
        if payload.get("action") != "completed":
            return Outcome.SKIPPED
        plan = None
        wants_apk = bool(integ.events & EventFlag.ARTIFACTS) and run.get("conclusion") == "success"
        if self._artifacts and wants_apk:
            plan = await self._artifacts.plan_run(integ, payload, token)
        rendered = fmt.render_workflow_run(payload, apk_link=plan.button_url if plan else None)
        if rendered is None:
            return Outcome.SKIPPED
        msg_id = await self.publisher.publish(target, rendered, token)
        if plan and msg_id:
            await self._artifacts.deliver_run(integ, payload, plan, token, msg_id)
        return Outcome.DONE

    # ── debounced flushes ─────────────────────────────────────────────────
    async def _finish(self, batch: Batch, status: str = "done") -> None:
        async with self.ctx.db() as s, s.begin():
            for pk in batch.delivery_ids:
                await db.finish_delivery(s, pk, status)

    async def _load(self, integration_id: int) -> Integration | None:
        async with self.ctx.db() as s:
            return await db.get_integration(s, integration_id)

    async def _flush_push(self, key: PushKey, batch: Batch) -> None:
        integ = await self._load(key[0])
        if integ is None or not integ.active:
            await self._finish(batch, "skipped")
            return
        rendered = fmt.render_push(batch.items)
        if rendered is not None:
            target = Target(integ.chat_id, integ.id, integ.quiet)
            await self.publisher.publish(target, rendered, self.ctx.user_token(integ.owner))
        await self._finish(batch)

    async def _flush_review(self, key: ReviewKey, batch: Batch) -> None:
        integ = await self._load(key[0])
        if integ is None or not integ.active:
            await self._finish(batch, "skipped")
            return
        review = None
        comments: list[dict[str, Any]] = []
        base: dict[str, Any] | None = None
        for kind, payload in batch.items:
            base = payload
            if kind == "review":
                review = payload.get("review")
            else:
                comments.append(payload.get("comment") or {})
        if base is not None:
            rendered = fmt.render_review_batch(review, comments, base)
            if rendered is not None:
                target = Target(integ.chat_id, integ.id, integ.quiet)
                await self.publisher.publish(target, rendered, self.ctx.user_token(integ.owner))
        await self._finish(batch)
