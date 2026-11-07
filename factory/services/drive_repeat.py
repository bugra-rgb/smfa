"""Opt-in, bounded Drive replay. Never reset publication history or credentials.

The scheduler already owns the Account row lock. Each repeat is a NEW Video
and DRIVE_PIPELINE job; production downloads the original input again and runs
the existing QA, caption and publishing path. Original DriveFile.video_id stays
unchanged. A successful publication is required before another repeat of that
source can be created. Job idempotency also protects repeated scheduler ticks.
"""
from collections import defaultdict
from datetime import timedelta

from sqlalchemy import case, func, literal, select
from factory.models import (Connection, DriveConnection, DriveFile, DriveFolder,
                            Job, PublishJob, PublishedPost, SystemState, Video)
from factory.services.jobs import enqueue_job, log_event

FLAG = 'smfa_drive_repeat_v1'
ACTIVE = ('QUEUED', 'RETRYING', 'RUNNING')
PIPELINES = ('AI_PIPELINE', 'DRIVE_PIPELINE', 'RERENDER')
BAD_PUBLICATIONS = ('UNKNOWN', 'BLOCKED', 'POLICY_BLOCKED', 'FAILED')


def in_scope(account):
    cfg = account.settings or {}
    from factory.services.drive_shared import enabled as shared_input
    return (account.active and account.auto_publish and account.daily_target > 0
            and account.source_mode == 'DRIVE_ONLY'
            and not cfg.get('archived') and not cfg.get('smfa_archived_v1')
            and account.approval_mode != 'MANUAL_APPROVAL'
            and (cfg.get('publication_mode') == 'planner' or (
                cfg.get('smfa_auto_replay_all_v1') is True
                and cfg.get('publication_mode') in (None, '', 'interval')
                and isinstance(cfg.get('publish_interval_hours'), (int, float))
                and not isinstance(cfg.get('publish_interval_hours'), bool)
                and 0 < cfg['publish_interval_hours'] <= 24))
            and (cfg.get('managed_platform', 'instagram') in ('instagram', 'youtube') or shared_input(account))
            and bool(cfg.get('drive_input_folder_id'))
            and not cfg.get('disable_replay'))


def enabled(account):
    return in_scope(account) and (account.settings or {}).get(FLAG) is True


def ready_order(account):
    # Existing random order remains the tie-breaker. Only opted-in accounts
    # choose freshly added content ahead of an already prepared repeat.
    return case((Video.fingerprint.like('drive-repeat:%'), 1), else_=0) if enabled(account) else literal(0)


def scan_result(account, folder_id, files):
    if not enabled(account):
        return {}
    from factory.services.drive_shared import source_key
    return {FLAG: {'folder_id': folder_id,
                   'file_ids': sorted({source_key(account, f.get('id') or f.get('file_id')) for f in files
                                       if f.get('id') or f.get('file_id')})}}


def fresh_inventory(session, account, now):
    """Require a completed, fresh scan of the CURRENT folder and same connection."""
    drive = session.scalar(select(DriveConnection).where(DriveConnection.account_id == account.id))
    link = session.get(Connection, drive.connection_id) if drive else None
    cutoff = now - timedelta(seconds=max(900, min(3600,
        3 * int((account.settings or {}).get('drive_poll_seconds', 300)))))
    if (not drive or drive.status.upper() != 'CONNECTED' or not drive.last_sync
            or drive.last_sync < cutoff or not link or link.account_id != account.id
            or link.platform not in ('google', 'drive') or link.status.upper() != 'CONNECTED'):
        return None
    scan = session.scalar(select(Job).where(Job.account_id == account.id,
        Job.kind == 'DRIVE_SYNC', Job.status == 'SUCCEEDED', Job.completed_at >= cutoff)
        .order_by(Job.completed_at.desc(), Job.id).limit(1))
    data = (scan.result or {}).get(FLAG) if scan else None
    if (not isinstance(data, dict)
            or data.get('folder_id') != account.settings.get('drive_input_folder_id')
            or not isinstance(data.get('file_ids'), list)):
        return None
    return set(data['file_ids'])


