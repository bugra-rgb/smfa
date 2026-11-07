"""Opaque sessions, CSRF, encrypted credentials and bounded login attempts."""
import hashlib
import hmac
import json
import secrets
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError
from cryptography.fernet import Fernet
from fastapi import Depends, HTTPException, Request
from sqlalchemy import delete, select

from factory.config import settings
from factory.db import SessionLocal
from factory.models import AuthSession, Connection, OAuthToken, User

password_hasher = PasswordHasher()
_dummy_hash = password_hasher.hash(secrets.token_urlsafe(24))
_memory_limits: dict[str, list[float]] = {}
_rate_lock = threading.Lock()


def now_utc():
    return datetime.now(timezone.utc)


def aware(value: datetime):
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value


def digest(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def _local_secret(name: str, supplied: str, factory):
    if supplied:
        return supplied
    if settings.mode == "production":
        raise RuntimeError(f"{name} eksik")
    path = Path(settings.data_dir) / name
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with path.open("x") as stream:
            path.chmod(0o600)
            stream.write(factory())
    except FileExistsError:
        pass
    return path.read_text().strip()


def signing_secret():
    return _local_secret("session.key", settings.secret_key, lambda: secrets.token_hex(32))


def encrypt_json(payload: dict) -> str:
    key = _local_secret("encryption.key", settings.encryption_key, lambda: Fernet.generate_key().decode())
    return Fernet(key.encode()).encrypt(json.dumps(payload).encode()).decode()


def decrypt_json(payload: str) -> dict:
    key = _local_secret("encryption.key", settings.encryption_key, lambda: Fernet.generate_key().decode())
    return json.loads(Fernet(key.encode()).decrypt(payload.encode()))


def decrypt_credentials(connection_id: str, session, *, mark_expired=True) -> dict:
    """Refresh under a row lock to avoid rotating a refresh token twice."""
    connection = session.get(Connection, connection_id)
    if not connection or connection.status not in ("CONNECTED", "connected"):
        raise RuntimeError("Hesap bağlantısı etkin değil.")
    token = session.scalar(select(OAuthToken).where(OAuthToken.connection_id == connection_id).with_for_update())
    if not token:
        if settings.mode == "mock":
            return {"mock": True, "external_id": connection.external_id}
        raise RuntimeError("OAuth yetkilendirmesi eksik.")
    credentials = decrypt_json(token.encrypted_payload)
    expires = credentials.get("expires_at")
    if expires:
        expires_epoch = float(expires) if isinstance(expires, (int, float)) else datetime.fromisoformat(expires).timestamp()
        if expires_epoch < time.time() + 120:
            from factory.providers.oauth import refresh
            from factory.providers.base import ProviderError
            try:
                credentials = refresh(connection.platform, credentials)
            except ProviderError as exc:
                if mark_expired and exc.code in ("oauth_expired", "oauth_missing", "invalid_grant"):
                    connection.status = "EXPIRED"
                    session.commit()
                raise
            token.encrypted_payload = encrypt_json(credentials)
            token.updated_at = now_utc()
            session.flush()
    credentials.setdefault("external_id", connection.external_id)
    credentials.setdefault("account_id", connection.external_id)
    credentials.setdefault("ig_user_id", connection.external_id)
    return credentials


def hash_password(password: str):
    return password_hasher.hash(password)


def verify_password(password: str, hashed: str | None):
    try:
        valid = password_hasher.verify(hashed or _dummy_hash, password)
        return bool(hashed and valid)
    except (VerificationError, InvalidHashError):
        return False


def rate_limit(key: str, limit: int = 10, window: int = 60):
    if settings.mode == "production":
        import redis
        try:
            client = redis.Redis.from_url(settings.redis_url, socket_connect_timeout=2, socket_timeout=2)
            bucket = f"factory:rate:{digest(key)}:{int(time.time()) // window}"
            with client.pipeline() as pipe:
                pipe.incr(bucket)
                pipe.expire(bucket, window + 1)
                count, _ = pipe.execute()
            if count > limit:
                raise HTTPException(429, "Çok fazla istek. Biraz sonra yeniden deneyin.", headers={"Retry-After": str(window)})
        except redis.RedisError:
            raise HTTPException(503, "Güvenlik servisi geçici olarak kullanılamıyor.") from None
    else:
        with _rate_lock:
            current = time.time()
            if len(_memory_limits) > 10000:
                _memory_limits.clear()
            bucket_times = [t for t in _memory_limits.get(key, []) if t > current - window]
            if len(bucket_times) >= limit:
                raise HTTPException(429, "Çok fazla istek. Biraz sonra yeniden deneyin.", headers={"Retry-After": str(window)})
            _memory_limits[key] = [*bucket_times, current]


def secure_token_equal(first: str, second: str) -> bool:
    # Header/cookie strings may be non-ASCII; compare_digest(str) would raise TypeError.
    if len(first) > 256 or len(second) > 256 or not first.isascii() or not second.isascii():
        return False
    return hmac.compare_digest(first, second)


def check_origin(request: Request):
    origin = request.headers.get("origin")
    allowed = {settings.frontend_origin.rstrip("/"), *[v.strip().rstrip("/") for v in settings.frontend_aliases.split(",") if v.strip()]}
    if origin and origin.rstrip("/") not in allowed:
        raise HTTPException(403, "İstek kaynağı geçersiz.")
    if request.headers.get("sec-fetch-site") == "cross-site":
        raise HTTPException(403, "Çapraz site isteği reddedildi.")


def csrf_preauth():
    nonce = f"{int(time.time())}.{secrets.token_urlsafe(24)}"
    signature = hmac.new(signing_secret().encode(), nonce.encode(), hashlib.sha256).hexdigest()
    return f"{nonce}.{signature}"


def verify_preauth(request: Request):
    check_origin(request)
    token = request.headers.get("x-csrf-token", "")
    if not token or not secure_token_equal(token, request.cookies.get("cf_csrf", "")):
        raise HTTPException(403, "Güvenlik kodu geçersiz. Sayfayı yenileyin.")
    try:
        timestamp, nonce, signature = token.split(".")
        data = f"{timestamp}.{nonce}"
        expected = hmac.new(signing_secret().encode(), data.encode(), hashlib.sha256).hexdigest()
        if not secure_token_equal(signature, expected) or not 0 <= time.time() - int(timestamp) <= 3600:
            raise ValueError()
    except ValueError:
        raise HTTPException(403, "Güvenlik kodunun süresi doldu.") from None


def current_user(request: Request):
    raw = request.cookies.get("cf_session", "")
    if raw and (len(raw) > 128 or not raw.isascii()):
        raise HTTPException(401, "Oturum geçersiz.")
    with SessionLocal() as session:
        auth_session = session.scalar(select(AuthSession).where(AuthSession.token_hash == digest(raw))) if raw else None
        if not auth_session or aware(auth_session.expires_at) <= now_utc():
            raise HTTPException(401, "Oturum açmanız gerekiyor.")
        user = session.get(User, auth_session.user_id)
        if not user or not user.active:
            raise HTTPException(401, "Oturum geçersiz.")
        if request.method not in ("GET", "HEAD", "OPTIONS"):
            check_origin(request)
            supplied = request.headers.get("x-csrf-token", "")
            if not secure_token_equal(supplied, auth_session.csrf_token):
                raise HTTPException(403, "Güvenlik kodu geçersiz.")
        request.state.auth_session_id = auth_session.id
        request.state.csrf_token = auth_session.csrf_token
        return {"id": user.id, "email": user.email, "role": user.role}


def operator(user=Depends(current_user)):
    if user["role"].lower() not in ("admin", "operator"):
        raise HTTPException(403, "Bu işlem için düzenleme yetkisi gerekiyor.")
    return user


def admin(user=Depends(current_user)):
    if user["role"].lower() != "admin":
        raise HTTPException(403, "Yönetici yetkisi gerekiyor.")
    return user


def create_session(session, user_id):
    # Bound stale sessions and keep at most five concurrent browser sessions.
    session.execute(delete(AuthSession).where(AuthSession.expires_at <= now_utc()))
    active = session.scalars(select(AuthSession).where(AuthSession.user_id == user_id)
                             .order_by(AuthSession.expires_at.desc())).all()
    for previous in active[4:]:
        session.delete(previous)
    raw = secrets.token_urlsafe(48)
    row = AuthSession(user_id=user_id, token_hash=digest(raw), csrf_token=secrets.token_urlsafe(32),
                      expires_at=now_utc() + timedelta(hours=settings.session_hours))
    session.add(row)
    session.flush()
    return raw, row


def signed_media_url(video_id: str, lifetime: int = 3600):
    expires = int(time.time()) + min(lifetime, 86400)
    message = f"media:{video_id}:{expires}"
    signature = hmac.new(signing_secret().encode(), message.encode(), hashlib.sha256).hexdigest()
    origin = settings.media_base_url or settings.public_base_url
    return f"{origin.rstrip('/')}/api/media/{video_id}?expires={expires}&signature={signature}"


def verify_media_signature(video_id: str, expires: int, signature: str):
    message = f"media:{video_id}:{expires}"
    expected = hmac.new(signing_secret().encode(), message.encode(), hashlib.sha256).hexdigest()
    if expires < time.time() or expires > time.time() + 86400 or not secure_token_equal(signature, expected):
        raise HTTPException(403, "Medya bağlantısı geçersiz veya süresi dolmuş.")
