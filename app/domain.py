"""Domain constants shared across layers: event toggles, branch filters."""

from __future__ import annotations

import fnmatch
from dataclasses import dataclass
from enum import IntFlag


class EventFlag(IntFlag):
    ISSUES = 1 << 0
    ISSUE_COMMENTS = 1 << 1
    PULL_REQUESTS = 1 << 2
    REVIEWS = 1 << 3
    PUSHES = 1 << 4
    WORKFLOWS = 1 << 5
    RELEASES = 1 << 6
    ARTIFACTS = 1 << 7


ALL_EVENTS = int(
    EventFlag.ISSUES
    | EventFlag.ISSUE_COMMENTS
    | EventFlag.PULL_REQUESTS
    | EventFlag.REVIEWS
    | EventFlag.PUSHES
    | EventFlag.WORKFLOWS
    | EventFlag.RELEASES
    | EventFlag.ARTIFACTS
)


@dataclass(frozen=True, slots=True)
class EventToggle:
    flag: EventFlag
    emoji: str
    label: str


# Order = order of the toggle grid in the settings panel.
EVENT_TOGGLES: tuple[EventToggle, ...] = (
    EventToggle(EventFlag.ISSUES, "🐛", "Issues"),
    EventToggle(EventFlag.ISSUE_COMMENTS, "💬", "Comments"),
    EventToggle(EventFlag.PULL_REQUESTS, "🔀", "Pull requests"),
    EventToggle(EventFlag.REVIEWS, "👀", "Reviews"),
    EventToggle(EventFlag.PUSHES, "🔨", "Pushes"),
    EventToggle(EventFlag.WORKFLOWS, "⚙️", "Workflows"),
    EventToggle(EventFlag.RELEASES, "🚀", "Releases"),
    EventToggle(EventFlag.ARTIFACTS, "📦", "APK uploads"),
)

# GitHub webhook events we subscribe to (filtering happens on our side, so
# toggling a switch never needs a GitHub API call).
GITHUB_EVENTS: list[str] = [
    "issues",
    "issue_comment",
    "pull_request",
    "pull_request_review",
    "pull_request_review_comment",
    "push",
    "workflow_run",
    "release",
]

EVENT_TO_FLAG: dict[str, EventFlag] = {
    "issues": EventFlag.ISSUES,
    "issue_comment": EventFlag.ISSUE_COMMENTS,
    "pull_request": EventFlag.PULL_REQUESTS,
    "pull_request_review": EventFlag.REVIEWS,
    "pull_request_review_comment": EventFlag.REVIEWS,
    "push": EventFlag.PUSHES,
    "workflow_run": EventFlag.WORKFLOWS,
    "release": EventFlag.RELEASES,
}


def event_enabled(mask: int, event: str) -> bool:
    flag = EVENT_TO_FLAG.get(event)
    return flag is not None and bool(mask & flag)


def parse_branch_filter(raw: str | None) -> list[str]:
    if not raw:
        return []
    parts = [p.strip() for p in raw.replace("\n", ",").split(",")]
    return [p for p in parts if p and p not in {"*", "all"}]


def branch_allowed(raw_filter: str | None, branch: str) -> bool:
    """Glob match (``main``, ``release/*``); ``!pattern`` excludes."""
    patterns = parse_branch_filter(raw_filter)
    if not patterns:
        return True
    includes = [p for p in patterns if not p.startswith("!")]
    excludes = [p[1:] for p in patterns if p.startswith("!")]
    if any(fnmatch.fnmatchcase(branch, p) for p in excludes):
        return False
    return not includes or any(fnmatch.fnmatchcase(branch, p) for p in includes)
