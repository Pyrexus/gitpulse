"""Async GitHub REST/GraphQL client.

* one pooled ``httpx.AsyncClient`` (keep-alive, HTTP/1.1)
* conditional requests: ETag / Last-Modified are replayed, and ``304`` answers
  (which don't count against the rate limit) are served from memory
* short-lived response cache for hot GETs (repo pickers, stats)
* rate-limit awareness: remaining/reset tracked per token, secondary limits respected
* retries with exponential backoff on network errors and 5xx
"""

from __future__ import annotations

import asyncio
import hashlib
import time
from collections import OrderedDict
from collections.abc import AsyncIterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx

from app import __version__
from app.log import get_logger

log = get_logger(__name__)

JSON = Any


class GitHubError(Exception):
    def __init__(self, status: int, message: str) -> None:
        super().__init__(f"GitHub {status}: {message}")
        self.status = status
        self.message = message

    @property
    def is_auth(self) -> bool:
        return self.status == 401

    @property
    def is_forbidden(self) -> bool:
        return self.status in {403, 404}


class RateLimited(GitHubError):
    def __init__(self, reset_at: float) -> None:
        super().__init__(429, "rate limit exceeded")
        self.reset_at = reset_at

    @property
    def retry_in(self) -> float:
        return max(0.0, self.reset_at - time.time())


class TooLarge(GitHubError):
    def __init__(self, size: int) -> None:
        super().__init__(413, f"file too large ({size} bytes)")
        self.size = size


@dataclass(slots=True)
class _CacheEntry:
    etag: str | None
    last_modified: str | None
    data: JSON
    headers: dict[str, str]
    stored: float


@dataclass(slots=True)
class Response:
    status: int
    data: JSON
    headers: httpx.Headers | dict[str, str]

    def header(self, name: str) -> str | None:
        return self.headers.get(name)


def _token_key(token: str | None) -> str:
    return hashlib.sha256(token.encode()).hexdigest()[:16] if token else "anon"


