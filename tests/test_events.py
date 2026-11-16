from app.formatting.events import (
    artifact_caption,
    matching_assets,
    render_issue,
    render_issue_comment,
    render_pull_request,
    render_push,
    render_release,
    render_review_batch,
    render_workflow_run,
    thread_key,
)
from app.formatting.html import sanitize, visible_len
from app.formatting.render import CAPTION_LIMIT, TEXT_LIMIT, Rendered, human_duration, human_size
from tests.payloads import ALICE, BOB, BOT, REPO, commit, issue, pr, push, workflow_run


def valid(r: Rendered | None) -> Rendered:
    assert r is not None
    assert sanitize(r.text) == r.text, "output must be sanitizer-stable"
    assert visible_len(r.text) <= TEXT_LIMIT
    if r.caption:
        assert visible_len(r.caption) <= CAPTION_LIMIT
    assert sum(len(row) for row in r.buttons) <= 3
    return r


# ── issues ────────────────────────────────────────────────────────────────────


def test_issue_opened() -> None:
    r = valid(render_issue({"action": "opened", "issue": issue(), "repository": REPO}))
    assert r.text.startswith("🐛 <b>Issue</b>")
    assert "🟢 Open" in r.text
    assert "#42 · App crashes on &lt;launch&gt;" in r.text  # title escaped
    assert "🏷 bug · android" in r.text
    assert "<blockquote>" in r.text
    assert "No response" not in r.text and "Logs" not in r.text
    assert r.images == ["https://x.io/s.png"]
    assert r.meta.thread_key == "lastwave/lastwave-native#42"
    assert r.meta.kind == "issue" and not r.edit_thread
    assert [b.text for b in r.buttons[0]] == ["↗ Open", "💬 Comment"]
    assert r.buttons[0][0].style == "primary"


def test_issue_closed_is_an_edit() -> None:
    p = {
        "action": "closed",
        "issue": issue(state="closed", state_reason="completed"),
        "repository": REPO,
        "sender": BOB,
    }
    r = valid(render_issue(p))
    assert r.edit_thread
    assert "✅ Closed by" in r.text
    np = {**p, "issue": issue(state="closed", state_reason="not_planned")}
    assert "⚪ Not planned" in valid(render_issue(np)).text


def test_issue_ignored_actions() -> None:
    assert render_issue({"action": "labeled", "issue": issue(), "repository": REPO}) is None


def test_long_issue_body_is_expandable_and_fits() -> None:
    body = "\n\n".join(f"Paragraph {i} " + "word " * 80 for i in range(40))
    r = valid(render_issue({"action": "opened", "issue": issue(body=body), "repository": REPO}))
    assert "<blockquote expandable>" in r.text
    assert visible_len(r.text) > 3000  # uses the space it has
    assert r.caption and "…" in r.caption


def test_code_block_outside_quote() -> None:
    body = "Steps:\n\n```kotlin\nval x = 1\n```\n\nThanks"
    r = valid(render_issue({"action": "opened", "issue": issue(body=body), "repository": REPO}))
    assert (
        '</blockquote>\n<pre><code class="language-kotlin">val x = 1</code></pre>\n<blockquote>'
        in r.text
    )


def test_empty_body() -> None:
    r = valid(render_issue({"action": "opened", "issue": issue(body=None), "repository": REPO}))
    assert "blockquote" not in r.text
    assert r.images == []


# ── pull requests ─────────────────────────────────────────────────────────────


def test_pr_opened() -> None:
    r = valid(render_pull_request({"action": "opened", "pull_request": pr(), "repository": REPO}))
    assert "🔀 <b>Pull request</b>" in r.text and "🟢 Open" in r.text
    assert "<code>feat/offline</code> → <code>main</code>" in r.text
    assert "📊 <b>+120</b> −30 · 5 files" in r.text
    assert r.meta.kind == "pr" and r.meta.state == "open"
    assert not r.edit_thread


def test_pr_merged_updates_badge() -> None:
    p = {
        "action": "closed",
        "pull_request": pr(state="closed", merged=True, merged_by=BOB),
        "repository": REPO,
        "sender": BOB,
    }
    r = valid(render_pull_request(p))
    assert r.edit_thread
    assert "🟣 Merged by" in r.text and "bob" in r.text
    assert r.meta.state == "merged"


