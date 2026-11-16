"""GitHub webhook payload → Telegram message.

Pure functions: no I/O, no Telegram types. Each returns a :class:`Rendered`
(or ``None`` to stay silent). Visual language:

    🐛 issue  🔀 pull request  💬 comment  👀/✅/⛔ review  💭 review comment
    🔨 push  ⚙️/✅/❌ workflow  🚀 release  📦 artifact
    state badges: 🟢 open  🟣 merged  ✅ closed (done)  ⚪ draft / not planned  🔴 closed
"""

from __future__ import annotations

import fnmatch
import re
from datetime import datetime
from typing import Any

from app.formatting.html import code, esc, link
from app.formatting.images import extract_images
from app.formatting.markdown import MdContext, Segment, markdown_to_segments
from app.formatting.render import (
    CAPTION_LIMIT,
    Button,
    LinkMeta,
    Rendered,
    assemble,
    first_line,
    human_duration,
    human_size,
    repo_link,
    user_link,
)

P = dict[str, Any]

# ── shared helpers ────────────────────────────────────────────────────────────


def thread_key(repo: str, number: int | None) -> str | None:
    return f"{repo.lower()}#{number}" if number is not None else None


def _md(repo: P) -> MdContext:
    return MdContext(repo_url=str(repo.get("html_url") or ""))


def _item_title(url: str, number: int, title: str) -> str:
    return f"<b>{link(url, f'#{number} · {first_line(title, 120)}')}</b>"


def _open_comment_buttons(url: str, number: int | None, label: str = "Open") -> list[list[Button]]:
    row = [Button(f"↗ {label}", url=url, style="primary")]
    if number is not None:
        row.append(Button("💬 Comment", action="cm"))
    return [row]


def _labels(item: P, limit: int = 4) -> str:
    names = [str(lb.get("name")) for lb in item.get("labels") or [] if lb.get("name")]
    if not names:
        return ""
    more = f" +{len(names) - limit}" if len(names) > limit else ""
    return "🏷 " + " · ".join(esc(n) for n in names[:limit]) + more


def _with_body(
    header: list[str],
    body: str | None,
    repo: P,
    footer: list[str] | None = None,
) -> tuple[str, str, list[str]]:
    segments = markdown_to_segments(body, _md(repo))
    text = assemble(header, segments, footer)
    caption = assemble(header, segments, footer, limit=CAPTION_LIMIT)
    return text, caption, extract_images(body)


def issue_badge(issue: P) -> tuple[str, str]:
    state = issue.get("state")
    if state == "open":
        return "🟢", "Open"
    if issue.get("state_reason") == "not_planned":
        return "⚪", "Not planned"
    return "✅", "Closed"


def pr_badge(pr: P) -> tuple[str, str]:
    if pr.get("merged") or pr.get("merged_at"):
        return "🟣", "Merged"
    if pr.get("state") == "closed":
        return "🔴", "Closed"
    if pr.get("draft"):
        return "⚪", "Draft"
    return "🟢", "Open"


# ── issues ────────────────────────────────────────────────────────────────────

_ISSUE_ACTIONS = {"opened", "closed", "reopened"}


def render_issue(payload: P) -> Rendered | None:
    action = payload.get("action")
    if action not in _ISSUE_ACTIONS:
        return None
    issue, repo = payload["issue"], payload["repository"]
    badge, label = issue_badge(issue)
    by = ""
    if action in {"closed", "reopened"} and payload.get("sender"):
        by = f" by {user_link(payload['sender'])}"
    header = [
        f"🐛 <b>Issue</b> · {repo_link(repo)} · {badge} {label}{by}",
        _item_title(issue["html_url"], issue["number"], issue.get("title", "")),
        " · ".join(x for x in [f"👤 {user_link(issue.get('user'))}", _labels(issue)] if x),
    ]
    text, caption, images = _with_body(header, issue.get("body"), repo)
    return Rendered(
        text=text,
        caption=caption,
        images=images,
        image_api_url=issue.get("url"),
        buttons=_open_comment_buttons(issue["html_url"], issue["number"]),
        meta=LinkMeta(
            repo=repo["full_name"],
            kind="issue",
            number=issue["number"],
            thread_key=thread_key(repo["full_name"], issue["number"]),
            state=label.lower(),
            html_url=issue["html_url"],
        ),
        edit_thread=action != "opened",
    )


