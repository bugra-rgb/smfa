"""Türkçe yönetim API'si. Üretim başlatmadan önce Alembic migration çalıştırılır."""
import io
import json
import logging
import secrets
import time
from collections import defaultdict
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path
from urllib.parse import urlencode

from fastapi import Depends, FastAPI, HTTPException, Request, Response, UploadFile
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from PIL import Image, UnidentifiedImageError
from prometheus_client import CONTENT_TYPE_LATEST, Counter, Histogram, generate_latest
from sqlalchemy import func, inspect, select, text
from sqlalchemy.exc import IntegrityError
from starlette.middleware.trustedhost import TrustedHostMiddleware

from factory.config import settings
from factory.http_guard import RequestGuard, is_brand_upload
from factory.db import SessionLocal
from factory.models import (AIRun, Account, Analytics, AuthSession, Connection, DriveConnection,
                            DriveFile, DriveFolder, Idea, Job, Notification, OAuthState, OAuthToken,
                            PublishedPost, PublishJob, SystemLog, SystemState, User, Video)
from factory.schemas import AccountCreate, DEFAULT_ACCOUNT_SETTINGS, LoginInput, OAuthStartInput, UserCreate
from factory.security import (admin, aware, create_session, csrf_preauth, current_user,
                              decrypt_json, digest, encrypt_json, hash_password, now_utc, operator,
                              rate_limit, verify_media_signature, verify_password, verify_preauth)
from factory.services.jobs import AccountPaused, PermanentFailure

app = FastAPI(title="SMF — Social Media Fabrica", version="0.1.0",
              docs_url="/api/docs" if settings.mode == "mock" else None,
              redoc_url="/redoc" if settings.mode == "mock" else None,
              openapi_url="/openapi.json" if settings.mode == "mock" else None)
app.add_middleware(RequestGuard)
app.add_middleware(TrustedHostMiddleware, allowed_hosts=settings.allowed_hosts.split(","))
requests_total = Counter("factory_http_requests_total", "HTTP requests", ["method", "status"])
request_seconds = Histogram("factory_http_request_seconds", "HTTP request latency")
logger = logging.getLogger("factory")

# Specific feature routes precede the legacy /accounts/{id}/{action} route.
from factory.update_api import router as update_router
app.include_router(update_router)
from factory.captions_api import router as captions_router
app.include_router(captions_router)
from factory.xnews_api import router as xnews_router
app.include_router(xnews_router)


@app.middleware("http")
async def security_headers(request: Request, call_next):
    started = time.monotonic()
    try:
        if settings.mode == "production" and request.url.path.startswith("/api/"):
            # Use the trusted transport client, never an arbitrary forwarded header.
            address = request.client.host if request.client else "unknown"
            await run_in_threadpool(rate_limit, f"api:ip:{address}", 240, 60)
            if request.url.path == "/api/auth/login":
                await run_in_threadpool(rate_limit, "login:global", 60, 60)
        if is_brand_upload(request.url.path, request.method):
            # FastAPI normally parses multipart before dependencies. Authenticate first.
            upload_user = await run_in_threadpool(current_user, request)
            if upload_user["role"].lower() not in ("admin", "operator"):
                raise HTTPException(403, "Dosya yüklemek için düzenleme yetkisi gerekiyor.")
            if settings.mode == "production":
                await run_in_threadpool(rate_limit, f"upload:user:{upload_user['id']}", 10, 60)
        response = await call_next(request)
    except HTTPException as exc:
        response = JSONResponse({"detail": exc.detail}, status_code=exc.status_code, headers=exc.headers)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
    response.headers["Cache-Control"] = "no-store"
    response.headers["Content-Security-Policy"] = "default-src 'none'; frame-ancestors 'none'; base-uri 'none'; form-action 'none'" if settings.mode == "production" else "frame-ancestors 'none'"
    if request.url.path == "/api/control/hashtags/panel" and response.headers.get("content-type", "").startswith("text/html"):
        response.headers["Content-Security-Policy"] = "default-src 'none'; script-src 'self'; style-src 'self'; connect-src 'self'; img-src 'self'; base-uri 'none'; frame-ancestors 'none'; form-action 'none'; object-src 'none'"

    response.headers["X-Robots-Tag"] = "noindex, nofollow"
    if settings.mode == "production":
        response.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
    requests_total.labels(request.method, str(response.status_code)).inc()
    request_seconds.observe(time.monotonic() - started)
    return response


@app.exception_handler(RequestValidationError)
async def invalid_request(request, exc):
    # Pydantic's default includes the submitted value (including login passwords).
    errors = [{"loc": item["loc"], "type": item["type"], "msg": "Geçersiz alan değeri."}
              for item in exc.errors()]
    return JSONResponse(status_code=422, content={"detail": errors})


@app.exception_handler(IntegrityError)
async def duplicate_error(request, exc):
    return JSONResponse(status_code=409, content={"detail": "Bu kayıt zaten var veya başka bir işlem tarafından değiştirildi."})


@app.exception_handler(AccountPaused)
@app.exception_handler(PermanentFailure)
async def workflow_conflict(request, exc):
    return JSONResponse(status_code=409, content={"detail": str(exc)})


def record(row, exclude=()):
    private = {"encrypted_payload", "encrypted_verifier", "token_hash", "csrf_token", "checkpoint"}
    return jsonable_encoder({column.key: getattr(row, column.key) for column in inspect(type(row)).columns if column.key not in (*exclude, *private)}, custom_encoder={Decimal: float})


def audit(session, event, user=None, account_id=None, data=None):
    session.add(SystemLog(account_id=account_id, level="INFO", event=event, message=event,
                          data={"user_id": (user or {}).get("id"), **(data or {})}))


