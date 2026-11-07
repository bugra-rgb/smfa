"""Read-only account status; a planned slot is not evidence of a runnable publication."""
from sqlalchemy import select

from factory.models import Job, PublishJob, PublishedPost, Video


REVIEW_STATES = ('UNKNOWN', 'BLOCKED', 'POLICY_BLOCKED', 'FAILED')


def publication_status(session, account, *, blockers=(), paused=False, planned_at=None, manual=False):
    pending = session.scalars(select(PublishJob).where(
        PublishJob.account_id == account.id,
        PublishJob.status.not_in(('PUBLISHED', 'CANCELLED')),
    ).order_by(PublishJob.scheduled_at, PublishJob.id)).all()
    review = next((p for p in pending if p.status in REVIEW_STATES), None)
    current = review or next((p for p in pending if p.status in ('PUBLISHING', 'RETRYING')), None)
    current = current or (pending[0] if pending else None)
    result = {'publication_state': 'waiting_content', 'next_at': None,
              'review_required': bool(review), 'pending_status': current.status if current else None,
              'pending_publication_id': current.id if current else None}
    if review:
        result['publication_state'] = 'review_required'
    elif paused or not account.active:
        result['publication_state'] = 'paused'
    elif blockers:
        result['publication_state'] = 'connection_required'
    else:
        # Mirror the existing global admission hold, without releasing or changing it.
        # UNKNOWN means a prior external side effect has not been reconciled.
        hold = session.scalar(select(PublishJob).join(Job,
            Job.payload['publish_job_id'].as_string() == PublishJob.id).where(
            Job.kind == 'PUBLISH', Job.result['cycle_admitted'].as_boolean().is_(True),
            PublishJob.account_id != account.id, PublishJob.status == 'UNKNOWN',
        ).order_by(PublishJob.created_at, PublishJob.id).limit(1)) if (account.settings or {}).get('cycle_v2') and not manual else None
        if hold:
            result.update(publication_state='global_review_required',
                          blocking_account_id=hold.account_id, blocking_publication_id=hold.id)
            return result
        return _available_status(session, account, current, result, planned_at, manual)
    return result


def _available_status(session, account, current, result, planned_at, manual):
    if current:
        result['publication_state'] = 'scheduled' if current.status == 'SCHEDULED' else 'publishing'
        if current.status == 'SCHEDULED':
            result['next_at'] = current.scheduled_at
    else:
        ready = session.scalar(select(Video.id).where(
            Video.account_id == account.id,
            Video.status.in_(('READY', 'AWAITING_APPROVAL')),
            Video.qa['passed'].as_boolean().is_(True),
            ~Video.id.in_(select(PublishJob.video_id)),
            ~Video.id.in_(select(PublishedPost.video_id)),
        ).limit(1))
        if ready:
            result['publication_state'] = 'ready'
            result['next_at'] = None if manual else planned_at
    return result
