import pytest

from app.domain import ALL_EVENTS, EventFlag, branch_allowed, event_enabled, parse_branch_filter


@pytest.mark.parametrize(
    ("flt", "branch", "ok"),
    [
        (None, "main", True),
        ("", "feature/x", True),
        ("*", "anything", True),
        ("main", "main", True),
        ("main", "dev", False),
        ("main, release/*", "release/1.2", True),
        ("release/*", "release", False),
        ("!dependabot/*", "dependabot/npm/foo", False),
        ("!dependabot/*", "main", True),
        ("main,!main", "main", False),
    ],
)
def test_branch_allowed(flt: str | None, branch: str, ok: bool) -> None:
    assert branch_allowed(flt, branch) is ok


def test_parse_branch_filter_trims() -> None:
    assert parse_branch_filter(" main ,\nrelease/* ,, all") == ["main", "release/*"]


def test_event_enabled() -> None:
    assert event_enabled(ALL_EVENTS, "push")
    assert event_enabled(ALL_EVENTS, "pull_request_review_comment")
    assert not event_enabled(ALL_EVENTS & ~EventFlag.PUSHES, "push")
    assert not event_enabled(ALL_EVENTS, "star")
