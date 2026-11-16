# Security Policy

## Reporting a vulnerability

Please **do not open a public issue**. Use GitHub's
[private vulnerability reporting](../../security/advisories/new) for this repository.
We aim to acknowledge within 72 hours and ship a fix as fast as the severity requires.

## How GitPulse protects secrets

- **Personal access tokens** are deleted from the chat the moment they arrive, validated,
  and stored only as AES-256-GCM ciphertext bound to the owning Telegram user
  (associated data), using a key from `ENCRYPTION_KEY`. They are never logged; a logging
  filter redacts anything token-shaped.
- **Webhook secrets** are random 256-bit values, unique per integration, encrypted the same way.
  Every delivery is verified with `X-Hub-Signature-256` in constant time before it is stored.
- **Telegram webhook** requests must carry `X-Telegram-Bot-Api-Secret-Token`.
- **Callback buttons** are HMAC-signed and expire, so they can't be forged or replayed into other panels.
- **All GitHub content is escaped** and rebuilt through a whitelist HTML sanitizer before it is sent.
- Only `/health`, `/telegram/webhook` and `/github/webhook/*` are exposed by the bundled Caddy config.

## Least-privilege tokens

Use a fine-grained token limited to the repositories you connect, with:
Webhooks (read & write), Issues and Pull requests (read & write), Contents, Actions
and Metadata (read). Classic tokens need `repo`, `admin:repo_hook`, `read:org`
(+ `admin:org_hook` for org-wide integrations).

## Rotating the encryption key

Changing `ENCRYPTION_KEY` makes stored tokens and webhook secrets unreadable: users must
log in again and integrations must be re-added.
