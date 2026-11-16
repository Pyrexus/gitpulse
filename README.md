<div align="center">

# ⚡ GitPulse

**GitHub, live in your Telegram chat.**
Issues, PRs, reviews, pushes, CI runs, releases and APKs — beautifully formatted, instantly delivered,
and you can reply to comment right from Telegram.

[![CI](https://github.com/Pyrexus/gitpulse/actions/workflows/ci.yml/badge.svg)](https://github.com/Pyrexus/gitpulse/actions/workflows/ci.yml)
![Python](https://img.shields.io/badge/python-3.12+-3776AB)
![aiogram](https://img.shields.io/badge/aiogram-3.32-2CA5E0)
![Bot API](https://img.shields.io/badge/Bot%20API-10.4-26A5E4)
![License](https://img.shields.io/badge/license-MIT-green)

</div>

---

## ✨ Features

- **Premium messages** — strict Telegram HTML: bold titles, linked everything (repo, `#number`, SHA,
  author, branch, compare), bodies in `<blockquote expandable>`, code blocks kept separate, consistent emoji language.
- **Real GitHub-Markdown converter** — headings, nested & task lists, tables (monospace grid or key/value),
  fenced code with language, `<details>`, alerts (`> [!NOTE]`), emoji shortcodes, autolinked `@mentions`/`#refs`,
  and template noise like empty `_No response_` sections stripped.
- **Images** — every image in issues, PRs and comments (`![]()`, `<img>`, `user-attachments`) is sent: one → photo
  with caption, many → media group linked to the text. Private attachments are fetched through GitHub's signed URLs.
- **Grouped pushes** — one message per push, rapid pushes debounced together, merge/squash commits link their PR.
- **Live status badges** — PR opened → merged / closed edits the *same* message (🟢 → 🟣) instead of spamming.
- **Reply to comment** — reply to any event message and it's posted on GitHub as you. 👍 reaction confirms.
- **APKs in chat** — successful workflow runs and releases deliver matching artifacts (`*.apk` by default) as
  documents, deduplicated by artifact ID; up to 2 GB with a Local Bot API server, a Download button otherwise.
- **`/stats` cards** — commits, ±LOC, PRs opened/merged, issues, reviews, first/last commit, top repos bar chart,
  with 7 days / 30 days / All time buttons that edit the card in place.
- **Button-driven UX** — exactly two commands. Everything else is one self-editing panel with styled
  (blue/green/red) buttons, copy-to-clipboard buttons and native group/channel pickers.
- **Fast & robust** — webhook verifies HMAC, enqueues and returns 200 in milliseconds; durable Postgres-backed
  queue, delivery-ID dedupe, retries with backoff, per-chat ordered sending with flood control, ETag caching,
  GitHub rate-limit awareness, graceful handling when the bot is kicked.
- **Secure** — PATs deleted instantly from chat, stored AES-256-GCM encrypted, never logged; unique webhook
  secret per integration; signed, expiring callback buttons.

## 💬 Message previews

```
🐛 Issue · lastwave/LastWave-Native · 🟢 Open
#42 · App crashes on launch
👤 alice · 🏷 bug · android

┃ Steps
┃ 1. Open the app
┃ 2. Tap Settings → crash
                                [↗ Open]  [💬 Comment]
```

```
🔨 4 new commits to LastWave-Native:main
👤 pushed by bob

┃ a1b2c3d Fix crash on start — alice
┃ d4e5f6a 🔀 Merge #12 from alice/offline — bob
┃ 9f8e7d6 Tweak padding (#7) — alice
┃ 0c0ffee Bump version — bob
                                [🔍 Compare changes]
```

```
🔀 Pull request · lastwave/LastWave-Native · 🟣 Merged by bob
#12 · Add offline mode
👤 alice · 🌿 feat/offline → main
📊 +120 −30 · 5 files
```

```
✅ Android CI passed · lastwave/LastWave-Native
🌿 main · c0ffee1 Bump version
⏱ 3m 12s · #142 · push · 👤 alice
                        [↗ Open run]  [⬇️ Download APK]
  └─ 📦 app-release.apk · 18.4 MB   (document, reply)
```

```
📊 alice · Alice Liddell
🗓 Last 30 days · 3 repos in this chat

┃ 🔨 128 commits
┃ 📝 +12,430 / −3,201 lines
┃ 🔀 14 PRs opened · 11 merged
┃ 🐛 6 issues opened
┃ 👀 23 PRs reviewed
🏆 Top repo: LastWave-Native · 96 commits
LastWave-Native ██████████ 96
gitpulse        ███░░░░░░░ 31
         [7 days] [• 30 days •] [All time]
         [↗ Open on GitHub]  [🔄 Refresh]
```

## 🏗 Architecture

```mermaid
flowchart LR
    GH[GitHub] -- webhook + HMAC --> W[/aiohttp<br/>/github/webhook/:key/]
    TG[Telegram] -- updates --> T[/aiohttp<br/>/telegram/webhook/]
    W -- verify, insert, 200 --> DB[(PostgreSQL)]
    W -- enqueue id --> Q[[Async worker queue]]
    Q --> P[Processor]
    P -- debounce pushes/reviews --> P
    P --> F[Formatters<br/>Markdown → TG HTML]
    P --> A[Artifact service]
    F --> S[Per-chat sender<br/>flood control]
    A --> S
    S --> TG
    T --> H[aiogram handlers<br/>panels · replies · /stats]
    H -- PAT / GraphQL / REST --> GHAPI[GitHub API<br/>ETag cache]
    P --> GHAPI
    H <--> DB
```

- **Receiver**: HMAC check against a cached per-integration secret → one idempotent insert keyed by
  `X-GitHub-Delivery` → enqueue → `200`. Nothing slow happens on the request path.
- **Workers**: N async workers; queued rows are re-enqueued on restart; failures retry with exponential backoff;
  GitHub rate limits wait for the reset.
- **Message map**: every Telegram message is stored with repo, number, kind and thread key — that's what makes
  replies and in-place status edits work.

## 🚀 Quick start (VPS, Docker)

Requirements: a VPS with Docker, a domain pointing at it (A record), ports 80/443 open.

```bash
git clone https://github.com/Pyrexus/gitpulse.git && cd gitpulse
cp .env.example .env
python3 -c "import os,base64;print(base64.urlsafe_b64encode(os.urandom(32)).decode())"  # → ENCRYPTION_KEY
nano .env   # BOT_TOKEN, PUBLIC_URL, DOMAIN, ENCRYPTION_KEY, POSTGRES_PASSWORD (+ same in DATABASE_URL), TELEGRAM_WEBHOOK_SECRET
docker compose up -d
docker compose logs -f bot   # look for "started"
```

The bot **sets its Telegram webhook itself** on startup (`PUBLIC_URL/telegram/webhook`) and registers the
command menu. Caddy obtains the HTTPS certificate automatically. Open your bot and send `/start`.

Check health: `curl https://your-domain/health`.

### Running without Docker (systemd)

```bash
sudo useradd -r -m -d /opt/gitpulse gitpulse && sudo -u gitpulse git clone https://github.com/Pyrexus/gitpulse /opt/gitpulse
cd /opt/gitpulse && sudo -u gitpulse python3.12 -m venv .venv && sudo -u gitpulse .venv/bin/pip install .
sudo -u gitpulse cp .env.example .env   # set DATABASE_URL to your local Postgres, HOST=127.0.0.1
sudo cp deploy/gitpulse.service /etc/systemd/system/ && sudo systemctl enable --now gitpulse
```

Put any reverse proxy with TLS in front of `127.0.0.1:8080` (see `deploy/Caddyfile`).

## ⚙️ Configuration

| Variable | Required | Default | Description |
|---|:-:|---|---|
| `BOT_TOKEN` | ✅ | — | Token from @BotFather |
| `PUBLIC_URL` | ✅ | — | Public HTTPS base URL (webhooks are derived from it) |
| `ENCRYPTION_KEY` | ✅ | — | 32-byte urlsafe base64 key (`python -m app.keygen`) |
| `DATABASE_URL` | ✅ | `postgresql+asyncpg://gitpulse:gitpulse@postgres:5432/gitpulse` | asyncpg URL |
| `DOMAIN` | compose | — | Domain Caddy issues a certificate for |
| `POSTGRES_USER` / `POSTGRES_PASSWORD` / `POSTGRES_DB` | compose | `gitpulse` / — / `gitpulse` | Postgres container |
| `TELEGRAM_MODE` | | `webhook` | `webhook` (prod) or `polling` (dev) |
| `TELEGRAM_WEBHOOK_SECRET` | recommended | — | Verified on every Telegram update |
| `TELEGRAM_API_BASE` | | — | Local Bot API server URL, e.g. `http://telegram-bot-api:8081` |
| `TELEGRAM_API_ID` / `TELEGRAM_API_HASH` | local API | — | From my.telegram.org, for the Local Bot API container |
| `ADMIN_IDS` | | — | Telegram IDs that see the status line on the home panel |
| `GITHUB_OAUTH_CLIENT_ID` | | — | Enables "Login with GitHub" (device flow) |
| `GITHUB_FALLBACK_TOKEN` | | — | Token for public lookups when no user token is available |
| `PUSH_DEBOUNCE_SECONDS` | | `6` | Quiet time before a push batch is sent |
| `REVIEW_DEBOUNCE_SECONDS` | | `4` | Groups a review with its inline comments |
| `ARTIFACT_PATTERN` | | `*.apk` | Comma-separated globs for files to deliver |
| `MAX_ARTIFACT_MB` | | `0` (auto) | Upload cap; auto = 50 MB cloud / 2000 MB local API |
| `WORKER_CONCURRENCY` | | `8` | Parallel event workers |
| `PANEL_TTL_HOURS` | | `48` | Panel buttons older than this show "expired" |
| `INPUT_TTL_MINUTES` | | `15` | How long a pending text input is awaited |
| `LOG_LEVEL` / `LOG_JSON` | | `INFO` / `true` | Structured logging |

## 🔑 GitHub token scopes

**Fine-grained (recommended, least privilege)** — limit to the repos you connect:
Webhooks *read & write*, Issues *read & write*, Pull requests *read & write*, Contents *read*,
Actions *read* (APKs), Metadata *read*. Organization webhooks need org-level *Webhooks: read & write*.

**Classic**: `repo`, `admin:repo_hook`, `read:org`, plus `admin:org_hook` for org-wide integrations.
For public repos only, `public_repo` can replace `repo`.

Webhooks require **admin** access to the repo/org; the picker only lists repos where you're an admin.

## 🌐 Webhooks & HTTPS

- Each integration gets its own URL `PUBLIC_URL/github/webhook/<random-key>` and a unique 256-bit secret.
  GitPulse creates the webhook through the API (JSON, all needed events) and deletes it on removal.
- If creation isn't allowed, choose **🛠 Set up manually** — you get the URL and secret with copy buttons.
- Telegram requires HTTPS on 443/80/88/8443; the bundled Caddy handles certificates and exposes only
  `/health`, `/telegram/webhook` and `/github/webhook/*`.

## 📦 Local Bot API server (2 GB uploads)

The public Bot API caps uploads at 50 MB. To deliver bigger APKs:

1. Get `api_id` / `api_hash` at <https://my.telegram.org>.
2. In `.env`: `TELEGRAM_API_ID=…`, `TELEGRAM_API_HASH=…`, `TELEGRAM_API_BASE=http://telegram-bot-api:8081`.
3. Log the bot out of the cloud API once: `curl https://api.telegram.org/bot<TOKEN>/logOut`.
4. `docker compose --profile local-api up -d`

Files over the limit always fall back to a **⬇️ Download** button.

## 🕹 Commands & UX

GitPulse has **exactly two commands** in the Telegram menu:

| Command | What it does |
|---|---|
| `/start` | Opens the home panel |
| `/stats <github-username>` | Stats card for that user across this chat's repos (no argument = you) |

Everything else happens in **one panel message that edits itself**:

- **🔑 Login with PAT** → paste a token → it's deleted instantly, validated, your identity is shown.
  (**🐙 Login with GitHub** appears when OAuth device flow is configured.) **✖️ Cancel** is always there.
- **➕ Add integration** → paginated picker with **🔍 Search** → repo or **🏢 whole organization** →
  **💬 This chat / 👥 A group / 📢 A channel** (Telegram's native chat picker) → webhook created → settings.
- **📋 Integrations** → each opens a detail panel: event toggle grid (green = on), **🌿 Branch filter**
  (`main, release/*, !dependabot/*`), **🔕 Quiet** mode, **🔄 Test webhook**, **🗑 Remove** (with confirmation).
- **ℹ️ Help** — a short inline guide.

On event messages: **↗ Open** and **💬 Comment** (or **⬇️ Download APK**) — never more than three buttons.
**Reply** to an event to comment as yourself. In groups, only admins or the integration owner can change settings;
in groups, text input (search, branch filter) is given by replying to the panel. Stale buttons show
"This panel expired" with a button to reopen home.

## ❓ FAQ / Troubleshooting

**The bot doesn't answer.** `docker compose logs bot`. Check `PUBLIC_URL` is HTTPS and reachable, and
`curl https://api.telegram.org/bot<TOKEN>/getWebhookInfo` shows no `last_error_message`.

**Integration stays 🟡 "waiting for first event".** Tap **🔄 Test webhook**. In GitHub → Settings → Webhooks →
Recent deliveries you'll see the response; `401` means a secret mismatch (re-add the integration).

**"GitHub refused to create the webhook".** Your token lacks `admin:repo_hook` / *Webhooks: write*, or you're not
an admin. Use manual setup or a better-scoped token.

**Images from private repos don't show.** The integration owner's token must be able to read the repo.

**No line counts in /stats.** GitHub computes contributor stats lazily (the card says so — tap 🔄), and reports
0 lines for repos with 10,000+ commits.

**APK not delivered.** Enable **APK uploads**, make sure the run succeeded, the artifact contains a file matching
`ARTIFACT_PATTERN`, and it's under the upload limit (otherwise you'll get the Download button).

**Can I use it in a channel?** Yes: add the bot as a channel admin, then in the private chat pick **📢 A channel**.
Manage it later from the private chat's integrations list.

## 🗺 Roadmap

- Telegram Rich Messages (Bot API 10.1+) for long PR descriptions with real headings and tables
- Forum topic routing (one topic per repo)
- Per-integration label and author filters
- Discussions, deployments and security alerts
- GitHub App mode (no personal tokens)
- i18n

## 📄 License

[MIT](LICENSE)
