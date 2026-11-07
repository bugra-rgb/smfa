from datetime import timedelta
from pathlib import Path

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import sessionmaker

from factory import providers, security
from factory.config import settings
from factory.db import make_engine
from factory.models import (
    Account, AIRun, Analytics, Base, Connection, DriveFile, DriveFolder, Job,
    PublishJob, PublishedPost, Video,
)
from factory.providers.base import ProviderError
from factory.providers.mock import MockPublisher
from factory.services import jobs, pipeline, scheduler


@pytest.fixture
def pipeline_db(tmp_path, monkeypatch):
    engine = make_engine("sqlite://")
    Base.metadata.create_all(engine)
    sessions = sessionmaker(bind=engine, expire_on_commit=False)
    for module in (jobs, pipeline, scheduler, security):
        monkeypatch.setattr(module, "SessionLocal", sessions)
    monkeypatch.setattr(settings, "data_dir", str(tmp_path))
    monkeypatch.setattr(settings, "mode", "mock")
    with sessions.begin() as session:
        account = Account(name="Test Kanalı", niche="Bilim", language="tr", daily_target=1,
            auto_publish=True, approval_mode="AUTO_PUBLISH", source_mode="AI_ONLY",
            settings={"duration": 2, "width": 180, "height": 320, "subtitle": {"enabled": True, "font_size": 12},
                      "inventory_min": 0, "inventory_target": 1, "schedule": ["00:01"]})
        session.add(account)
        session.flush()
        for platform in ("drive", "youtube", "instagram", "tiktok"):
            session.add(Connection(account_id=account.id, platform=platform, status="CONNECTED", external_id=f"mock-{platform}"))
        account_id = account.id
    yield sessions, account_id, tmp_path
    engine.dispose()


def test_real_ffmpeg_mock_pipeline_drive_publish_analytics_no_duplicate(pipeline_db):
    sessions, account_id, tmp_path = pipeline_db
    queued = pipeline.run_pipeline(account_id)
    result = jobs.process_job(queued["job_id"])
    assert result["status"] == "SUCCEEDED", result
    with sessions() as session:
        video = session.scalar(select(Video).where(Video.account_id == account_id))
        assert video.status == "READY"
        assert video.qa["passed"] is True
        assert Path(video.local_path).stat().st_size > 1000
        assert video.drive_file_id
        assert (Path(settings.data_dir) / "mock-drive" / video.drive_file_id).is_file()
        assert session.scalar(select(func.count(AIRun.id))) >= 5
    assert scheduler.tick()["scheduled_videos"] == 1
    with sessions() as session:
        publishing_ids = session.scalars(select(Job.id).where(Job.kind == "PUBLISH")).all()
    assert len(publishing_ids) == 3
    for job_id in publishing_ids:
        assert jobs.process_job(job_id)["status"] == "SUCCEEDED"
        assert jobs.process_job(job_id)["status"] == "SKIPPED"
    with sessions.begin() as session:
        assert session.scalar(select(func.count(PublishedPost.id))) == 3
        pub = session.scalar(select(PublishJob))
        replay = jobs.enqueue_job(session, "PUBLISH", account_id=account_id, video_id=pub.video_id,
                                  payload={"publish_job_id": pub.id}, idempotency_key="malformed-duplicate-delivery")
        replay_id = replay.id
    assert jobs.process_job(replay_id)["status"] == "SUCCEEDED"
    scheduler.tick()
    with sessions() as session:
        analytics_ids = session.scalars(select(Job.id).where(Job.kind == "ANALYTICS")).all()
    for job_id in analytics_ids:
        assert jobs.process_job(job_id)["status"] == "SUCCEEDED"
    scheduler.tick()
    with sessions() as session:
        assert session.scalar(select(func.count(Analytics.id))) == 3
        assert session.scalar(select(func.count(PublishedPost.id))) == 3


def ready_publish_jobs(sessions, account_id, tmp_path):
    final = tmp_path / "already-checked.mp4"
    final.write_bytes(b"file exists; media QA belongs to production tests")
    ids = {}
    with sessions.begin() as session:
        video = Video(account_id=account_id, title="Hazır", status="SCHEDULED", local_path=str(final), qa={"passed": True},
            metadata_json={"youtube": {"title": "Test", "description": "Test"}, "instagram": {"caption": "Test"}, "tiktok": {"caption": "Test"}})
        session.add(video)
        session.flush()
        for connection in session.scalars(select(Connection).where(Connection.platform.in_(("youtube", "instagram", "tiktok")))):
            pub = PublishJob(account_id=account_id, video_id=video.id, connection_id=connection.id,
                platform=connection.platform, status="SCHEDULED", scheduled_at=jobs.utcnow() - timedelta(seconds=1),
                idempotency_key=f"pub:{connection.id}")
            session.add(pub)
            session.flush()
            ids[connection.platform] = jobs.enqueue_job(session, "PUBLISH", account_id=account_id, video_id=video.id,
                payload={"publish_job_id": pub.id}, idempotency_key=pub.idempotency_key).id
    return ids


