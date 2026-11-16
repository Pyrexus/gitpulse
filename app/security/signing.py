"""HMAC helpers: GitHub webhook signatures and signed Telegram callback data."""

from __future__ import annotations

import base64
import hashlib
import hmac
import time
from dataclasses import dataclass

_CB_MAX = 64  # Telegram callback_data limit, bytes
_SIG_LEN = 8  # chars of urlsafe base64 → 48 bits


def verify_github_signature(secret: str, body: bytes, header: str | None) -> bool:
    """Validate ``X-Hub-Signature-256`` in constant time."""
    if not header or not header.startswith("sha256=") or not secret:
        return False
    expected = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, header.removeprefix("sha256="))


def sign_github_payload(secret: str, body: bytes) -> str:
    return "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


@dataclass(frozen=True, slots=True)
class Callback:
    action: str
    args: tuple[str, ...]
    issued_hour: int

    def arg(self, i: int, default: str = "") -> str:
        return self.args[i] if i < len(self.args) else default

    def int_arg(self, i: int, default: int = 0) -> int:
        try:
            return int(self.args[i])
        except (IndexError, ValueError):
            return default


class CallbackError(Exception):
    """Invalid or tampered callback payload."""


class CallbackExpired(CallbackError):
    """Valid signature, but the panel is older than its TTL."""


def _b36(n: int) -> str:
    chars = "0123456789abcdefghijklmnopqrstuvwxyz"
    out = ""
    while True:
        n, r = divmod(n, 36)
        out = chars[r] + out
        if n == 0:
            return out


class CallbackSigner:
    """Packs ``action:arg1:arg2`` + issue hour + truncated HMAC into ≤64 bytes.

    Format: ``<action>:<args…>:<hour36>.<sig>``. Arguments must not contain ``:``.
    """

    def __init__(self, key: bytes) -> None:
        self._key = key

    def _sig(self, payload: str) -> str:
        mac = hmac.new(self._key, payload.encode(), hashlib.sha256).digest()
        return base64.urlsafe_b64encode(mac).decode()[:_SIG_LEN]

    def pack(self, action: str, *args: object, now: float | None = None) -> str:
        parts = [action, *(str(a) for a in args)]
        if any(":" in p or "." in p for p in parts):
            raise ValueError("callback parts must not contain ':' or '.'")
        hour = int((now if now is not None else time.time()) // 3600)
        payload = ":".join([*parts, _b36(hour)])
        data = f"{payload}.{self._sig(payload)}"
        if len(data.encode()) > _CB_MAX:
            raise ValueError(f"callback data too long ({len(data)} bytes): {action}")
        return data

    def unpack(self, data: str, ttl_hours: int | None, now: float | None = None) -> Callback:
        payload, _, sig = data.rpartition(".")
        if not payload or not hmac.compare_digest(sig, self._sig(payload)):
            raise CallbackError("bad signature")
        *parts, hour36 = payload.split(":")
        try:
            hour = int(hour36, 36)
        except ValueError as exc:
            raise CallbackError("bad timestamp") from exc
        if not parts:
            raise CallbackError("empty action")
        now_hour = int((now if now is not None else time.time()) // 3600)
        if ttl_hours is not None and now_hour - hour > ttl_hours:
            raise CallbackExpired(parts[0])
        return Callback(action=parts[0], args=tuple(parts[1:]), issued_hour=hour)

    @staticmethod
    def peek_action(data: str) -> str:
        """Action name without verification — used only to pick a TTL policy."""
        return data.split(":", 1)[0].split(".", 1)[0]