def required(session, model, record_id):
    row = session.get(model, record_id)
    if not row:
        raise HTTPException(404, "Kayıt bulunamadı.")
    return row


def safe_file(path):
    if not path:
        raise HTTPException(404, "Dosya henüz hazır değil.")
    resolved = Path(path).resolve()
    try:
        resolved.relative_to(Path(settings.data_dir).resolve())
    except ValueError:
        raise HTTPException(403, "Bu dosyaya erişim yok.") from None
    if not resolved.is_file():
        raise HTTPException(404, "Dosya bulunamadı.")
    return resolved


@app.get("/health")
def health():
    return {"status": "ok", "mode": settings.mode}


def service_health():
    result = {"database": "unavailable", "redis": "unavailable"}
    try:
        with SessionLocal() as session:
            session.execute(text("SELECT 1"))
            result["database"] = "ok"
    except Exception:
        pass
    try:
        import redis
        redis.Redis.from_url(settings.redis_url, socket_connect_timeout=1, socket_timeout=1).ping()
        result["redis"] = "ok"
    except Exception:
        pass
    return result


@app.get("/ready")
def ready():
    status = service_health()
    return JSONResponse(status_code=200 if all(v == "ok" for v in status.values()) else 503, content=status)


@app.get("/metrics")
def metrics(user=Depends(admin)):
    return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)


@app.get("/api/auth/csrf")
def get_csrf(request: Request, response: Response):
    try:
        current_user(request)
        return {"csrf_token": request.state.csrf_token}
    except HTTPException:
        token = csrf_preauth()
        response.set_cookie("cf_csrf", token, httponly=True, secure=settings.mode == "production", samesite="lax", max_age=3600)
        return {"csrf_token": token}


@app.post("/api/auth/login")
def login(data: LoginInput, request: Request, response: Response):
    verify_preauth(request)
    email = data.email.strip().lower()
    rate_limit(f"login:ip:{request.client.host if request.client else 'unknown'}", 20, 60)
    rate_limit(f"login:email:{email}", 10, 300)
    with SessionLocal.begin() as session:
        user = session.scalar(select(User).where(User.email == email))
        valid = verify_password(data.password, user.password_hash if user else None)
        if not valid or not user or not user.active:
            raise HTTPException(401, "E-posta veya şifre yanlış.")
        raw, auth_session = create_session(session, user.id)
        response.set_cookie("cf_session", raw, httponly=True, secure=settings.mode == "production", samesite="lax", max_age=settings.session_hours * 3600)
        audit(session, "auth.login", {"id": user.id})
        return {"email": user.email, "role": user.role, "csrf_token": auth_session.csrf_token}


@app.get("/api/auth/me")
def me(user=Depends(current_user)):
    return user


@app.post("/api/auth/logout")
def logout(request: Request, response: Response, user=Depends(current_user)):
    with SessionLocal.begin() as session:
        row = session.get(AuthSession, request.state.auth_session_id)
        if row:
            session.delete(row)
    response.delete_cookie("cf_session")
    return {"ok": True}


@app.post("/api/users")
def create_user(data: UserCreate, user=Depends(admin)):
    with SessionLocal.begin() as session:
        row = User(email=data.email.strip().lower(), password_hash=hash_password(data.password), role=data.role)
        session.add(row)
        session.flush()
        audit(session, "user.created", user, data={"created_user_id": row.id})
        return record(row, ("password_hash",))


@app.get("/api/accounts")
def accounts(user=Depends(current_user)):
    with SessionLocal() as session:
        return [record(row) for row in session.scalars(select(Account).order_by(Account.created_at.desc())) if not (row.settings or {}).get("smfa_archived_v1")]


@app.post("/api/accounts", status_code=201)
def create_account(data: AccountCreate, user=Depends(operator)):
    payload = data.model_dump()
    defaults = json.loads(json.dumps(DEFAULT_ACCOUNT_SETTINGS))
    if settings.mode == "production":
        defaults.update({"llm_provider": "openai", "video_provider": "image_motion", "voice": {"provider": "openai", "voice": "alloy", "speed": 1}})
    payload["settings"] = {**defaults, **payload["settings"]}
    with SessionLocal.begin() as session:
        row = Account(**payload)
        session.add(row)
        session.flush()
        audit(session, "account.created", user, row.id)
        return record(row)


@app.get("/api/accounts/{account_id}")
def account_detail(account_id: str, user=Depends(current_user)):
    with SessionLocal() as session:
        result = record(required(session, Account, account_id))
        result["connections"] = [record(row) for row in session.scalars(select(Connection).where(Connection.account_id == account_id))]
        result["drive_folders"] = [record(row) for row in session.scalars(select(DriveFolder).where(DriveFolder.account_id == account_id))]
        return result


@app.patch("/api/accounts/{account_id}")
def update_account(account_id: str, data: dict, user=Depends(operator)):
    with SessionLocal.begin() as session:
        row = required(session, Account, account_id)
        if (row.settings or {}).get("smfa_archived_v1"):
            raise HTTPException(409, "Hesap arşivlendi; önce arşivden çıkarılmalı.")
        existing = {field: getattr(row, field) for field in AccountCreate.model_fields}
        if "settings" in data:
            if not isinstance(data["settings"], dict):
                raise HTTPException(422, "Ayarlar nesne olmalı.")
            data["settings"] = {**row.settings, **data["settings"]}
        try:
            validated = AccountCreate.model_validate({**existing, **data})
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from None
        for key, value in validated.model_dump().items():
            setattr(row, key, value)
        audit(session, "account.updated", user, row.id)
        session.flush()
        return record(row)


