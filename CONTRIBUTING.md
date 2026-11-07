# Contributing

Use a fresh mock installation and synthetic accounts. Never develop against a production database or connect real accounts in automated tests.

Keep changes focused. Add regression tests for publishing safety, scheduling, identity checks and state transitions. Never remove idempotency guards or treat an uncertain external result as safe to retry. Include migration and rollback considerations for schema changes.

Run backend tests and frontend tests/typecheck/build. Explain checks that need external services and were not run. Do not claim real publishing was verified using mock results.

Before committing, run `python scripts/check_public_tree.py`. Review the staged diff for private account names, infrastructure and content as well as secrets. The scanner is a guardrail, not a complete security audit.
