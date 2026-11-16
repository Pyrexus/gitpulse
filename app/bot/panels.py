"""Panel views: (html, keyboard) builders for the self-editing control panel."""

from __future__ import annotations

import math
from collections.abc import Sequence
from datetime import datetime

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from app import __version__
from app.bot.keyboards import A, Keys
from app.db.models import Integration, User
from app.domain import EVENT_TOGGLES, parse_branch_filter
from app.formatting.html import code, esc, link
from app.github.api import OrgInfo, RepoInfo

Panel = tuple[str, InlineKeyboardMarkup]
PAGE_SIZE = 8

TOKEN_URL = (
    "https://github.com/settings/tokens/new?description=GitPulse"
    "&scopes=repo,admin:repo_hook,admin:org_hook,read:org"
)
FINE_GRAINED_URL = "https://github.com/settings/personal-access-tokens/new"


def ago(dt: datetime | None, fallback: str = "never") -> str:
    """Relative time rendered by the client in the reader's locale."""
    if dt is None:
        return esc(fallback)
    ts = int(dt.timestamp())
    return f'<tg-time unix="{ts}" format="r">{dt:%Y-%m-%d %H:%M} UTC</tg-time>'


def _gh_user(user: User) -> str:
    login = user.github_login or "?"
    return link(f"https://github.com/{login}", f"@{login}")


def _health(integ: Integration) -> tuple[str, str]:
    if not integ.active:
        return "🔴", "paused"
    if integ.last_event_at is None:
        return "🟡", "waiting for first event"
    return "🟢", f"last event {ago(integ.last_event_at)}"


def _target_link(integ: Integration) -> str:
    return link(f"https://github.com/{integ.target}", integ.target)


def _enabled_count(integ: Integration) -> int:
    return sum(1 for t in EVENT_TOGGLES if integ.events & t.flag)


# ── home ──────────────────────────────────────────────────────────────────────


def home(
    keys: Keys,
    user: User,
    chat_title: str | None,
    is_private: bool,
    integrations: Sequence[Integration],
    oauth_enabled: bool,
    login_deep_link: str,
    banner: str | None = None,
    admin_status: str | None = None,
) -> Panel:
    lines = []
    if banner:
        lines += [banner, ""]
    lines += ["⚡ <b>GitPulse</b>", "<i>GitHub, live in your chat.</i>", ""]
    if user.logged_in:
        name = f" · {esc(user.github_name)}" if user.github_name else ""
        lines.append(f"👤 Signed in as {_gh_user(user)}{name}")
    else:
        lines.append("🔒 <b>Not signed in</b> — connect GitHub to add repos and reply to comments.")
    if not is_private and chat_title:
        lines.append(f"📍 Chat: <b>{esc(chat_title)}</b>")
    n = len(integrations)
    if n:
        healthy = sum(1 for i in integrations if i.active and i.last_event_at)
        status = "🟢 all healthy" if healthy == n else f"🟢 {healthy} · 🟡 {n - healthy}"
        lines.append(f"📡 <b>{n} integration{'s' if n != 1 else ''}</b> here · {status}")
    else:
        lines.append("📡 No integrations here yet.")
    lines += ["", "<i>Reply to any event message to comment on GitHub.</i>"]
    if admin_status:
        lines += ["", admin_status]

    rows: list[list[InlineKeyboardButton]] = [
        [keys.cb("➕ Add integration", A.ADD, 0, "r", style="primary")],
        [keys.cb(f"📋 Integrations ({n})", A.LIST), keys.cb("ℹ️ Help", A.HELP)],
    ]
    if user.logged_in:
        rows.append([keys.cb("🚪 Logout", A.LOGOUT, style="danger")])
    elif is_private:
        login_row = [keys.cb("🔑 Login with PAT", A.LOGIN, style="success")]
        if oauth_enabled:
            login_row.append(keys.cb("🐙 Login with GitHub", A.LOGIN_OAUTH))
        rows.append(login_row)
    else:
        rows.append([keys.url("🔑 Login (private chat)", login_deep_link, style="success")])
    return "\n".join(lines), InlineKeyboardMarkup(inline_keyboard=rows)


