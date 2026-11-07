from datetime import timedelta

import pytest
from sqlalchemy import select
from sqlalchemy.orm import sessionmaker

from factory.config import settings
from factory.db import make_engine
from factory.models import Account, Base, FailedJob, Job, SystemState
from factory.services import jobs, pipeline, scheduler


@pytest.fixture
def job_db(tmp_path, monkeypatch):
    engine = make_engine("sqlite://")
    Base.metadata.create_all(engine)
    sessions = sessionmaker(bind=engine, expire_on_commit=False)
    for module in (jobs, pipeline, scheduler):
        monkeypatch.setattr(module, "SessionLocal", sessions)
    monkeypatch.setattr(settings, "data_dir", str(tmp_path))
    monkeypatch.setattr(settings, "mode", "mock")
    with sessions.begin() as session:
        account = Account(name="Test", active=True)
        session.add(account)
        session.flush()
        account_id = account.id
    yield sessions, account_id
    engine.dispose()


def test_durable_claim_rejects_double_delivery_and_recovers_expiry(job_db):
    sessions, account_id = job_db
    with sessions.begin() as session:
        job = jobs.enqueue_job(session, "OPTIMIZE", account_id=account_id, idempotency_key="once")
        duplicate = jobs.enqueue_job(session, "OPTIMIZE", account_id=account_id, idempotency_key="once")
        assert duplicate.id == job.id
        job_id = job.id
    with sessions() as first:
        claimed = jobs.claim_job(first, job_id)
        first_owner = claimed.lease_owner
        assert claimed.attempts == 1
    with sessions() as duplicate:
        assert jobs.claim_job(duplicate, job_id) is None
    with sessions.begin() as session:
        session.get(Job, job_id).lease_until = jobs.utcnow() - timedelta(seconds=1)
    with sessions() as resumed:
        claimed = jobs.claim_job(resumed, job_id)
        assert claimed.lease_owner != first_owner
        assert claimed.attempts == 2


def test_retry_backoff_and_dead_letter_are_durable(job_db, monkeypatch):
    sessions, account_id = job_db

    def broken(ctx):
        raise TimeoutError("Sağlayıcı zaman aşımı")

    monkeypatch.setitem(pipeline.HANDLERS, "BROKEN", broken)
    with sessions.begin() as session:
        job = jobs.enqueue_job(session, "BROKEN", account_id=account_id, max_attempts=2)
        job_id = job.id
    assert jobs.process_job(job_id)["status"] == "RETRYING"
    with sessions.begin() as session:
        job = session.get(Job, job_id)
        assert 25 <= (job.run_after - jobs.utcnow()).total_seconds() <= 35
        job.run_after = jobs.utcnow() - timedelta(seconds=1)
    assert jobs.process_job(job_id)["status"] == "DLQ"
    with sessions() as session:
        assert session.scalar(select(FailedJob).where(FailedJob.job_id == job_id))
        assert session.get(Job, job_id).attempts == 2


def test_global_pause_blocks_effect_without_consuming_retry(job_db, monkeypatch):
    sessions, account_id = job_db
    called = []
    monkeypatch.setitem(pipeline.HANDLERS, "EFFECT", lambda ctx: called.append(True))
    with sessions.begin() as session:
        session.add(SystemState(id="global", paused=True))
        job = jobs.enqueue_job(session, "EFFECT", account_id=account_id)
        job_id = job.id
    assert jobs.process_job(job_id)["status"] == "RETRYING"
    assert not called
    with sessions() as session:
        assert session.get(Job, job_id).attempts == 0


def test_checkpoint_persists_inflight_result_before_pause(job_db):
    sessions, account_id = job_db
    with sessions.begin() as session:
        job = jobs.enqueue_job(session, "OPTIMIZE", account_id=account_id)
        job_id = job.id
    with sessions() as session:
        claimed = jobs.claim_job(session, job_id)
        ctx = jobs.JobContext(session, claimed)
        session.get(Account, account_id).active = False
        session.commit()
        with pytest.raises(jobs.AccountPaused):
            ctx.checkpoint(remote_id="remote-already-created")
    with sessions() as session:
        assert session.get(Job, job_id).result["remote_id"] == "remote-already-created"


def test_uncertain_effect_never_automatically_retries(job_db, monkeypatch):
    sessions, account_id = job_db
    monkeypatch.setitem(pipeline.HANDLERS, "UNSURE", lambda ctx: (_ for _ in ()).throw(jobs.UncertainEffect("Sonuç belirsiz")))
    with sessions.begin() as session:
        job_id = jobs.enqueue_job(session, "UNSURE", account_id=account_id).id
    assert jobs.process_job(job_id)["status"] == "UNKNOWN"
    assert jobs.process_job(job_id)["status"] == "SKIPPED"


def test_broker_loss_leaves_database_outbox_for_reconciliation(job_db, monkeypatch):
    from factory.worker import execute_job
    sessions, account_id = job_db
    with sessions.begin() as session:
        job_id = jobs.enqueue_job(session, "OPTIMIZE", account_id=account_id).id
    monkeypatch.setattr(execute_job, "apply_async", lambda **kwargs: (_ for _ in ()).throw(ConnectionError("Redis kapalı")))
    assert jobs.dispatch_pending() == 0
    with sessions() as session:
        assert session.get(Job, job_id).status == "QUEUED"
    sent = []
    monkeypatch.setattr(execute_job, "apply_async", lambda **kwargs: sent.append(kwargs))
    assert jobs.dispatch_pending() == 1
    assert sent[0]["args"] == [job_id]
