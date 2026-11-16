"""Structured logging with automatic secret redaction."""

from __future__ import annotations

import logging
import re
import sys
from collections.abc import MutableMapping
from typing import Any

import structlog

# Anything that looks like a GitHub token or a Telegram bot token is masked,
# no matter where it ends up in a log line.
_TOKEN_RE = re.compile(
    r"(gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,}|\d{6,}:[A-Za-z0-9_-]{30,})"
)
_SENSITIVE_KEYS = {"token", "secret", "password", "authorization", "pat", "key", "access_token"}


def redact_text(value: str) -> str:
    return _TOKEN_RE.sub("[REDACTED]", value)


def _redact(_: Any, __: str, event_dict: MutableMapping[str, Any]) -> MutableMapping[str, Any]:
    for k, v in list(event_dict.items()):
        if any(s in k.lower() for s in _SENSITIVE_KEYS):
            event_dict[k] = "[REDACTED]"
        elif isinstance(v, str):
            event_dict[k] = redact_text(v)
    return event_dict


class _RedactingFilter(logging.Filter):
    """Scrubs stdlib log records (aiogram, aiohttp, httpx) as well."""

    def filter(self, record: logging.LogRecord) -> bool:
        if isinstance(record.msg, str):
            record.msg = redact_text(record.msg)
        if record.args:
            record.args = tuple(
                redact_text(a) if isinstance(a, str) else a
                for a in (record.args if isinstance(record.args, tuple) else (record.args,))
            )
        return True


def setup_logging(level: str = "INFO", json: bool = True) -> None:
    shared: list[Any] = [
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.add_log_level,
        structlog.stdlib.add_logger_name,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        _redact,
    ]
    renderer: Any = (
        structlog.processors.JSONRenderer() if json else structlog.dev.ConsoleRenderer(colors=True)
    )
    structlog.configure(
        processors=[*shared, structlog.stdlib.ProcessorFormatter.wrap_for_formatter],
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.stdlib.BoundLogger,
        cache_logger_on_first_use=True,
    )
    formatter = structlog.stdlib.ProcessorFormatter(
        foreign_pre_chain=shared,
        processors=[structlog.stdlib.ProcessorFormatter.remove_processors_meta, renderer],
    )
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(formatter)
    handler.addFilter(_RedactingFilter())
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(level.upper())
    # httpx logs full URLs at INFO; keep it quiet.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("aiohttp.access").setLevel(logging.WARNING)


def get_logger(name: str) -> structlog.stdlib.BoundLogger:
    return structlog.get_logger(name)  # type: ignore[no-any-return]