def account_action(account_id: str, action: str, user=Depends(operator)):
    if action not in ("pause", "resume", "run"):
        raise HTTPException(404, "İşlem bulunamadı.")
    if action == "run":
        from factory.services.pipeline import run_pipeline
        with SessionLocal() as session:
            required(session, Account, account_id)
        return run_pipeline(account_id)
    with SessionLocal.begin() as session:
        row = required(session, Account, account_id)
        if (row.settings or {}).get("smfa_archived_v1"):
            raise HTTPException(409, "Hesap arşivlendi; önce arşivden çıkarılmalı.")
        row.active = action == "resume"
        audit(session, f"account.{action}", user, row.id)
        return {"active": row.active}


@app.post("/api/system/{action}")
def global_action(action: str, user=Depends(admin)):
    if action not in ("pause", "resume"):
        raise HTTPException(404, "İşlem bulunamadı.")
    with SessionLocal.begin() as session:
        state = session.get(SystemState, "global")
        if not state:
            state = SystemState(id="global", paused=False)
            session.add(state)
        state.paused = action == "pause"
        audit(session, f"system.{action}", user)
        return {"paused": state.paused}


@app.get("/api/dashboard")
def dashboard(user=Depends(current_user)):
    with SessionLocal() as session:
        def count(model, *conditions):
            return session.scalar(select(func.count()).select_from(model).where(*conditions)) or 0
        today = now_utc().replace(hour=0, minute=0, second=0, microsecond=0)
        state = session.get(SystemState, "global")
        return {"accounts_total": count(Account), "accounts_active": count(Account, Account.active.is_(True)),
                "daily_target": session.scalar(select(func.sum(Account.daily_target)).where(Account.active.is_(True))) or 0,
                "in_production": count(Job, Job.status == "RUNNING"), "ready": count(Video, Video.status == "READY"),
                "scheduled": count(PublishJob, PublishJob.status == "SCHEDULED"),
                "published_today": count(PublishedPost, PublishedPost.published_at >= today),
                "failed": count(Job, Job.status.in_(["FAILED", "DLQ", "BLOCKED", "UNKNOWN"])),
                "cost_today": float(session.scalar(select(func.sum(AIRun.actual_cost)).where(AIRun.created_at >= today)) or 0),
                "estimated_cost_today": float(session.scalar(select(func.sum(AIRun.estimated_cost)).where(AIRun.created_at >= today)) or 0),
                "cost_status": "unknown" if count(AIRun, AIRun.created_at >= today, AIRun.actual_cost.is_(None)) else "known",
                "cost_currency": "USD", "mode": settings.mode, "paused": state.paused if state else False,
                "health": service_health()}


LIST_MODELS = {"ideas": Idea, "videos": Video, "jobs": Job, "publishing": PublishJob,
               "analytics": Analytics, "logs": SystemLog, "notifications": Notification}


def list_endpoint(model):
    def endpoint(account_id: str | None = None, offset: int = 0, limit: int = 100, user=Depends(current_user)):
        if not 1 <= limit <= 500 or offset < 0:
            raise HTTPException(422, "Sayfalama değeri geçersiz.")
        with SessionLocal() as session:
            query = select(model)
            if account_id:
                query = query.where(model.account_id == account_id)
            return [record(row) for row in session.scalars(query.order_by(model.created_at.desc()).offset(offset).limit(limit))]
    return endpoint


for resource, model_class in LIST_MODELS.items():
    app.add_api_route(f"/api/{resource}", list_endpoint(model_class), methods=["GET"], name=f"list_{resource}")


@app.get("/api/drive")
def drive_status(account_id: str | None = None, user=Depends(current_user)):
    with SessionLocal() as session:
        query = select(Account)
        if account_id:
            query = query.where(Account.id == account_id)
        result = []
        for row in session.scalars(query):
            connection = session.scalar(select(DriveConnection).where(DriveConnection.account_id == row.id))
            result.append({"account_id": row.id, "account_name": row.name,
                           "status": connection.status if connection else "DISCONNECTED",
                           "last_sync": connection.last_sync if connection else None,
                           "folders": [record(f) for f in session.scalars(select(DriveFolder).where(DriveFolder.account_id == row.id))],
                           "file_count": session.scalar(select(func.count()).select_from(DriveFile).where(DriveFile.account_id == row.id)) or 0})
        return result


@app.post("/api/drive/{account_id}/sync")
def drive_sync(account_id: str, user=Depends(operator)):
    from factory.services.jobs import enqueue_job
    with SessionLocal.begin() as session:
        required(session, Account, account_id)
        row = enqueue_job(session, kind="drive_sync", account_id=account_id,
                          idempotency_key=f"drive_sync:{account_id}:{int(time.time()) // 60}")
        return {"job_id": row.id, "status": row.status}


@app.post("/api/videos/{video_id}/approve")
def approve_video(video_id: str, user=Depends(operator)):
    with SessionLocal.begin() as session:
        video = required(session, Video, video_id)
        if not video.qa.get("passed") or video.status not in ("AWAITING_APPROVAL", "DRAFT", "READY", "PENDING_APPROVAL"):
            raise HTTPException(409, "Kalite kontrolünü geçen ve onay bekleyen bir video seçin.")
        video.status = "READY"
        video.metadata_json = {**video.metadata_json, "approved": True, "approved_at": now_utc().isoformat()}
        audit(session, "video.approved", user, video.account_id, {"video_id": video.id})
        return {"status": video.status}


@app.post("/api/jobs/{job_id}/retry")
def retry_job(job_id: str, user=Depends(operator)):
    with SessionLocal.begin() as session:
        job = required(session, Job, job_id)
        if job.kind == "PUBLICATION_REQUEST":
            raise HTTPException(409, "Paylaşım talebi için Paylaşım merkezini kullanın.")
        if job.status not in ("FAILED", "DLQ", "BLOCKED") or job.kind.upper() == "PUBLISH" and job.status == "BLOCKED":
            raise HTTPException(409, "Bu iş güvenli biçimde tekrar başlatılamaz; bağlantı ve yayın durumunu kontrol edin.")
        job.status, job.attempts, job.error_message, job.run_after = "QUEUED", 0, None, now_utc()
        audit(session, "job.retried", user, job.account_id, {"job_id": job.id})
        return {"status": job.status}


