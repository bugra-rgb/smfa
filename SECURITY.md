# Security

Never commit `.env`, OAuth exports, database backups, credentials, logs containing tokens, or account media. Use `.env.example` only as a template. Keep all provider secrets server-side.

The public release starts in mock mode and globally paused. Production credentials and activation are separate operator actions. Do not disable OAuth identity validation or resend an operation whose external outcome is unknown.

Report suspected vulnerabilities privately through GitHub's private vulnerability reporting when available. Do not include live credentials or personal account data in public issues. If private reporting is unavailable, request a private contact method without posting exploit details or secrets.

If a secret is exposed, revoke/rotate it at the provider first. Removing a file or rewriting Git history alone does not invalidate a leaked key.