# ── pull requests ─────────────────────────────────────────────────────────────

_PR_NEW = {"opened", "ready_for_review"}
_PR_STATE = {"closed", "reopened", "converted_to_draft"}


def render_pull_request(payload: P) -> Rendered | None:
    action = payload.get("action")
    if action not in _PR_NEW | _PR_STATE:
        return None
    pr, repo = payload["pull_request"], payload["repository"]
    badge, label = pr_badge(pr)
    by = ""
    if label == "Merged" and pr.get("merged_by"):
        by = f" by {user_link(pr['merged_by'])}"
    elif action in _PR_STATE and payload.get("sender"):
        by = f" by {user_link(payload['sender'])}"
    head, base = pr.get("head") or {}, pr.get("base") or {}
    branches = f"🌿 {code(head.get('ref', '?'))} → {code(base.get('ref', '?'))}"
    stats = ""
    if pr.get("additions") is not None:
        files = pr.get("changed_files")
        stats = f"📊 <b>+{pr['additions']}</b> −{pr.get('deletions', 0)}" + (
            f" · {files} file{'s' if files != 1 else ''}" if files is not None else ""
        )
    header = [
        f"🔀 <b>Pull request</b> · {repo_link(repo)} · {badge} {label}{by}",
        _item_title(pr["html_url"], pr["number"], pr.get("title", "")),
        f"👤 {user_link(pr.get('user'))} · {branches}",
        " · ".join(x for x in [stats, _labels(pr)] if x),
    ]
    text, caption, images = _with_body(header, pr.get("body"), repo)
    return Rendered(
        text=text,
        caption=caption,
        images=images,
        image_api_url=pr.get("url"),
        buttons=_open_comment_buttons(pr["html_url"], pr["number"]),
        meta=LinkMeta(
            repo=repo["full_name"],
            kind="pr",
            number=pr["number"],
            thread_key=thread_key(repo["full_name"], pr["number"]),
            state=label.lower(),
            html_url=pr["html_url"],
        ),
        # "ready_for_review" is news when the draft was never announced, else an edit.
        edit_thread=action in _PR_STATE or action == "ready_for_review",
    )


# ── issue comments ────────────────────────────────────────────────────────────


def render_issue_comment(payload: P) -> Rendered | None:
    if payload.get("action") != "created":
        return None
    issue, comment, repo = payload["issue"], payload["comment"], payload["repository"]
    is_pr = "pull_request" in issue
    kind_label = "PR" if is_pr else "issue"
    header = [
        f"💬 <b>Comment</b> on {kind_label} · {repo_link(repo)}",
        _item_title(comment["html_url"], issue["number"], issue.get("title", "")),
        f"👤 {user_link(comment.get('user'))}",
    ]
    text, caption, images = _with_body(header, comment.get("body"), repo)
    return Rendered(
        text=text,
        caption=caption,
        images=images,
        image_api_url=comment.get("url"),
        buttons=_open_comment_buttons(comment["html_url"], issue["number"]),
        meta=LinkMeta(
            repo=repo["full_name"],
            kind="comment",
            number=issue["number"],
            thread_key=thread_key(repo["full_name"], issue["number"]),
            html_url=comment["html_url"],
        ),
    )


# ── reviews (review + its inline comments arrive as one batch) ────────────────

_REVIEW_STATES = {
    "approved": ("✅", "Approved"),
    "changes_requested": ("⛔", "Changes requested"),
    "commented": ("👀", "Reviewed"),
    "dismissed": ("↩️", "Review dismissed"),
}


def _review_comment_block(c: P, repo: P) -> tuple[str, list[Segment]]:
    path = c.get("path") or "file"
    line = c.get("line") or c.get("original_line")
    loc = f"{path}:{line}" if line else path
    head = f"📄 {link(c.get('html_url'), loc)}"
    return head, markdown_to_segments(c.get("body"), _md(repo))