@app.post("/api/publishing/{publish_id}/cancel")
def cancel_publish(publish_id: str, user=Depends(operator)):
    with SessionLocal.begin() as session:
        row = session.scalar(select(PublishJob).where(PublishJob.id == publish_id).with_for_update())
        if not row: raise HTTPException(404, "Yayın bulunamadı.")
        if row.checkpoint or session.scalar(select(Job.id).where(Job.idempotency_key == row.idempotency_key, Job.status == "RUNNING")):
            raise HTTPException(409, "Başlamış platform işlemi iptal edilemez; sonucunu bekleyin.")
        if row.status in ("PUBLISHING", "PUBLISHED", "UNKNOWN"):
            raise HTTPException(409, "Başlamış veya belirsiz durumdaki yayın iptal edilemez.")
        row.status = "CANCELLED"
        for job in session.scalars(select(Job).where(Job.idempotency_key == row.idempotency_key)):
            if job.status != "RUNNING":
                job.status = "CANCELLED"
        session.flush()
        if not session.scalar(select(PublishJob.id).where(PublishJob.video_id == row.video_id, PublishJob.status != "CANCELLED")):
            video = session.get(Video, row.video_id)
            if video: video.status = "CANCELLED"
        audit(session, "publish.cancelled", user, row.account_id, {"publish_job_id": row.id})
        return {"status": row.status}


@app.post("/api/publishing/{publish_id}/reschedule")
def reschedule(publish_id: str, data: dict, user=Depends(operator)):
    try:
        when = datetime.fromisoformat(data["scheduled_at"].replace("Z", "+00:00"))
        if when.tzinfo is None or when <= now_utc():
            raise ValueError()
    except (KeyError, TypeError, ValueError, AttributeError):
        raise HTTPException(422, "Saat dilimi içeren gelecekte bir tarih girin.") from None
    with SessionLocal.begin() as session:
        row = required(session, PublishJob, publish_id)
        if row.status not in ("DRAFT", "READY", "SCHEDULED", "CANCELLED"):
            raise HTTPException(409, "Bu yayının saati değiştirilemez.")
        row.scheduled_at, row.status = when, "SCHEDULED"
        for job in session.scalars(select(Job).where(Job.idempotency_key == row.idempotency_key)):
            if job.status == "RUNNING":
                raise HTTPException(409, "Yayın işi başladı; saati değiştirilemez.")
            job.run_after, job.status = when, "QUEUED"
        return record(row)


@app.get("/api/videos/{video_id}/file")
def video_file(video_id: str, user=Depends(current_user)):
    with SessionLocal() as session:
        video = required(session, Video, video_id)
        return FileResponse(safe_file(video.local_path), media_type="video/mp4", filename=f"{video.id}.mp4")


@app.get("/api/videos/{video_id}/media-link")
def video_media_link(video_id: str, user=Depends(current_user)):
    """Issue a short media URL after authentication; media bytes stay on the origin."""
    from factory.security import signed_media_url

    with SessionLocal() as session:
        video = required(session, Video, video_id)
        if not video.qa.get("passed"):
            raise HTTPException(409, "Video yayına hazır değil.")
        safe_file(video.local_path)
        url = signed_media_url(video.id, lifetime=300)
        return {"url": url, "download_url": f"{url}&download=1", "expires_in": 300}


@app.api_route("/api/media/{video_id}", methods=["GET", "HEAD"])
def public_media(video_id: str, expires: int, signature: str, download: bool = False):
    verify_media_signature(video_id, expires, signature)
    with SessionLocal() as session:
        video = required(session, Video, video_id)
        if not video.qa.get("passed"):
            raise HTTPException(409, "Video yayına hazır değil.")
        return FileResponse(safe_file(video.local_path), media_type="video/mp4",
                            filename=f"{video.id}.mp4" if download else None)


@app.post("/api/accounts/{account_id}/logo")
async def upload_logo(account_id: str, file: UploadFile, user=Depends(operator)):
    with SessionLocal() as session:
        required(session, Account, account_id)
    content = await file.read(5 * 1024 * 1024 + 1)
    if len(content) > 5 * 1024 * 1024:
        raise HTTPException(413, "Logo en fazla 5 MB olabilir.")
    try:
        uploaded: Image.Image = Image.open(io.BytesIO(content))
        if uploaded.width > 4096 or uploaded.height > 4096:
            raise ValueError()
        uploaded.load()
        uploaded = uploaded.convert("RGBA")
    except (UnidentifiedImageError, OSError, ValueError, Image.DecompressionBombError):
        raise HTTPException(422, "Geçerli bir PNG, JPEG veya WebP görseli yükleyin (en fazla 4096 piksel).") from None
    destination = Path(settings.data_dir).resolve() / "logos" / f"{account_id}-{secrets.token_hex(8)}.png"
    destination.parent.mkdir(parents=True, exist_ok=True)
    uploaded.save(destination, format="PNG")
    with SessionLocal.begin() as session:
        row = required(session, Account, account_id)
        if (row.settings or {}).get("smfa_archived_v1"):
            raise HTTPException(409, "Hesap arşivlendi; önce arşivden çıkarılmalı.")
        row.settings = {**row.settings, "logo_path": str(destination)}
        audit(session, "account.logo_uploaded", user, account_id)
        return {"logo_path": str(destination)}


