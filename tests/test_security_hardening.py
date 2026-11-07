import asyncio
from datetime import timedelta

import pytest
from fastapi import HTTPException
from sqlalchemy import select

from factory import main, security
from factory.http_guard import RequestGuard
from factory.models import AuthSession, User
from test_api import api as api_fixture, login  # noqa: F401


@pytest.fixture
def api(api_fixture):  # noqa: F811 - pytest resolves imported fixture by name
    return api_fixture


def test_validation_does_not_echo_password(api):
    client, _ = api
    password = 'do-not-echo-this-secret-' * 60
    response = client.post('/api/auth/login', json={'email': 'admin@example.test', 'password': password})
    assert response.status_code == 422
    assert password not in response.text
    assert all('input' not in error and 'ctx' not in error for error in response.json()['detail'])


def test_upload_authentication_happens_before_multipart_parsing(api):
    client, _ = api
    response = client.post('/api/accounts/missing/logo', content=b'invalid multipart',
                           headers={'Content-Type': 'multipart/form-data; boundary=x'})
    assert response.status_code == 401
    assert response.headers['Cache-Control'] == 'no-store'


def test_viewer_upload_rejected_before_parsing(api):
    client, _ = api
    headers = login(client, 'viewer@example.test')
    response = client.post('/api/accounts/missing/media', content=b'invalid multipart',
                           headers={**headers, 'Content-Type': 'multipart/form-data; boundary=x'})
    assert response.status_code == 403


def test_large_json_rejected_and_normal_login_preserved(api):
    client, _ = api
    response = client.post('/api/auth/login', content=b'x' * (1024 * 1024 + 1))
    assert response.status_code == 413
    login(client)
    assert client.get('/api/auth/me').status_code == 200


async def guard_exchange(events, headers=(), *, timeout=1):
    sent, called = [], []

    async def application(scope, receive, send):
        called.append(await receive())
        await send({'type': 'http.response.start', 'status': 204, 'headers': []})
        await send({'type': 'http.response.body', 'body': b''})

    async def receive():
        if events:
            return events.pop(0)
        await asyncio.sleep(1)
        return {'type': 'http.disconnect'}

    async def send(message):
        sent.append(message)

    guard = RequestGuard(application, json_limit=4, timeout=timeout)
    await guard({'type': 'http', 'method': 'POST', 'path': '/api/test', 'headers': list(headers)}, receive, send)
    return sent, called


def test_chunked_request_size_rejected_before_route():
    sent, called = asyncio.run(guard_exchange([
        {'type': 'http.request', 'body': b'abc', 'more_body': True},
        {'type': 'http.request', 'body': b'def', 'more_body': False},
    ]))
    assert sent[0]['status'] == 413 and not called


def test_small_chunked_body_replayed_exactly():
    sent, called = asyncio.run(guard_exchange([
        {'type': 'http.request', 'body': b'ab', 'more_body': True},
        {'type': 'http.request', 'body': b'cd', 'more_body': False},
    ]))
    assert sent[0]['status'] == 204
    assert called[0]['body'] == b'abcd' and not called[0]['more_body']


@pytest.mark.parametrize('headers,status', [
    ([(b'content-length', b'8')], 413),
    ([(b'content-length', b'abc')], 400),
    ([(b'content-length', b'9' * 5000)], 400),
    ([(b'content-length', b'0'), (b'content-length', b'0')], 400),
    ([(b'content-encoding', b'gzip')], 415),
    ([(b'x-big', b'x' * 17000)], 431),
])
def test_invalid_headers_rejected_without_reading_body(headers, status):
    sent, called = asyncio.run(guard_exchange([], headers))
    assert sent[0]['status'] == status and not called


def test_slow_body_times_out():
    sent, called = asyncio.run(guard_exchange([], timeout=0.001))
    assert sent[0]['status'] == 408 and not called


def test_token_comparison_handles_hostile_unicode():
    assert not security.secure_token_equal('é', 'token')
    assert not security.secure_token_equal('a' * 300, 'a' * 300)
    assert security.secure_token_equal('same', 'same')
    with pytest.raises(HTTPException) as err:
        security.verify_media_signature('video', int(security.time.time()) + 60, 'é')
    assert err.value.status_code == 403


def test_sessions_are_bounded_and_expired_sessions_removed(api):
    _, sessions = api
    with sessions.begin() as session:
        user = session.scalar(select(User).where(User.email == 'admin@example.test'))
        session.add(AuthSession(user_id=user.id, token_hash='old', csrf_token='old',
                                expires_at=security.now_utc() - timedelta(seconds=1)))
        session.flush()
        for _ in range(8):
            security.create_session(session, user.id)
        session.flush()
        remaining = session.scalars(select(AuthSession)).all()
        assert len(remaining) == 5
        assert all(row.token_hash != 'old' for row in remaining)


def test_rate_limit_has_retry_after(api):
    security.rate_limit('security-test', 1, 60)
    with pytest.raises(HTTPException) as err:
        security.rate_limit('security-test', 1, 60)
    assert err.value.status_code == 429 and err.value.headers['Retry-After'] == '60'


def test_production_rate_guard_fails_closed(api, monkeypatch):
    client, _ = api
    monkeypatch.setattr(main.settings, 'mode', 'production')
    def unavailable(*args):
        raise HTTPException(503, 'Güvenlik servisi geçici olarak kullanılamıyor.')
    monkeypatch.setattr(main, 'rate_limit', unavailable)
    response = client.get('/api/accounts')
    assert response.status_code == 503
    assert response.headers['Cache-Control'] == 'no-store'
