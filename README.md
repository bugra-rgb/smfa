# SMFA — Social Media Fabrica

Self-hosted, multi-account video preparation and publishing automation, with a Turkish management panel.

**Community source release. Starts in mock mode with publishing paused. No live accounts, tokens, passwords, media, or production database are included.** You must supply your own provider applications, credentials and content before real publishing can work.

## What is included

- FastAPI API, PostgreSQL persistence, Redis/Celery workers and a scheduler.
- Google Drive video ingestion, preparation, FFmpeg rendering and quality checks.
- Per-account publishing schedules, approval workflows, archive/pause controls and bounded inventory.
- Optional sequential replay of previously published Drive videos after fresh content is exhausted, with publication-history checks.
- Unicode captions (including Japanese), manual hashtag pools, and optional Reels cover support.
- Direct Instagram, YouTube, Facebook and X adapters; additional experimental provider code.
- A Next.js administration panel, Docker Compose setup and automated tests.

The repository does not include the original installation's custom dashboard overlays, media library, tenant-specific recovery scripts or Buffer Instagram experiment. Some newer settings are available through the authenticated API rather than this base panel.

## Quick start: local mock mode

Requires Docker with Linux containers and Docker Compose, plus Python 3.12+ for the configuration generator. On Windows, Docker Desktop must be running.

```sh
git clone https://github.com/bugra-rgb/smfa.git
cd smfa
python scripts/bootstrap.py --email admin@example.com
docker compose up -d --build
```

Open `http://localhost:3000`. Read `ADMIN_EMAIL` and the generated `ADMIN_PASSWORD` from your local `.env`. Keep that file private. Bootstrap will not overwrite it. There is no shared default password.

The installation starts **globally paused**, with `MODE=mock`. Mock providers simulate external operations; mock publication records are not real social posts. Create a test account and explicitly resume the system to explore the mock workflow. Do not connect real accounts while experimenting with mock mode.

```sh
docker compose ps
docker compose logs --tail=80 migrate backend worker scheduler
docker compose down
```

`down` keeps database/media volumes. Do not add `--volumes` unless you intend to erase your installation's data.

## Enabling your own installation

See [configuration and production setup](docs/CONFIGURATION.md). Production requires HTTPS, your own provider app approvals and OAuth connections, and explicit activation of each account. Changing a connection's database status to “connected” does not grant provider access.

There is **no license-based account quota** in this source distribution. This is not a promise of unlimited throughput: worker capacity, provider quotas, app roles/approvals and hardware still matter. The legacy six-hour cycle has 180 two-minute scheduling positions; some batch endpoints accept at most 180 IDs per request. Multiple accounts and planner schedules are supported, but large deployments need capacity planning.

## Development

```sh
python -m venv .venv
# Activate .venv using your shell, then:
python -m pip install -r requirements-dev.lock
python -m pytest -m "not integration and not postgres"
cd apps/web
npm ci
npm test
npm run typecheck
npm run build
```

Run FFmpeg media tests in Linux with `ffmpeg`/`ffprobe` installed. Service integration tests additionally need isolated PostgreSQL/Redis test instances. Never point tests at a production database. See [release validation](docs/VALIDATION.md) for what was actually checked.

## Status and limits

This is a starting point for self-hosted use, not a hosted service or a guarantee of successful publishing. Provider APIs and permissions change. Verify captions, covers and a real test post on your own account before enabling unattended publishing. An API acceptance response alone does not prove a cover is visible or a caption was retained.

Content replay is opt-in and preserves history; uncertain or failed external operations intentionally remain blocked rather than risking duplicate posts. Direct TikTok publishing is not enabled by this distribution; its adapter reports a policy block. Experimental Buffer integrations require independent setup and verification.

Use only content you are authorized to publish. No account credentials or media are shared by this project.

## Contributing and security

See [CONTRIBUTING.md](CONTRIBUTING.md), [SECURITY.md](SECURITY.md) and the [MIT license](LICENSE).