@app.post("/api/videos/{video_id}/{action}")
def modify_video(video_id: str, action: str, user=Depends(operator)):
    if action not in ("regenerate", "render"):
        raise HTTPException(404, "İşlem bulunamadı.")
    from factory.services.jobs import enqueue_job
    with SessionLocal.begin() as session:
        video = required(session, Video, video_id)
        ongoing = session.scalar(select(PublishJob).where(PublishJob.video_id == video_id, PublishJob.status.in_(["PUBLISHING", "PUBLISHED", "UNKNOWN"])))
        if ongoing:
            raise HTTPException(409, "Yayını başlamış videoyu değiştirmek için yeni içerik oluşturun.")
        for row in session.scalars(select(PublishJob).where(PublishJob.video_id == video_id)):
            row.status = "CANCELLED"
        video.status = "DRAFT"
        video.qa = {}
        source_payload = {"drive_file_id": video.source_file_id} if video.source_type == "DRIVE" else {}
        rerender = action == "render" or video.source_type == "DRIVE"
        kind = "RERENDER" if rerender else "AI_PIPELINE"
        row = enqueue_job(session, kind=kind, account_id=video.account_id,
                          video_id=video.id if rerender else None, payload=source_payload, idempotency_key=f"override:{video.id}:{secrets.token_hex(8)}")
        if not rerender:
            video.status = "REPLACED"
        audit(session, f"video.{action}", user, video.account_id, {"video_id": video_id})
        return {"job_id": row.id, "status": row.status}


@app.post("/api/accounts/{account_id}/media")
async def upload_account_media(account_id: str, kind: str, file: UploadFile, user=Depends(operator)):
    if kind not in ("music", "intro", "outro"):
        raise HTTPException(422, "Medya türü music, intro veya outro olmalı.")
    with SessionLocal() as session:
        required(session, Account, account_id)
    suffix = Path(file.filename or "").suffix.lower()
    allowed = {".wav", ".mp3", ".m4a"} if kind == "music" else {".mp4", ".mov", ".webm"}
    if suffix not in allowed:
        raise HTTPException(422, "Bu medya türü desteklenmiyor.")
    destination = Path(settings.data_dir).resolve() / "branding" / account_id / f"{kind}-{secrets.token_hex(10)}{suffix}"
    destination.parent.mkdir(parents=True, exist_ok=True)
    size = 0
    try:
        with destination.open("xb") as stream:
            while chunk := await file.read(1024 * 1024):
                size += len(chunk)
                if size > 9 * 1024 * 1024:
                    raise HTTPException(413, "Marka medyası en fazla 9 MiB olabilir.")
                stream.write(chunk)
        from factory.media.engine import _probe
        info = await run_in_threadpool(_probe, destination)
        expected = "audio" if kind == "music" else "video"
        if not any(s.get("codec_type") == expected for s in info.get("streams", [])):
            raise ValueError("Medya akışı bulunamadı.")
        duration = float(info.get("format", {}).get("duration", 0))
        if duration <= 0 or duration > (600 if kind == "music" else 30):
            raise ValueError("Müzik 600, intro/outro 30 saniyeyi aşamaz.")
    except HTTPException:
        destination.unlink(missing_ok=True)
        raise
    except Exception:
        destination.unlink(missing_ok=True)
        raise HTTPException(422, "Dosya doğrulanamadı. Müzik en fazla 600, intro/outro 30 saniye olmalı.") from None
    with SessionLocal.begin() as session:
        account = required(session, Account, account_id)
        account.settings = {**account.settings, f"{kind}_path": str(destination)}
        audit(session, "account.media_uploaded", user, account_id, {"kind": kind})
        return {f"{kind}_path": str(destination)}


@app.delete("/api/videos/{video_id}")
def delete_video(video_id: str, user=Depends(operator)):
    with SessionLocal.begin() as session:
        video = required(session, Video, video_id)
        if session.scalar(select(PublishJob).where(PublishJob.video_id == video_id, PublishJob.status.in_(["PUBLISHING", "UNKNOWN"]))):
            raise HTTPException(409, "Devam eden yayın önce uzlaştırılmalı.")
        for row in session.scalars(select(PublishJob).where(PublishJob.video_id == video_id, PublishJob.status != "PUBLISHED")):
            row.status = "CANCELLED"
        video.status = "DELETED"
        audit(session, "video.deleted", user, video.account_id, {"video_id": video_id})
        return {"status": "DELETED", "detail": "Kuyruktan kaldırıldı; yayın ve denetim geçmişi korundu."}


@app.get("/api/providers")
def providers(user=Depends(current_user)):
    return [
        {"name": "Mock", "id": "mock", "kind": "test", "status": "AVAILABLE" if settings.mode == "mock" else "DISABLED", "detail": "Yerel test; gerçek paylaşım ve ücretli üretim yapmaz."},
        {"name": "OpenAI", "id": "openai", "kind": "llm,image,voice", "status": "CONFIGURED" if settings.openai_api_key else "MISSING", "detail": "Bağlantı ve kota gerçek istek sırasında doğrulanır."},
        {"name": "Görselden video", "id": "image_motion", "kind": "video", "status": "AVAILABLE", "detail": "Üretilen görselleri hareket, ses ve altyazıyla birleştirir."},
        {"name": "YouTube", "id": "youtube", "kind": "publisher", "status": "CONFIGURED" if settings.youtube_client_id or settings.google_client_id else "MISSING", "detail": "Herkese açık yükleme için proje denetimi gerekebilir."},
        {"name": "Instagram", "id": "instagram", "kind": "publisher", "status": "CONFIGURED" if settings.instagram_client_id else "MISSING", "detail": "Profesyonel hesap, uygulama yetkileri ve erişilebilir HTTPS medya gerekir."},
        {"name": "TikTok", "id": "tiktok", "kind": "publisher", "status": "CONFIGURED" if settings.buffer_api_key else "MISSING", "detail": "Buffer Free üzerinden otomatik yayın; toplam 3 kanal ve istek bütçesi."},
    ]