def help_panel(keys: Keys) -> Panel:
    text = "\n".join(
        [
            "ℹ️ <b>How GitPulse works</b>",
            "",
            "<b>1 · Connect</b> — 🔑 log in with a GitHub token. It's deleted from the chat "
            "instantly and stored encrypted.",
            "<b>2 · Add</b> — ➕ pick a repo or a whole org; I create the webhook for you.",
            "<b>3 · Relax</b> — issues, PRs, reviews, pushes, CI runs, releases and APKs "
            "arrive here, beautifully formatted.",
            "",
            "💬 <b>Reply</b> to any event to comment on GitHub as yourself.",
            "📊 <code>/stats username</code> — contribution card for this chat's repos.",
            "⚙️ Event toggles, branch filters and quiet mode live under 📋 Integrations.",
            "",
            "<blockquote expandable><b>Groups &amp; channels</b>\n"
            "Add me to a group and send /start there. Only admins and the integration owner "
            "can change settings. For a channel, add the integration from our private chat "
            "and choose 📢 <b>A channel</b> — I need to be an admin there.\n\n"
            "<b>Branch filters</b>\n"
            "Comma-separated globs: <code>main, release/*</code>. Prefix with <code>!</code> "
            "to exclude: <code>!dependabot/*</code>.\n\n"
            "<b>Privacy</b>\n"
            "Tokens are AES-256-GCM encrypted at rest, never logged, and only used for actions "
            "you trigger. Webhooks are verified with a unique HMAC secret each.</blockquote>",
            "",
            f"<i>GitPulse v{__version__}</i>",
        ]
    )
    return text, keys.markup([keys.back()])


def expired(keys: Keys) -> Panel:
    return (
        "⌛ <b>This panel expired.</b>\nTap below to open a fresh one.",
        keys.markup([keys.cb("🏠 Open home", A.HOME, style="primary")]),
    )


def notice(keys: Keys, text: str, back: str = A.HOME, *args: object) -> Panel:
    return text, keys.markup([keys.back(back, *args)])


def need_login(keys: Keys, private: bool, deep_link: str) -> Panel:
    button = (
        keys.cb("🔑 Login with PAT", A.LOGIN, style="success")
        if private
        else keys.url("🔑 Login (private chat)", deep_link, style="success")
    )
    return (
        "🔒 <b>Log in first</b>\n\nAdding integrations uses <b>your</b> GitHub token to create "
        "the webhook — it takes ten seconds.",
        keys.markup([button], [keys.back()]),
    )


def loading(text: str = "⏳ <i>Loading…</i>") -> Panel:
    return text, InlineKeyboardMarkup(inline_keyboard=[])


# ── login ─────────────────────────────────────────────────────────────────────


def login_prompt(keys: Keys, error: str | None = None) -> Panel:
    lines = []
    if error:
        lines += [f"❌ {error}", ""]
    lines += [
        "🔑 <b>Connect GitHub</b>",
        "",
        "Send me a <b>personal access token</b> as your next message. "
        "I'll delete it immediately and keep it encrypted.",
        "",
        "<b>Scopes</b> (classic token)",
        f"• {code('repo')} — read repos, post your comments",
        f"• {code('admin:repo_hook')} — create webhooks",
        f"• {code('read:org')} — list your organizations",
        f"• {code('admin:org_hook')} — org-wide webhooks <i>(optional)</i>",
        "",
        "<blockquote expandable><b>Fine-grained token?</b> Works too. Grant repository "
        "permissions: <b>Webhooks</b> (read &amp; write), <b>Issues</b> and <b>Pull requests</b> "
        "(read &amp; write), <b>Contents</b>, <b>Actions</b> and <b>Metadata</b> (read).</blockquote>",
    ]
    return "\n".join(lines), keys.markup(
        [keys.url("🔗 Create classic token", TOKEN_URL, style="primary")],
        [keys.url("🧩 Fine-grained token", FINE_GRAINED_URL)],
        [keys.cb("✖️ Cancel", A.CANCEL)],
    )


