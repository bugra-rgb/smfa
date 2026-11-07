"""PostgreSQL is the job authority; Redis is only a replaceable delivery mechanism."""
from __future__ import annotations

import logging
import random
import re
from datetime import datetime, timedelta, timezone
from uuid import uuid4

from sqlalchemy import or_, select, update
from sqlalchemy.exc import IntegrityError

from factory.config import settings
from factory.db import SessionLocal
from factory.models import Account, Connection, DriveConnection, DriveFile, FailedJob, Job, Notification, PublishJob, PublishedPost, SystemLog, SystemState, Video

logger = logging.getLogger(__name__)
READY_STATES = ("QUEUED", "RETRYING")
TERMINAL_STATES = ("SUCCEEDED", "FAILED", "DLQ", "BLOCKED", "UNKNOWN", "CANCELLED")
# A lease may not expire while its worker is still within the configured hard limit.
LEASE_SECONDS = settings.job_timeout + 300


def utcnow():
    return datetime.now(timezone.utc)


def aware(value):
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


class AccountPaused(Exception):
    pass


class LeaseLost(Exception):
    pass


class RetryLater(Exception):
    def __init__(self, message, seconds=60, consume_attempt=False):
        super().__init__(message)
        self.seconds = seconds
        self.consume_attempt = consume_attempt


class PermanentFailure(Exception):
    pass


class UncertainEffect(Exception):
    pass


def log_event(session, event, message, account_id=None, level="INFO", data=None):
    session.add(SystemLog(account_id=account_id, level=level, event=event, message=message, data=data or {}))
    logger.log(getattr(logging, level, logging.INFO), "%s: %s", event, message)


def safe_error(exc):
    from factory.config import settings
    trusted = isinstance(exc, (AccountPaused, LeaseLost, RetryLater, PermanentFailure, UncertainEffect)) or type(exc).__module__.startswith("factory.")
    message = str(exc) if trusted else f"İşlem başarısız: {type(exc).__name__}. Bağlantı ve sağlayıcı durumunu kontrol edin."
    message = re.sub(r"https?://\S+", "[gizli bağlantı]", message)
    for key, value in vars(settings).items():
        if any(word in key for word in ("key", "secret", "password", "database_url", "redis_url")) and isinstance(value, str) and len(value) >= 6:
            message = message.replace(value, "[gizli]")
    return message[:1800]


def assert_running(session, account_id):
    """Refresh flags immediately before each outbound effect, including recovery calls."""
    state = session.get(SystemState, "global", populate_existing=True)
    account = session.get(Account, account_id, populate_existing=True) if account_id else None
    if (state and state.paused) or (account_id and (not account or not account.active or (account.settings or {}).get("smfa_archived_v1"))):
        raise AccountPaused("Hesap veya sistem duraklatıldı.")
    return account


def enqueue_job(session, kind, account_id=None, video_id=None, payload=None,
                idempotency_key=None, run_after=None, priority=0, max_attempts=5):
    kind = kind.upper()
    key = idempotency_key or f"{kind}:{account_id}:{uuid4()}"
    existing = session.scalar(select(Job).where(Job.idempotency_key == key))
    if existing:
        return existing
    job = Job(account_id=account_id, video_id=video_id, kind=kind, status="QUEUED",
              payload=payload or {}, result={}, priority=priority, attempts=0,
              max_attempts=max_attempts, idempotency_key=key, run_after=run_after or utcnow())
    try:
        with session.begin_nested():
            session.add(job)
            session.flush()
    except IntegrityError:
        return session.scalar(select(Job).where(Job.idempotency_key == key))
    return job


def claim_job(session, job_id, now=None):
    now = now or utcnow()
    token = str(uuid4())
    # Atomic compare-and-set also protects SQLite tests. PostgreSQL serializes updates.
    result = session.execute(update(Job).where(
        Job.id == job_id,
        or_(Job.status.in_(READY_STATES),
            (Job.status == "RUNNING") & (Job.lease_until < now)),
        or_(Job.run_after.is_(None), Job.run_after <= now),
    ).values(status="RUNNING", lease_until=now + timedelta(seconds=LEASE_SECONDS),
             started_at=now, lease_owner=token, attempts=Job.attempts + 1).execution_options(synchronize_session=False))
    if result.rowcount != 1:
        session.rollback()
        return None
    job = session.get(Job, job_id, populate_existing=True)
    result_data = dict(job.result or {})
    result_data["_lease_token"] = token
    job.result = result_data
    session.commit()
    return job