def test_platform_failure_does_not_stop_other_platforms(pipeline_db, monkeypatch):
    sessions, account_id, tmp_path = pipeline_db
    ids = ready_publish_jobs(sessions, account_id, tmp_path)

    class BrokenInstagram(MockPublisher):
        def publish(self, *args, **kwargs):
            raise ProviderError("Oran sınırı", "rate_limit", retryable=True, retry_after=120)

    monkeypatch.setattr(providers, "get_publisher", lambda platform, settings, **kwargs: BrokenInstagram(platform) if platform == "instagram" else MockPublisher(platform))
    assert jobs.process_job(ids["instagram"])["status"] == "RETRYING"
    assert jobs.process_job(ids["youtube"])["status"] == "SUCCEEDED"
    assert jobs.process_job(ids["tiktok"])["status"] == "SUCCEEDED"
    with sessions() as session:
        assert session.scalar(select(func.count(PublishedPost.id))) == 2


def test_ambiguous_publish_checkpoint_blocks_reposting(pipeline_db, monkeypatch):
    sessions, account_id, tmp_path = pipeline_db
    ids = ready_publish_jobs(sessions, account_id, tmp_path)
    effects = []

    class UnknownPublisher(MockPublisher):
        def publish(self, video_path, metadata, credentials, checkpoint=None, save_checkpoint=None):
            effects.append(True)
            cp = {"attempted": True, "upload_url": "https://example.test/secret-capability"}
            save_checkpoint(cp)
            return {"status": "UNKNOWN", "checkpoint": cp, "message": "Yayın sonucu belirsiz"}

    monkeypatch.setattr(providers, "get_publisher", lambda platform, settings, **kwargs: UnknownPublisher(platform))
    assert jobs.process_job(ids["youtube"])["status"] == "UNKNOWN"
    assert jobs.process_job(ids["youtube"])["status"] == "SKIPPED"
    with sessions() as session:
        pub = session.scalar(select(PublishJob).where(PublishJob.platform == "youtube"))
        assert pub.status == "UNKNOWN"
        assert "encrypted" in pub.checkpoint
        assert "secret-capability" not in str(pub.checkpoint)
        assert session.scalar(select(func.count(PublishedPost.id))) == 0
    assert len(effects) == 1


def test_distinct_processing_jobs_cannot_share_live_publication_lease(pipeline_db, monkeypatch):
    sessions, account_id, tmp_path = pipeline_db
    ids = ready_publish_jobs(sessions, account_id, tmp_path)
    with sessions.begin() as session:
        pub = session.scalar(select(PublishJob).where(PublishJob.platform == "youtube"))
        pub.checkpoint = {"worker_lease": {"job_id": "another-live-worker", "token": "owner-token",
                          "until": (jobs.utcnow() + timedelta(minutes=10)).isoformat()}}
    monkeypatch.setattr(providers, "get_publisher", lambda *args: (_ for _ in ()).throw(AssertionError("Yinelenen yayın adaptörüne erişilmemeli")))
    assert jobs.process_job(ids["youtube"])["status"] == "RETRYING"
    with sessions() as session:
        assert session.scalar(select(func.count(PublishedPost.id))) == 0


def test_drive_input_detected_only_once_and_account_isolated(pipeline_db):
    sessions, account_id, tmp_path = pipeline_db
    with sessions.begin() as session:
        session.get(Account, account_id).source_mode = "DRIVE_ONLY"
        job_id = jobs.enqueue_job(session, "DRIVE_SYNC", account_id=account_id).id
    assert jobs.process_job(job_id)["status"] == "SUCCEEDED"
    with sessions() as session:
        folder = session.scalar(select(DriveFolder).where(DriveFolder.account_id == account_id, DriveFolder.kind == "input"))
        incoming = tmp_path / "mock-drive" / folder.external_id / "new.mp4"
        incoming.write_bytes(b"ready source; scan only")
    for index in range(2):
        with sessions.begin() as session:
            job_id = jobs.enqueue_job(session, "DRIVE_SYNC", account_id=account_id, idempotency_key=f"sync:{index}").id
        assert jobs.process_job(job_id)["status"] == "SUCCEEDED"
    with sessions() as session:
        files = session.scalars(select(DriveFile)).all()
        assert len(files) == 1
        assert files[0].account_id == account_id
        assert files[0].processing_status == "DISCOVERED"


def test_memory_similarity_and_diversity_rank():
    assert pipeline.similarity("Futbolda yeni transfer kuralı", "FUTBOLDA yeni transfer kuralı!") == 1
    idea = {"topic": "Transfer", "novelty_score": 90, "relevance_score": 90, "risk_score": 0}
    assert pipeline.rank_idea(idea, {"transfer": 6}, {}) < pipeline.rank_idea(idea, {}, {})
    assert pipeline.rank_idea({**idea, "risk_score": 90}, {}, {}) == -1


