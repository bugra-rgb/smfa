"""Elapsed-time publishing: one content item in flight, no catch-up bursts."""
from datetime import datetime, timedelta

from sqlalchemy import func, select

from factory.models import PublishJob, PublishedPost


def interval_hours(account):
    return int((account.settings or {}).get("publish_interval_hours", 0))


def next_due(session, account, now, video_id=None):
    if (account.settings or {}).get("cycle_v2"):
        from factory.services.control import next_cycle
        return next_cycle(session, account, now, video_id)
    query = select(func.max(PublishedPost.published_at)).where(PublishedPost.account_id == account.id)
    if video_id:
        # Multiple platforms of the same video occupy one content slot.
        query = query.where(PublishedPost.video_id != video_id)
    last = session.scalar(query)
    due = max(now, last + timedelta(hours=interval_hours(account))) if last else now
    anchor = (account.settings or {}).get("publish_not_before")
    if anchor:
        due = max(due, datetime.fromisoformat(anchor.replace("Z", "+00:00")))
    return due


def pending_video(session, account_id):
    # Unknown/failed publications require review before releasing the next video.
    return session.scalar(select(PublishJob.video_id).where(
        PublishJob.account_id == account_id,
        PublishJob.status.not_in(("PUBLISHED", "CANCELLED")),
    ).order_by(PublishJob.created_at, PublishJob.id).limit(1))