class JobContext:
    def __init__(self, session, job):
        self.session = session
        self.job = job
        self.token = job.lease_owner

    def guard(self):
        self.check_lease()
        if self.job.kind in ("AI_IDEAS", "CONNECT_DRIVE_LIBRARY", "INSTAGRAM_INSIGHTS"):
            assert_running(self.session, None)
            account = self.session.get(Account, self.job.account_id)
            if not account:
                raise PermanentFailure("Hesap bulunamadı.")
            return account
        return assert_running(self.session, self.job.account_id)

    def check_lease(self):
        self.session.refresh(self.job)
        if self.job.status != "RUNNING" or self.job.lease_owner != self.token:
            raise LeaseLost("İşin çalışma kilidi başka işçiye devredildi.")
        if self.job.lease_until and aware(self.job.lease_until) <= utcnow():
            raise LeaseLost("İşin çalışma kilidi zaman aşımına uğradı.")

    def checkpoint(self, **changes):
        self.check_lease()
        self.job.result = {**(self.job.result or {}), **changes}
        self.session.commit()
        # An in-flight result must be persisted even if pause was pressed mid-call.
        # Checking after commit still gates callbacks made before a network mutation.
        self.guard()

    def external(self, func, *args, **kwargs):
        self.guard()
        from factory.rate_control import provider_slot
        owner = getattr(func, "__self__", None)
        if owner is not None and type(owner).__module__.startswith("factory.providers"):
            with provider_slot(type(owner).__name__):
                return func(*args, **kwargs)
        return func(*args, **kwargs)



def isolated_drive_quality_failure(session, job, exc):
    """Only a persisted, unshared, rejected Drive video is safe to skip."""
    if job.kind != "DRIVE_PIPELINE" or not isinstance(exc, PermanentFailure):
        return False
    if not str(exc).startswith("Kalite kontrolü başarısız:") or not job.video_id:
        return False
    video = session.get(Video, job.video_id)
    if (not video or video.account_id != job.account_id or video.source_type != "DRIVE"
            or video.status != "FAILED" or (video.qa or {}).get("passed") is not False):
        return False
    if not (video.qa or {}).get("errors") or not video.source_file_id:
        return False
    source = session.get(DriveFile, video.source_file_id)
    if not source or source.account_id != job.account_id:
        return False
    from factory.services.drive_repeat import FLAG
    replay = (job.payload or {}).get(FLAG) or {}
    if source.video_id != video.id and not (
            isinstance(replay, dict) and replay.get("source_id") == source.id
            and video.fingerprint == job.idempotency_key
            and job.idempotency_key.startswith("drive-repeat:")):
        return False
    result = job.result or {}
    if result.get("qa_passed") or result.get("drive_checkpoint") or result.get("video_checkpoint"):
        return False
    if session.scalar(select(PublishJob.id).where(PublishJob.video_id == video.id).limit(1)):
        return False
    if session.scalar(select(PublishedPost.id).where(PublishedPost.video_id == video.id).limit(1)):
        return False
    return True


def retry_delay(attempt, retry_after=None):
    if retry_after is not None:
        try:
            return max(1, min(float(retry_after), 86400))
        except (TypeError, ValueError):
            pass
    return min(3600, 30 * (2 ** min(max(attempt - 1, 0), 7))) + random.uniform(0, 5)


