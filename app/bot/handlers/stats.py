"""/stats <github-username> — contribution card with period switch."""

from __future__ import annotations

import time

from aiogram import Router
from aiogram.filters import Command, CommandObject
from aiogram.types import InlineKeyboardMarkup, Message

from app.bot.common import NO_PREVIEW, CallbackCtx, load_user, route, show
from app.bot.keyboards import A, Keys
from app.context import AppContext
from app.db import repo as db
from app.formatting.html import esc, link
from app.github.client import GitHubError, RateLimited
from app.services.stats import PERIOD_LABEL, Stats, StatsError, compute, valid_login

router = Router(name="stats")


def _n(v: int | None) -> str:
    return "—" if v is None else f"{v:,}"


def _bar(value: int, top: int, width: int = 10) -> str:
    filled = round(width * value / top) if top else 0
    return "█" * filled + "░" * (width - filled)


def _when(ts: float | None, fmt: str) -> str:
    if ts is None:
        return "—"
    return f'<tg-time unix="{int(ts)}" format="{fmt}">{time.strftime("%Y-%m-%d", time.gmtime(ts))}</tg-time>'


def render_card(keys: Keys, st: Stats) -> tuple[str, InlineKeyboardMarkup]:
    name = f" · <i>{esc(st.name)}</i>" if st.name else ""
    lines = [
        f"📊 <b>{link(st.html_url, st.login)}</b>{name}",
        f"🗓 {PERIOD_LABEL[st.period]} · {st.repos_total} repo{'s' if st.repos_total != 1 else ''} in this chat",
        "",
        "<blockquote>"
        f"🔨 <b>{_n(st.commits)}</b> commits\n"
        f"📝 <b>+{_n(st.additions)}</b> / <b>−{_n(st.deletions)}</b> lines\n"
        f"🔀 <b>{_n(st.prs_opened)}</b> PRs opened · <b>{_n(st.prs_merged)}</b> merged\n"
        f"🐛 <b>{_n(st.issues_opened)}</b> issues opened\n"
        f"👀 <b>{_n(st.reviews)}</b> PRs reviewed"
        "</blockquote>",
    ]
    if st.top_repo:
        repo, c = st.top_repo
        lines.append(
            f"🏆 Top repo: {link(f'https://github.com/{repo}', repo.split('/')[-1])} · {c:,} commits"
        )
        top = st.per_repo[0][1]
        rows = st.per_repo[:5]
        width = max(len(r.split("/")[-1][:18]) for r, _ in rows)
        chart = "\n".join(
            f"{r.split('/')[-1][:18].ljust(width)} {_bar(v, top)} {v}" for r, v in rows
        )
        lines.append(f"<pre>{esc(chart)}</pre>")
    elif not st.computing:
        lines.append("<i>No commits in this period.</i>")
    lines.append(
        f"⏮ First commit {_when(st.first_commit, 'D')} · ⏭ last {_when(st.last_commit, 'r')}"
    )
    notes = []
    if st.computing:
        notes.append(
            f"⏳ GitHub is still computing {len(st.computing)} repo(s) — tap 🔄 in a minute."
        )
    if st.period != "all":
        notes.append("≈ line counts use GitHub's weekly stats.")
    if st.loc_zeroed:
        notes.append("Repos with 10k+ commits report no line counts.")
    if notes:
        lines += ["", "<i>" + " ".join(notes) + "</i>"]

    periods = [
        keys.cb(
            ("• " + lbl + " •") if p == st.period else lbl,
            A.STATS,
            p,
            st.login,
            style="primary" if p == st.period else None,
        )
        for p, lbl in (("7d", "7 days"), ("30d", "30 days"), ("all", "All time"))
    ]
    markup = keys.markup(
        periods,
        [
            keys.url("↗ Open on GitHub", st.html_url),
            keys.cb("🔄 Refresh", A.STATS, st.period, st.login, "r"),
        ],
    )
    return "\n".join(lines), markup


async def _token_for(ctx: AppContext, user_id: int, chat_id: int) -> str | None:
    token = ctx.user_token(await load_user(ctx, user_id))
    if token:
        return token
    async with ctx.db() as s:
        for integ in await db.chat_integrations(s, chat_id):
            if t := ctx.user_token(integ.owner):
                return t
    return None


async def run_stats(
    ctx: AppContext,
    chat_id: int,
    msg_id: int | None,
    user_id: int,
    login: str,
    period: str,
    refresh: bool = False,
) -> None:
    keys = ctx.keys
    last_edit = 0.0
    msg_id = await show(
        ctx,
        chat_id,
        msg_id,
        (f"📊 <b>{esc(login)}</b>\n⏳ <i>Crunching numbers…</i>", keys.markup()),
    )

    async def progress(done: int, total: int) -> None:
        nonlocal last_edit
        if time.monotonic() - last_edit < 1.2 or done == total:
            return
        last_edit = time.monotonic()
        await show(
            ctx,
            chat_id,
            msg_id,
            (
                f"📊 <b>{esc(login)}</b>\n⏳ <i>Crunching numbers…</i>\n<code>{_bar(done, total, 12)}</code> {done}/{total} repos",
                keys.markup(),
            ),
        )

    token = await _token_for(ctx, user_id, chat_id)
    try:
        st = await compute(ctx, chat_id, login, period, token, progress, refresh)
    except StatsError as exc:
        await show(ctx, chat_id, msg_id, (f"📊 {exc}", keys.markup()))
        return
    except RateLimited:
        await show(
            ctx,
            chat_id,
            msg_id,
            ("⏳ GitHub rate limit reached — try again shortly.", keys.markup()),
        )
        return
    except GitHubError as exc:
        await show(ctx, chat_id, msg_id, (f"⚠️ GitHub error {exc.status}.", keys.markup()))
        return
    await show(ctx, chat_id, msg_id, render_card(keys, st))


@router.message(Command("stats"))
async def cmd_stats(message: Message, command: CommandObject, ctx: AppContext) -> None:
    if message.from_user is None:
        return
    raw = (command.args or "").strip()
    if raw:
        login = valid_login(raw.split()[0])
        if not login:
            await message.answer(
                "🤔 That isn't a valid GitHub username.", link_preview_options=NO_PREVIEW
            )
            return
    else:
        user = await load_user(ctx, message.from_user.id)
        if not user.github_login:
            await message.answer(
                "📊 Usage: <code>/stats username</code>\nOr log in to see your own stats.",
                reply_markup=ctx.keys.markup(
                    [ctx.keys.url("🔑 Log in", ctx.deep_link("login"), style="success")]
                ),
            )
            return
        login = user.github_login
    await run_stats(ctx, message.chat.id, None, message.from_user.id, login, "30d")


@route(A.STATS)
async def stats_cb(c: CallbackCtx) -> None:
    period = c.cb.arg(0, "30d")
    if period not in PERIOD_LABEL:
        period = "30d"
    login = valid_login(c.cb.arg(1))
    if login:
        await run_stats(
            c.ctx, c.chat.id, c.message_id, c.user_id, login, period, c.cb.arg(2) == "r"
        )
