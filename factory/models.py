"""İçerik fabrikasının kalıcı veri modeli.

Durumlar uygulama durum makinelerine aittir; ilişkiler ve tekrar önleme kuralları
veritabanında uygulanır. Kimlikler taşınabilir UUID dizeleri, tarihler UTC'dir.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import (
    JSON, Boolean, CheckConstraint, DateTime, Float, ForeignKey, Index,
    Integer, MetaData, Numeric, String, Text, UniqueConstraint,
)
from sqlalchemy.ext.mutable import MutableDict, MutableList
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column
from sqlalchemy.types import TypeDecorator


def new_id() -> str:
    return str(uuid.uuid4())


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class UTCDateTime(TypeDecorator):
    """SQLite testleri dahil tüm okumalarda timezone-aware UTC döndürür."""
    impl = DateTime(timezone=True)
    cache_ok = True

    def process_bind_param(self, value, dialect):
        if value is None:
            return None
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)

    def process_result_value(self, value, dialect):
        if value is None:
            return None
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention={
        "ix": "ix_%(table_name)s_%(column_0_name)s",
        "uq": "uq_%(table_name)s_%(column_0_name)s",
        "ck": "ck_%(table_name)s_%(constraint_name)s",
        "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
        "pk": "pk_%(table_name)s",
    })


class Identity:
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow, nullable=False)


class Updated:
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow, onupdate=utcnow, nullable=False)


class AccountRef:
    account_id: Mapped[str] = mapped_column(ForeignKey("accounts.id", ondelete="CASCADE"), index=True)


def json_dict():
    return mapped_column(MutableDict.as_mutable(JSON), default=dict, nullable=False)


def json_list():
    return mapped_column(MutableList.as_mutable(JSON), default=list, nullable=False)


class User(Identity, Base):
    __tablename__ = "users"
    email: Mapped[str] = mapped_column(String(320), unique=True)
    password_hash: Mapped[str] = mapped_column(Text)
    role: Mapped[str] = mapped_column(String(32), default="admin")
    active: Mapped[bool] = mapped_column(Boolean, default=True)


class Account(Identity, Updated, Base):
    __tablename__ = "accounts"
    __table_args__ = (CheckConstraint("daily_target >= 0", name="daily_target_nonnegative"),)
    name: Mapped[str] = mapped_column(String(200))
    username: Mapped[str] = mapped_column(String(200), default="")
    niche: Mapped[str] = mapped_column(String(500), default="")
    language: Mapped[str] = mapped_column(String(40), default="tr")
    target_audience: Mapped[str] = mapped_column(Text, default="")
    content_style: Mapped[str] = mapped_column(String(200), default="")
    tone: Mapped[str] = mapped_column(String(200), default="")
    daily_target: Mapped[int] = mapped_column(Integer, default=3)
    timezone: Mapped[str] = mapped_column(String(100), default="Europe/Istanbul")
    active: Mapped[bool] = mapped_column(Boolean, default=True, index=True)
    auto_publish: Mapped[bool] = mapped_column(Boolean, default=False)
    approval_mode: Mapped[str] = mapped_column(String(32), default="MANUAL_APPROVAL")
    source_mode: Mapped[str] = mapped_column(String(32), default="AI_ONLY")
    settings: Mapped[dict[str, Any]] = json_dict()


class AccountSetting(Identity, AccountRef, Updated, Base):
    """İsteğe bağlı sürümlü ayar kaydı; geçerli ayarlar Account.settings içindedir."""
    __tablename__ = "account_settings"
    __table_args__ = (UniqueConstraint("account_id", "key", name="uq_account_settings_key"),)
    key: Mapped[str] = mapped_column(String(100))
    value: Mapped[dict[str, Any]] = json_dict()


class Connection(Identity, AccountRef, Base):
    __tablename__ = "platform_connections"
    __table_args__ = (UniqueConstraint("account_id", "platform", name="uq_connection_account_platform"),)
    platform: Mapped[str] = mapped_column(String(32))
    external_id: Mapped[str | None] = mapped_column(String(255))
    status: Mapped[str] = mapped_column(String(32), default="disconnected")
    scopes: Mapped[list[str]] = json_list()
    settings: Mapped[dict[str, Any]] = json_dict()


class OAuthToken(Identity, Updated, Base):
    __tablename__ = "oauth_tokens"
    connection_id: Mapped[str] = mapped_column(ForeignKey("platform_connections.id", ondelete="CASCADE"), unique=True)
    encrypted_payload: Mapped[str] = mapped_column(Text)


class DriveConnection(Identity, AccountRef, Base):
    __tablename__ = "drive_connections"
    __table_args__ = (UniqueConstraint("account_id", name="uq_drive_connection_account"),)
    connection_id: Mapped[str] = mapped_column(ForeignKey("platform_connections.id", ondelete="CASCADE"))
    last_sync: Mapped[datetime | None] = mapped_column(UTCDateTime())
    cursor: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(32), default="disconnected")
    watch_channel_id: Mapped[str | None] = mapped_column(String(255))
    watch_resource_id: Mapped[str | None] = mapped_column(String(255))
    watch_expires_at: Mapped[datetime | None] = mapped_column(UTCDateTime())


class DriveFolder(Identity, AccountRef, Base):
    __tablename__ = "drive_folders"
    __table_args__ = (UniqueConstraint("account_id", "kind", name="uq_drive_folder_account_kind"),)
    kind: Mapped[str] = mapped_column(String(32))
    external_id: Mapped[str] = mapped_column(String(255))


class DriveFile(Identity, AccountRef, Base):
    __tablename__ = "drive_files"
    drive_file_id: Mapped[str] = mapped_column(String(255), unique=True)
    folder_id: Mapped[str | None] = mapped_column(ForeignKey("drive_folders.id", ondelete="SET NULL"))
    filename: Mapped[str] = mapped_column(String(512))
    source_type: Mapped[str] = mapped_column(String(32), default="DRIVE")
    processing_status: Mapped[str] = mapped_column(String(32), default="PENDING", index=True)
    publishing_status: Mapped[str] = mapped_column(String(32), default="PENDING")
    video_id: Mapped[str | None] = mapped_column(ForeignKey("videos.id", ondelete="SET NULL", use_alter=True, name="fk_drive_files_video_id_videos"))
    published_at: Mapped[datetime | None] = mapped_column(UTCDateTime())


class Idea(Identity, AccountRef, Base):
    __tablename__ = "content_ideas"
    __table_args__ = (
        UniqueConstraint("account_id", "fingerprint", name="uq_idea_account_fingerprint"),
        Index("ix_ideas_selection", "account_id", "status", "score"),
    )
    title: Mapped[str] = mapped_column(String(500))
    hook: Mapped[str] = mapped_column(Text, default="")
    topic: Mapped[str] = mapped_column(String(255), default="")
    summary: Mapped[str] = mapped_column(Text, default="")
    score: Mapped[float] = mapped_column(Float, default=0)
    fingerprint: Mapped[str | None] = mapped_column(String(128))
    status: Mapped[str] = mapped_column(String(32), default="PENDING")
    data: Mapped[dict[str, Any]] = json_dict()


class Script(Identity, AccountRef, Base):
    __tablename__ = "scripts"
    idea_id: Mapped[str | None] = mapped_column(ForeignKey("content_ideas.id", ondelete="SET NULL"), index=True)
    text: Mapped[str] = mapped_column(Text)
    data: Mapped[dict[str, Any]] = json_dict()


class Asset(Identity, AccountRef, Base):
    __tablename__ = "assets"
    video_id: Mapped[str | None] = mapped_column(ForeignKey("videos.id", ondelete="CASCADE"), index=True)
    kind: Mapped[str] = mapped_column(String(32))
    provider: Mapped[str | None] = mapped_column(String(100))
    local_path: Mapped[str | None] = mapped_column(Text)
    storage_key: Mapped[str | None] = mapped_column(Text)
    checksum: Mapped[str | None] = mapped_column(String(128))
    metadata_json: Mapped[dict[str, Any]] = json_dict()


class Video(Identity, AccountRef, Updated, Base):
    __tablename__ = "videos"
    __table_args__ = (
        UniqueConstraint("account_id", "fingerprint", name="uq_video_account_fingerprint"),
        Index("ix_video_inventory", "account_id", "status", "created_at"),
    )
    idea_id: Mapped[str | None] = mapped_column(ForeignKey("content_ideas.id", ondelete="SET NULL"))
    script_id: Mapped[str | None] = mapped_column(ForeignKey("scripts.id", ondelete="SET NULL"))
    title: Mapped[str] = mapped_column(String(500), default="")
    status: Mapped[str] = mapped_column(String(32), default="DRAFT")
    source_type: Mapped[str] = mapped_column(String(32), default="AI")
    source_file_id: Mapped[str | None] = mapped_column(ForeignKey("drive_files.id", ondelete="SET NULL"))
    local_path: Mapped[str | None] = mapped_column(Text)
    drive_file_id: Mapped[str | None] = mapped_column(String(255))
    fingerprint: Mapped[str | None] = mapped_column(String(128))
    qa: Mapped[dict[str, Any]] = json_dict()
    metadata_json: Mapped[dict[str, Any]] = json_dict()


class VideoVariant(Identity, AccountRef, Base):
    __tablename__ = "video_variants"
    __table_args__ = (UniqueConstraint("video_id", "platform", "variant", name="uq_video_variant_platform"),)
    video_id: Mapped[str] = mapped_column(ForeignKey("videos.id", ondelete="CASCADE"), index=True)
    platform: Mapped[str] = mapped_column(String(32))
    variant: Mapped[str] = mapped_column(String(32), default="default")
    local_path: Mapped[str] = mapped_column(Text)
    width: Mapped[int] = mapped_column(Integer)
    height: Mapped[int] = mapped_column(Integer)
    duration: Mapped[float] = mapped_column(Float)
    metadata_json: Mapped[dict[str, Any]] = json_dict()


class Job(Identity, AccountRef, Base):
    __tablename__ = "processing_jobs"
    __table_args__ = (
        CheckConstraint("attempts >= 0", name="attempts_nonnegative"),
        CheckConstraint("max_attempts > 0", name="max_attempts_positive"),
        Index("ix_jobs_dispatch", "status", "run_after", "priority"),
        Index("ix_jobs_lease", "status", "lease_until"),
    )
    video_id: Mapped[str | None] = mapped_column(ForeignKey("videos.id", ondelete="SET NULL"), index=True)
    kind: Mapped[str] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(32), default="QUEUED")
    priority: Mapped[int] = mapped_column(Integer, default=0)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    max_attempts: Mapped[int] = mapped_column(Integer, default=3)
    idempotency_key: Mapped[str] = mapped_column(String(512), unique=True)
    payload: Mapped[dict[str, Any]] = json_dict()
    result: Mapped[dict[str, Any]] = json_dict()
    error_message: Mapped[str | None] = mapped_column(Text)
    run_after: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow)
    started_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    completed_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    lease_until: Mapped[datetime | None] = mapped_column(UTCDateTime())
    lease_owner: Mapped[str | None] = mapped_column(String(128))


class RenderJob(Identity, AccountRef, Updated, Base):
    __tablename__ = "render_jobs"
    job_id: Mapped[str] = mapped_column(ForeignKey("processing_jobs.id", ondelete="CASCADE"), unique=True)
    video_id: Mapped[str] = mapped_column(ForeignKey("videos.id", ondelete="CASCADE"), index=True)
    status: Mapped[str] = mapped_column(String(32), default="PENDING")
    settings: Mapped[dict[str, Any]] = json_dict()
    output_path: Mapped[str | None] = mapped_column(Text)
    error_message: Mapped[str | None] = mapped_column(Text)


class PublishJob(Identity, AccountRef, Updated, Base):
    __tablename__ = "publishing_jobs"
    __table_args__ = (
        UniqueConstraint("video_id", "connection_id", name="uq_publish_video_connection"),
        Index("ix_publish_dispatch", "status", "scheduled_at"),
        CheckConstraint("attempts >= 0", name="attempts_nonnegative"),
    )
    video_id: Mapped[str] = mapped_column(ForeignKey("videos.id", ondelete="CASCADE"), index=True)
    connection_id: Mapped[str] = mapped_column(ForeignKey("platform_connections.id", ondelete="CASCADE"))
    platform: Mapped[str] = mapped_column(String(32))
    status: Mapped[str] = mapped_column(String(32), default="SCHEDULED")
    scheduled_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow)
    idempotency_key: Mapped[str] = mapped_column(String(512), unique=True)
    remote_id: Mapped[str | None] = mapped_column(String(512))
    checkpoint: Mapped[dict[str, Any]] = json_dict()
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    error_message: Mapped[str | None] = mapped_column(Text)


class PublishedPost(Identity, AccountRef, Base):
    __tablename__ = "published_posts"
    __table_args__ = (
        UniqueConstraint("video_id", "connection_id", name="uq_published_video_connection"),
        UniqueConstraint("platform", "remote_id", name="uq_published_remote"),
    )
    video_id: Mapped[str] = mapped_column(ForeignKey("videos.id", ondelete="CASCADE"), index=True)
    connection_id: Mapped[str] = mapped_column(ForeignKey("platform_connections.id", ondelete="CASCADE"))
    platform: Mapped[str] = mapped_column(String(32))
    remote_id: Mapped[str] = mapped_column(String(512))
    url: Mapped[str | None] = mapped_column(Text)
    published_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow, index=True)


class Analytics(Identity, AccountRef, Base):
    __tablename__ = "analytics"
    __table_args__ = (UniqueConstraint("post_id", "bucket_at", name="uq_analytics_post_bucket"),)
    post_id: Mapped[str] = mapped_column(ForeignKey("published_posts.id", ondelete="CASCADE"), index=True)
    bucket_at: Mapped[datetime] = mapped_column(UTCDateTime())
    metrics: Mapped[dict[str, Any]] = json_dict()


class ContentPerformance(Identity, AccountRef, Updated, Base):
    __tablename__ = "content_performance"
    video_id: Mapped[str] = mapped_column(ForeignKey("videos.id", ondelete="CASCADE"), unique=True)
    score: Mapped[float] = mapped_column(Float, default=0)
    topic: Mapped[str] = mapped_column(String(255), default="")
    hook: Mapped[str] = mapped_column(Text, default="")
    metrics: Mapped[dict[str, Any]] = json_dict()


class Schedule(Identity, AccountRef, Updated, Base):
    __tablename__ = "schedules"
    __table_args__ = (UniqueConstraint("account_id", "local_time", name="uq_schedule_account_time"),)
    local_time: Mapped[str] = mapped_column(String(5))
    timezone: Mapped[str] = mapped_column(String(100), default="Europe/Istanbul")
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    weekdays: Mapped[list[int]] = json_list()


class AIRun(Identity, AccountRef, Base):
    __tablename__ = "ai_runs"
    __table_args__ = (Index("ix_ai_runs_cost_day", "account_id", "created_at"),)
    provider: Mapped[str] = mapped_column(String(100))
    model: Mapped[str] = mapped_column(String(200), default="")
    operation: Mapped[str] = mapped_column(String(100))
    input_tokens: Mapped[int] = mapped_column(Integer, default=0)
    output_tokens: Mapped[int] = mapped_column(Integer, default=0)
    estimated_cost: Mapped[float | None] = mapped_column(Numeric(18, 8), nullable=True)
    actual_cost: Mapped[float | None] = mapped_column(Numeric(18, 8))
    platform: Mapped[str | None] = mapped_column(String(32))


class ProviderConfig(Identity, Updated, Base):
    __tablename__ = "provider_configs"
    __table_args__ = (UniqueConstraint("kind", "name", name="uq_provider_kind_name"),)
    name: Mapped[str] = mapped_column(String(100))
    kind: Mapped[str] = mapped_column(String(32))
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    model: Mapped[str | None] = mapped_column(String(200))
    settings: Mapped[dict[str, Any]] = json_dict()
    secret_env_name: Mapped[str | None] = mapped_column(String(100))


class SystemState(Base):
    __tablename__ = "system_state"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default="global")
    paused: Mapped[bool] = mapped_column(Boolean, default=False)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow, onupdate=utcnow)


class SystemLog(Identity, Base):
    __tablename__ = "system_logs"
    __table_args__ = (Index("ix_logs_account_time", "account_id", "created_at"),)
    account_id: Mapped[str | None] = mapped_column(ForeignKey("accounts.id", ondelete="SET NULL"))
    level: Mapped[str] = mapped_column(String(16), default="info")
    event: Mapped[str] = mapped_column(String(100))
    message: Mapped[str] = mapped_column(Text)
    data: Mapped[dict[str, Any]] = json_dict()


class FailedJob(Identity, AccountRef, Base):
    __tablename__ = "failed_jobs"
    job_id: Mapped[str | None] = mapped_column(ForeignKey("processing_jobs.id", ondelete="SET NULL"), index=True)
    kind: Mapped[str] = mapped_column(String(64))
    error_message: Mapped[str] = mapped_column(Text)
    payload: Mapped[dict[str, Any]] = json_dict()
    resolved_at: Mapped[datetime | None] = mapped_column(UTCDateTime())


class Notification(Identity, Base):
    __tablename__ = "notifications"
    account_id: Mapped[str | None] = mapped_column(ForeignKey("accounts.id", ondelete="CASCADE"), index=True)
    kind: Mapped[str] = mapped_column(String(64))
    message: Mapped[str] = mapped_column(Text)
    read: Mapped[bool] = mapped_column(Boolean, default=False, index=True)


class AuthSession(Identity, Base):
    __tablename__ = "sessions"
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    token_hash: Mapped[str] = mapped_column(String(128), unique=True)
    csrf_token: Mapped[str] = mapped_column(String(128))
    expires_at: Mapped[datetime] = mapped_column(UTCDateTime(), index=True)


class OAuthState(Identity, Base):
    __tablename__ = "oauth_states"
    state_hash: Mapped[str] = mapped_column(String(128), unique=True)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    account_id: Mapped[str] = mapped_column(ForeignKey("accounts.id", ondelete="CASCADE"), index=True)
    provider: Mapped[str] = mapped_column(String(32))
    encrypted_verifier: Mapped[str | None] = mapped_column(Text)
    expires_at: Mapped[datetime] = mapped_column(UTCDateTime(), index=True)
    used: Mapped[bool] = mapped_column(Boolean, default=False)