@app.get("/api/costs")
def costs(account_id: str | None = None, days: int = 90, user=Depends(current_user)):
    if not 1 <= days <= 3660:
        raise HTTPException(422, "Gün aralığı 1–3660 arasında olmalı.")
    cutoff = now_utc() - timedelta(days=days)
    with SessionLocal() as session:
        query = select(AIRun).where(AIRun.created_at >= cutoff)
        if account_id:
            query = query.where(AIRun.account_id == account_id)
        rows = list(session.scalars(query.order_by(AIRun.created_at)))
        def summarize(items):
            actual = [float(r.actual_cost) for r in items if r.actual_cost is not None]
            estimated = [float(r.estimated_cost) for r in items if r.estimated_cost is not None]
            return {"actual": sum(actual) if actual else None, "estimated": sum(estimated) if estimated else None,
                    "unpriced_runs": sum(r.actual_cost is None and r.estimated_cost is None for r in items),
                    "actual_missing_runs": len(items) - len(actual), "estimated_missing_runs": len(items) - len(estimated),
                    "runs": len(items)}
        def grouped(key, label):
            groups = defaultdict(list)
            for row in rows:
                groups[key(row)].append(row)
            return [{label: value, **summarize(items)} for value, items in groups.items()]
        total = summarize(rows)
        return {"currency": "USD", "timezone": "UTC", "days": days,
                "actual_total": total.pop("actual"), "estimated_total": total.pop("estimated"), **total,
                "by_account": grouped(lambda r: r.account_id, "account_id"),
                "by_platform": grouped(lambda r: r.platform or "shared", "platform"),
                "daily": grouped(lambda r: aware(r.created_at).strftime("%Y-%m-%d"), "date"),
                "monthly": grouped(lambda r: aware(r.created_at).strftime("%Y-%m"), "month")}


@app.get("/api/readiness")
def readiness(user=Depends(current_user)):
    health_result = service_health()
    return [
        {"name": "Drive yayın planı", "status": "ok", "detail": "Hesaba özel klasör ve açıklama; saat aralığı v1."},
        {"name": "Çalışma modu", "status": "ok" if settings.mode == "production" else "warning", "detail": "Canlı mod" if settings.mode == "production" else "Test modu; gerçek hesaba paylaşım yapılmaz."},
        {"name": "PostgreSQL", "status": "ok" if settings.database_url.startswith("postgresql") and health_result["database"] == "ok" else "warning", "detail": "Kalıcı üretim veritabanı gerekir."},
        {"name": "Redis", "status": health_result["redis"], "detail": "İş dağıtımı ve eşzamanlılık kontrolü."},
        {"name": "AI sağlayıcısı", "status": "configured" if settings.openai_api_key and settings.openai_model else "missing", "detail": "API anahtarı ve model adları .env üzerinden tanımlanır."},
        {"name": "Google OAuth", "status": "configured" if settings.google_client_id and settings.google_client_secret else "missing", "detail": "Drive ve YouTube için uygulama yetkilendirmesi gerekir."},
        {"name": "Instagram OAuth", "status": "configured" if settings.instagram_client_id and settings.instagram_client_secret and settings.graph_api_version else "missing", "detail": "Instagram Login ve uygulama incelemesi."},
        {"name": "TikTok otomatik yayın", "status": "blocked", "detail": "Resmi platform kuralı nedeniyle dışa aktarma ve kullanıcı tamamlama akışı."},
    ]


def normalize_provider(provider):
    provider = provider.lower()
    if provider in ("google", "google_drive"):
        provider = "drive"
    if provider not in ("drive", "youtube", "instagram", "tiktok", "x", "facebook"):
        raise HTTPException(404, "Sağlayıcı bulunamadı.")
    return provider


def save_connection(session, account_id, provider, credentials, identity):
    target = session.get(Account, account_id)
    managed_platform = (target.settings or {}).get("managed_platform") if target else None
    if provider == 'tiktok' and managed_platform == 'tiktok':
        raise HTTPException(409, 'Bağımsız TikTok hesabını Buffer üzerinden bağlayın.')
    if provider == "facebook" and (managed_platform != "facebook" or credentials.get("credential_kind") != "facebook_page"
                                    or credentials.get("page_id") != str(identity.get("external_id"))):
        raise HTTPException(409, "Facebook için doğrulanmış bağımsız Sayfa bağlantısı gerekli.")
    if managed_platform and provider not in (managed_platform, "drive"):
        raise HTTPException(409, "Bu bağımsız kanala farklı bir platform bağlanamaz.")
    if managed_platform == provider:
        session.scalar(select(SystemState).where(SystemState.id == "cycle-config").with_for_update())
        external = str(identity.get("external_id") or identity.get("id") or "")
        duplicate = session.scalar(select(Connection.id).where(Connection.platform == provider,
            Connection.account_id != account_id, Connection.external_id == external,
            Connection.status.in_(("CONNECTED", "connected"))).limit(1)) if external else None
        if duplicate:
            raise HTTPException(409, "Bu platform hesabı başka bir SMF kanalına bağlı.")
    connection = session.scalar(select(Connection).where(Connection.account_id == account_id, Connection.platform == provider))
    if provider == "youtube" and connection and (connection.settings or {}).get("publish_via") == "buffer":
        if session.scalar(select(PublishJob.id).where(PublishJob.account_id == account_id,
                PublishJob.status.not_in(("PUBLISHED", "CANCELLED"))).limit(1)):
            raise HTTPException(409, "Bekleyen Buffer yayını sonuçlanmadan sağlayıcı değiştirilemez.")
    previous_external_id = connection.external_id if connection else None
    if not connection:
        connection = Connection(account_id=account_id, platform=provider)
        session.add(connection)
        session.flush()
    connection.external_id = str(identity.get("external_id") or identity.get("id") or "")
    connection.status = "CONNECTED"
    scope = credentials.get("scope", [])
    connection.scopes = scope.replace(",", " ").split() if isinstance(scope, str) else scope
    connection.settings = {**connection.settings, "username": identity.get("username", ""), "mock": bool(credentials.get("mock"))}
    if provider == "youtube":
        connection.settings = {k: v for k, v in connection.settings.items()
                               if k != "publish_via" and not k.startswith("buffer_")}
    if provider == "facebook":
        connection.settings = {**connection.settings, "facebook_page_selected": True}
    if provider == "x" or (managed_platform == provider and provider != "instagram"):
        connection.settings = {**connection.settings, "auto_publish_enabled": False}
    if managed_platform == provider and provider != "instagram":
        target.active = False
    token = session.scalar(select(OAuthToken).where(OAuthToken.connection_id == connection.id))
    if not token:
        token = OAuthToken(connection_id=connection.id, encrypted_payload=encrypt_json(credentials))
        session.add(token)
    else:
        previous = decrypt_json(token.encrypted_payload)
        if previous_external_id == connection.external_id and not credentials.get("refresh_token") and previous.get("refresh_token"):
            credentials["refresh_token"] = previous["refresh_token"]
        token.encrypted_payload = encrypt_json(credentials)
    if provider == "drive":
        drive = session.scalar(select(DriveConnection).where(DriveConnection.account_id == account_id))
        if not drive:
            drive = DriveConnection(account_id=account_id, connection_id=connection.id)
            session.add(drive)
        drive.status = "CONNECTED"
    return connection