def device_prompt(keys: Keys, user_code: str, verification_uri: str) -> Panel:
    text = "\n".join(
        [
            "🐙 <b>Login with GitHub</b>",
            "",
            f"1 · Open {link(verification_uri, verification_uri.removeprefix('https://'))}",
            f"2 · Enter the code <code>{esc(user_code)}</code>",
            "",
            "⏳ <i>Waiting for you to approve…</i>",
        ]
    )
    return text, keys.markup(
        [keys.copy(f"📋 Copy {user_code}", user_code), keys.url("🔗 Open GitHub", verification_uri, style="primary")],
        [keys.cb("✖️ Cancel", A.CANCEL)],
    )


def logout_confirm(keys: Keys, user: User) -> Panel:
    return (
        f"🚪 <b>Log out {_gh_user(user)}?</b>\n\nYour token is erased. Integrations you created "
        "keep running, but managing them and replying to comments needs a new login.",
        keys.markup(
            [keys.cb("🚪 Yes, log out", A.LOGOUT_YES, style="danger"), keys.back()],
        ),
    )


# ── repo picker ───────────────────────────────────────────────────────────────


def picker(
    keys: Keys,
    repos: Sequence[RepoInfo],
    orgs: Sequence[OrgInfo],
    scope: str,
    page: int,
    query: str | None,
    destination: str,
) -> Panel:
    if scope == "o":
        items = [(o.login, A.PICK_ORG, o.id, "🏢") for o in orgs]
        noun = "organization"
    else:
        visible = [r for r in repos if r.can_admin]
        items = [(r.full_name, A.PICK_REPO, r.id, "🔒" if r.private else "📦") for r in visible]
        noun = "repository"
    if query:
        q = query.lower()
        items = [i for i in items if q in i[0].lower()]
    pages = max(1, math.ceil(len(items) / PAGE_SIZE))
    page = min(max(page, 0), pages - 1)
    chunk = items[page * PAGE_SIZE : (page + 1) * PAGE_SIZE]

    lines = [
        "➕ <b>Add integration</b>",
        f"Pick a {noun} to stream into <b>{esc(destination)}</b>.",
        "",
    ]
    if query:
        lines.append(f"🔎 Filter: <code>{esc(query)}</code>")
    if items:
        lines.append(f"<i>{len(items)} {noun if len(items) == 1 else noun + 's'} · page {page + 1}/{pages}"
                     + (" · sorted by last push" if scope == "r" else "") + "</i>")
    else:
        lines.append("<i>Nothing found.</i>" if query else (
            "<i>You don't administer any repositories with this token.</i>" if scope == "r"
            else "<i>No organizations visible — the token may lack</i> <code>read:org</code>."
        ))
    if scope == "r":
        lines.append("<i>Only repos where you're an admin are listed (webhooks need it).</i>")

    rows: list[list[InlineKeyboardButton]] = [
        [keys.cb(f"{emoji} {name}", action, ident)] for name, action, ident, emoji in chunk
    ]
    if pages > 1:
        rows.append(
            [
                keys.cb("‹ Prev", A.ADD, max(page - 1, 0), scope) if page > 0 else keys.cb("·", A.NOOP),
                keys.cb(f"{page + 1}/{pages}", A.NOOP),
                keys.cb("Next ›", A.ADD, page + 1, scope) if page < pages - 1 else keys.cb("·", A.NOOP),
            ]
        )
    search_row = [keys.cb("🔍 Search", A.SEARCH, scope)]
    if query:
        search_row.append(keys.cb("✖️ Clear filter", A.CLEAR_SEARCH, scope))
    rows.append(search_row)
    rows.append(
        [keys.cb("📦 Repositories", A.ADD, 0, "r")] if scope == "o"
        else [keys.cb("🏢 Whole organization", A.ADD, 0, "o")]
    )
    rows.append([keys.back()])
    return "\n".join(lines), InlineKeyboardMarkup(inline_keyboard=rows)


def search_prompt(keys: Keys, scope: str, in_group: bool) -> Panel:
    how = "<b>Reply to this message</b> with" if in_group else "Send me"
    noun = "organization" if scope == "o" else "repository"
    return (
        f"🔍 <b>Search</b>\n\n{how} part of the {noun} name, e.g. <code>wave</code>.",
        keys.markup([keys.cb("✖️ Cancel", A.ADD, 0, scope)]),
    )


