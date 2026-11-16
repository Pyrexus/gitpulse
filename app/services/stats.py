"""Per-user contribution stats across the repos connected to a chat."""

from __future__ import annotations

import asyncio
import re
import time
from collections.abc import Awaitable, Callable
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from app.context import AppContext
from app.db import repo as db
from app.github import api
from app.github.client import GitHubError
from app.log import get_logger

log = get_logger(__name__)

PERIODS = {"7d": 7, "30d": 30, "all": None}
PERIOD_LABEL = {"7d": "Last 7 days", "30d": "Last 30 days", "all": "All time"}
MAX_REPOS = 40
CONTRIB_TTL = timedelta(hours=6)
RESULT_TTL = timedelta(minutes=15)
LOGIN_RE = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9]|-(?=[A-Za-z0-9])){0,38}$")

Progress = Callable[[int, int], Awaitable[None]]


class StatsError(Exception):
    pass


@dataclass
class Stats:
    login: str
    name: str | None
    html_url: str
    period: str
    repos_total: int
    commits: int = 0
    additions: int = 0
    deletions: int = 0
    prs_opened: int | None = None
    prs_merged: int | None = None
    issues_opened: int | None = None
    reviews: int | None = None
    first_commit: float | None = None
    last_commit: float | None = None
    per_repo: list[tuple[str, int]] = field(default_factory=list)
    computing: list[str] = field(default_factory=list)
    loc_zeroed: list[str] = field(default_factory=list)  # repos ≥10k commits: GitHub reports 0 LOC
    generated_at: float = field(default_factory=time.time)

    @property
    def top_repo(self) -> tuple[str, int] | None:
        return self.per_repo[0] if self.per_repo else None


def valid_login(raw: str) -> str | None:
    login = raw.strip().lstrip("@")
    return login if LOGIN_RE.match(login) else None


async def chat_scopes(
    ctx: AppContext, chat_id: int, token: str | None
) -> tuple[list[str], list[str]]:
    """(repo full names, search qualifiers) for every integration in the chat."""
    async with ctx.db() as s:
        integrations = await db.chat_integrations(s, chat_id)
    repos: list[str] = []
    quals: list[str] = []
    for integ in integrations:
        if integ.kind == "repo":
            repos.append(integ.target)
            quals.append(f"repo:{integ.target}")
        else:
            quals.append(f"org:{integ.target}")
            try:
                repos.extend(await api.org_repos(ctx.gh, token, integ.target, limit_pages=1))
            except GitHubError:
                log.info("org_repos_failed", org=integ.target)
    seen: set[str] = set()
    unique = [r for r in repos if not (r.lower() in seen or seen.add(r.lower()))]  # type: ignore[func-returns-value]
    return unique[:MAX_REPOS], sorted(set(quals))


async def _contributors(
    ctx: AppContext, repo: str, token: str | None
) -> dict[str, list[list[int]]] | None:
    """{login_lower: [[week, adds, dels, commits], …]} — ``None`` while GitHub computes."""
    key = f"contrib:{repo.lower()}"
    async with ctx.db() as s:
        cached = await db.cache_get(s, key, CONTRIB_TTL)
    if cached is not None:
        authors_cached: dict[str, list[list[int]]] = cached.get("authors", {})
        return authors_cached
    delay = 1.5
    for _ in range(4):
        resp = await ctx.gh.request(
            "GET", f"/repos/{repo}/stats/contributors", token=token, conditional=False
        )
        if resp.status == 202:  # GitHub is computing it in the background
            await asyncio.sleep(delay)
            delay *= 1.8
            continue
        authors: dict[str, list[list[int]]] = {}
        for entry in resp.data or []:
            author = (entry.get("author") or {}).get("login")
            if not author:
                continue
            authors[author.lower()] = [
                [w["w"], w["a"], w["d"], w["c"]]
                for w in entry.get("weeks", [])
                if w.get("c") or w.get("a") or w.get("d")
            ]
        async with ctx.db() as s, s.begin():
            await db.cache_put(s, key, {"authors": authors})
        return authors
    return None


async def _commit_bounds(
    ctx: AppContext, repo: str, login: str, since: datetime | None, token: str | None
) -> tuple[float | None, float | None]:
    params: dict[str, Any] = {"author": login, "per_page": 1}
    if since:
        params["since"] = since.isoformat()
    try:
        resp = await ctx.gh.request(
            "GET", f"/repos/{repo}/commits", token=token, params=params, cache_ttl=600
        )
    except GitHubError:
        return None, None
    if not resp.data:
        return None, None

    def _date(c: dict[str, Any]) -> float | None:
        raw = ((c.get("commit") or {}).get("author") or {}).get("date")
        return datetime.fromisoformat(raw.replace("Z", "+00:00")).timestamp() if raw else None

    last = _date(resp.data[0])
    first = last
    link_hdr = resp.header("link") or ""
    m = re.search(r'[?&]page=(\d+)>; rel="last"', link_hdr)
    if m:
        try:
            tail = await ctx.gh.request(
                "GET",
                f"/repos/{repo}/commits",
                token=token,
                params={**params, "page": int(m.group(1))},
                cache_ttl=3600,
            )
            if tail.data:
                first = _date(tail.data[0])
        except GitHubError:
            pass
    return first, last