def process_job(job_id):
    from factory.services.pipeline import HANDLERS

    with SessionLocal() as session:
        job = claim_job(session, job_id)
        if not job:
            return {"status": "SKIPPED", "job_id": job_id}
        ctx = JobContext(session, job)
        try:
            ctx.guard()
            if job.attempts > job.max_attempts:
                raise RuntimeError("İşçi tekrar başlatma sınırına ulaşıldı.")
            handler = HANDLERS.get(job.kind.upper())
            if not handler:
                raise PermanentFailure(f"Bilinmeyen iş türü: {job.kind}")
            outcome = handler(ctx) or {}
            ctx.guard()
            job.result = {**(job.result or {}), **outcome}
            job.status = "SUCCEEDED"
            job.error_message = None
            job.completed_at = utcnow()
            job.lease_until = None
            log_event(session, "job.completed", "İş tamamlandı.", job.account_id, data={"job_id": job.id, "kind": job.kind})
            session.commit()
        except LeaseLost:
            session.rollback()
            return {"status": "LEASE_LOST", "job_id": job_id}
        except Exception as exc:
            session.rollback()
            job = session.get(Job, job_id, populate_existing=True)
            if job is None:
                return {"status": "DELETED", "job_id": job_id}
            if job.lease_owner != ctx.token:
                return {"status": "LEASE_LOST", "job_id": job_id}
            # Persist only sanitized provider messages; never log credential values.
            message = safe_error(exc)
            code = str(getattr(exc, "code", ""))
            uncertain = isinstance(exc, UncertainEffect) or bool(getattr(exc, "uncertain", False))
            retryable = getattr(exc, "retryable", True)
            if isinstance(exc, AccountPaused):
                job.status = "RETRYING"
                job.attempts = max(0, job.attempts - 1)
                job.run_after = utcnow() + timedelta(seconds=60)
            elif isinstance(exc, RetryLater):
                if exc.consume_attempt and job.attempts >= job.max_attempts:
                    job.status = "DLQ"
                    session.add(FailedJob(account_id=job.account_id, job_id=job.id,
                                          kind=job.kind, error_message=message, payload={"job_id": job.id}))
                else:
                    job.status = "RETRYING"
                    if not exc.consume_attempt:
                        job.attempts = max(0, job.attempts - 1)
                    job.run_after = utcnow() + timedelta(seconds=exc.seconds)
            elif uncertain:
                job.status = "UNKNOWN"
                session.add(Notification(account_id=job.account_id, kind="PUBLISH_UNCERTAIN",
                                         message="Dış işlem sonucu belirsiz; yinelenen yayın oluşturmamak için otomatik yeniden deneme durduruldu."))
            elif isolated_drive_quality_failure(session, job, exc):
                job.status = "FAILED"
            elif isinstance(exc, PermanentFailure) or not retryable or code in {"POLICY_BLOCKED", "configuration", "policy_blocked"}:
                job.status = "BLOCKED"
                session.add(Notification(account_id=job.account_id, kind="ACTION_REQUIRED", message=message))
            elif job.attempts >= job.max_attempts:
                job.status = "DLQ"
                session.add(FailedJob(account_id=job.account_id, job_id=job.id,
                                      kind=job.kind, error_message=message, payload={"job_id": job.id}))
                session.add(Notification(account_id=job.account_id, kind="REPEATED_FAILURE", message=message))
            else:
                job.status = "RETRYING"
                job.run_after = utcnow() + timedelta(seconds=retry_delay(job.attempts, getattr(exc, "retry_after", None)))
            job.error_message = message
            job.lease_until = None
            if code in ("oauth_expired", "oauth_missing", "invalid_token") and job.kind not in ("ANALYTICS", "INSTAGRAM_INSIGHTS"):
                connection_id = None
                if job.kind == "PUBLISH":
                    publication = session.get(PublishJob, job.payload.get("publish_job_id"))
                    connection_id = publication.connection_id if publication else None
                elif job.kind == "ANALYTICS":
                    post = session.get(PublishedPost, job.payload.get("post_id"))
                    connection_id = post.connection_id if post else None
                else:
                    connection_id = session.scalar(select(Connection.id).where(
                        Connection.account_id == job.account_id, Connection.platform.in_(("drive", "google"))))
                connection = session.get(Connection, connection_id) if connection_id else None
                if connection:
                    connection.status = "DISCONNECTED"
                    if connection.platform in ("drive", "google"):
                        drive = session.scalar(select(DriveConnection).where(DriveConnection.connection_id == connection.id))
                        if drive:
                            drive.status = "DISCONNECTED"
            if job.status in TERMINAL_STATES:
                job.completed_at = utcnow()
                if job.kind in ("AI_PIPELINE", "DRIVE_PIPELINE", "RERENDER") and job.video_id:
                    video = session.get(Video, job.video_id)
                    if video and video.status not in ("PUBLISHED", "SCHEDULED", "PUBLISHING"):
                        video.status = "NEEDS_REVIEW" if job.status == "UNKNOWN" else "FAILED"
                        if video.source_file_id:
                            source = session.get(DriveFile, video.source_file_id)
                            from factory.services.drive_repeat import FLAG
                            if source and not (job.payload or {}).get(FLAG):
                                source.processing_status = video.status
                                if job.status == "FAILED" and video.source_type == "DRIVE" and (video.qa or {}).get("passed") is False:
                                    source.publishing_status = "NOT_PUBLISHABLE"
                        if job.status != "UNKNOWN":
                            enqueue_job(session, "DRIVE_FAILURE_UPLOAD", account_id=job.account_id,
                                        video_id=video.id, idempotency_key=f"failed-artifact:{job.id}", priority=1)
                if job.kind == "PUBLISH":
                    pub = session.get(PublishJob, job.payload.get("publish_job_id"))
                    if pub and pub.status not in ("PUBLISHED", "CANCELLED", "POLICY_BLOCKED", "UNKNOWN"):
                        pub.status = "FAILED" if job.status == "DLQ" else job.status
                        pub.error_message = message
            log_event(session, "job.error", message, job.account_id, "ERROR", {"job_id": job.id, "kind": job.kind, "status": job.status, "code": code})
            session.commit()
            logger.error("job.failed id=%s exception=%s status=%s", job_id, type(exc).__name__, job.status)
        return {"status": job.status, "job_id": job_id, "error": job.error_message}


def dispatch_pending(limit=200):
    """Reconcile DB outbox every tick. Redeliveries are harmless due to atomic claims."""
    from factory.worker import execute_job

    now = utcnow()
    with SessionLocal() as session:
        state = session.get(SystemState, "global")
        if state and state.paused:
            return 0
        jobs = session.execute(select(Job.id, Job.kind).where(
            or_(Job.status.in_(READY_STATES), (Job.status == "RUNNING") & (Job.lease_until < now)),
            or_(Job.run_after.is_(None), Job.run_after <= now),
        ).order_by(Job.priority.desc(), Job.run_after, Job.created_at).limit(limit)).all()
    sent = 0
    for job_id, kind in jobs:
        try:
            options = {"queue": "smfa_analytics"} if kind == "INSTAGRAM_INSIGHTS" or (kind == "ANALYTICS" and settings.smfa_analytics_enabled) else {}
            execute_job.apply_async(args=[job_id], task_id=str(uuid4()), retry=False, **options)
            sent += 1
        except Exception:
            logger.warning("Broker unavailable; pending jobs remain durable in PostgreSQL")
            break
    return sent
