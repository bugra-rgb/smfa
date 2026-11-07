"""One indexed latest snapshot per published post, without a table-wide self join.

PostgreSQL uses a bounded LATERAL lookup over the existing unique
(post_id, bucket_at) index. Its parallel workers are disabled for this read
transaction only; no database-wide settings or schema are changed.
SQLite uses an equivalent correlated-id query for tests and mock mode.
"""
from sqlalchemy import select, text, true
from sqlalchemy.orm import aliased
from factory.models import Analytics, PublishedPost

VERSION = 'smfa-latest-metrics-20260929.1'


def statement(dialect, published_from=None, published_before=None):
    if dialect == 'postgresql':
        source = (select(Analytics).where(Analytics.post_id == PublishedPost.id)
                  .order_by(Analytics.bucket_at.desc()).limit(1)
                  .correlate(PublishedPost).lateral('latest_snapshot'))
        latest = aliased(Analytics, source)
        query = select(latest, PublishedPost.connection_id).select_from(PublishedPost).join(source, true())
    else:
        candidate = aliased(Analytics)
        latest_id = (select(candidate.id).where(candidate.post_id == PublishedPost.id)
                     .order_by(candidate.bucket_at.desc()).limit(1)
                     .correlate(PublishedPost).scalar_subquery())
        query = select(Analytics, PublishedPost.connection_id).select_from(PublishedPost).join(Analytics, Analytics.id == latest_id)
    if published_from is not None:
        query = query.where(PublishedPost.published_at >= published_from)
    if published_before is not None:
        query = query.where(PublishedPost.published_at < published_before)
    return query


def latest_metrics(session, published_from=None, published_before=None):
    dialect = session.get_bind().dialect.name
    if dialect == 'postgresql':
        # SET LOCAL resets automatically on transaction end. This prevents
        # dynamic shared-memory allocation by a parallel query plan.
        session.execute(text('SET LOCAL max_parallel_workers_per_gather = 0'))
    return session.execute(statement(dialect, published_from, published_before)).all()