def render_review_batch(review: P | None, comments: list[P], payload: P) -> Rendered | None:
    """``review`` from pull_request_review; ``comments`` from pull_request_review_comment."""
    pr, repo = payload["pull_request"], payload["repository"]
    if review is None and not comments:
        return None
    if review:
        state = str(review.get("state", "commented")).lower()
        # A bare "commented" review with no body is just the wrapper of inline comments.
        if state == "commented" and not (review.get("body") or "").strip() and not comments:
            return None
        emoji, label = _REVIEW_STATES.get(state, ("👀", "Reviewed"))
        author = review.get("user")
        url = review.get("html_url") or pr["html_url"]
    else:
        emoji, label = "💭", f"{len(comments)} review comment{'s' if len(comments) != 1 else ''}"
        author = comments[0].get("user")
        url = comments[0].get("html_url") or pr["html_url"]

    header = [
        f"{emoji} <b>{label}</b> · {repo_link(repo)}",
        _item_title(url, pr["number"], pr.get("title", "")),
        f"👤 {user_link(author)}"
        + (
            f" · {len(comments)} inline comment{'s' if len(comments) != 1 else ''}"
            if review and comments
            else ""
        ),
    ]
    segments: list[Segment] = []
    if review and (review.get("body") or "").strip():
        segments.extend(markdown_to_segments(review.get("body"), _md(repo)))
    for c in comments[:8]:
        head, segs = _review_comment_block(c, repo)
        if segs and segs[0].kind == "text":
            segs[0] = Segment("text", f"{head}\n{segs[0].html}")
        else:
            segs.insert(0, Segment("text", head))
        segments.extend(segs)
    if len(comments) > 8:
        segments.append(Segment("text", f"<i>… and {len(comments) - 8} more</i>"))
    text = assemble(header, segments)
    caption = assemble(header, segments, limit=CAPTION_LIMIT)
    images = extract_images((review or {}).get("body"))
    comment_id = comments[0].get("id") if len(comments) == 1 and not review else None
    return Rendered(
        text=text,
        caption=caption,
        images=images,
        buttons=_open_comment_buttons(url, pr["number"]),
        meta=LinkMeta(
            repo=repo["full_name"],
            kind="review",
            number=pr["number"],
            thread_key=thread_key(repo["full_name"], pr["number"]),
            html_url=url,
            comment_id=int(comment_id) if comment_id else None,
        ),
    )


# ── pushes ────────────────────────────────────────────────────────────────────

_MERGE_PR_RE = re.compile(r"^Merge pull request #(\d+) from (\S+)")
_SQUASH_PR_RE = re.compile(r"\(#(\d+)\)\s*$")
MAX_COMMIT_LINES = 15


def _commit_line(c: P, repo_url: str) -> str:
    sha = str(c.get("id") or c.get("sha") or "")[:7]
    msg = str(c.get("message") or "")
    title = first_line(msg, 64)
    m = _MERGE_PR_RE.match(msg)
    if m:
        pr_url = f"{repo_url}/pull/{m.group(1)}"
        title_html = f"🔀 Merge {link(pr_url, '#' + m.group(1))} from {esc(m.group(2))}"
    else:
        sq = _SQUASH_PR_RE.search(title)
        if sq:
            pr_url = f"{repo_url}/pull/{sq.group(1)}"
            title_html = esc(title[: sq.start()].rstrip()) + f" ({link(pr_url, '#' + sq.group(1))})"
        else:
            title_html = esc(title)
    author = c.get("author") or {}
    username = author.get("username")
    who = (
        link(f"https://github.com/{username}", username)
        if username
        else esc(author.get("name") or "?")
    )
    return f"{link(c.get('url'), sha)} {title_html} — <i>{who}</i>"


