"""Authenticated encryption for secrets at rest (PATs, webhook secrets).

AES-256-GCM with a random 96-bit nonce. Every ciphertext is bound to a
*context* string (e.g. ``pat:123456``) through GCM associated data, so a
ciphertext copied into another row simply fails to decrypt.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import os

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

_VERSION = "v1"


class CryptoError(Exception):
    """Raised when a secret cannot be decrypted (wrong key, tampering, wrong context)."""


def generate_key() -> str:
    return base64.urlsafe_b64encode(os.urandom(32)).decode()


def _decode_key(key: str) -> bytes:
    try:
        raw = base64.urlsafe_b64decode(key.strip() + "=" * (-len(key.strip()) % 4))
    except (ValueError, TypeError) as exc:
        raise CryptoError("ENCRYPTION_KEY is not valid urlsafe base64") from exc
    if len(raw) != 32:
        raise CryptoError("ENCRYPTION_KEY must decode to exactly 32 bytes")
    return raw


class SecretBox:
    def __init__(self, key: str) -> None:
        self._master = _decode_key(key)
        self._aead = AESGCM(self._derive(b"gitpulse/aead/v1"))

    def _derive(self, label: bytes) -> bytes:
        # Simple HMAC-based KDF: independent subkeys for separate purposes.
        return hmac.new(self._master, label, hashlib.sha256).digest()

    def subkey(self, label: str) -> bytes:
        return self._derive(f"gitpulse/{label}".encode())

    def encrypt(self, plaintext: str, context: str) -> str:
        nonce = os.urandom(12)
        ct = self._aead.encrypt(nonce, plaintext.encode(), context.encode())
        return f"{_VERSION}.{base64.urlsafe_b64encode(nonce + ct).decode()}"

    def decrypt(self, token: str, context: str) -> str:
        try:
            version, blob = token.split(".", 1)
        except ValueError as exc:
            raise CryptoError("malformed ciphertext") from exc
        if version != _VERSION:
            raise CryptoError(f"unsupported ciphertext version {version!r}")
        try:
            raw = base64.urlsafe_b64decode(blob)
            return self._aead.decrypt(raw[:12], raw[12:], context.encode()).decode()
        except (InvalidTag, ValueError) as exc:
            raise CryptoError("cannot decrypt secret") from exc