def stock(session, account):
    from factory.services.scheduler import INVENTORY_STATES
    videos = session.scalars(select(Video).where(Video.account_id == account.id,
        Video.status.in_(INVENTORY_STATES))).all()
    pubs = session.scalars(select(PublishJob).where(PublishJob.account_id == account.id)).all()
    by_video = defaultdict(list)
    for pub in pubs:
        by_video[pub.video_id].append(pub)
    count = 0
    for video in videos:
        history = by_video.get(video.id, [])
        # A stale READY/SCHEDULED video already used by a terminal publication
        # is not reusable inventory. Its historical record is left untouched.
        if history and all(p.status in ('PUBLISHED', 'CANCELLED') for p in history):
            continue
        count += 1
    count += session.scalar(select(func.count(Job.id)).where(Job.account_id == account.id,
        Job.kind.in_(PIPELINES), Job.status.in_(ACTIVE), Job.video_id.is_(None))) or 0
    return count


def replay_proof(session, account, source):
    """Conservative, immutable proof of every previous use of one input file."""
    if source.processing_status != 'READY' or source.publishing_status != 'PUBLISHED':
        return None
    videos = session.scalars(select(Video).where(Video.account_id == account.id,
        Video.source_file_id == source.id).order_by(Video.created_at, Video.id)).all()
    if not videos or source.video_id not in {v.id for v in videos}:
        return None
    ids = {v.id for v in videos}
    if any(v.status != 'PUBLISHED' or v.source_type != 'DRIVE'
           or (v.qa or {}).get('passed') is not True
           or (v.metadata_json or {}).get('buffer_unresolved_quarantine') for v in videos):
        return None
    pubs = session.scalars(select(PublishJob).where(PublishJob.video_id.in_(ids))).all()
    posts = session.scalars(select(PublishedPost).where(PublishedPost.video_id.in_(ids))).all()
    confirmed = {(p.video_id, p.connection_id, p.platform, p.remote_id): p for p in posts
                 if p.account_id == account.id and p.remote_id}
    if not pubs or {p.video_id for p in pubs} != ids:
        return None
    for pub in pubs:
        if (pub.account_id != account.id or pub.status != 'PUBLISHED' or not pub.remote_id
                or (pub.video_id, pub.connection_id, pub.platform, pub.remote_id) not in confirmed):
            return None
    jobs = session.scalars(select(Job).where(Job.account_id == account.id,
        Job.kind.in_((*PIPELINES, 'PUBLISH')))).all()
    for job in jobs:
        if job.video_id in ids or (job.payload or {}).get('drive_file_id') == source.id:
            if (job.status != 'SUCCEEDED' or (job.result or {}).get('do_not_resend')
                    or (job.result or {}).get('external_outcome') == 'UNKNOWN'):
                return None
    latest = max(confirmed.values(), key=lambda p: (p.published_at, p.id))
    return {'last_video_id': latest.video_id, 'last_published_at': latest.published_at,
            'completed_uses': len(videos)}


def repeat_fingerprint(key):
    """Keep legacy identities; bound new recovery fingerprints to VARCHAR(128)."""
    if len(key) <= 128:
        return key
    import hashlib
    return 'drive-repeat:' + hashlib.sha256(key.encode('utf-8')).hexdigest()