class GitHub:
    def __init__(
        self,
        base_url: str = "https://api.github.com",
        fallback_token: str | None = None,
        cache_size: int = 2048,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.fallback_token = fallback_token
        self._client = httpx.AsyncClient(
            timeout=httpx.Timeout(20.0, connect=5.0),
            limits=httpx.Limits(max_connections=64, max_keepalive_connections=32),
            headers={
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
                "User-Agent": f"GitPulse/{__version__}",
            },
            follow_redirects=True,  # httpx drops Authorization on cross-origin redirects
        )
        self._cache: OrderedDict[str, _CacheEntry] = OrderedDict()
        self._cache_size = cache_size
        self._limits: dict[str, tuple[int, float]] = {}  # token key → (remaining, reset)

    async def close(self) -> None:
        await self._client.aclose()

    # ── low level ─────────────────────────────────────────────────────────
    def _url(self, path: str) -> str:
        return path if path.startswith("http") else f"{self.base_url}{path}"

    def _headers(self, token: str | None, accept: str | None) -> dict[str, str]:
        h: dict[str, str] = {}
        tok = token or self.fallback_token
        if tok:
            h["Authorization"] = f"Bearer {tok}"
        if accept:
            h["Accept"] = accept
        return h

    def _check_budget(self, token: str | None) -> None:
        remaining, reset = self._limits.get(_token_key(token or self.fallback_token), (1, 0.0))
        if remaining <= 0 and reset > time.time():
            raise RateLimited(reset)

    def _record_limits(self, token: str | None, headers: httpx.Headers) -> None:
        rem, reset = headers.get("x-ratelimit-remaining"), headers.get("x-ratelimit-reset")
        if rem is not None and reset is not None:
            try:
                self._limits[_token_key(token or self.fallback_token)] = (int(rem), float(reset))
            except ValueError:
                pass

    def rate_limit_snapshot(self) -> dict[str, tuple[int, float]]:
        return dict(self._limits)

    async def request(
        self,
        method: str,
        path: str,
        *,
        token: str | None = None,
        params: dict[str, Any] | None = None,
        json: JSON | None = None,
        accept: str | None = None,
        cache_ttl: float = 0.0,
        conditional: bool = True,
        retries: int = 3,
    ) -> Response:
        url = self._url(path)
        cache_key = None
        entry = None
        if method == "GET" and conditional:
            cache_key = "|".join(
                [url, repr(sorted((params or {}).items())), accept or "", _token_key(token)]
            )
            entry = self._cache.get(cache_key)
            if entry and cache_ttl and time.monotonic() - entry.stored < cache_ttl:
                self._cache.move_to_end(cache_key)
                return Response(200, entry.data, entry.headers)

        self._check_budget(token)
        headers = self._headers(token, accept)
        if entry:
            if entry.etag:
                headers["If-None-Match"] = entry.etag
            if entry.last_modified:
                headers["If-Modified-Since"] = entry.last_modified

        delay = 0.5
        for attempt in range(retries + 1):
            try:
                resp = await self._client.request(
                    method, url, params=params, json=json, headers=headers
                )
            except (httpx.TransportError, httpx.TimeoutException) as exc:
                if attempt >= retries:
                    raise GitHubError(0, f"network error: {type(exc).__name__}") from exc
                await asyncio.sleep(delay)
                delay *= 2
                continue

            self._record_limits(token, resp.headers)

            if resp.status_code == 304 and entry and cache_key:
                entry.stored = time.monotonic()
                self._cache.move_to_end(cache_key)
                return Response(200, entry.data, entry.headers)

            if resp.status_code in {403, 429} and self._is_rate_limited(resp):
                retry_after = resp.headers.get("retry-after")
                wait = float(retry_after) if retry_after and retry_after.isdigit() else None
                if wait is not None and wait <= 10 and attempt < retries:
                    await asyncio.sleep(wait)
                    continue
                reset = resp.headers.get("x-ratelimit-reset")
                raise RateLimited(float(reset) if reset else time.time() + (wait or 60))

            if resp.status_code >= 500 and attempt < retries:
                await asyncio.sleep(delay)
                delay *= 2
                continue

            data: JSON = None
            if resp.content:
                ctype = resp.headers.get("content-type", "")
                data = resp.json() if "json" in ctype else resp.text

            if resp.status_code >= 400:
                message = data.get("message", "") if isinstance(data, dict) else str(data)[:200]
                raise GitHubError(resp.status_code, message or resp.reason_phrase)

            if cache_key and resp.status_code == 200:
                etag, lm = resp.headers.get("etag"), resp.headers.get("last-modified")
                if etag or lm or cache_ttl:
                    self._cache[cache_key] = _CacheEntry(
                        etag, lm, data, dict(resp.headers), time.monotonic()
                    )
                    self._cache.move_to_end(cache_key)
                    while len(self._cache) > self._cache_size:
                        self._cache.popitem(last=False)
            return Response(resp.status_code, data, resp.headers)
        raise GitHubError(0, "retries exhausted")  # pragma: no cover

    @staticmethod
    def _is_rate_limited(resp: httpx.Response) -> bool:
        if resp.status_code == 429:
            return True
        if resp.headers.get("x-ratelimit-remaining") == "0":
            return True
        if "retry-after" in resp.headers:
            return True
        try:
            msg = str(resp.json().get("message", "")).lower()
        except ValueError:
            return False
        return "rate limit" in msg

    async def get(self, path: str, token: str | None = None, **kw: Any) -> JSON:
        return (await self.request("GET", path, token=token, **kw)).data

    async def paginate(
        self,
        path: str,
        token: str | None = None,
        params: dict[str, Any] | None = None,
        max_pages: int = 5,
        cache_ttl: float = 0.0,
        item_key: str | None = None,
    ) -> list[JSON]:
        items: list[JSON] = []
        params = {"per_page": 100, **(params or {})}
        for page in range(1, max_pages + 1):
            resp = await self.request(
                "GET", path, token=token, params={**params, "page": page}, cache_ttl=cache_ttl
            )
            batch = resp.data[item_key] if item_key else resp.data
            items.extend(batch or [])
            if len(batch or []) < params["per_page"]:
                break
        return items

    async def graphql(
        self, query: str, variables: dict[str, Any], token: str | None, cache_ttl: float = 0.0
    ) -> JSON:
        resp = await self.request(
            "POST", "/graphql", token=token, json={"query": query, "variables": variables}
        )
        data = resp.data or {}
        if data.get("errors") and not data.get("data"):
            raise GitHubError(400, data["errors"][0].get("message", "GraphQL error"))
        return data.get("data") or {}

    # ── downloads ─────────────────────────────────────────────────────────
    async def stream(
        self, url: str, token: str | None, max_bytes: int, accept: str | None = None
    ) -> AsyncIterator[bytes]:
        headers = self._headers(token, accept)
        async with self._client.stream("GET", url, headers=headers) as resp:
            if resp.status_code >= 400:
                raise GitHubError(resp.status_code, "download failed")
            length = int(resp.headers.get("content-length") or 0)
            if length and length > max_bytes:
                raise TooLarge(length)
            got = 0
            async for chunk in resp.aiter_bytes(256 * 1024):
                got += len(chunk)
                if got > max_bytes:
                    raise TooLarge(got)
                yield chunk

    async def download_bytes(
        self, url: str, token: str | None, max_bytes: int, accept: str | None = None
    ) -> bytes:
        buf = bytearray()
        async for chunk in self.stream(url, token, max_bytes, accept):
            buf.extend(chunk)
        return bytes(buf)

    async def download_file(
        self, url: str, dest: Path, token: str | None, max_bytes: int, accept: str | None = None
    ) -> int:
        size = 0
        with dest.open("wb") as fh:
            async for chunk in self.stream(url, token, max_bytes, accept):
                fh.write(chunk)
                size += len(chunk)
        return size

    async def fetch_public(self, url: str, max_bytes: int) -> bytes | None:
        """Fetch any URL without credentials; ``None`` on failure (dead links never raise)."""
        try:
            async with self._client.stream(
                "GET", url, headers={"Accept": "image/*,*/*;q=0.5"}
            ) as resp:
                if resp.status_code != 200:
                    return None
                buf = bytearray()
                async for chunk in resp.aiter_bytes(128 * 1024):
                    buf.extend(chunk)
                    if len(buf) > max_bytes:
                        return None
                return bytes(buf)
        except (httpx.HTTPError, ValueError):
            return None
