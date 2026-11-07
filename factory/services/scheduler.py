"""Timezone-aware target reservation and bounded inventory replenishment."""
from __future__ import annotations

import logging
import time
from datetime import date, datetime, time as daytime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from sqlalchemy import func, select

from factory.db import SessionLocal
from factory.models import Account, Connection, DriveFile, Job, PublishJob, PublishedPost, SystemState, Video
from factory.services.jobs import assert_running, dispatch_pending, enqueue_job, log_event, safe_error, utcnow
from factory.services.cadence import interval_hours, next_due, pending_video

logger = logging.getLogger(__name__)
INVENTORY_STATES = ("DRAFT", "RESEARCH", "SCRIPTING", "GENERATING", "RENDERING", "QA", "UPLOADING", "READY", "AWAITING_APPROVAL", "SCHEDULED")
ACTIVE_JOB_STATES = ("QUEUED", "RETRYING", "RUNNING")


def local_day_bounds(now, zone):
    tz = ZoneInfo(zone)
    day = now.astimezone(tz).date()
    return (datetime.combine(day, daytime.min, tz).astimezone(timezone.utc),
            datetime.combine(day + timedelta(days=1), daytime.min, tz).astimezone(timezone.utc))


def schedule_slots(day: date, zone: str, configured: list[str], target: int):
    """An autumn repeated hour yields one slot; a missing spring hour shifts forward.

    A short explicit schedule is supplemented evenly to honor the daily target.
    No modulo-24 UTC arithmetic: DST days may be 23 or 25 hours long.
    """
    if target <= 0:
        return []
    tz = ZoneInfo(zone)
    minutes = []
    for item in configured:
        try:
            hour, minute = map(int, item.split(":"))
            if 0 <= hour < 24 and 0 <= minute < 60 and hour * 60 + minute not in minutes:
                minutes.append(hour * 60 + minute)
        except (ValueError, AttributeError):
            continue
    for index in range(target):
        minute = int((index + 0.5) * 1440 / target)
        if minute not in minutes:
            minutes.append(minute)
    slots = set()
    for minute in minutes:
        local = datetime.combine(day, daytime(minute // 60, minute % 60), tz).replace(fold=0)
        as_utc = local.astimezone(timezone.utc)
        roundtrip = as_utc.astimezone(tz)
        # zoneinfo maps nonexistent wall time using the pre-change UTC offset;
        # roundtripping gives the next valid time, preserving its minute component.
        if roundtrip.date() == day:
            slots.add(as_utc)
        if len(slots) >= target:
            break
    return sorted(slots)


def inventory_counts(session, account_id):
    account = session.get(Account, account_id)
    if account and account.source_mode == "DRIVE_ONLY":
        from factory.services.drive_repeat import stock
        return stock(session, account)
    inventory = session.scalar(select(func.count(Video.id)).where(
        Video.account_id == account_id, Video.status.in_(INVENTORY_STATES))) or 0
    unstarted = session.scalar(select(func.count(Job.id)).where(
        Job.account_id == account_id, Job.kind.in_(("AI_PIPELINE", "DRIVE_PIPELINE")),
        Job.status.in_(ACTIVE_JOB_STATES), Job.video_id.is_(None))) or 0
    return inventory + unstarted


def inventory_limits(account):
    config = account.settings or {}
    minimum = max(0, int(config.get("inventory_min", account.daily_target)))
    maximum = max(minimum, int(config.get("inventory_max", max(1, account.daily_target * 3))))
    target = min(maximum, max(minimum, int(config.get("inventory_target", account.daily_target * 2))))
    return minimum, target, maximum


def replenish(session, account, now):
    from factory.services.resilient_hourly import resume_jobs
    resume_jobs(session, account, now)
    from factory.services.drive_repeat import enabled, replenish as repeat_replenish
    if enabled(account):
        return repeat_replenish(session, account, now)
    blocked = session.scalar(select(Job.id).where(Job.account_id == account.id,
        Job.kind.in_(("AI_PIPELINE", "DRIVE_PIPELINE")), Job.status.in_(("BLOCKED", "UNKNOWN"))).limit(1))
    repeated = session.scalar(select(func.count(Job.id)).where(Job.account_id == account.id,
        Job.kind.in_(("AI_PIPELINE", "DRIVE_PIPELINE")), Job.status == "DLQ",
        Job.completed_at >= now - timedelta(hours=1))) or 0
    if blocked or repeated >= 3:
        return 0
    if (account.settings or {}).get('publication_mode') == 'manual':
        from factory.services.planner import open_request
        request = open_request(session, account.id)
        if not request or request.status != 'WAITING_CONTENT': return 0
    minimum, desired, maximum = inventory_limits(account)
    current = inventory_counts(session, account.id)
    if not account.daily_target or current >= minimum or current >= maximum:
        return 0
    amount = max(0, min(desired, maximum) - current)
    made = 0
    candidates = session.scalars(select(DriveFile).where(
        DriveFile.account_id == account.id, DriveFile.source_type == "DRIVE",
        DriveFile.processing_status == "DISCOVERED").order_by(func.random() if (account.settings or {}).get('selection_policy') == 'random_unused' else DriveFile.created_at).limit(amount).with_for_update(skip_locked=True)).all()
    if account.source_mode in ("DRIVE_ONLY", "AI_AND_DRIVE"):
        # Alternating source allocation gives both sources capacity in mixed mode.
        drive_limit = amount if account.source_mode == "DRIVE_ONLY" else max(1, (amount + 1) // 2)
        for source in candidates[:drive_limit]:
            enqueue_job(session, "DRIVE_PIPELINE", account_id=account.id,
                        payload={"drive_file_id": source.id},
                        idempotency_key=f"drive-input:{source.drive_file_id}",
                        priority=80 + min(10, max(0, minimum - current)))
            source.processing_status = "QUEUED"
            made += 1
    if account.source_mode in ("AI_ONLY", "AI_AND_DRIVE"):
        from factory.config import settings
        has_drive = session.scalar(select(Connection.id).where(
            Connection.account_id == account.id, Connection.platform.in_(("google", "drive")), Connection.status.in_(("CONNECTED", "connected"))))
        if settings.mode == "production" and not has_drive:
            return made
        for _ in range(amount - made):
            enqueue_job(session, "AI_PIPELINE", account_id=account.id,
                        priority=50 + min(25, max(0, minimum - current) * 5) + int((account.settings or {}).get("importance", 0)))
            made += 1
    return made


def reserve_publishing(session, account, now):
    from factory.services.drive_repeat import ready_order
    from factory.services.planner import open_request, reserve
    if (account.settings or {}).get('publication_mode') in ('manual','planner') or open_request(session,account.id):
        return reserve(session,account,now)
    if account.daily_target <= 0:
        return 0
    connections = session.scalars(select(Connection).where(
        Connection.account_id == account.id, Connection.platform.in_(("youtube", "instagram", "tiktok")),
        Connection.status.in_(("CONNECTED", "connected")))).all()
    if (account.settings or {}).get("cycle_v2"):
        connections = [c for c in connections if c.platform != "tiktok"]
    if not connections:
        return 0
    if interval_hours(account):
        return reserve_interval(session, account, connections, now)
    start, end = local_day_bounds(now, account.timezone)
    existing = session.execute(select(PublishJob.video_id, PublishJob.scheduled_at).where(
        PublishJob.account_id == account.id, PublishJob.scheduled_at >= start,
        PublishJob.scheduled_at < end, PublishJob.status != "CANCELLED")).all()
    reserved = {row.video_id for row in existing}
    remaining = max(0, account.daily_target - len(reserved))
    if remaining == 0:
        return 0
    occupied = {row.scheduled_at for row in existing}
    config = account.settings or {}
    configured = config.get("schedule") or (config.get("optimization", {}).get("posting_schedule", []) if config.get("optimize_schedule", True) else [])
    slots = [slot for slot in schedule_slots(now.astimezone(ZoneInfo(account.timezone)).date(), account.timezone,
             configured, account.daily_target) if slot not in occupied]
    ready_query = select(Video).where(
        Video.account_id == account.id, Video.status == "READY",
        ~Video.id.in_(select(PublishJob.video_id)),
    )
    if not account.auto_publish or account.approval_mode == "MANUAL_APPROVAL":
        ready_query = ready_query.where(Video.metadata_json["approved"].as_boolean().is_(True))
    ready = session.scalars(ready_query.order_by(ready_order(account), Video.created_at).limit(remaining).with_for_update(skip_locked=True)).all()
    made = 0
    for video, slot in zip(ready, slots[:remaining]):
        if not (video.qa or {}).get("passed"):
            continue
        if not account.auto_publish and not (video.metadata_json or {}).get("approved"):
            continue
        for connection in connections:
            key = f"publish:{video.id}:{connection.id}"
            publishing = PublishJob(account_id=account.id, video_id=video.id, connection_id=connection.id,
                                    platform=connection.platform, status="SCHEDULED", scheduled_at=slot,
                                    idempotency_key=key, checkpoint={})
            session.add(publishing)
            session.flush()
            enqueue_job(session, "PUBLISH", account_id=account.id, video_id=video.id,
                        payload={"publish_job_id": publishing.id}, idempotency_key=key,
                        priority=100, run_after=max(slot, now))
        video.status = "SCHEDULED"
        made += 1
    return made


def reserve_interval(session, account, connections, now):
    from factory.services.drive_repeat import ready_order
    if pending_video(session, account.id):
        return 0
    query = select(Video).where(Video.account_id == account.id, Video.status == "READY",
        ~Video.id.in_(select(PublishJob.video_id)))
    if not account.auto_publish or account.approval_mode == "MANUAL_APPROVAL":
        query = query.where(Video.metadata_json["approved"].as_boolean().is_(True))
    candidates = session.scalars(query.order_by(ready_order(account), Video.created_at).with_for_update(skip_locked=True)).all()
    video = next((v for v in candidates if (v.qa or {}).get("passed")), None)
    if not video:
        return 0
    due = next_due(session, account, now)
    for connection in connections:
        key = f"publish:{video.id}:{connection.id}"
        pub = PublishJob(account_id=account.id, video_id=video.id, connection_id=connection.id,
            platform=connection.platform, status="SCHEDULED", scheduled_at=due,
            idempotency_key=key, checkpoint={})
        session.add(pub)
        session.flush()
        enqueue_job(session, "PUBLISH", account_id=account.id, video_id=video.id,
            payload={"publish_job_id": pub.id}, idempotency_key=key, priority=100, run_after=due)
    video.status = "SCHEDULED"
    return 1


def tick(now=None):
    now = now or utcnow()
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    summary = {"accounts": 0, "production_jobs": 0, "scheduled_videos": 0, "errors": 0, "paused": False}
    with SessionLocal() as session:
        state = session.get(SystemState, "global")
        if state and state.paused:
            summary["paused"] = True
            return summary
        ids = session.scalars(select(Account.id).where(Account.active.is_(True))).all()
    for account_id in ids:
        try:
            with SessionLocal() as session:
                # This account row serializes concurrent schedulers and API run requests.
                account = session.scalar(select(Account).where(Account.id == account_id).with_for_update(skip_locked=True))
                if not account or not account.active:
                    continue
                assert_running(session, account_id)
                summary["scheduled_videos"] += reserve_publishing(session, account, now)
                summary["production_jobs"] += replenish(session, account, now)
                interval = max(60, int((account.settings or {}).get("drive_poll_seconds", 300)))
                google = session.scalar(select(Connection.id).where(Connection.account_id == account.id,
                    Connection.platform.in_(("google", "drive")), Connection.status.in_(("CONNECTED", "connected"))))
                if google:
                    enqueue_job(session, "DRIVE_SYNC", account_id=account.id,
                                idempotency_key=f"drive-sync:{account.id}:{int(now.timestamp()) // interval}", priority=90)
                from factory.config import settings
                if settings.smfa_analytics_enabled:
                    from factory.services.instagram_insights import enqueue_collection
                    from factory.services.insight_ranges import ZONE
                    day = now.astimezone(ZONE).date()
                    enqueue_collection(session, [account.id], (day - timedelta(days=2)).isoformat(), day.isoformat())
                bucket = now.replace(minute=0, second=0, microsecond=0)
                posts = session.scalars(select(PublishedPost).where(PublishedPost.account_id == account.id,
                    PublishedPost.published_at >= now - timedelta(days=30))).all()
                for post in posts:
                    enqueue_job(session, "ANALYTICS", account_id=account.id, video_id=post.video_id,
                                payload={"post_id": post.id, "bucket_at": bucket.isoformat()},
                                idempotency_key=f"analytics:{post.id}:{bucket.isoformat()}", priority=10)
                enqueue_job(session, "OPTIMIZE", account_id=account.id,
                            idempotency_key=f"optimize:{account.id}:{now.date().isoformat()}", priority=5)
                session.commit()
                summary["accounts"] += 1
        except Exception as exc:
            summary["errors"] += 1
            logger.error("Scheduler account isolated failure: %s (%s)", account_id, type(exc).__name__)
            with SessionLocal() as session:
                log_event(session, "scheduler.error", safe_error(exc), account_id, "ERROR")
                session.commit()
    return summary


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    while True:
        try:
            summary = tick()
            dispatched = dispatch_pending()
            Path("/tmp/scheduler-heartbeat").touch()
            logger.info("scheduler.tick %s dispatched=%s", summary, dispatched)
        except KeyboardInterrupt:
            return
        except Exception:
            # Database/Redis restarts should not discard the scheduler process.
            logger.exception("Scheduler connection unavailable; recovery on next tick")
        time.sleep(15)


if __name__ == "__main__":
    main()
