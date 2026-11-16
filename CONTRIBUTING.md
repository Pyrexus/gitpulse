# Contributing to GitPulse

Thanks for helping! A few ground rules keep the codebase fast and tidy.

## Setup

```bash
python -m venv .venv && . .venv/bin/activate
pip install -e ".[dev]"
cp .env.example .env    # set BOT_TOKEN, ENCRYPTION_KEY, TELEGRAM_MODE=polling
docker compose up -d postgres
alembic upgrade head
python -m app
```

## Before you open a PR

```bash
ruff check . && ruff format --check .
mypy app
pytest tests/test_<area>.py   # run what you touched; CI runs the rest
```

## Conventions

- **Conventional commits**: `feat:`, `fix:`, `docs:`, `refactor:`, `test:`, `chore:`, `ci:`.
- **Formatters stay pure.** `app/formatting/*` must not do I/O and must emit HTML that
  survives `sanitize()` unchanged. Add a test for every new event type or markup case.
- **Never log secrets.** Tokens are redacted automatically, but don't pass them as log fields.
- **Keep the UX quiet.** Navigation edits the panel in place; event messages get at most 3 buttons.
- Database changes need an Alembic migration in `migrations/versions/`.

## Layout

| Path | What lives there |
|------|------------------|
| `app/bot/` | Telegram handlers, panels, keyboards |
| `app/formatting/` | Markdown → Telegram HTML, event formatters (pure) |
| `app/github/` | Async REST/GraphQL client, API ops, OAuth device flow |
| `app/services/` | Publishing, artifacts, integrations, stats |
| `app/workers/` | Durable queue, debouncer, event processor |
| `app/web/` | aiohttp server: webhooks + health |
| `app/db/` | Models, sessions, repositories |
