from datetime import date, datetime, timezone

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import sessionmaker

from factory.config import settings
from factory.db import make_engine
from factory.models import Account, Base, Connection, DriveFile, Job, PublishJob, SystemState, Video
from factory.services import scheduler


@pytest.fixture
def schedule_db(tmp_path, monkeypatch):
    engine = make_engine("sqlite://")
    Base.metadata.create_all(engine)
    sessions = sessionmaker(bind=engine, expire_on_commit=False)
    monkeypatch.setattr(scheduler, "SessionLocal", sessions)
    monkeypatch.setattr(settings, "mode", "mock")
    monkeypatch.setattr(settings, "data_dir", str(tmp_path))
    yield sessions
    engine.dispose()


def test_dst_days_and_ambiguous_hour_have_one_slot():
    spring = datetime(2026, 3, 29, 12, tzinfo=timezone.utc)
    autumn = datetime(2026, 10, 25, 12, tzinfo=timezone.utc)
    start, end = scheduler.local_day_bounds(spring, "Europe/Warsaw")
    assert (end - start).total_seconds() == 23 * 3600
    start, end = scheduler.local_day_bounds(autumn, "Europe/Warsaw")
    assert (end - start).total_seconds() == 25 * 3600
    slots = scheduler.schedule_slots(date(2026, 10, 25), "Europe/Warsaw", ["02:30", "02:30"], 1)
    assert slots == [datetime(2026, 10, 25, 0, 30, tzinfo=timezone.utc)]
    assert scheduler.schedule_slots(date(2026, 3, 29), "Europe/Warsaw", ["02:30"], 1) == [
        datetime(2026, 3, 29, 1, 30, tzinfo=timezone.utc)]


def test_repeated_ticks_reserve_daily_content_once_for_all_platforms(schedule_db):
    with schedule_db.begin() as session:
        account = Account(name="Hedef", daily_target=2, timezone="Europe/Istanbul", active=True,
            auto_publish=True, approval_mode="AUTO_PUBLISH", settings={"schedule": ["09:00", "18:00"], "inventory_min": 0})
        session.add(account)
        session.flush()
        for platform in ("youtube", "instagram", "tiktok"):
            session.add(Connection(account_id=account.id, platform=platform, status="CONNECTED"))
        for index in range(4):
            session.add(Video(account_id=account.id, title=f"Video {index}", status="READY", qa={"passed": True}))
        account_id = account.id
    now = datetime(2026, 9, 14, 8, tzinfo=timezone.utc)
    assert scheduler.tick(now)["scheduled_videos"] == 2
    assert scheduler.tick(now)["scheduled_videos"] == 0
    with schedule_db() as session:
        assert session.scalar(select(func.count(PublishJob.id))) == 6
        assert session.scalar(select(func.count(func.distinct(PublishJob.video_id)))) == 2
        assert session.scalar(select(func.count(Job.id)).where(Job.kind == "PUBLISH")) == 6
        assert session.get(Account, account_id).daily_target == 2


def test_inventory_is_bounded_and_drive_only_never_generates_ai(schedule_db):
    with schedule_db.begin() as session:
        account = Account(name="Drive", daily_target=3, source_mode="DRIVE_ONLY",
            settings={"inventory_min": 3, "inventory_target": 4, "inventory_max": 4})
        session.add(account)
        session.flush()
        for index in range(8):
            session.add(DriveFile(account_id=account.id, drive_file_id=f"drive-{index}", filename=f"{index}.mp4",
                source_type="DRIVE", processing_status="DISCOVERED"))
    now = datetime.now(timezone.utc)
    assert scheduler.tick(now)["production_jobs"] == 4
    assert scheduler.tick(now)["production_jobs"] == 0
    with schedule_db() as session:
        assert session.scalar(select(func.count(Job.id)).where(Job.kind == "DRIVE_PIPELINE")) == 4
        assert session.scalar(select(func.count(Job.id)).where(Job.kind == "AI_PIPELINE")) == 0


def test_account_failure_isolated_and_global_pause(schedule_db):
    with schedule_db.begin() as session:
        session.add(Account(name="İyi", daily_target=1, settings={"inventory_min": 1, "inventory_target": 1}))
        session.add(Account(name="Bozuk", timezone="No/Such-Zone", daily_target=1, auto_publish=True))
        session.flush()
        bad = session.scalar(select(Account).where(Account.name == "Bozuk"))
        session.add(Connection(account_id=bad.id, platform="youtube", status="CONNECTED"))
    result = scheduler.tick()
    assert result["errors"] == 1
    assert result["production_jobs"] == 1
    with schedule_db.begin() as session:
        session.add(SystemState(id="global", paused=True))
    assert scheduler.tick()["paused"] is True


def test_manual_approved_video_schedules_while_auto_publish_off(schedule_db):
    with schedule_db.begin() as session:
        account = Account(name="Onay", daily_target=1, auto_publish=False, approval_mode="MANUAL_APPROVAL",
                          settings={"inventory_min": 0})
        session.add(account)
        session.flush()
        session.add(Connection(account_id=account.id, platform="youtube", status="CONNECTED"))
        session.add(Video(account_id=account.id, title="Önceki onaysız", status="READY", qa={"passed": True}))
        session.add(Video(account_id=account.id, title="Onaylı", status="READY", qa={"passed": True}, metadata_json={"approved": True}))
    assert scheduler.tick()["scheduled_videos"] == 1