def confirm_target(
    keys: Keys, kind: str, target_id: int, name: str, private_repo: bool | None,
    description: str | None, is_private_chat: bool, destination: str,
) -> Panel:
    k = "r" if kind == "repo" else "o"
    icon = "🏢" if kind == "org" else ("🔒" if private_repo else "📦")
    lines = [f"{icon} <b>{link(f'https://github.com/{name}', name)}</b>"]
    if description:
        lines.append(f"<i>{esc(description[:200])}</i>")
    if kind == "org":
        lines.append("<i>Organization webhook — every repo in the org, current and future.</i>")
    lines += [
        "",
        "All event types start <b>on</b>; fine-tune them right after.",
    ]
    if is_private_chat:
        lines.append("Where should events go?")
        rows = [
            [keys.cb("💬 This chat", A.CREATE, k, target_id, "h", style="primary")],
            [keys.cb("👥 A group", A.CREATE, k, target_id, "g"), keys.cb("📢 A channel", A.CREATE, k, target_id, "c")],
            [keys.back(A.ADD, 0, k)],
        ]
    else:
        lines.append(f"Events will stream into <b>{esc(destination)}</b>.")
        rows = [
            [keys.cb("✅ Connect", A.CREATE, k, target_id, "h", style="success")],
            [keys.back(A.ADD, 0, k)],
        ]
    return "\n".join(lines), InlineKeyboardMarkup(inline_keyboard=rows)


def pick_chat_hint(kind: str) -> str:
    if kind == "c":
        return ("📢 <b>Choose a channel</b> with the button below.\n"
                "<i>Add me as an admin with “Post messages” first.</i>")
    return ("👥 <b>Choose a group</b> with the button below.\n"
            "<i>Add me to the group first — you need to be an admin there.</i>")


def hook_failed(keys: Keys, error: str, kind: str, target_id: int, allow_manual: bool) -> Panel:
    k = "r" if kind == "repo" else "o"
    rows = []
    if allow_manual:
        rows.append([keys.cb("🛠 Set up manually", A.CREATE, k, target_id, "m")])
    rows.append([keys.back(A.ADD, 0, k)])
    return f"⚠️ <b>Couldn't create the webhook</b>\n\n{error}", InlineKeyboardMarkup(inline_keyboard=rows)


def manual_setup(keys: Keys, integ: Integration, url: str, secret: str) -> Panel:
    text = "\n".join(
        [
            f"🛠 <b>Manual webhook</b> · {_target_link(integ)}",
            "",
            "In GitHub → <b>Settings → Webhooks → Add webhook</b>:",
            f"• Payload URL: <code>{esc(url)}</code>",
            "• Content type: <code>application/json</code>",
            "• Secret: <tg-spoiler>" + esc(secret) + "</tg-spoiler>",
            "• Events: <i>Send me everything</i> (I filter on my side)",
            "",
            "<i>The secret is shown only once. This status turns 🟢 on the first delivery.</i>",
        ]
    )
    return text, keys.markup(
        [keys.copy("📋 Copy URL", url), keys.copy("🔐 Copy secret", secret)],
        [keys.cb("⚙️ Settings", A.OPEN, integ.id, style="primary")],
    )


# ── integrations ──────────────────────────────────────────────────────────────


def integrations_list(
    keys: Keys,
    items: Sequence[tuple[Integration, str | None]],
    chat_label: str,
    banner: str | None = None,
) -> Panel:
    """``items`` = (integration, where) — ``where`` labels integrations living in other chats."""
    lines = [banner, ""] if banner else []
    lines += [f"📋 <b>Integrations</b> · {esc(chat_label)}", ""]
    if not items:
        lines.append("<i>Nothing connected yet. Tap ➕ to add your first repo.</i>")
    for integ, where in items:
        dot, status = _health(integ)
        kind = " <i>(org)</i>" if integ.kind == "org" else ""
        place = f" → {esc(where)}" if where else ""
        lines.append(
            f"{dot} {_target_link(integ)}{kind}{place}\n"
            f"      {_enabled_count(integ)}/{len(EVENT_TOGGLES)} events"
            f"{' · 🔕' if integ.quiet else ''} · {status}"
        )
    rows = [
        [keys.cb(f"{'🏢' if i.kind == 'org' else '📦'} {i.target}" + (f" → {w}" if w else ""), A.OPEN, i.id)]
        for i, w in items[:20]
    ]
    rows.append([keys.cb("➕ Add", A.ADD, 0, "r", style="primary"), keys.back()])
    return "\n".join(lines), InlineKeyboardMarkup(inline_keyboard=rows)


