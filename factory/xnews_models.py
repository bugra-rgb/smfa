"""Additive X news storage. Existing video/account tables are never altered."""
from datetime import datetime
from sqlalchemy import Boolean, ForeignKey, Integer, String, Text, UniqueConstraint, Index
from sqlalchemy.orm import Mapped, mapped_column
from factory.models import Base, Identity, Updated, UTCDateTime, json_dict, utcnow

class XNewsAccount(Updated, Base):
    __tablename__ = 'xnews_accounts'
    account_id: Mapped[str] = mapped_column(ForeignKey('accounts.id', ondelete='CASCADE'), primary_key=True)
    news_enabled: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    automation_enabled: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    config: Mapped[dict] = json_dict()
    buffer_channel_id: Mapped[str | None] = mapped_column(String(255),unique=True)
    schedule_version: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    start_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    next_run_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), index=True)
    lease_owner: Mapped[str | None] = mapped_column(String(36))
    lease_until: Mapped[datetime | None] = mapped_column(UTCDateTime())

class XNewsSource(Identity, Updated, Base):
    __tablename__ = 'xnews_sources'
    name: Mapped[str] = mapped_column(String(200))
    url: Mapped[str] = mapped_column(Text)
    kind: Mapped[str] = mapped_column(String(20), default='rss')
    enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    config: Mapped[dict] = json_dict()
    last_fetched_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    last_error: Mapped[str | None] = mapped_column(String(100))

class XNewsStory(Identity, Base):
    __tablename__ = 'xnews_stories'
    source_id: Mapped[str] = mapped_column(ForeignKey('xnews_sources.id', ondelete='RESTRICT'), index=True)
    canonical_url: Mapped[str] = mapped_column(Text, unique=True)
    title: Mapped[str] = mapped_column(Text)
    available_text: Mapped[str] = mapped_column(Text)
    source_published_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), index=True)
    source_updated_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    event_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    retrieved_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow)
    category: Mapped[str] = mapped_column(String(80), default='general')
    language: Mapped[str] = mapped_column(String(16), default='tr')
    turkey_relevance: Mapped[bool] = mapped_column(Boolean, default=False)
    story_cluster_id: Mapped[str] = mapped_column(String(128), index=True)
    data: Mapped[dict] = json_dict()

class XNewsTrendSnapshot(Identity, Base):
    __tablename__ = 'xnews_trend_snapshots'
    provider: Mapped[str] = mapped_column(String(200))
    region: Mapped[str] = mapped_column(String(16), default='TR')
    fetched_at: Mapped[datetime] = mapped_column(UTCDateTime(), index=True)
    provider_timestamp: Mapped[datetime | None] = mapped_column(UTCDateTime())
    data: Mapped[dict] = json_dict()

class XNewsJob(Identity, Updated, Base):
    __tablename__ = 'xnews_jobs'
    __table_args__ = (UniqueConstraint('account_id','content_type','scheduled_slot','schedule_version',name='uq_xnews_job_slot'),
        Index('ix_xnews_dispatch','status','next_attempt_at'))
    account_id: Mapped[str] = mapped_column(ForeignKey('accounts.id',ondelete='CASCADE'), index=True)
    content_type: Mapped[str] = mapped_column(String(20),default='ai_news',nullable=False)
    scheduled_slot: Mapped[datetime] = mapped_column(UTCDateTime())
    schedule_version: Mapped[int] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(32),default='DISCOVERED',index=True)
    next_attempt_at: Mapped[datetime] = mapped_column(UTCDateTime(),default=utcnow)
    payload: Mapped[dict] = json_dict()
    payload_hash: Mapped[str | None] = mapped_column(String(64))
    story_id: Mapped[str | None] = mapped_column(ForeignKey('xnews_stories.id',ondelete='RESTRICT'))
    trend_id: Mapped[str | None] = mapped_column(ForeignKey('xnews_trend_snapshots.id',ondelete='RESTRICT'))
    buffer_post_id: Mapped[str | None] = mapped_column(String(255),unique=True)
    buffer_binding: Mapped[dict] = json_dict()
    submit_started_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    accepted_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    published_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    published_url: Mapped[str | None] = mapped_column(Text)
    last_checked_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    poll_count: Mapped[int] = mapped_column(Integer,default=0)
    reconcile_count: Mapped[int] = mapped_column(Integer,default=0)
    attempts: Mapped[int] = mapped_column(Integer,default=0)
    error_code: Mapped[str | None] = mapped_column(String(100))
    error_message: Mapped[str | None] = mapped_column(Text)
    cancel_requested: Mapped[bool] = mapped_column(Boolean,default=False)
    cancellation_checked: Mapped[bool] = mapped_column(Boolean,default=False)
    lease_owner: Mapped[str | None] = mapped_column(String(36))
    lease_until: Mapped[datetime | None] = mapped_column(UTCDateTime())
    audit: Mapped[dict] = json_dict()

class XNewsBudget(Base):
    __tablename__ = 'xnews_budgets'
    key: Mapped[str] = mapped_column(String(160),primary_key=True)
    used: Mapped[int] = mapped_column(Integer,default=0,nullable=False)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime(),default=utcnow)

class XNewsDedup(Base):
    __tablename__ = 'xnews_dedup'
    fingerprint: Mapped[str] = mapped_column(String(160),primary_key=True)
    job_id: Mapped[str] = mapped_column(ForeignKey('xnews_jobs.id',ondelete='CASCADE'),index=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(),default=utcnow)


def migrate(engine):
    """Only this feature's tables, never Base.metadata.create_all without subset."""
    from sqlalchemy import text
    tables = [t for t in Base.metadata.sorted_tables if t.name.startswith('xnews_')]
    with engine.begin() as conn:
        if conn.dialect.name == 'postgresql':
            conn.execute(text("SELECT pg_advisory_xact_lock(724920260930)"))
        Base.metadata.create_all(conn, tables=tables, checkfirst=True)

class XNewsConfig(Updated, Base):
    __tablename__ = 'xnews_config'
    id: Mapped[str] = mapped_column(String(32),primary_key=True,default='global')
    config: Mapped[dict] = json_dict()
    encrypted_secrets: Mapped[str | None] = mapped_column(Text)
    revision: Mapped[int] = mapped_column(Integer,default=1)
