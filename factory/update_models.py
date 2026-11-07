"""Additive feature tables; existing account/token/job rows keep their schema."""
from sqlalchemy import Boolean, Float, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column
from factory.models import Base, Identity, AccountRef, UTCDateTime, json_dict, utcnow
from datetime import datetime

class ReelsCoverAsset(Identity, AccountRef, Base):
    __tablename__ = 'reels_cover_assets'
    storage_key: Mapped[str] = mapped_column(String(200), unique=True)
    checksum: Mapped[str] = mapped_column(String(64))
    bytes_count: Mapped[int] = mapped_column(Integer)
    width: Mapped[int] = mapped_column(Integer)
    height: Mapped[int] = mapped_column(Integer)

class ReelsCoverSetting(Base):
    __tablename__ = 'reels_cover_settings'
    account_id: Mapped[str] = mapped_column(ForeignKey('accounts.id', ondelete='CASCADE'), primary_key=True)
    asset_id: Mapped[str | None] = mapped_column(ForeignKey('reels_cover_assets.id', ondelete='RESTRICT'))
    enabled: Mapped[bool] = mapped_column(Boolean, default=False, server_default='false')
    revision: Mapped[int] = mapped_column(Integer, default=0, server_default='0')
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow)

class PublicationCover(Base):
    __tablename__ = 'publication_covers'
    publication_id: Mapped[str] = mapped_column(ForeignKey('publishing_jobs.id', ondelete='CASCADE'), primary_key=True)
    mode: Mapped[str] = mapped_column(String(20), default='default')
    asset_id: Mapped[str | None] = mapped_column(ForeignKey('reels_cover_assets.id', ondelete='RESTRICT'))
    revision: Mapped[int] = mapped_column(Integer, default=0)
    frozen_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    sent_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    # Only an explicit human verification may fill this; never from a POST response.
    visually_verified_at: Mapped[datetime | None] = mapped_column(UTCDateTime())

class InsightMeasure(Identity, AccountRef, Base):
    __tablename__ = 'instagram_insight_measures'
    __table_args__ = (UniqueConstraint('cache_key', name='uq_instagram_insight_cache_key'),)
    cache_key: Mapped[str] = mapped_column(String(64))
    metric: Mapped[str] = mapped_column(String(60))
    scope: Mapped[str] = mapped_column(String(40))
    period_start: Mapped[datetime] = mapped_column(UTCDateTime(), index=True)
    period_end: Mapped[datetime] = mapped_column(UTCDateTime())
    value: Mapped[float | None] = mapped_column(Float)
    status: Mapped[str] = mapped_column(String(40))
    fetched_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow)
    last_attempt_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow)
    last_error: Mapped[str | None] = mapped_column(String(100))
    provenance: Mapped[dict] = json_dict()

class InstagramCapability(Base):
    __tablename__ = 'instagram_capabilities'
    account_id: Mapped[str] = mapped_column(ForeignKey('accounts.id', ondelete='CASCADE'), primary_key=True)
    identity: Mapped[str] = mapped_column(String(40), default='unknown')
    publishing: Mapped[str] = mapped_column(String(40), default='unknown')
    insights: Mapped[str] = mapped_column(String(40), default='unknown')
    checked_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow)
    expires_at: Mapped[str | None] = mapped_column(String(80))
    context: Mapped[dict] = json_dict()
