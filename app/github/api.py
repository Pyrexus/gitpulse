"""High-level GitHub operations used by the bot."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from app.github.client import GitHub, GitHubError

FULL_JSON = "application/vnd.github.full+json"


@dataclass(slots=True)
class Identity:
    id: int
    login: str
    name: str | None
    html_url: str
    scopes: list[str]
    token_kind: str  # classic | fine-grained | oauth


@dataclass(slots=True)
class RepoInfo:
    id: int
    full_name: str
    private: bool
    can_admin: bool
    pushed_at: str | None
    description: str | None


@dataclass(slots=True)
class OrgInfo:
    id: int
    login: str


def _token_kind(token: str) -> str:
    if token.startswith("github_pat_"):
        return "fine-grained"
    if token.startswith("gho_"):
        return "oauth"
    return "classic"


async def whoami(gh: GitHub, token: str) -> Identity:
    resp = await gh.request("GET", "/user", token=token, conditional=False)
    scopes_hdr = resp.header("x-oauth-scopes") or ""
    u = resp.data
    return Identity(
        id=int(u["id"]),
        login=u["login"],
        name=u.get("name"),
        html_url=u["html_url"],
        scopes=[s.strip() for s in scopes_hdr.split(",") if s.strip()],
        token_kind=_token_kind(token),
    )


def _repo(r: dict[str, Any]) -> RepoInfo:
    perms = r.get("permissions") or {}
    return RepoInfo(
        id=int(r["id"]),
        full_name=r["full_name"],
        private=bool(r.get("private")),
        can_admin=bool(perms.get("admin")),
        pushed_at=r.get("pushed_at"),
        description=r.get("description"),
    )


async def list_repos(gh: GitHub, token: str) -> list[RepoInfo]:
    items = await gh.paginate(
        "/user/repos",
        token=token,
        params={"sort": "pushed", "affiliation": "owner,collaborator,organization_member"},
        max_pages=5,
        cache_ttl=120,
    )
    return [_repo(r) for r in items]


async def list_orgs(gh: GitHub, token: str) -> list[OrgInfo]:
    items = await gh.paginate("/user/orgs", token=token, max_pages=2, cache_ttl=300)
    return [OrgInfo(id=int(o["id"]), login=o["login"]) for o in items]


async def repo_by_id(gh: GitHub, token: str, repo_id: int) -> RepoInfo:
    return _repo(await gh.get(f"/repositories/{repo_id}", token=token, cache_ttl=60))


async def repo_by_name(gh: GitHub, token: str | None, full_name: str) -> RepoInfo:
    return _repo(await gh.get(f"/repos/{full_name}", token=token, cache_ttl=60))


async def org_by_id(gh: GitHub, token: str, org_id: int) -> OrgInfo:
    o = await gh.get(f"/organizations/{org_id}", token=token, cache_ttl=300)
    return OrgInfo(id=int(o["id"]), login=o["login"])


async def org_repos(gh: GitHub, token: str | None, org: str, limit_pages: int = 3) -> list[str]:
    items = await gh.paginate(
        f"/orgs/{org}/repos",
        token=token,
        params={"sort": "pushed", "type": "all"},
        max_pages=limit_pages,
        cache_ttl=600,
    )
    return [r["full_name"] for r in items if not r.get("archived")]


# ── webhooks ─────────────────────────────────────────────────────────────────


def _hook_body(url: str, secret: str, events: list[str]) -> dict[str, Any]:
    return {
        "name": "web",
        "active": True,
        "events": events,
        "config": {"url": url, "content_type": "json", "secret": secret, "insecure_ssl": "0"},
    }


async def create_hook(
    gh: GitHub, token: str, kind: str, target: str, url: str, secret: str, events: list[str]
) -> int:
    path = f"/repos/{target}/hooks" if kind == "repo" else f"/orgs/{target}/hooks"
    data = (await gh.request("POST", path, token=token, json=_hook_body(url, secret, events))).data
    return int(data["id"])


async def delete_hook(gh: GitHub, token: str, kind: str, target: str, hook_id: int) -> None:
    path = (
        f"/repos/{target}/hooks/{hook_id}" if kind == "repo" else f"/orgs/{target}/hooks/{hook_id}"
    )
    try:
        await gh.request("DELETE", path, token=token)
    except GitHubError as exc:
        if exc.status != 404:
            raise


async def ping_hook(gh: GitHub, token: str, kind: str, target: str, hook_id: int) -> None:
    path = (
        f"/repos/{target}/hooks/{hook_id}/pings"
        if kind == "repo"
        else f"/orgs/{target}/hooks/{hook_id}/pings"
    )
    await gh.request("POST", path, token=token)


# ── comments & bodies ────────────────────────────────────────────────────────


async def post_issue_comment(
    gh: GitHub, token: str, repo: str, number: int, body: str
) -> dict[str, Any]:
    resp = await gh.request(
        "POST", f"/repos/{repo}/issues/{number}/comments", token=token, json={"body": body}
    )
    return dict(resp.data)


async def reply_review_comment(
    gh: GitHub, token: str, repo: str, number: int, comment_id: int, body: str
) -> dict[str, Any]:
    resp = await gh.request(
        "POST",
        f"/repos/{repo}/pulls/{number}/comments/{comment_id}/replies",
        token=token,
        json={"body": body},
    )
    return dict(resp.data)


async def body_html(gh: GitHub, token: str | None, api_url: str) -> str | None:
    """Rendered HTML of an issue/PR/comment — includes signed URLs for private images."""
    try:
        data = await gh.get(api_url, token=token, accept=FULL_JSON, conditional=False)
    except GitHubError:
        return None
    return data.get("body_html") if isinstance(data, dict) else None


# ── actions & releases ───────────────────────────────────────────────────────


async def run_artifacts(
    gh: GitHub, token: str | None, repo: str, run_id: int
) -> list[dict[str, Any]]:
    data = await gh.get(
        f"/repos/{repo}/actions/runs/{run_id}/artifacts",
        token=token,
        params={"per_page": 100},
        conditional=False,
    )
    return [a for a in data.get("artifacts", []) if not a.get("expired")]
