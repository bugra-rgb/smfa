"""Veritabanının uygulamadan bağımsız koruması ve migration sözleşmesi."""
import importlib.util
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest
from sqlalchemy import inspect
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from factory.db import make_engine
from factory.models import (
    Account, AIRun, Analytics, AuthSession, Base, Connection, DriveFile, Idea, Job,
    OAuthToken, PublishedPost, PublishJob, User, Video,
)


@pytest.fixture
def db_session():
    engine = make_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine, expire_on_commit=False) as session:
        yield session
    engine.dispose()


def account(session, name="Hesap"):
    row = Account(name=name)
    session.add(row)
    session.flush()
    return row


def post_graph(session):
    owner = account(session)
    connection = Connection(account_id=owner.id, platform="youtube")
    video = Video(account_id=owner.id, title="Test")
    session.add_all([connection, video])
    session.flush()
    return owner, connection, video


def test_requested_tables_exist(db_session):
    required = {"users", "accounts", "platform_connections", "oauth_tokens", "account_settings",
                "drive_connections", "drive_folders", "drive_files", "content_ideas", "scripts",
                "assets", "videos", "video_variants", "render_jobs", "processing_jobs",
                "publishing_jobs", "published_posts", "analytics", "content_performance", "schedules",
                "ai_runs", "provider_configs", "system_logs", "failed_jobs", "notifications"}
    assert required <= set(inspect(db_session.bind).get_table_names())


def test_drive_file_unique_across_accounts(db_session):
    first, second = account(db_session, "A"), account(db_session, "B")
    db_session.add(DriveFile(account_id=first.id, drive_file_id="drive-123", filename="video.mp4"))
    db_session.flush()
    db_session.add(DriveFile(account_id=second.id, drive_file_id="drive-123", filename="video.mp4"))
    with pytest.raises(IntegrityError):
        db_session.flush()


def test_job_idempotency_enforced(db_session):
    owner = account(db_session)
    db_session.add(Job(account_id=owner.id, kind="render", idempotency_key="render:1"))
    db_session.flush()
    db_session.add(Job(account_id=owner.id, kind="render", idempotency_key="render:1"))
    with pytest.raises(IntegrityError):
        db_session.flush()


def test_publish_cannot_bypass_dedup_with_different_key(db_session):
    owner, connection, video = post_graph(db_session)
    for key in ("key-one", "key-two"):
        db_session.add(PublishJob(account_id=owner.id, connection_id=connection.id, video_id=video.id,
                                  platform="youtube", idempotency_key=key))
    with pytest.raises(IntegrityError):
        db_session.flush()


def test_published_post_unique_on_video_connection(db_session):
    owner, connection, video = post_graph(db_session)
    for remote_id in ("remote-one", "remote-two"):
        db_session.add(PublishedPost(account_id=owner.id, connection_id=connection.id, video_id=video.id,
                                    platform="youtube", remote_id=remote_id))
    with pytest.raises(IntegrityError):
        db_session.flush()


def test_analytics_bucket_unique(db_session):
    owner, connection, video = post_graph(db_session)
    post = PublishedPost(account_id=owner.id, connection_id=connection.id, video_id=video.id,
                         platform="youtube", remote_id="remote-one")
    db_session.add(post)
    db_session.flush()
    when = datetime(2026, 9, 14, tzinfo=timezone.utc)
    for _ in range(2):
        db_session.add(Analytics(account_id=owner.id, post_id=post.id, bucket_at=when))
    with pytest.raises(IntegrityError):
        db_session.flush()


def test_idea_memory_is_scoped_per_account(db_session):
    first, second = account(db_session, "A"), account(db_session, "B")
    db_session.add_all([Idea(account_id=owner.id, title="Aynı fikir", fingerprint="same-fingerprint")
                        for owner in (first, second)])
    db_session.flush()
    db_session.add(Idea(account_id=first.id, title="Tekrar", fingerprint="same-fingerprint"))
    with pytest.raises(IntegrityError):
        db_session.flush()


def test_foreign_keys_enforced_in_sqlite(db_session):
    db_session.add(Connection(account_id="missing-account", platform="youtube"))
    with pytest.raises(IntegrityError):
        db_session.flush()


def test_account_delete_cascades_and_tokens_are_ciphertext_only(db_session):
    owner, connection, _ = post_graph(db_session)
    db_session.add(OAuthToken(connection_id=connection.id, encrypted_payload="ciphertext"))
    db_session.commit()
    db_session.delete(owner)
    db_session.commit()
    assert db_session.query(Connection).count() == 0
    assert db_session.query(OAuthToken).count() == 0
    assert set(OAuthToken.__table__.columns.keys()).isdisjoint({"access_token", "refresh_token"})