def test_rerender_reuses_source_and_creates_new_checked_revision(pipeline_db):
    sessions, account_id, _ = pipeline_db
    queued = pipeline.run_pipeline(account_id)
    assert jobs.process_job(queued["job_id"])["status"] == "SUCCEEDED"
    with sessions.begin() as session:
        video = session.scalar(select(Video))
        previous_drive_id, video_id = video.drive_file_id, video.id
        account = session.get(Account, account_id)
        account.settings = {**account.settings, "text_overlay": "Yeniden düzenlendi"}
        rerender_id = jobs.enqueue_job(session, "RERENDER", account_id=account_id, video_id=video.id).id
    result = jobs.process_job(rerender_id)
    assert result["status"] == "SUCCEEDED", result
    with sessions() as session:
        video = session.get(Video, video_id)
        assert video.status == "READY" and video.qa["passed"]
        assert video.drive_file_id != previous_drive_id
        assert session.scalar(select(func.count(Video.id))) == 1


def test_drive_short_silent_video_skips_transcription_when_disabled(pipeline_db, monkeypatch):
    import subprocess
    sessions, account_id, tmp_path = pipeline_db
    with sessions.begin() as session:
        account = session.get(Account, account_id)
        account.source_mode = "DRIVE_ONLY"
        account.settings = {**account.settings, "duration": 30, "subtitle": {"enabled": False}, "qa": {"require_audio": False}}
        sync_id = jobs.enqueue_job(session, "DRIVE_SYNC", account_id=account_id).id
    assert jobs.process_job(sync_id)["status"] == "SUCCEEDED"
    with sessions() as session:
        folder = session.scalar(select(DriveFolder).where(DriveFolder.kind == "input"))
        source = tmp_path / "mock-drive" / folder.external_id / "silent.mp4"
    subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-f", "lavfi", "-i", "testsrc2=size=180x320:rate=15",
                    "-t", "1", "-an", "-c:v", "libx264", "-threads", "1", "-y", str(source)], check=True)
    with sessions.begin() as session:
        sync_id = jobs.enqueue_job(session, "DRIVE_SYNC", account_id=account_id).id
    assert jobs.process_job(sync_id)["status"] == "SUCCEEDED"
    with sessions.begin() as session:
        drive_file = session.scalar(select(DriveFile))
        process_id = jobs.enqueue_job(session, "DRIVE_PIPELINE", account_id=account_id,
                                      payload={"drive_file_id": drive_file.id}).id
    from factory.providers.mock import MockVoiceProvider
    monkeypatch.setattr(MockVoiceProvider, "transcribe", lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("Sessiz videoda STT çağrılmamalı")))
    result = jobs.process_job(process_id)
    assert result["status"] == "SUCCEEDED", result
    with sessions() as session:
        video = session.scalar(select(Video))
        assert video.qa["passed"] and video.status == "READY"


def test_rerender_cannot_mutate_scheduled_video(pipeline_db):
    sessions, account_id, tmp_path = pipeline_db
    ready_publish_jobs(sessions, account_id, tmp_path)
    with sessions.begin() as session:
        video = session.scalar(select(Video))
        video_id = video.id
        job_id = jobs.enqueue_job(session, "RERENDER", account_id=account_id, video_id=video.id).id
    assert jobs.process_job(job_id)["status"] == "BLOCKED"
    with sessions() as session:
        assert session.get(Video, video_id).status == "SCHEDULED"


def test_optimization_requires_evidence_and_respects_pinned_settings():
    account = Account(name="Analiz", daily_target=2, content_style="", settings={"duration": 30, "schedule": []})
    samples = [{"duration": 30, "hour": 8, "content_format": "story", "score": 0.02} for _ in range(6)]
    samples += [{"duration": 15, "hour": 20, "content_format": "explainer", "score": 0.10} for _ in range(6)]
    config, decisions = pipeline.feature_strategy(account, samples[:9])
    assert config["duration"] == 30
    assert not decisions["applied"] and decisions["status"] == "insufficient_data"
    config, decisions = pipeline.feature_strategy(account, samples)
    assert config["duration"] == 15
    assert config["content_format"] == "explainer"
    assert decisions["posting_schedule"] == ["20:00"]
    assert set(decisions["applied"]) == {"duration", "schedule", "content_format"}
    account.settings = {**config, "optimization": {"features": decisions}}
    formats = {pipeline.production_options(account, f"test-{index}")["content_format"] for index in range(100)}
    assert formats == {"story", "explainer"}
    assert pipeline.production_options(account, "stable") == pipeline.production_options(account, "stable")
    account.content_style = "Kullanıcının sabit biçimi"
    account.settings = {"duration": 30, "duration_min": 20, "schedule": ["09:00", "16:00"]}
    config, decisions = pipeline.feature_strategy(account, samples)
    assert config["duration"] == 30 and config["schedule"] == ["09:00", "16:00"]
    assert "content_format" not in config
    assert not decisions["applied"]
