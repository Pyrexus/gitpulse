"""Application settings loaded from the environment (and an optional .env file)."""

from __future__ import annotations

from functools import lru_cache
from typing import Literal

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # ── Telegram ──────────────────────────────────────────────────────────
    bot_token: SecretStr
    telegram_mode: Literal["webhook", "polling"] = "webhook"
    telegram_webhook_secret: SecretStr = Field(
        default=SecretStr(""),
        description="Secret token Telegram sends in X-Telegram-Bot-Api-Secret-Token.",
    )
    telegram_api_base: str | None = Field(
        default=None,
        description="Self-hosted Local Bot API server, e.g. http://telegram-bot-api:8081",
    )
    admin_ids: list[int] = Field(default_factory=list)

    # ── Public HTTP ───────────────────────────────────────────────────────
    public_url: str = Field(description="Public HTTPS base URL, e.g. https://gitpulse.example.com")
    host: str = "0.0.0.0"  # noqa: S104 - container bind
    port: int = 8080

    # ── Storage / security ────────────────────────────────────────────────
    database_url: str = "postgresql+asyncpg://gitpulse:gitpulse@postgres:5432/gitpulse"
    encryption_key: SecretStr = Field(
        description="Fernet key (urlsafe base64, 32 bytes). Generate with `python -m app.keygen`."
    )

    # ── GitHub ────────────────────────────────────────────────────────────
    github_api_url: str = "https://api.github.com"
    github_oauth_client_id: str | None = Field(
        default=None, description="Enables 'Login with GitHub' (OAuth device flow)."
    )
    github_fallback_token: SecretStr | None = Field(
        default=None, description="Optional token for public lookups when no user token exists."
    )

    # ── Behaviour ─────────────────────────────────────────────────────────
    push_debounce_seconds: float = 6.0
    review_debounce_seconds: float = 4.0
    artifact_pattern: str = "*.apk"
    max_artifact_mb: int = 0  # 0 = auto (50 MB cloud API, 2000 MB local API)
    worker_concurrency: int = 8
    panel_ttl_hours: int = 48
    input_ttl_minutes: int = 15
    log_level: str = "INFO"
    log_json: bool = True

    @field_validator("admin_ids", mode="before")
    @classmethod
    def _split_ids(cls, v: object) -> object:
        if isinstance(v, str):
            return [int(x) for x in v.replace(" ", "").split(",") if x]
        if isinstance(v, int):
            return [v]
        return v

    @field_validator("public_url")
    @classmethod
    def _strip_slash(cls, v: str) -> str:
        return v.rstrip("/")

    @property
    def uses_local_bot_api(self) -> bool:
        return bool(self.telegram_api_base)

    @property
    def upload_limit_bytes(self) -> int:
        if self.max_artifact_mb > 0:
            return self.max_artifact_mb * 1024 * 1024
        return (2000 if self.uses_local_bot_api else 50) * 1024 * 1024

    @property
    def telegram_webhook_path(self) -> str:
        return "/telegram/webhook"

    @property
    def github_webhook_base(self) -> str:
        return f"{self.public_url}/github/webhook"


@lru_cache
def get_settings() -> Settings:
    return Settings()