def test_utc_roundtrip_and_session_lifecycle(db_session):
    owner = User(email="admin@example.test", password_hash="hashed")
    db_session.add(owner)
    db_session.flush()
    expires = datetime(2026, 9, 14, 9, 0, tzinfo=timezone.utc)
    row = AuthSession(user_id=owner.id, token_hash="hash", csrf_token="csrf", expires_at=expires)
    db_session.add(row)
    db_session.commit()
    db_session.expire_all()
    restored = db_session.get(AuthSession, row.id)
    assert restored.expires_at == expires
    assert restored.created_at.tzinfo is not None
    db_session.delete(owner)
    db_session.commit()
    assert db_session.query(AuthSession).count() == 0


def test_negative_daily_target_rejected(db_session):
    db_session.add(Account(name="Yanlış", daily_target=-1))
    with pytest.raises(IntegrityError):
        db_session.flush()


def test_migration_up_down_and_model_consistency(tmp_path):
    root = Path(__file__).resolve().parents[1]
    env = {**os.environ, "MODE": "mock", "DATABASE_URL": f"sqlite:///{tmp_path / 'migration.db'}",
           "DATA_DIR": str(tmp_path)}
    for args in (("upgrade", "head"), ("check",), ("downgrade", "base"), ("upgrade", "head")):
        proc = subprocess.run([sys.executable, "-m", "alembic", *args], cwd=root, env=env,
                              text=True, capture_output=True, timeout=30)
        assert proc.returncode == 0, proc.stdout + proc.stderr
    migrated = make_engine(env["DATABASE_URL"])
    assert set(Base.metadata.tables) <= set(inspect(migrated).get_table_names())
    foreign_keys = inspect(migrated).get_foreign_keys("drive_files")
    assert any(fk["referred_table"] == "videos" for fk in foreign_keys)
    migrated.dispose()


def test_bootstrap_generates_secrets_without_overwriting(tmp_path, monkeypatch, capsys):
    spec = importlib.util.spec_from_file_location("bootstrap_test", Path(__file__).resolve().parents[1] / "scripts/bootstrap.py")
    bootstrap = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(bootstrap)
    monkeypatch.setattr(bootstrap, "ROOT", tmp_path)
    bootstrap.generate_env("admin@example.test", "http://localhost:3000", False)
    content = (tmp_path / ".env").read_text()
    values = dict(line.split("=", 1) for line in content.splitlines() if line and not line.startswith("#"))
    assert len(values["ADMIN_PASSWORD"]) >= 24
    assert len(values["SECRET_KEY"]) >= 64
    assert values["SECRET_KEY"] != values["POSTGRES_PASSWORD"]
    assert values["ADMIN_PASSWORD"] not in capsys.readouterr().out
    bootstrap.generate_env("changed@example.test", "http://localhost:3000", False)
    assert (tmp_path / ".env").read_text() == content


def test_bootstrap_production_requires_https(tmp_path, monkeypatch):
    spec = importlib.util.spec_from_file_location("bootstrap_test", Path(__file__).resolve().parents[1] / "scripts/bootstrap.py")
    bootstrap = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(bootstrap)
    monkeypatch.setattr(bootstrap, "ROOT", tmp_path)
    with pytest.raises(SystemExit, match="https"):
        bootstrap.generate_env("admin@example.test", "http://example.test", True)
    assert not (tmp_path / ".env").exists()


def test_bootstrap_admin_idempotent_and_password_not_reset(tmp_path):
    root = Path(__file__).resolve().parents[1]
    env = {**os.environ, "MODE": "mock", "DATABASE_URL": f"sqlite:///{tmp_path / 'bootstrap.db'}",
           "DATA_DIR": str(tmp_path), "ADMIN_EMAIL": "bootstrap@example.test",
           "ADMIN_PASSWORD": "test-bootstrap-strong-password"}
    subprocess.run([sys.executable, "-m", "alembic", "upgrade", "head"], cwd=root, env=env,
                   check=True, capture_output=True, timeout=30)
    for password in ("test-bootstrap-strong-password", "different-strong-password", ""):
        subprocess.run([sys.executable, "scripts/bootstrap.py", "--init-admin"], cwd=root,
                       env={**env, "ADMIN_PASSWORD": password}, check=True, capture_output=True, timeout=30)
    database = make_engine(env["DATABASE_URL"])
    from argon2 import PasswordHasher
    with Session(database) as session:
        rows = session.query(User).all()
        assert len(rows) == 1
        assert PasswordHasher().verify(rows[0].password_hash, "test-bootstrap-strong-password")
    database.dispose()


def test_unpriced_ai_run_preserves_null_estimated_and_actual_cost(db_session):
    owner = account(db_session)
    unknown = AIRun(account_id=owner.id, provider="unpriced-provider", operation="video",
                    estimated_cost=None, actual_cost=None)
    free = AIRun(account_id=owner.id, provider="mock", operation="video",
                 estimated_cost=0, actual_cost=0)
    omitted = AIRun(account_id=owner.id, provider="unpriced-provider", operation="voice")
    db_session.add_all([unknown, free, omitted])
    db_session.commit()
    db_session.expire_all()
    assert unknown.estimated_cost is None and unknown.actual_cost is None
    assert omitted.estimated_cost is None and omitted.actual_cost is None
    assert free.estimated_cost == 0 and free.actual_cost == 0
