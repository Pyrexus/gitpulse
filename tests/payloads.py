"""Minimal but realistic GitHub webhook payloads for formatter tests."""

from typing import Any

REPO: dict[str, Any] = {
    "id": 1,
    "full_name": "lastwave/LastWave-Native",
    "name": "LastWave-Native",
    "html_url": "https://github.com/lastwave/LastWave-Native",
}
ALICE = {"login": "alice", "html_url": "https://github.com/alice"}
BOB = {"login": "bob", "html_url": "https://github.com/bob"}
BOT = {"login": "dependabot[bot]", "html_url": "https://github.com/apps/dependabot"}


def issue(**kw: Any) -> dict[str, Any]:
    base = {
        "number": 42,
        "title": "App crashes on <launch>",
        "html_url": "https://github.com/lastwave/LastWave-Native/issues/42",
        "url": "https://api.github.com/repos/lastwave/LastWave-Native/issues/42",
        "state": "open",
        "user": ALICE,
        "labels": [{"name": "bug"}, {"name": "android"}],
        "body": "### Steps\n1. Open\n2. Crash\n\n### Logs\n\n_No response_\n\n![s](https://x.io/s.png)",
    }
    base.update(kw)
    return base


def pr(**kw: Any) -> dict[str, Any]:
    base = {
        "number": 12,
        "title": "Add offline mode",
        "html_url": "https://github.com/lastwave/LastWave-Native/pull/12",
        "url": "https://api.github.com/repos/lastwave/LastWave-Native/pulls/12",
        "state": "open",
        "draft": False,
        "merged": False,
        "user": ALICE,
        "head": {"ref": "feat/offline"},
        "base": {"ref": "main"},
        "additions": 120,
        "deletions": 30,
        "changed_files": 5,
        "labels": [],
        "body": "Implements **offline** mode. Closes #42",
    }
    base.update(kw)
    return base


def commit(sha: str, message: str, username: str | None = "alice") -> dict[str, Any]:
    return {
        "id": sha,
        "message": message,
        "url": f"https://github.com/lastwave/LastWave-Native/commit/{sha}",
        "author": {"name": (username or "Ghost").title(), "username": username},
        "distinct": True,
    }


def push(commits: list[dict[str, Any]], **kw: Any) -> dict[str, Any]:
    base = {
        "ref": "refs/heads/main",
        "before": "a" * 40,
        "after": "b" * 40,
        "created": False,
        "deleted": False,
        "forced": False,
        "compare": "https://github.com/lastwave/LastWave-Native/compare/aaa...bbb",
        "commits": commits,
        "repository": REPO,
        "sender": ALICE,
    }
    base.update(kw)
    return base


def workflow_run(conclusion: str = "success", **kw: Any) -> dict[str, Any]:
    run = {
        "id": 9001,
        "name": "Android CI",
        "run_number": 142,
        "html_url": "https://github.com/lastwave/LastWave-Native/actions/runs/9001",
        "conclusion": conclusion,
        "head_branch": "main",
        "head_sha": "c0ffee1234567890",
        "head_commit": {"message": "Bump version\n\nbody"},
        "run_started_at": "2026-11-16T10:00:00Z",
        "updated_at": "2026-11-16T10:03:12Z",
        "event": "push",
        "pull_requests": [],
        "actor": ALICE,
    }
    run.update(kw)
    return {"action": "completed", "workflow_run": run, "repository": REPO}
