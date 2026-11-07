# Community release validation

Validated on 2026-11-07 against a clean source copy and synthetic local data:

- Backend: **154 tests passed**, including actual FFmpeg rendering, SQLite migration upgrade/check/downgrade/re-upgrade, authentication, idempotency, uncertain-publish handling and safe initial pause.
- Two Redis/Celery integration tests were skipped because no isolated `TEST_REDIS_URL` was supplied.
- Frontend: **6 tests passed**, TypeScript checking passed, Next.js production build passed.
- All Python application modules parsed successfully.
- Tracked-source credential-pattern scan passed; private account bindings, installation addresses and operational exports were removed during manual review.

The local Docker engine was unavailable, so a full Compose deployment and PostgreSQL/Redis runtime test were not executed locally. GitHub CI is configured to run the backend/media and frontend checks independently. This file records local results; it does not claim a CI run has passed.

No real social account was connected and no real post was published by these release tests. Live captions, covers, OAuth provider approvals, quotas and production capacity require verification on the operator's own installation.

The public source starts a new Git history. Production data, deployment history, logs, backups, keys and the original media library are not part of this release.