def test_pr_closed_unmerged_and_draft() -> None:
    closed = render_pull_request(
        {"action": "closed", "pull_request": pr(state="closed"), "repository": REPO}
    )
    assert "🔴 Closed" in valid(closed).text
    draft = render_pull_request(
        {"action": "opened", "pull_request": pr(draft=True), "repository": REPO}
    )
    assert "⚪ Draft" in valid(draft).text


def test_pr_by_bot() -> None:
    r = valid(
        render_pull_request({"action": "opened", "pull_request": pr(user=BOT), "repository": REPO})
    )
    assert "dependabot</a> 🤖" in r.text


# ── comments & reviews ────────────────────────────────────────────────────────


def test_issue_comment_on_pr() -> None:
    p = {
        "action": "created",
        "issue": issue(pull_request={}),
        "comment": {
            "id": 5,
            "body": "Looks good @bob <img src='https://x.io/c.png'>",
            "user": BOB,
            "html_url": "https://github.com/x/y/pull/42#issuecomment-5",
            "url": "https://api.github.com/x",
        },
        "repository": REPO,
    }
    r = valid(render_issue_comment(p))
    assert r.text.startswith("💬 <b>Comment</b> on PR")
    assert r.images == ["https://x.io/c.png"]
    assert r.meta.number == 42 and r.meta.kind == "comment"
    assert render_issue_comment({**p, "action": "deleted"}) is None


def test_review_with_inline_comments() -> None:
    review = {"state": "changes_requested", "body": "Please fix", "user": BOB, "html_url": "u"}
    comments = [
        {
            "path": f"src/f{i}.kt",
            "line": i + 1,
            "body": f"c{i}",
            "html_url": "u",
            "user": BOB,
            "id": i,
        }
        for i in range(10)
    ]
    r = valid(render_review_batch(review, comments, {"pull_request": pr(), "repository": REPO}))
    assert "⛔ <b>Changes requested</b>" in r.text
    assert "10 inline comments" in r.text
    assert "📄 src/f0.kt:1\nc0" in r.text and "… and 2 more" in r.text
    assert r.meta.comment_id is None


def test_empty_commented_review_is_silent() -> None:
    review = {"state": "commented", "body": "", "user": BOB}
    assert render_review_batch(review, [], {"pull_request": pr(), "repository": REPO}) is None


def test_lone_review_comment_keeps_comment_id() -> None:
    c = {"path": "a.kt", "line": 3, "body": "hm", "html_url": "u", "user": BOB, "id": 77}
    r = valid(render_review_batch(None, [c], {"pull_request": pr(), "repository": REPO}))
    assert r.text.startswith("💭 <b>1 review comment</b>")
    assert r.meta.comment_id == 77


# ── pushes ────────────────────────────────────────────────────────────────────


def test_push_grouped_format() -> None:
    commits = [
        commit("abc1234aaaa", "Fix crash on start\n\nlong body"),
        commit("def5678bbbb", "Merge pull request #12 from alice/feat\n\nx", "bob"),
        commit("1234567cccc", "Tweak padding (#7)", None),
    ]
    r = valid(render_push([push(commits)]))
    lines = r.text.split("\n")
    assert lines[0].startswith("🔨 <b>3 new commits</b> to ")
    assert ">LastWave-Native</a>:<a " in lines[0] and ">main</a>" in lines[0]
    assert ">abc1234</a> Fix crash on start — <i>" in r.text
    assert (
        '🔀 Merge <a href="https://github.com/lastwave/LastWave-Native/pull/12">#12</a>' in r.text
    )
    assert '(<a href="https://github.com/lastwave/LastWave-Native/pull/7">#7</a>)' in r.text
    assert "<i>Ghost</i>" in r.text
    assert "long body" not in r.text
    assert "pushed by" in r.text  # bob authored one commit
    assert r.buttons[0][0].text == "🔍 Compare changes"


def test_push_single_commit_and_same_author() -> None:
    r = valid(render_push([push([commit("abc1234", "one")])]))
    assert "<b>1 new commit</b>" in r.text
    assert "pushed by" not in r.text


