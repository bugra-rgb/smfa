# Configuration

## Installation secrets

`python scripts/bootstrap.py` generates a private `.env` with random database, Redis, administrator, session and encryption secrets. `.env.example` contains no live credentials. Keep `ENCRYPTION_KEY` backed up securely: changing it makes existing encrypted OAuth credentials unreadable.

The bundled Compose setup exposes only the web panel, on loopback by default. PostgreSQL and Redis are internal services. For production, put the panel behind a trusted HTTPS reverse proxy and configure exact host/origin values. Do not expose PostgreSQL or Redis publicly.

Start with mock mode, then stop the workers before configuring production. Set:

- `MODE=production`
- `PUBLIC_BASE_URL` and `FRONTEND_ORIGIN` to the same HTTPS origin, with `ALLOWED_HOSTS` set appropriately.
- A PostgreSQL `DATABASE_URL` and authenticated `REDIS_URL` (Compose supplies these for its internal services).
- Your provider client IDs and secrets. Leave unused providers blank.
- `GRAPH_API_VERSION` for your own Meta application's supported API version.
- AI provider keys/models only if you use AI generation; Drive-only preparation does not require AI generation.

Recreate the services after changing environment configuration. Keep the global pause active until migrations, login, OAuth identities, media URLs and a controlled test post are verified.

## OAuth and content

Register callback URLs in each provider application to match this API's `/api/oauth/{platform}/callback` route. Supported route names include `drive`, `instagram`, `youtube`, `facebook`, `x` and `tiktok`; enable only the adapters you actually use. Use your own account and complete the provider consent flow. The project does not supply a shared Google or Meta application.

Select a Drive folder in the account settings. The connected identity must have access to that folder. Do not reuse another account's token by copying database rows. OAuth app test-user/role limits and provider verification are external requirements, not limits this software can remove.

Create accounts inactive initially (the `scripts/setup_accounts.py` helper does this), configure content and schedule, connect the intended identity, then enable account automation and resume globally. A fresh database contains no preconfigured social accounts.

## Optional features

These are per-account settings unless stated otherwise. Inspect the authenticated API schema in mock mode at `/api/docs` and the API source for request payloads.

- Drive replay: `smfa_drive_repeat_v1=true`; planner mode, a current successful folder scan and confirmed publication history are required. Fresh content takes precedence, then older successful posts are considered. `disable_replay=true` always excludes an account. Archived, inactive and manual-approval accounts are excluded. Never clear historical publications to force a replay.
- Captions: use `/api/control/captions` routes to store per-account manual text. UTF-8/Unicode is preserved; language is not forced to Japanese. Each installation supplies its own text.
- Caption-only mode: `smfa_no_hashtags_v1=true` for supported Instagram/TikTok manual captions. Check the account-caption API validation before enabling.
- Reels covers: server flag `SMFA_COVERS_ENABLED=true`, followed by an account-specific uploaded asset and cover setting. A separate provider-side visual check is still required.
- Caption verification: `verify_caption_delivery=true` enables the direct Instagram delivery guard for a configured manual caption. A missing or mismatched remote caption requires review, not an automatic duplicate post.
- `instagram_wait_for_ready=true` opts into the direct Instagram readiness helper.

Defaults intentionally leave optional features off. The source retains some `smfa_*` configuration names for compatibility; they contain no credentials or tenant identifiers.

## Backups and upgrades

Back up the PostgreSQL database, media volume and encryption key outside Git. Test restore procedures. Apply Alembic migrations before starting the API/worker after an upgrade. This public distribution is intended for a fresh installation; it is not an in-place migration package for a customized existing deployment.
