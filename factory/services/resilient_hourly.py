"""Opt-in recovery for explicitly opted-in Instagram accounts.

Never retries publication effects, suppresses QA, decrypts tokens or changes pause
flags. Production work is resumed through the normal worker and its guards.
"""
from datetime import datetime, timedelta, timezone
from pathlib import Path
import hashlib

from sqlalchemy import select
from factory.config import settings
from factory.models import (Account, Connection, DriveConnection, DriveFile, Job,
                            PublishJob, PublishedPost, SystemLog, Video)

FLAG = 'smfa_resilient_hourly_v1'

CONNECTION_ERROR = 'Google Drive bağlantısı gerekli; hesabı panelden bağlayın.'


def enabled(account):
    cfg = account.settings or {}
    return (cfg.get(FLAG) is True and cfg.get('publication_mode') == 'planner'
            and cfg.get('planner_interval_hours') == 1)


def renewable(account, request):
    return (enabled(account) and request.account_id == account.id
            and request.status in ('WAITING_CONTENT', 'TRACKING')
            and (request.payload or {}).get(FLAG) is True)


def extend_window(request, now):
    request.payload = {**(request.payload or {}),
                       'expires_at': (now + timedelta(minutes=30)).isoformat()}


def next_due(session, account, now):
    """One hour after a confirmed post; no replay of missed hourly slots."""
    cfg = account.settings or {}
    activated = datetime.fromisoformat(cfg['smfa_hourly_started_at'])
    post = session.scalar(select(PublishedPost).where(
        PublishedPost.account_id == account.id, PublishedPost.platform == 'instagram',
        PublishedPost.published_at >= activated).order_by(PublishedPost.published_at.desc()).limit(1))
    offset = timedelta(minutes=int(cfg.get('planner_offset', 0)))
    if post:
        due = post.published_at + timedelta(hours=1)
        # Existing panel reads planner_anchor. Keep it aligned with completion.
        anchor = (post.published_at - offset).isoformat()
        if cfg.get('planner_anchor') != anchor:
            account.settings = {**cfg, 'planner_anchor': anchor}
    else:
        due = activated + offset
    return max(now, due)


def recoverable(session, account, job, now, *, explicit=False):
    if not enabled(account) or not account.active or not account.auto_publish:
        return False
    if account.source_mode != 'DRIVE_ONLY' or job.account_id != account.id:
        return False
    if job.kind != 'DRIVE_PIPELINE' or job.status != 'BLOCKED' or job.lease_until:
        return False
    if not job.completed_at or job.attempts >= job.max_attempts:
        return False
    # Recurring recovery is restricted to this known pre-provider connection gate.
    # Explicit first recovery additionally names the four inspected job IDs.
    if not explicit and job.error_message != CONNECTION_ERROR:
        return False
    result = job.result or {}
    # claim_job retains its internal lease token after a terminal failure.
    # It is bookkeeping, not a provider checkpoint; the next claim replaces it.
    if any(value for key, value in result.items() if key not in (FLAG, '_lease_token')):
        return False
    drive = session.scalar(select(DriveConnection).where(DriveConnection.account_id == account.id))
    link = session.get(Connection, drive.connection_id) if drive else None
    if (not drive or drive.status.upper() != 'CONNECTED' or not link
            or link.account_id != account.id or link.platform not in ('drive', 'google')
            or link.status.upper() != 'CONNECTED' or not drive.last_sync
            or drive.last_sync <= job.completed_at or drive.last_sync < now - timedelta(minutes=15)):
        return False
    video = session.get(Video, job.video_id) if job.video_id else None
    source = session.get(DriveFile, (job.payload or {}).get('drive_file_id'))
    if (not video or video.account_id != account.id or video.status != 'FAILED'
            or video.source_type != 'DRIVE' or video.qa or not source
            or source.account_id != account.id or source.source_type != 'DRIVE'
            or source.video_id != video.id or video.source_file_id != source.id
            or source.processing_status != 'FAILED' or source.publishing_status != 'PENDING'):
        return False
    directory = Path(settings.data_dir) / 'accounts' / account.id / 'videos' / video.id
    if video.local_path or any((directory / name).exists() for name in ('source.mp4', 'final.mp4')):
        return False
    if session.scalar(select(PublishJob.id).where(PublishJob.video_id == video.id).limit(1)):
        return False
    if session.scalar(select(PublishedPost.id).where(PublishedPost.video_id == video.id).limit(1)):
        return False
    other = session.scalars(select(Job).where(Job.video_id == video.id, Job.id != job.id,
        Job.status.not_in(('SUCCEEDED', 'FAILED', 'CANCELLED')))).all()
    # A queued failure archive could otherwise consume media after the retry.
    for archive in other:
        if (archive.kind != 'DRIVE_FAILURE_UPLOAD' or archive.status not in ('QUEUED', 'RETRYING')
                or archive.lease_until or archive.result):
            return False
    return True


def resume_jobs(session, account, now, explicit_ids=()):
    if not enabled(account):
        return []
    explicit_ids = set(explicit_ids)
    jobs = session.scalars(select(Job).where(Job.account_id == account.id,
        Job.kind == 'DRIVE_PIPELINE', Job.status == 'BLOCKED')
        .order_by(Job.created_at, Job.id).with_for_update()).all()
    changed = []
    for job in jobs:
        if job.video_id:
            session.scalar(select(Video).where(Video.id == job.video_id)
                .with_for_update().execution_options(populate_existing=True))
            session.scalars(select(Job).where(Job.video_id == job.video_id, Job.id != job.id)
                .order_by(Job.id).with_for_update().execution_options(populate_existing=True)).all()
        if (job.payload or {}).get('drive_file_id'):
            session.scalar(select(DriveFile).where(DriveFile.id == job.payload['drive_file_id'])
                .with_for_update().execution_options(populate_existing=True))
        if not recoverable(session, account, job, now, explicit=job.id in explicit_ids):
            continue
        video = session.get(Video, job.video_id)
        source = session.get(DriveFile, video.source_file_id)
        for archive in session.scalars(select(Job).where(Job.video_id == video.id,
                Job.kind == 'DRIVE_FAILURE_UPLOAD', Job.status.in_(('QUEUED', 'RETRYING')))
                .with_for_update()).all():
            archive.status = 'CANCELLED'
            archive.completed_at = now
        previous = hashlib.sha256((job.error_message or '').encode()).hexdigest()
        video.status, source.processing_status = 'DRAFT', 'QUEUED'
        job.status, job.run_after, job.error_message = 'QUEUED', now, None
        job.lease_until = job.lease_owner = job.completed_at = None
        job.result = {**(job.result or {}), FLAG: {'recovered_at': now.isoformat(), 'error_sha256': previous}}
        session.add(SystemLog(account_id=account.id, level='INFO', event='drive.connection_recovered',
            message='Doğrulanmış Drive bağlantısından sonra aynı hazırlama işi yeniden sıraya alındı.',
            data={'job_id': job.id, 'video_id': video.id, 'attempts_preserved': job.attempts,
                  'explicit_recovery': job.id in explicit_ids, 'previous_error_sha256': previous}))
        changed.append(job.id)
    session.flush()
    return changed