_SEARCH = """
query($a: String!, $b: String!, $c: String!, $d: String!) {
  prs: search(query: $a, type: ISSUE, first: 0) { issueCount }
  merged: search(query: $b, type: ISSUE, first: 0) { issueCount }
  issues: search(query: $c, type: ISSUE, first: 0) { issueCount }
  reviews: search(query: $d, type: ISSUE, first: 0) { issueCount }
}
"""


def _chunks(quals: list[str], budget: int = 160) -> list[str]:
    out: list[str] = []
    cur = ""
    for q in quals:
        if cur and len(cur) + len(q) + 1 > budget:
            out.append(cur)
            cur = ""
        cur = f"{cur} {q}".strip()
    if cur:
        out.append(cur)
    return out


async def _search_counts(
    ctx: AppContext, quals: list[str], login: str, since: datetime | None, token: str
) -> tuple[int, int, int, int]:
    d = f" created:>={since:%Y-%m-%d}" if since else ""
    m = f" merged:>={since:%Y-%m-%d}" if since else ""
    u = f" updated:>={since:%Y-%m-%d}" if since else ""
    totals = [0, 0, 0, 0]
    for scope in _chunks(quals):
        data = await ctx.gh.graphql(
            _SEARCH,
            {
                "a": f"{scope} is:pr author:{login}{d}",
                "b": f"{scope} is:pr is:merged author:{login}{m}",
                "c": f"{scope} is:issue author:{login}{d}",
                "d": f"{scope} is:pr reviewed-by:{login} -author:{login}{u}",
            },
            token,
        )
        for i, k in enumerate(("prs", "merged", "issues", "reviews")):
            totals[i] += int((data.get(k) or {}).get("issueCount") or 0)
    return totals[0], totals[1], totals[2], totals[3]


async def compute(
    ctx: AppContext,
    chat_id: int,
    login: str,
    period: str,
    token: str | None,
    progress: Progress | None = None,
    refresh: bool = False,
) -> Stats:
    result_key = f"stats:{chat_id}:{login.lower()}:{period}"
    if not refresh:
        async with ctx.db() as s:
            hit = await db.cache_get(s, result_key, RESULT_TTL)
        if hit:
            hit["per_repo"] = [tuple(x) for x in hit.get("per_repo", [])]
            return Stats(**hit)

    try:
        profile = await ctx.gh.get(f"/users/{login}", token=token, cache_ttl=3600)
    except GitHubError as exc:
        if exc.status == 404:
            raise StatsError(f"GitHub user <code>{login}</code> doesn't exist.") from exc
        raise
    login = profile["login"]
    repos, quals = await chat_scopes(ctx, chat_id, token)
    if not repos and not quals:
        raise StatsError("No repositories are connected to this chat yet.")

    days = PERIODS[period]
    since = datetime.now(UTC) - timedelta(days=days) if days else None
    week_floor = (since - timedelta(days=6)).timestamp() if since else 0

    stats = Stats(
        login=login,
        name=profile.get("name"),
        html_url=profile["html_url"],
        period=period,
        repos_total=len(repos),
    )
    sem = asyncio.Semaphore(6)
    done = 0
    per_repo: dict[str, int] = {}

    async def one(repo: str) -> None:
        nonlocal done
        async with sem:
            try:
                authors = await _contributors(ctx, repo, token)
            except GitHubError:
                authors = {}
        done += 1
        if progress:
            await progress(done, len(repos))
        if authors is None:
            stats.computing.append(repo)
            return
        weeks = [w for w in authors.get(login.lower(), []) if w[0] >= week_floor]
        c = sum(w[3] for w in weeks)
        if c:
            per_repo[repo] = c
            stats.commits += c
            stats.additions += sum(w[1] for w in weeks)
            stats.deletions += sum(w[2] for w in weeks)
            if not any(w[1] or w[2] for w in weeks):
                stats.loc_zeroed.append(repo)

    await asyncio.gather(*(one(r) for r in repos))
    stats.per_repo = sorted(per_repo.items(), key=lambda kv: -kv[1])

    active = [r for r, _ in stats.per_repo[:10]]
    bounds = await asyncio.gather(*(_commit_bounds(ctx, r, login, since, token) for r in active))
    firsts = [f for f, _ in bounds if f]
    lasts = [last for _, last in bounds if last]
    stats.first_commit = min(firsts) if firsts else None
    stats.last_commit = max(lasts) if lasts else None

    search_token = token or ctx.gh.fallback_token
    if search_token and quals:
        try:
            (
                stats.prs_opened,
                stats.prs_merged,
                stats.issues_opened,
                stats.reviews,
            ) = await _search_counts(ctx, quals, login, since, search_token)
        except GitHubError as exc:
            log.info("stats_search_failed", status=exc.status)

    if not stats.computing:
        async with ctx.db() as s, s.begin():
            await db.cache_put(s, result_key, asdict(stats))
    return stats
