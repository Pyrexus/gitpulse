"""GitHub OAuth device flow (optional alternative to pasting a PAT)."""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass

import httpx

DEVICE_CODE_URL = "https://github.com/login/device/code"
TOKEN_URL = "https://github.com/login/oauth/access_token"  # noqa: S105 - endpoint, not a secret
SCOPES = "repo admin:repo_hook admin:org_hook read:org read:user"


@dataclass(slots=True)
class DeviceCode:
    device_code: str
    user_code: str
    verification_uri: str
    interval: int
    expires_at: float


class DeviceFlowError(Exception):
    pass


async def start(client_id: str) -> DeviceCode:
    async with httpx.AsyncClient(timeout=10) as c:
        resp = await c.post(
            DEVICE_CODE_URL,
            data={"client_id": client_id, "scope": SCOPES},
            headers={"Accept": "application/json"},
        )
    data = resp.json()
    if "device_code" not in data:
        raise DeviceFlowError(data.get("error_description") or "device flow unavailable")
    return DeviceCode(
        device_code=data["device_code"],
        user_code=data["user_code"],
        verification_uri=data.get("verification_uri", "https://github.com/login/device"),
        interval=int(data.get("interval", 5)),
        expires_at=time.time() + int(data.get("expires_in", 900)),
    )


async def poll(client_id: str, code: DeviceCode) -> str:
    """Poll until the user authorises; returns the access token."""
    interval = code.interval
    async with httpx.AsyncClient(timeout=10) as c:
        while time.time() < code.expires_at:
            await asyncio.sleep(interval)
            resp = await c.post(
                TOKEN_URL,
                data={
                    "client_id": client_id,
                    "device_code": code.device_code,
                    "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
                },
                headers={"Accept": "application/json"},
            )
            data = resp.json()
            if token := data.get("access_token"):
                return str(token)
            err = data.get("error")
            if err == "authorization_pending":
                continue
            if err == "slow_down":
                interval = int(data.get("interval", interval + 5))
                continue
            raise DeviceFlowError(data.get("error_description") or str(err))
    raise DeviceFlowError("expired")