def render_push(payloads: list[P]) -> Rendered | None:
    """One message for one or more (debounced) pushes to the same ref."""
    if not payloads:
        return None
    last = payloads[-1]
    repo = last["repository"]
    ref = str(last.get("ref", ""))
    if ref.startswith("refs/tags/"):
        return None  # tags are covered by release events
    branch = ref.removeprefix("refs/heads/")
    repo_url = str(repo["html_url"])
    branch_url = f"{repo_url}/tree/{branch}"
    pusher = last.get("sender") or {}

    commits: list[P] = []
    seen: set[str] = set()
    for p in payloads:
        for c in p.get("commits") or []:
            if c.get("id") not in seen and c.get("distinct", True):
                seen.add(c.get("id"))
                commits.append(c)

    if last.get("deleted"):
        header = [
            f"🗑 <b>Branch deleted</b> · {repo_link(repo)}:{code(branch)}",
            f"👤 {user_link(pusher)}",
        ]
        return Rendered(
            text="\n".join(header),
            meta=LinkMeta(repo=repo["full_name"], kind="push"),
        )

    forced = any(p.get("forced") for p in payloads)
    created = payloads[0].get("created") and not commits
    if created:
        header = [
            f"🌱 <b>New branch</b> {link(branch_url, branch)} · {repo_link(repo)}",
            f"👤 {user_link(pusher)}",
        ]
        return Rendered(
            text="\n".join(header),
            buttons=[[Button("↗ Open branch", url=branch_url, style="primary")]],
            meta=LinkMeta(repo=repo["full_name"], kind="push", html_url=branch_url),
        )
    if not commits:
        return None

    n = len(commits)
    noun = "new commit" if n == 1 else "new commits"
    title = f"🔨 <b>{n} {noun}</b> to {repo_link(repo, short=True)}:{link(branch_url, branch)}"
    if forced:
        title += " · ⚠️ <b>force-pushed</b>"
    lines = [_commit_line(c, repo_url) for c in commits[-MAX_COMMIT_LINES:]]
    hidden = n - len(lines)
    if hidden > 0:
        lines.insert(0, f"<i>… {hidden} earlier commit{'s' if hidden != 1 else ''}</i>")
    authors = {(c.get("author") or {}).get("username") for c in commits}
    header = [title]
    if pusher.get("login") and authors != {pusher.get("login")}:
        header.append(f"👤 pushed by {user_link(pusher)}")

    body = [Segment("text", "\n".join(lines))]
    text = assemble(header, body, expandable=n > 6)
    compare = str(last.get("compare") or payloads[0].get("compare") or branch_url)
    if len(payloads) > 1:
        before = payloads[0].get("before")
        after = last.get("after")
        if before and after and not str(before).startswith("0000000"):
            compare = f"{repo_url}/compare/{str(before)[:12]}...{str(after)[:12]}"
    return Rendered(
        text=text,
        buttons=[[Button("🔍 Compare changes", url=compare, style="primary")]],
        meta=LinkMeta(repo=repo["full_name"], kind="push", html_url=compare),
    )


# ── workflow runs ─────────────────────────────────────────────────────────────

_CONCLUSIONS = {
    "success": ("✅", "passed"),
    "failure": ("❌", "failed"),
    "cancelled": ("⚪", "cancelled"),
    "timed_out": ("⏱", "timed out"),
    "action_required": ("⚠️", "needs approval"),
    "startup_failure": ("💥", "failed to start"),
    "neutral": ("⚪", "finished"),
    "stale": ("⚪", "stale"),
}


def _duration(run: P) -> str | None:
    try:
        start = datetime.fromisoformat(str(run["run_started_at"]).replace("Z", "+00:00"))
        end = datetime.fromisoformat(str(run["updated_at"]).replace("Z", "+00:00"))
    except (KeyError, ValueError):
        return None
    return human_duration((end - start).total_seconds())


def render_workflow_run(payload: P, apk_link: str | None = None) -> Rendered | None:
    if payload.get("action") != "completed":
        return None
    run, repo = payload["workflow_run"], payload["repository"]
    conclusion = str(run.get("conclusion") or "neutral")
    if conclusion == "skipped":
        return None
    emoji, verb = _CONCLUSIONS.get(conclusion, ("⚪", conclusion))
    name = run.get("name") or (payload.get("workflow") or {}).get("name") or "Workflow"
    repo_url = str(repo["html_url"])
    branch = run.get("head_branch") or "?"
    sha = str(run.get("head_sha") or "")
    commit = run.get("head_commit") or {}
    prs = run.get("pull_requests") or []
    if prs:
        pr_num = prs[0].get("number")
        where = f"🔀 {link(f'{repo_url}/pull/{pr_num}', f'PR #{pr_num}')} · 🌿 {code(branch)}"
    else:
        where = f"🌿 {link(f'{repo_url}/tree/{branch}', branch)}"
    commit_line = (
        f"{link(f'{repo_url}/commit/{sha}', sha[:7])} {esc(first_line(commit.get('message'), 60))}"
    )
    meta_bits = [f"#{run.get('run_number', '?')}"]
    if dur := _duration(run):
        meta_bits.insert(0, f"⏱ {dur}")
    if run.get("event"):
        meta_bits.append(esc(run["event"]))
    actor = run.get("triggering_actor") or run.get("actor")
    header = [
        f"{emoji} <b>{esc(name)} {verb}</b> · {repo_link(repo)}",
        f"{where} · {commit_line}",
        " · ".join(meta_bits) + (f" · 👤 {user_link(actor)}" if actor else ""),
    ]
    row = [
        Button(
            "↗ Open run",
            url=run["html_url"],
            style="primary" if conclusion == "success" else "danger",
        )
    ]
    if apk_link:
        row.append(Button("⬇️ Download APK", url=apk_link, style="success"))
    return Rendered(
        text="\n".join(header),
        buttons=[row],
        meta=LinkMeta(
            repo=repo["full_name"],
            kind="workflow",
            html_url=run["html_url"],
            state=conclusion,
            thread_key=f"run:{run.get('id')}",
        ),
    )


