"""Release regressions using only synthetic identities and local mock data."""
from types import SimpleNamespace

import pytest
from sqlalchemy.orm import Session

from factory.config import Settings
from factory.db import make_engine
from factory.models import Account, Base, Connection, PublishJob, SystemState, Video
from factory.providers import get_publisher
from factory.providers.base import ProviderError
from factory.providers.buffer_instagram import BufferInstagramPublisher, ready
from factory.providers.mock import MockPublisher
from factory.services.drive_repeat import enabled
from factory.services.scheduler import inventory_counts


def test_default_provider_cannot_publish_externally(tmp_path):
    settings = Settings(_env_file=None, mode='mock', data_dir=str(tmp_path))
    for platform in ('instagram', 'youtube', 'facebook', 'x', 'tiktok'):
        assert isinstance(get_publisher(platform, settings), MockPublisher)


def test_bootstrap_starts_paused_without_resetting_an_existing_choice(monkeypatch):
    from contextlib import contextmanager
    from factory import db
    from factory.config import settings
    from scripts.bootstrap import init_admin
    engine = make_engine('sqlite://')
    Base.metadata.create_all(engine)
    @contextmanager
    def local_session():
        with Session(engine) as session, session.begin():
            yield session
    monkeypatch.setattr(db, 'session_scope', local_session)
    monkeypatch.setattr(settings, 'admin_email', 'admin@example.test')
    monkeypatch.setattr(settings, 'admin_password', 'synthetic-test-password')
    init_admin()
    with Session(engine) as session, session.begin():
        assert session.get(SystemState, 'global').paused is True
        session.get(SystemState, 'global').paused = False
    init_admin()
    with Session(engine) as session:
        assert session.get(SystemState, 'global').paused is False
    engine.dispose()


def test_inventory_ignores_terminal_reservations_for_any_drive_account():
    engine = make_engine('sqlite://')
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        account = Account(name='Synthetic', username='demo.channel', source_mode='DRIVE_ONLY')
        session.add(account); session.flush()
        connection = Connection(account_id=account.id, platform='instagram')
        video = Video(account_id=account.id, title='Synthetic', status='SCHEDULED')
        session.add_all([connection, video]); session.flush()
        session.add(PublishJob(account_id=account.id, connection_id=connection.id,
            video_id=video.id, platform='instagram', status='CANCELLED', idempotency_key='demo'))
        session.flush()
        assert inventory_counts(session, account.id) == 0
        session.add(Video(account_id=account.id, title='Fresh', status='READY')); session.flush()
        assert inventory_counts(session, account.id) == 1
    engine.dispose()


def test_replay_is_opt_in_and_never_applies_to_archived_accounts():
    account = SimpleNamespace(active=True, auto_publish=True, daily_target=1,
        source_mode='DRIVE_ONLY', approval_mode='AUTO_PUBLISH', settings={
            'publication_mode':'planner', 'drive_input_folder_id':'demo-folder'})
    assert not enabled(account)
    account.settings['smfa_drive_repeat_v1'] = True
    assert enabled(account)
    account.settings['archived'] = True
    assert not enabled(account)
    account.settings['archived'] = False
    account.settings['disable_replay'] = True
    assert not enabled(account)


def test_tenant_bound_buffer_experiment_is_disabled():
    assert ready(SimpleNamespace()) is False
    with pytest.raises(ProviderError, match='direct Instagram'):
        BufferInstagramPublisher()


def test_unicode_manual_caption_survives_validation():
    from factory.services.account_captions import CAPTION_ONLY_VERSION, provider_caption
    text = '今日の会話を楽しんでください。🎙️'
    metadata = {'manual_caption_version':CAPTION_ONLY_VERSION, 'manual_caption_text':text,
                'caption':text, 'description':text, 'hashtags':[], 'tags':[], 'caption_policy':'manual_text_only'}
    assert provider_caption(metadata, 'instagram') == text
