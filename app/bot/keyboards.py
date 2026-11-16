"""Inline keyboards: signed callbacks, styled buttons, copy-text buttons."""

from __future__ import annotations

from aiogram.types import CopyTextButton, InlineKeyboardButton, InlineKeyboardMarkup

from app.formatting.render import Button
from app.security.signing import CallbackSigner


class A:
    """Callback action codes (kept tiny: callback_data is limited to 64 bytes)."""

    HOME = "h"
    HELP = "hl"
    LOGIN = "lg"
    LOGIN_OAUTH = "od"
    LOGOUT = "lo"
    LOGOUT_YES = "ly"
    CANCEL = "x"
    ADD = "ad"  # page, scope(r|o)
    SEARCH = "as"
    CLEAR_SEARCH = "aq"
    PICK_REPO = "ar"  # repo id
    PICK_ORG = "ao"  # org id
    CREATE = "ac"  # kind(r|o), id, dest(h|g|c)
    LIST = "il"
    OPEN = "io"  # integration id
    TOGGLE = "it"  # integration id, bit
    QUIET = "iq"
    BRANCH = "ib"
    PING = "ip"
    REMOVE = "ir"
    REMOVE_YES = "ix"
    STATS = "st"  # period, login
    COMMENT = "cm"
    NOOP = "nn"


# Panels expire (stale state), event-message buttons live forever.
PERSISTENT = {A.COMMENT, A.NOOP, A.HOME}
LONG_LIVED = {A.STATS: 24 * 30}


def ttl_for(action: str, panel_ttl_hours: int) -> int | None:
    if action in PERSISTENT:
        return None
    return LONG_LIVED.get(action, panel_ttl_hours)


class Keys:
    def __init__(self, signer: CallbackSigner) -> None:
        self.signer = signer

    def cb(
        self, text: str, action: str, *args: object, style: str | None = None
    ) -> InlineKeyboardButton:
        return InlineKeyboardButton(
            text=text, callback_data=self.signer.pack(action, *args), style=style
        )

    @staticmethod
    def url(text: str, url: str, style: str | None = None) -> InlineKeyboardButton:
        return InlineKeyboardButton(text=text, url=url, style=style)

    @staticmethod
    def copy(text: str, value: str) -> InlineKeyboardButton:
        return InlineKeyboardButton(text=text, copy_text=CopyTextButton(text=value[:256]))

    def back(
        self, action: str = A.HOME, *args: object, text: str = "⬅️ Back"
    ) -> InlineKeyboardButton:
        return self.cb(text, action, *args)

    @staticmethod
    def markup(*rows: list[InlineKeyboardButton] | None) -> InlineKeyboardMarkup:
        return InlineKeyboardMarkup(inline_keyboard=[r for r in rows if r])

    def from_rendered(self, rows: list[list[Button]]) -> InlineKeyboardMarkup | None:
        out: list[list[InlineKeyboardButton]] = []
        for row in rows:
            built: list[InlineKeyboardButton] = []
            for b in row:
                if b.url:
                    built.append(self.url(b.text, b.url, b.style))
                elif b.copy:
                    built.append(self.copy(b.text, b.copy))
                elif b.action:
                    built.append(self.cb(b.text, b.action, style=b.style))
            if built:
                out.append(built)
        return InlineKeyboardMarkup(inline_keyboard=out) if out else None