@app.post("/api/oauth/{provider}/start")
def oauth_start(provider: str, data: OAuthStartInput, request: Request, user=Depends(admin)):
    provider = normalize_provider(provider)
    with SessionLocal.begin() as session:
        target = required(session, Account, data.account_id)
        managed_platform = (target.settings or {}).get("managed_platform")
        if provider == 'instagram' and (target.settings or {}).get('smfa_instagram_buffer_v1'):
            raise HTTPException(409, 'Bu hesap Buffer uzerinden bagli. Baglantiyi Buffer icinden yonetin.')
        if provider == 'tiktok' and managed_platform == 'tiktok':
            raise HTTPException(409, 'TikTok hesabını Buffer içinde bağlayıp SMF listesinden seçin.')
        if provider == "facebook" and managed_platform != "facebook":
            raise HTTPException(409, "Facebook için ayrı bir Facebook kanalı oluşturun.")
        if managed_platform and provider not in (managed_platform, "drive"):
            raise HTTPException(409, "Bu bağımsız kanala farklı bir platform bağlanamaz.")
        if settings.mode == "mock":
            if provider == "facebook":
                raise HTTPException(409, "Facebook Sayfa seçimi gerçek OAuth yapılandırması gerektirir; önizleme canlı bağlantı kurmaz.")
            save_connection(session, data.account_id, provider, {"mock": True}, {"external_id": f"mock-{provider}-{data.account_id}", "username": "Test bağlantısı"})
            audit(session, "oauth.mock_connected", user, data.account_id, {"provider": provider})
            return {"url": "/?connection=mock", "mock": True}
        raw_state, verifier = secrets.token_urlsafe(40), secrets.token_urlsafe(64)
        verifier_data = {"verifier": verifier}
        if provider == "instagram" and settings.smfa_oauth_hardening_enabled:
            from factory.services.instagram_connection import begin_context
            verifier_data["instagram_context"] = begin_context(session, data.account_id, request.state.auth_session_id)
        state = OAuthState(state_hash=digest(raw_state), user_id=user["id"], account_id=data.account_id,
                           provider=provider, encrypted_verifier=encrypt_json(verifier_data), expires_at=now_utc() + timedelta(minutes=10), used=False)
        session.add(state)
        from factory.providers.oauth import authorization_url
        callback = f"{settings.public_base_url.rstrip('/')}/api/oauth/{provider}/callback"
        try:
            url = authorization_url(provider, callback, raw_state, verifier)
        except Exception:
            raise HTTPException(409, "OAuth uygulama bilgileri eksik veya geçersiz. Bağlantı ayarlarını kontrol edin.") from None
        return {"url": url}


