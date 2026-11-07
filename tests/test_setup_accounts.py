from copy import deepcopy

import pytest
from sqlalchemy import select
from sqlalchemy.orm import sessionmaker

from factory.db import make_engine
from factory.models import Account, Base, Connection, Job, OAuthToken, PublishJob
from factory.services import scheduler
from scripts.setup_accounts import model_defaults, prepare_accounts


@pytest.fixture
def sessions(monkeypatch):
    engine = make_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    sessions = sessionmaker(bind=engine, expire_on_commit=False)
    monkeypatch.setattr(scheduler, "SessionLocal", sessions)
    yield sessions
    engine.dispose()


def test_preparation_is_idempotent_and_scheduler_cannot_start_production(sessions):
    with sessions.begin() as session:
        first = prepare_accounts(session, ["@demo.bird", "demo.bear", "DEMO.BIRD"])
    with sessions.begin() as session:
        second = prepare_accounts(session, ["demo.bird", "@demo.bear"])
        assert [item["account_id"] for item in first] == [item["account_id"] for item in second]
        assert all(item["result"] == "existing_preserved" for item in second)
        accounts = session.scalars(select(Account)).all()
        assert len(accounts) == 2
        assert all(not row.active and row.daily_target == 0 and not row.auto_publish for row in accounts)
        assert all(row.approval_mode == "MANUAL_APPROVAL" for row in accounts)
        connections = session.scalars(select(Connection)).all()
        assert len(connections) == 2
        assert all(row.status == "DISCONNECTED" and row.external_id is None for row in connections)
        assert session.scalars(select(OAuthToken)).all() == []
    summary = scheduler.tick()
    assert summary["accounts"] == 0
    with sessions() as session:
        assert session.scalars(select(Job)).all() == []
        assert session.scalars(select(PublishJob)).all() == []


def test_existing_config_and_credentials_are_unchanged(sessions):
    with sessions.begin() as session:
        account = Account(name="Mevcut isim", username="@DEMO.BIRD", niche="Belirlenmiş konu",
                          active=True, auto_publish=True, daily_target=7,
                          settings={"llm_model": "chosen-by-owner", "schedule": ["12:00"]})
        session.add(account)
        session.flush()
        connection = Connection(account_id=account.id, platform="instagram", status="CONNECTED",
                                external_id="verified-provider-id", settings={"username": "demo.bird"})
        session.add(connection)
        session.flush()
        token = OAuthToken(connection_id=connection.id, encrypted_payload="existing-opaque-ciphertext")
        session.add(token)
        session.flush()
        before = deepcopy({column.name: getattr(account, column.name) for column in Account.__table__.columns})
        prepare_accounts(session, ["demo.bird", "demo.bear"])
        session.refresh(account)
        assert before == {column.name: getattr(account, column.name) for column in Account.__table__.columns}
        session.refresh(connection)
        session.refresh(token)
        assert connection.status == "CONNECTED"
        assert connection.external_id == "verified-provider-id"
        assert token.encrypted_payload == "existing-opaque-ciphertext"


def test_invalid_input_or_ambiguous_match_creates_nothing(sessions):
    with sessions.begin() as session:
        with pytest.raises(ValueError):
            prepare_accounts(session, ["demo.bird", "invalid/name"])
        assert session.scalars(select(Account)).all() == []
        session.add_all([Account(name="A", username="demo.bear"), Account(name="B", username="@DEMO.BEAR")])
        session.flush()
        with pytest.raises(ValueError, match="birden fazla"):
            prepare_accounts(session, ["demo.bird", "demo.bear"])
        assert len(session.scalars(select(Account)).all()) == 2


def test_defaults_do_not_override_selected_models_or_accept_secrets():
    existing = {"OPENAI_MODEL": "owner-selection", "OPENAI_API_KEY": "unmodified-test-placeholder"}
    before = dict(existing)
    defaults = model_defaults(existing)
    assert "OPENAI_MODEL" not in defaults
    assert "OPENAI_API_KEY" not in defaults
    assert defaults["OPENAI_TRANSCRIPTION_MODEL"] == "whisper-1"
    assert existing == before
