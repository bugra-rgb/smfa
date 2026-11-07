"""Opt-in read-only Drive sources for independent TikTok and YouTube accounts.

Legacy DriveFile.drive_file_id is globally unique. A shared input uses a
namespaced reference in that existing column, rather than changing old rows or
weakening the database constraint. Only the download boundary decodes it to a
real Google file ID. No Google folder, copy, upload or move is created here.
"""
import re
from sqlalchemy import select
from factory.models import DriveFile, PublishedPost, PublishJob

FLAG = 'smfa_shared_drive_readonly_v1'
PREFIX = 'smfa-shared:'


def enabled(account):
    cfg = account.settings or {}
    # Instagram is separately opted in only after empty-inventory validation.
    platform = cfg.get('managed_platform') or 'instagram'
    supported = platform in ('tiktok', 'youtube') or (
        platform == 'instagram' and cfg.get('smfa_instagram_shared_drive_v1') is True)
    return cfg.get(FLAG) is True and supported and account.source_mode == 'DRIVE_ONLY'


def source_key(account, external_id):
    if not enabled(account):
        return external_id
    if (not re.fullmatch(r'[A-Za-z0-9_-]{1,180}', str(external_id or ''))
            or not re.fullmatch(r'[A-Za-z0-9-]{1,36}', str(account.id))):
        raise ValueError('Invalid shared Drive reference')
    return PREFIX + account.id + ':' + external_id


def download_id(account, source):
    if not source or source.account_id != account.id or source.source_type != 'DRIVE':
        raise ValueError('Drive input ownership mismatch')
    key = source.drive_file_id or ''
    if not enabled(account):
        if key.startswith(PREFIX):
            raise ValueError('Shared Drive mode must remain enabled for this source')
        return key
    prefix = PREFIX + account.id + ':'
    if not key.startswith(prefix):
        raise ValueError('Shared Drive input belongs to another namespace')
    external_id = key[len(prefix):]
    if source_key(account, external_id) != key:
        raise ValueError('Invalid shared Drive reference')
    return external_id


def archive_local(ctx, account, video):
    """Record only the account's proven outcome; leave the remote source intact."""
    if not video or video.account_id != account.id or video.source_type != 'DRIVE':
        raise ValueError('Shared archive ownership mismatch')
    target = ctx.job.payload.get('folder', 'published')
    if target not in ('published', 'failed'):
        raise ValueError('Invalid archive outcome')
    source = ctx.session.get(DriveFile, video.source_file_id)
    download_id(account, source)
    if target == 'published':
        posts = ctx.session.scalars(select(PublishedPost).where(
            PublishedPost.account_id == account.id, PublishedPost.video_id == video.id,
            PublishedPost.platform == (account.settings.get('managed_platform') or 'instagram'))).all()
        pubs = ctx.session.scalars(select(PublishJob).where(
            PublishJob.account_id == account.id, PublishJob.video_id == video.id)).all()
        evidence = {(p.connection_id, p.remote_id) for p in posts if p.remote_id}
        if (video.status != 'PUBLISHED' or not pubs or not evidence
                or any(p.platform != (account.settings.get('managed_platform') or 'instagram') or p.status != 'PUBLISHED' or not p.remote_id
                       or (p.connection_id, p.remote_id) not in evidence for p in pubs)):
            raise ValueError('Publication evidence missing; shared source was not marked published')
        source.publishing_status = 'PUBLISHED'
        source.published_at = max(p.published_at for p in posts)
    else:
        source.publishing_status = 'NOT_PUBLISHABLE'
    ctx.session.commit()
    return {'archived': True, 'local_record_only': True, 'source_file_unchanged': True,
            'folder': target, 'account_id': account.id}
