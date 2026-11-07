"""Real Redis/Celery checks. TEST_REDIS_URL must point to an isolated test service."""
import os
from pathlib import Path
import subprocess
import sys
import time
from uuid import uuid4

import pytest
import redis
from celery import Celery
from sqlalchemy.orm import sessionmaker

from factory.config import settings
from factory.db import make_engine
from factory.models import Account, Base, Job
from factory.rate_control import CapacityBusy, provider_slot
from factory.services.jobs import enqueue_job

pytestmark = [pytest.mark.integration, pytest.mark.skipif(not os.environ.get("TEST_REDIS_URL"), reason="TEST_REDIS_URL gerekli")]


def test_distributed_provider_capacity_release_and_expired_lease(monkeypatch):
    url = os.environ["TEST_REDIS_URL"]
    monkeypatch.setattr(settings, "redis_url", url)
    monkeypatch.setattr(settings, "mode", "production")
    monkeypatch.setattr(settings, "provider_concurrency", 1)
    name = f"test-{uuid4()}"
    client = redis.Redis.from_url(url)
    key = f"factory:capacity:{name}"
    try:
        with provider_slot(name):
            assert client.zcard(key) == 1
            with pytest.raises(CapacityBusy):
                with provider_slot(name):
                    pytest.fail("capacity exceeded")
        assert client.zcard(key) == 0
        client.zadd(key, {"crashed-worker": time.time() - 1})
        with pytest.raises(ValueError, match="intentional"):
            with provider_slot(name):
                assert client.zcard(key) == 1
                raise ValueError("intentional")
        assert client.zcard(key) == 0
    finally:
        client.delete(key)


def test_real_celery_worker_executes_duplicate_delivery_once(tmp_path):
    url = os.environ["TEST_REDIS_URL"]
    database_url = f"sqlite:///{tmp_path / 'worker.db'}"
    engine = make_engine(database_url)
    Base.metadata.create_all(engine)
    sessions = sessionmaker(bind=engine, expire_on_commit=False)
    with sessions.begin() as session:
        account = Account(name="Isolated worker test", active=True)
        session.add(account)
        session.flush()
        job_id = enqueue_job(session, "OPTIMIZE", account_id=account.id).id
    queue = f"test-{uuid4()}"
    env = {**os.environ, "MODE": "mock", "DATABASE_URL": database_url, "REDIS_URL": url, "DATA_DIR": str(tmp_path / "media")}
    log_path = tmp_path / "worker.log"
    sender = Celery("integration_sender", broker=url)
    log = log_path.open("wb")
    worker = subprocess.Popen([sys.executable, "-m", "celery", "-A", "factory.worker:celery_app", "worker", "--pool=solo", "--concurrency=1", "--loglevel=WARNING", "--without-gossip", "--without-mingle", "--without-heartbeat", "-Q", queue], env=env, stdout=log, stderr=subprocess.STDOUT, cwd=Path(__file__).resolve().parents[1])
    try:
        sender.send_task("factory.execute_job", args=[job_id], queue=queue)
        sender.send_task("factory.execute_job", args=[job_id], queue=queue)
        deadline = time.monotonic() + 25
        succeeded = False
        while time.monotonic() < deadline:
            with sessions() as session:
                row = session.get(Job, job_id)
                if row.status == "SUCCEEDED":
                    succeeded = True
                    assert row.attempts == 1
                    break
            if worker.poll() is not None:
                break
            time.sleep(0.2)
        assert succeeded, log_path.read_text(errors="replace")[-4000:]
        # The second delivery must be consumed without a second attempt.
        client = redis.Redis.from_url(url)
        deadline = time.monotonic() + 5
        while client.llen(queue) and time.monotonic() < deadline:
            time.sleep(0.1)
        with sessions() as session:
            assert session.get(Job, job_id).attempts == 1
    finally:
        worker.terminate()
        try:
            worker.wait(timeout=10)
        except subprocess.TimeoutExpired:
            worker.kill()
            worker.wait(timeout=5)
        log.close()
        redis.Redis.from_url(url).delete(queue, f"_kombu.binding.{queue}")
        sender.close()
        engine.dispose()
