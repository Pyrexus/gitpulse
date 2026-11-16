"""Shared application services, created once at startup."""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from aiogram import Bot
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.bot.keyboards import Keys
from app.config import Settings
from app.db.models import User
from app.github.client import GitHub
from app.security.crypto import CryptoError, SecretBox
from app.security.signing import CallbackSigner
from app.telegram.sender import Sender

if TYPE_CHECKING:
    from app.workers.queue import EventQueue


@dataclass
class AppContext:
    settings: Settings
    bot: Bot
    db: async_sessionmaker[AsyncSession]
    gh: GitHub
    box: SecretBox
    signer: CallbackSigner
    sender: Sender
    keys: Keys
    bot_username: str = ""
    started_at: float = field(default_factory=time.time)
    queue: EventQueue | None = None

    # ── secrets ──────────────────────────────────────────────────────────
    def user_token(self, user: User | None) -> str | None:
        if user is None or not user.token_enc:
            return None
        try:
            return self.box.decrypt(user.token_enc, f"pat:{user.id}")
        except CryptoError:
            return None

    def encrypt_token(self, user_id: int, token: str) -> str:
        return self.box.encrypt(token, f"pat:{user_id}")

    def hook_secret(self, integration_id_or_key: str, secret_enc: str) -> str | None:
        try:
            return self.box.decrypt(secret_enc, f"hook:{integration_id_or_key}")
        except CryptoError:
            return None

    def deep_link(self, payload: str) -> str:
        return f"https://t.me/{self.bot_username}?start={payload}"