@app.get("/api/oauth/{provider}/callback")
def oauth_callback(provider: str, request: Request, state: str, code: str = "", error: str = "", user=Depends(current_user)):
    provider = normalize_provider(provider)
    with SessionLocal.begin() as session:
        row = session.scalar(select(OAuthState).where(OAuthState.state_hash == digest(state)).with_for_update())
        if not row or row.used or row.user_id != user["id"] or row.provider != provider or aware(row.expires_at) < now_utc():
            raise HTTPException(400, "Yetkilendirme isteği geçersiz veya süresi dolmuş.")
        verifier_data = decrypt_json(row.encrypted_verifier or "")
        instagram_context = verifier_data.get("instagram_context")
        if provider == "instagram" and settings.smfa_oauth_hardening_enabled:
            from factory.services.instagram_connection import verify_context
            verify_context(instagram_context, request.state.auth_session_id)
        row.used = True
        account_id, verifier = row.account_id, verifier_data["verifier"]
        target = required(session, Account, account_id)
        target_platform = (target.settings or {}).get('managed_platform') or 'instagram'
    if error or not code:
        query = urlencode({'connection': 'denied', 'account': account_id, 'platform': target_platform})
        return RedirectResponse(f"{settings.frontend_origin}/?{query}")
    from factory.providers.oauth import exchange_code, resolve_identity
    callback = f"{settings.public_base_url.rstrip('/')}/api/oauth/{provider}/callback"
    try:
        credentials = exchange_code(provider, code, callback, verifier)
        identity = resolve_identity(provider, credentials)
        if provider == "instagram" and settings.smfa_oauth_hardening_enabled:
            from factory.providers.oauth import instagram_permissions
            credentials["scope"] = instagram_permissions(credentials)
    except Exception:
        raise HTTPException(502, "Platform yetkilendirmesi tamamlanamadı. Hesabı yeniden bağlayın.") from None
    with SessionLocal.begin() as session:
        target = required(session, Account, account_id)
        if provider == 'instagram' and (target.settings or {}).get('smfa_instagram_buffer_v1'):
            raise HTTPException(409, 'Buffer baglantisi dogrudan Instagram girisiyle degistirilmedi.')
        expected = target.username.lstrip("@").lower()
        actual = str(identity.get("username", "")).lstrip("@").lower()
        if provider == "instagram" and settings.smfa_oauth_hardening_enabled:
            from factory.services.instagram_connection import validate_candidate
            validate_candidate(session, target, credentials, identity, instagram_context)
        elif provider == "instagram" and expected and expected != actual:
            raise HTTPException(409, "Seçilen Instagram hesabı hedef hesapla eşleşmiyor. Doğru hesapla yeniden bağlayın.")
        if provider == "facebook":
            if (target.settings or {}).get("managed_platform") != "facebook":
                raise HTTPException(409, "Facebook için ayrı bir Facebook kanalı oluşturun.")
            # A Facebook login is a person, not the publishing Page. Keep its
            # grant encrypted and disabled until the administrator selects a Page.
            session.scalar(select(SystemState).where(SystemState.id == "cycle-config").with_for_update())
            connected = session.scalar(select(Connection).where(Connection.account_id == account_id,
                Connection.platform == "facebook").with_for_update())
            if not connected:
                connected = Connection(account_id=account_id, platform="facebook", settings={})
                session.add(connected); session.flush()
            connected.status = "PAGE_SELECTION_REQUIRED"
            connected.scopes = credentials.get("scope", [])
            connected.settings = {**(connected.settings or {}), "auto_publish_enabled": False,
                                  "facebook_page_selected": False}
            target.active = False
            token = session.scalar(select(OAuthToken).where(OAuthToken.connection_id == connected.id))
            if not token:
                token = OAuthToken(connection_id=connected.id, encrypted_payload=encrypt_json(credentials))
                session.add(token)
            else:
                token.encrypted_payload = encrypt_json(credentials)
        else:
            connected = save_connection(session, account_id, provider, credentials, identity)
            if provider == "instagram" and settings.smfa_oauth_hardening_enabled:
                from factory.services.instagram_connection import update_capability
                connected.settings = {**connected.settings, "instagram_context": {"app_id": settings.instagram_client_id, "login": "instagram_login"}}
                target.username = str(identity["username"])
                update_capability(session, account_id, credentials)
        if provider == "drive" and (target.settings or {}).get("cycle_v2") and not (target.settings or {}).get("managed_platform"):
            from factory.services.jobs import enqueue_job
            enqueue_job(session, "CONNECT_DRIVE_LIBRARY", account_id=account_id, payload={"connection_id": connected.id},
                idempotency_key=f"drive-share:{connected.id}:{row.id}", priority=95, max_attempts=3)
        if (target.settings or {}).get("auto_discover") and provider not in ("drive", "facebook"):
            target.username = str(identity.get("username", target.username))
            target.name = str(identity.get("name") or identity.get("username") or target.name)
            target.settings = {**target.settings, "auto_discover": False}
        audit(session, "oauth.connected", user, account_id, {"provider": provider})
    query = urlencode({'connected': provider, 'account': account_id, 'platform': target_platform})
    return RedirectResponse(f"{settings.frontend_origin}/?{query}")


@app.post("/api/webhooks/drive")
def drive_webhook(request: Request):
    channel = request.headers.get("x-goog-channel-id", "")
    resource_id = request.headers.get("x-goog-resource-id", "")
    supplied = request.headers.get("x-goog-channel-token", "")
    if not channel or not supplied or not resource_id:
        raise HTTPException(403, "Webhook doğrulanamadı.")
    with SessionLocal.begin() as session:
        drive = session.scalar(select(DriveConnection).where(DriveConnection.watch_channel_id == channel, DriveConnection.watch_resource_id == resource_id))
        connection = session.get(Connection, drive.connection_id) if drive else None
        expected = connection.settings.get("watch_token_hash", "") if connection else ""
        if not drive or not expected or not secrets.compare_digest(expected, digest(supplied)):
            raise HTTPException(403, "Webhook doğrulanamadı.")
        if drive.watch_expires_at and aware(drive.watch_expires_at) < now_utc():
            raise HTTPException(403, "Webhook süresi doldu.")
        from factory.services.jobs import enqueue_job
        enqueue_job(session, kind="drive_sync", account_id=drive.account_id, idempotency_key=f"drive-hook:{drive.account_id}:{int(time.time()) // 30}")
    return Response(status_code=204)


# Concrete upload routes must be registered before this action route.
app.add_api_route("/api/accounts/{account_id}/{action}", account_action, methods=["POST"])

from factory.control_api import router as control_router
app.include_router(control_router)
from factory.channel_api import router as channel_router
app.include_router(channel_router)
from factory.buffer_api import router as buffer_router
app.include_router(buffer_router)


from factory.hashtag_api import router as hashtag_router
app.include_router(hashtag_router)

# SMFA analytics dashboard 2026-09-29; authenticated read-only endpoints.
from factory.dashboard_api import router as dashboard_metrics_router
app.include_router(dashboard_metrics_router)