def integration_detail(
    keys: Keys, integ: Integration, banner: str | None = None, where: str | None = None
) -> Panel:
    dot, status = _health(integ)
    owner = integ.owner.github_login if integ.owner else None
    branches = parse_branch_filter(integ.branch_filter)
    lines = []
    if banner:
        lines += [banner, ""]
    lines += [
        f"{'🏢' if integ.kind == 'org' else '📦'} <b>{_target_link(integ)}</b>",
        f"<i>{'Organization' if integ.kind == 'org' else 'Repository'}"
        + (f" · added by @{esc(owner)}" if owner else "")
        + f" · {ago(integ.created_at)}</i>",
        "",
        *([f"📍 Posting to <b>{esc(where)}</b>"] if where else []),
        f"{dot} Webhook {'managed' if integ.hook_id else 'manual'} · {status}",
        f"🌿 Push branches: {code(', '.join(branches)) if branches else '<b>all</b>'}",
        f"{'🔕 Quiet mode: <b>on</b> (silent notifications)' if integ.quiet else '🔔 Notifications: <b>on</b>'}",
        "",
        "<i>Tap an event to toggle it:</i>",
    ]
    grid: list[list[InlineKeyboardButton]] = []
    for i in range(0, len(EVENT_TOGGLES), 2):
        row = []
        for t in EVENT_TOGGLES[i : i + 2]:
            on = bool(integ.events & t.flag)
            row.append(
                keys.cb(
                    f"{'✅' if on else '▫️'} {t.label}",
                    A.TOGGLE,
                    integ.id,
                    int(t.flag).bit_length() - 1,
                    style="success" if on else None,
                )
            )
        grid.append(row)
    grid.append(
        [
            keys.cb("🌿 Branch filter", A.BRANCH, integ.id),
            keys.cb("🔔 Unmute" if integ.quiet else "🔕 Quiet", A.QUIET, integ.id),
        ]
    )
    grid.append([keys.cb("🔄 Test webhook", A.PING, integ.id), keys.cb("🗑 Remove", A.REMOVE, integ.id, style="danger")])
    grid.append([keys.back(A.LIST)])
    return "\n".join(lines), InlineKeyboardMarkup(inline_keyboard=grid)


def branch_prompt(keys: Keys, integ: Integration, in_group: bool, error: str | None = None) -> Panel:
    how = "<b>Reply to this message</b> with" if in_group else "Send me"
    current = integ.branch_filter or "all"
    lines = []
    if error:
        lines += [f"❌ {error}", ""]
    lines += [
        f"🌿 <b>Branch filter</b> · {_target_link(integ)}",
        "",
        f"{how} comma-separated branch globs.",
        "• <code>main</code> — only main",
        "• <code>main, release/*</code> — main and release branches",
        "• <code>!dependabot/*</code> — everything except Dependabot",
        "• <code>all</code> — no filter",
        "",
        f"Current: <code>{esc(current)}</code>",
    ]
    return "\n".join(lines), keys.markup(
        [keys.cb("♻️ Reset to all", A.BRANCH, integ.id, "reset")],
        [keys.cb("✖️ Cancel", A.OPEN, integ.id)],
    )


def remove_confirm(keys: Keys, integ: Integration) -> Panel:
    return (
        f"🗑 <b>Remove {_target_link(integ)}?</b>\n\n"
        "The GitHub webhook is deleted and events stop. Messages already sent stay.",
        keys.markup([keys.cb("🗑 Yes, remove", A.REMOVE_YES, integ.id, style="danger"), keys.back(A.OPEN, integ.id, text="⬅️ Cancel")]),
    )