# ── releases ──────────────────────────────────────────────────────────────────


def matching_assets(release: P, pattern: str) -> list[P]:
    pats = [p.strip() for p in pattern.split(",") if p.strip()]
    return [
        a
        for a in release.get("assets") or []
        if any(fnmatch.fnmatch(str(a.get("name", "")).lower(), p.lower()) for p in pats)
    ]


def render_release(payload: P, pattern: str = "*.apk") -> Rendered | None:
    if payload.get("action") not in {"published", "released"}:
        return None
    if payload.get("action") == "released" and not payload["release"].get("prerelease"):
        # GitHub sends both "published" and "released" for normal releases.
        return None
    rel, repo = payload["release"], payload["repository"]
    tag = rel.get("tag_name") or "?"
    name = rel.get("name") or tag
    flags = []
    if rel.get("prerelease"):
        flags.append("🧪 pre-release")
    header = [
        f"🚀 <b>Release</b> · {repo_link(repo)}",
        f"<b>{link(rel['html_url'], name)}</b>",
        " · ".join([f"🏷 {code(tag)}", f"👤 {user_link(rel.get('author'))}", *flags]),
    ]
    assets = rel.get("assets") or []
    footer = []
    if assets:
        shown = ", ".join(
            f"{link(a.get('browser_download_url'), a.get('name'))} ({human_size(int(a.get('size') or 0))})"
            for a in assets[:5]
        )
        more = f" +{len(assets) - 5} more" if len(assets) > 5 else ""
        footer.append(f"📦 {shown}{more}")
    text, caption, images = _with_body(header, rel.get("body"), repo, footer)
    row = [Button("↗ Open release", url=rel["html_url"], style="primary")]
    apks = matching_assets(rel, pattern)
    if apks:
        row.append(Button("⬇️ Download APK", url=apks[0]["browser_download_url"], style="success"))
    return Rendered(
        text=text,
        caption=caption,
        images=images,
        image_api_url=rel.get("url"),
        buttons=[row],
        meta=LinkMeta(repo=repo["full_name"], kind="release", html_url=rel["html_url"]),
    )


# ── artifacts (document caption) ──────────────────────────────────────────────


def artifact_caption(
    repo: P, filename: str, size: int, run: P | None = None, release: P | None = None
) -> str:
    repo_url = str(repo["html_url"])
    lines = [f"📦 <b>{esc(filename)}</b> · {human_size(size)}", repo_link(repo)]
    if run:
        sha = str(run.get("head_sha") or "")
        prs = run.get("pull_requests") or []
        where = (
            link(f"{repo_url}/pull/{prs[0]['number']}", f"PR #{prs[0]['number']}")
            if prs
            else link(f"{repo_url}/tree/{run.get('head_branch')}", run.get("head_branch") or "?")
        )
        lines.append(
            f"🌿 {where} · {link(f'{repo_url}/commit/{sha}', sha[:7])} · "
            f"{link(run.get('html_url'), 'run #' + str(run.get('run_number', '?')))}"
        )
    if release:
        lines.append(
            f"🚀 {link(release.get('html_url'), release.get('name') or release.get('tag_name'))}"
        )
    return "\n".join(lines)


# ── dispatch ──────────────────────────────────────────────────────────────────

SIMPLE_RENDERERS = {
    "issues": render_issue,
    "pull_request": render_pull_request,
    "issue_comment": render_issue_comment,
}
