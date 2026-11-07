from datetime import timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import sessionmaker

from factory import main, security
from factory.config import Settings, settings
from factory.db import make_engine
from factory.models import AuthSession, Base, Connection, OAuthState, OAuthToken, User


@pytest.fixture
def api(tmp_path, monkeypatch):
    engine = make_engine("sqlite://")
    Base.metadata.create_all(engine)
    sessions = sessionmaker(bind=engine, expire_on_commit=False)
    monkeypatch.setattr(main, "SessionLocal", sessions)
    monkeypatch.setattr(security, "SessionLocal", sessions)
    monkeypatch.setattr(settings, "data_dir", str(tmp_path))
    monkeypatch.setattr(settings, "mode", "mock")
    security._memory_limits.clear()
    with sessions.begin() as session:
        session.add(User(email="admin@example.test", password_hash=security.hash_password("long-secure-password"), role="admin"))
        session.add(User(email="viewer@example.test", password_hash=security.hash_password("long-secure-password"), role="viewer"))
    client = TestClient(main.app)
    yield client, sessions
    engine.dispose()


def login(client, email="admin@example.test"):
    csrf = client.get("/api/auth/csrf").json()["csrf_token"]
    response = client.post("/api/auth/login", json={"email": email, "password": "long-secure-password"}, headers={"X-CSRF-Token": csrf})
    assert response.status_code == 200, response.text
    return {"X-CSRF-Token": response.json()["csrf_token"]}


def test_auth_csrf_and_session_revocation(api):
    client, sessions = api
    assert client.get("/api/accounts").status_code == 401
    assert client.post("/api/auth/login", json={"email": "admin@example.test", "password": "long-secure-password"}).status_code == 403
    headers = login(client)
    assert client.get("/api/auth/me").json()["role"] == "admin"
    assert client.post("/api/system/pause").status_code == 403
    assert client.post("/api/system/pause", headers={**headers, "Origin": "https://evil.example"}).status_code == 403
    assert client.post("/api/system/pause", headers=headers).json() == {"paused": True}
    assert client.post("/api/auth/logout", headers=headers).status_code == 200
    assert client.get("/api/auth/me").status_code == 401
    with sessions() as session:
        assert session.scalar(select(AuthSession)) is None


def test_account_validation_edit_and_rbac(api):
    client, _ = api
    headers = login(client)
    payload = {"name": "FutbolTR", "niche": "Futbol", "daily_target": 5}
    bad = client.post("/api/accounts", json={**payload, "timezone": "Earth/Missing"}, headers=headers)
    assert bad.status_code == 422
    assert client.post("/api/accounts", json={**payload, "settings": {"logo_path": "/etc/passwd"}}, headers=headers).status_code == 422
    created = client.post("/api/accounts", json=payload, headers=headers)
    assert created.status_code == 201, created.text
    account_id = created.json()["id"]
    response = client.patch(f"/api/accounts/{account_id}", json={"auto_publish": True, "approval_mode": "AUTO_PUBLISH", "settings": {"duration": 20}}, headers=headers)
    assert response.status_code == 200, response.text
    assert response.json()["approval_mode"] == "AUTO_PUBLISH"
    assert response.json()["settings"]["inventory_target"] == 5
    assert client.post(f"/api/accounts/{account_id}/pause", headers=headers).json()["active"] is False
    assert client.get(f"/api/accounts/{account_id}").json()["connections"] == []
    client.post("/api/auth/logout", headers=headers)
    viewer_headers = login(client, "viewer@example.test")
    assert client.get("/api/accounts").status_code == 200
    assert client.post(f"/api/accounts/{account_id}/resume", headers=viewer_headers).status_code == 403
    assert client.post("/api/system/resume", headers=viewer_headers).status_code == 403


def test_mock_connections_encrypted_and_logo_upload_route(api):
    from io import BytesIO
    from PIL import Image
    client, sessions = api
    headers = login(client)
    account_id = client.post("/api/accounts", json={"name": "Teknoloji", "niche": "Teknoloji"}, headers=headers).json()["id"]
    response = client.post("/api/oauth/drive/start", json={"account_id": account_id}, headers=headers)
    assert response.status_code == 200 and response.json()["mock"]
    with sessions() as session:
        token = session.scalar(select(OAuthToken))
        assert "mock" not in token.encrypted_payload
        assert security.decrypt_json(token.encrypted_payload)["mock"] is True
        connection = session.scalar(select(Connection))
        assert connection.settings["mock"]
    buffer = BytesIO()
    Image.new("RGBA", (16, 16), "red").save(buffer, "PNG")
    response = client.post(f"/api/accounts/{account_id}/logo", files={"file": ("logo.png", buffer.getvalue(), "image/png")}, headers=headers)
    assert response.status_code == 200, response.text
    assert response.json()["logo_path"].endswith(".png")
    assert client.post(f"/api/accounts/{account_id}/logo", files={"file": ("x.svg", b"<script>alert(1)</script>")}, headers=headers).status_code == 422


def test_oauth_state_one_use_and_user_binding(api, monkeypatch):
    client, sessions = api
    headers = login(client)
    account_id = client.post("/api/accounts", json={"name": "Haber", "niche": "Haber"}, headers=headers).json()["id"]
    with sessions.begin() as session:
        user = session.scalar(select(User).where(User.email == "admin@example.test"))
        session.add(OAuthState(state_hash=security.digest("valid-state"), user_id=user.id, account_id=account_id,
                               provider="drive", encrypted_verifier=security.encrypt_json({"verifier": "test"}),
                               expires_at=security.now_utc() + timedelta(minutes=10), used=False))
    assert client.get("/api/oauth/drive/callback?state=bad&code=x").status_code == 400
    response = client.get("/api/oauth/drive/callback?state=valid-state&error=access_denied", follow_redirects=False)
    assert response.status_code == 307
    assert client.get("/api/oauth/drive/callback?state=valid-state&code=x").status_code == 400


def test_production_config_and_encryption(api):
    with pytest.raises(ValueError, match="PostgreSQL"):
        Settings(mode="production", database_url="sqlite://", _env_file=None)
    token = security.encrypt_json({"access_token": "sensitive", "refresh_token": "secret"})
    assert "sensitive" not in token
    assert security.decrypt_json(token)["refresh_token"] == "secret"
    with pytest.raises(Exception):
        security.decrypt_json(token[:-5] + "broken")


def test_signed_media_tamper_and_expiry(api):
    from urllib.parse import parse_qs, urlparse
    url = security.signed_media_url("video-1", 300)
    query = parse_qs(urlparse(url).query)
    expires, signature = int(query["expires"][0]), query["signature"][0]
    security.verify_media_signature("video-1", expires, signature)
    with pytest.raises(Exception):
        security.verify_media_signature("video-2", expires, signature)
    with pytest.raises(Exception):
        security.verify_media_signature("video-1", 0, signature)
