from urllib.parse import parse_qs, urlsplit

from cryptography.fernet import Fernet
from fastapi.testclient import TestClient
import pytest
from sqlalchemy.orm import sessionmaker

from factory import main, security
from factory.config import Settings, settings
from factory.db import make_engine
from factory.models import Account, Base, User, Video


def production_options(tmp_path):
    return {
        "_env_file": None, "mode": "production", "database_url": "postgresql+psycopg://test:test@db/test",
        "secret_key": "a" * 32, "encryption_key": Fernet.generate_key().decode(),
        "public_base_url": "https://panel.example.test", "frontend_origin": "https://panel.example.test",
        "data_dir": str(tmp_path),
    }


@pytest.mark.parametrize("origin", ["https://media.example.test/", "https://192.0.2.10", "https://[::1]:8443"])
def test_media_origin_accepts_https_without_changing_panel_origin(tmp_path, origin):
    config = Settings(**production_options(tmp_path), media_base_url=origin)
    assert config.media_base_url == origin.rstrip("/")
    assert config.public_base_url == "https://panel.example.test"


@pytest.mark.parametrize("origin", [
    "http://media.example.test", "https:///", "https://user:secret@media.example.test",
    "https://media.example.test/path", "https://media.example.test?token=secret",
    "https://media.example.test#fragment", "https://media.example.test:0",
    "https://media.example.test:65536", "https://media.example.test\n",
    "https://media.example.test\\evil", "https://media.example.test%2fwrong",
    "https://media.example.test?", "https://media.example.test#", "https://media.example.test:",
    "https://.", "https://-invalid.example.test", "https://invalid_.example.test",
])
def test_media_origin_rejects_invalid_production_destination(tmp_path, origin):
    with pytest.raises(ValueError, match="MEDIA_BASE_URL"):
        Settings(**production_options(tmp_path), media_base_url=origin)


def test_optional_media_origin_preserves_signature_scope_and_expiry(monkeypatch):
    monkeypatch.setattr(settings, "public_base_url", "https://panel.example.test")
    monkeypatch.setattr(settings, "media_base_url", "")
    monkeypatch.setattr(security.time, "time", lambda: 1000)
    fallback = security.signed_media_url("video-id", 300)
    monkeypatch.setattr(settings, "media_base_url", "https://media.example.test")
    direct = security.signed_media_url("video-id", 300)
    assert fallback.startswith("https://panel.example.test/api/media/")
    assert direct.startswith("https://media.example.test/api/media/")
    assert urlsplit(fallback).query == urlsplit(direct).query
    parameters = parse_qs(urlsplit(direct).query)
    expires = int(parameters["expires"][0])
    signature = parameters["signature"][0]
    security.verify_media_signature("video-id", expires, signature)
    with pytest.raises(main.HTTPException):
        security.verify_media_signature("different-video", expires, signature)
    with pytest.raises(main.HTTPException):
        security.verify_media_signature("video-id", expires + 1, signature)
    monkeypatch.setattr(security.time, "time", lambda: 1301)
    with pytest.raises(main.HTTPException):
        security.verify_media_signature("video-id", expires, signature)


def test_media_link_requires_login_qa_and_existing_safe_file(tmp_path, monkeypatch):
    engine = make_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    sessions = sessionmaker(bind=engine, expire_on_commit=False)
    monkeypatch.setattr(main, "SessionLocal", sessions)
    monkeypatch.setattr(security, "SessionLocal", sessions)
    monkeypatch.setattr(settings, "data_dir", str(tmp_path))
    monkeypatch.setattr(settings, "mode", "mock")
    monkeypatch.setattr(settings, "media_base_url", "https://media.example.test")
    media = tmp_path / "video.mp4"
    media.write_bytes(b"test media response bytes")
    with sessions.begin() as session:
        owner = Account(name="Preview")
        session.add_all([owner, User(email="viewer@example.test", password_hash="unused", role="viewer")])
        session.flush()
        ready = Video(account_id=owner.id, local_path=str(media), qa={"passed": True})
        rejected = Video(account_id=owner.id, local_path=str(media), qa={"passed": False})
        missing = Video(account_id=owner.id, local_path=str(tmp_path / "missing.mp4"), qa={"passed": True})
        session.add_all([ready, rejected, missing])
        session.flush()
        ready_id, rejected_id, missing_id = ready.id, rejected.id, missing.id
    client = TestClient(main.app)
    assert client.get(f"/api/videos/{ready_id}/media-link").status_code == 401
    # Exercise URL endpoint with a verified viewer identity; session enforcement above
    # remains real, and role authorization matches the existing authenticated /file.
    main.app.dependency_overrides[security.current_user] = lambda: {"id": "viewer", "role": "viewer"}
    try:
        response = client.get(f"/api/videos/{ready_id}/media-link")
        assert response.status_code == 200
        assert response.headers["cache-control"] == "no-store"
        assert response.json()["expires_in"] == 300
        url = response.json()["url"]
        assert url.startswith("https://media.example.test/api/media/")
        parsed = urlsplit(url)
        playback = client.get(parsed.path + "?" + parsed.query)
        assert playback.content == media.read_bytes()
        assert "content-disposition" not in playback.headers
        download = urlsplit(response.json()["download_url"])
        exported = client.get(download.path + "?" + download.query)
        assert exported.content == media.read_bytes()
        assert exported.headers["content-disposition"] == f'attachment; filename="{ready_id}.mp4"'
        assert client.get(f"/api/videos/{rejected_id}/media-link").status_code == 409
        assert client.get(f"/api/videos/{missing_id}/media-link").status_code == 404
        assert client.get(f"/api/videos/{ready_id}/file").content == media.read_bytes()
    finally:
        main.app.dependency_overrides.pop(security.current_user, None)
        engine.dispose()