def queue_repeat(session, account, source, proof, now):
    key = f'drive-repeat:{account.id}:{source.id}:{proof["last_video_id"]}'
    reserved = session.scalar(select(Job).where(Job.idempotency_key == key))
    if reserved:
        return 0  # Never reopen a historical reservation automatically.
    audit = {'source_id': source.id, 'previous_video_id': proof['last_video_id'],
             'use_number': proof['completed_uses'] + 1}
    video = Video(account_id=account.id, source_type='DRIVE', source_file_id=source.id,
        title=source.filename.rsplit('.', 1)[0][:500], status='DRAFT',
        fingerprint=repeat_fingerprint(key), metadata_json={FLAG: audit})
    session.add(video)
    session.flush()
    # Supplying a NEW video_id takes the normal ensure_video resume path without
    # overwriting the source's original video link or resetting its old job.
    job = enqueue_job(session, 'DRIVE_PIPELINE', account_id=account.id, video_id=video.id,
        payload={'drive_file_id': source.id, FLAG: audit}, idempotency_key=key,
        priority=80, run_after=now)
    if job.video_id != video.id:
        raise RuntimeError('Drive repeat reservation conflict; transaction must roll back.')
    log_event(session, 'drive.repeat.queued', 'Drive videosu yeni bir yayın döngüsüne alındı.',
              account.id, data={'video_id': video.id, 'job_id': job.id, **audit})
    return 1


def replenish(session, account, now):
    if not enabled(account):
        return 0
    state = session.get(SystemState, 'global')
    if state and state.paused:
        return 0
    from factory.services.drive_shared import enabled as shared_input
    platforms = ((account.settings.get('managed_platform') or 'instagram'),) if shared_input(account) else ('instagram', 'youtube')
    links = session.scalars(select(Connection).where(Connection.account_id == account.id,
        Connection.platform.in_(platforms),
        Connection.status.in_(('CONNECTED', 'connected')))).all()
    from factory.services.channels import eligible
    if not any(eligible(account, link) for link in links):
        return 0
    if session.scalar(select(PublishJob.id).where(PublishJob.account_id == account.id,
            PublishJob.status.in_(BAD_PUBLICATIONS)).limit(1)):
        return 0
    if session.scalar(select(Job.id).where(Job.account_id == account.id,
            Job.kind.in_(PIPELINES), Job.status.in_(('BLOCKED', 'UNKNOWN'))).limit(1)):
        return 0
    failures = session.scalar(select(func.count(Job.id)).where(Job.account_id == account.id,
        Job.kind.in_(PIPELINES), Job.status == 'DLQ', Job.completed_at >= now - timedelta(hours=1))) or 0
    if failures >= 3:
        return 0
    from factory.services.scheduler import inventory_limits
    _, target, maximum = inventory_limits(account)
    # Keep a spare ahead of the next scheduled publication within the existing
    # maximum; never increase a user's disk/inventory ceiling.
    capacity = min(maximum, max(2, target)) - stock(session, account)
    if capacity <= 0:
        return 0
    present = fresh_inventory(session, account, now)
    if present is None:
        return 0  # Normal scheduler DRIVE_SYNC will supply the fresh manifest.
    folder = session.scalar(select(DriveFolder).where(DriveFolder.account_id == account.id,
        DriveFolder.kind == 'input', DriveFolder.external_id == account.settings['drive_input_folder_id']))
    if not folder:
        return 0
    sources = session.scalars(select(DriveFile).where(DriveFile.account_id == account.id,
        DriveFile.folder_id == folder.id, DriveFile.source_type == 'DRIVE')
        .order_by(DriveFile.created_at, DriveFile.id).with_for_update(skip_locked=True)).all()
    sources = [s for s in sources if s.drive_file_id in present]
    made = 0
    for source in sources:
        if source.processing_status != 'DISCOVERED' or source.video_id:
            continue
        key = 'drive-input:' + source.drive_file_id
        if session.scalar(select(Job.id).where(Job.idempotency_key == key)):
            continue
        enqueue_job(session, 'DRIVE_PIPELINE', account_id=account.id,
            payload={'drive_file_id': source.id}, idempotency_key=key, priority=90, run_after=now)
        source.processing_status = 'QUEUED'
        made += 1
        if made >= capacity:
            return made
    candidates = []
    for source in sources:
        proof = replay_proof(session, account, source)
        if proof:
            candidates.append((proof['last_published_at'], source.id, source, proof))
    for _, _, source, proof in sorted(candidates, key=lambda row: (row[0], row[1])):
        made += queue_repeat(session, account, source, proof, now)
        if made >= capacity:
            break
    session.flush()
    return made