def test_push_debounced_batch_merges_and_dedupes() -> None:
    p1 = push([commit("a1", "first")], before="1" * 40, after="2" * 40)
    p2 = push([commit("a1", "first"), commit("a2", "second")], before="2" * 40, after="3" * 40)
    r = valid(render_push([p1, p2]))
    assert "<b>2 new commits</b>" in r.text
    assert r.meta.html_url and "/compare/111111111111...333333333333" in r.meta.html_url


def test_push_many_commits_clipped_and_expandable() -> None:
    commits = [commit(f"{i:07d}abc", f"commit {i}") for i in range(30)]
    r = valid(render_push([push(commits)]))
    assert "<b>30 new commits</b>" in r.text
    assert "15 earlier commits" in r.text
    assert "<blockquote expandable>" in r.text


def test_push_force_branch_created_deleted_tags() -> None:
    assert "force-pushed" in valid(render_push([push([commit("a", "x")], forced=True)])).text
    created = valid(render_push([push([], created=True, ref="refs/heads/feat/x")]))
    assert created.text.startswith("🌱 <b>New branch</b>")
    deleted = valid(render_push([push([], deleted=True)]))
    assert deleted.text.startswith("🗑 <b>Branch deleted</b>")
    assert render_push([push([commit("a", "x")], ref="refs/tags/v1")]) is None
    assert render_push([push([])]) is None


# ── workflows, releases, artifacts ────────────────────────────────────────────


def test_workflow_success_with_apk() -> None:
    r = valid(render_workflow_run(workflow_run(), apk_link="https://x.io/app.apk"))
    assert r.text.startswith("✅ <b>Android CI passed</b>")
    assert "⏱ 3m 12s" in r.text and "#142" in r.text and ">c0ffee1</a> Bump version" in r.text
    assert [b.text for b in r.buttons[0]] == ["↗ Open run", "⬇️ Download APK"]


def test_workflow_failure_on_pr() -> None:
    r = valid(
        render_workflow_run(
            workflow_run("failure", pull_requests=[{"number": 12}], event="pull_request")
        )
    )
    assert r.text.startswith("❌ <b>Android CI failed</b>")
    assert "PR #12" in r.text
    assert r.buttons[0][0].style == "danger"


def test_workflow_ignored() -> None:
    assert render_workflow_run(workflow_run("skipped")) is None
    assert render_workflow_run({**workflow_run(), "action": "requested"}) is None


def _release(**kw: object) -> dict[str, object]:
    rel = {
        "tag_name": "v1.2.0",
        "name": "Wave 1.2",
        "html_url": "https://github.com/x/y/releases/v1.2.0",
        "author": ALICE,
        "prerelease": False,
        "body": "## What's new\n- Offline mode",
        "assets": [
            {
                "name": "app-release.apk",
                "size": 12_900_000,
                "browser_download_url": "https://dl/app.apk",
            },
            {"name": "mapping.txt", "size": 1000, "browser_download_url": "https://dl/m.txt"},
        ],
    }
    rel.update(kw)
    return rel


def test_release() -> None:
    r = valid(render_release({"action": "published", "release": _release(), "repository": REPO}))
    assert r.text.startswith("🚀 <b>Release</b>")
    assert "<code>v1.2.0</code>" in r.text
    assert "app-release.apk</a> (12.3 MB)" in r.text
    assert r.buttons[0][1].url == "https://dl/app.apk"
    # normal release: only "published" is announced
    assert render_release({"action": "released", "release": _release(), "repository": REPO}) is None


def test_matching_assets_pattern() -> None:
    rel = _release()
    assert [a["name"] for a in matching_assets(rel, "*.apk")] == ["app-release.apk"]
    assert len(matching_assets(rel, "*.apk, *.txt")) == 2
    assert matching_assets(rel, "*.aab") == []


def test_artifact_caption() -> None:
    run = workflow_run()["workflow_run"]
    cap = artifact_caption(REPO, "app.apk", 5_000_000, run=run)
    assert cap.startswith("📦 <b>app.apk</b> · 4.8 MB")
    assert "run #142" in cap
    assert sanitize(cap) == cap


def test_helpers() -> None:
    assert thread_key("A/B", 3) == "a/b#3"
    assert thread_key("a/b", None) is None
    assert human_size(512) == "512 B"
    assert human_size(1536) == "1.5 KB"
    assert human_duration(59) == "59s"
    assert human_duration(192) == "3m 12s"
    assert human_duration(7300) == "2h 01m"
