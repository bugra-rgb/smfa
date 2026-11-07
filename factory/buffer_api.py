"""Admin-only binding of already authorized Buffer TikTok/YouTube channels to SMF.

OAuth happens in Buffer's own dashboard. SMF never asks for TikTok passwords and
never puts the server API key in the browser. Existing CSRF middleware applies.
"""
from typing import Literal
from fastapi import APIRouter, Depends, HTTPException
import httpx
from pydantic import BaseModel, Field
from sqlalchemy import select

from factory.config import settings
from factory.db import SessionLocal
from factory.models import Account, Connection, OAuthToken, PublishJob, SystemState
from factory.providers.base import ProviderError
from factory.providers.buffer import BufferClient, SCOPE, channel_problem
from factory.security import admin, encrypt_json, decrypt_json
from factory.services.buffer_budget import budget_status

router = APIRouter(prefix="/api/control/buffer")
DASHBOARD = "https://account.buffer.com/channels"


class BufferChannelInput(BaseModel):
    channel_id: str = Field(min_length=1, max_length=255, pattern=r"^[A-Za-z0-9_-]+$")


def _discover(platform="tiktok"):
    token = getattr(settings, "buffer_api_key", "")
    if not token:
        raise HTTPException(409, "Buffer API anahtarı sunucuya eklenmeli.")
    try:
        with httpx.Client(timeout=45, follow_redirects=False) as client:
            return token, BufferClient(client).discover(token, platform)
    except ProviderError as exc:
        raise HTTPException(429 if exc.code == "rate_limit" else 409, str(exc),
                            headers={"Retry-After": str(exc.retry_after)} if exc.retry_after else None) from None
    except (httpx.TransportError, ValueError, TypeError, KeyError, AttributeError):
        raise HTTPException(502, "Buffer kanal listesi alınamadı; bağlantı kaydedilmedi.") from None


@router.get("/channels")
def list_channels(platform: Literal["tiktok", "youtube"] = "tiktok", user=Depends(admin)):
    configured = bool(getattr(settings, "buffer_api_key", ""))
    if not configured:
        return {"configured": False, "channels": [], "dashboard_url": DASHBOARD,
                "budget": budget_status()}
    _, channels = _discover(platform)
    return {"configured": True, "channels": [
        {"id": c["id"], "name": c.get("name", ""), "username": c.get("name", ""),
         "organization_id": c["organizationId"], "organization_name": c["organization_name"],
         "external_id": c.get("serviceId", ""), "platform": c.get("service"), "available": not bool(channel_problem(c, platform)),
         "reason": channel_problem(c, platform)} for c in channels],
        "budget": budget_status(), "dashboard_url": DASHBOARD}


@router.post("/channels/{account_id}/connect")
def connect_channel(account_id: str, data: BufferChannelInput, user=Depends(admin)):
    # Reject missing/wrong SMF accounts before spending the shared Free budget.
    with SessionLocal() as session:
        account = session.get(Account, account_id)
        if not account or (account.settings or {}).get("managed_platform") not in ("tiktok", "youtube"):
            raise HTTPException(404, "Bağımsız TikTok/YouTube kanalı bulunamadı.")
        if account.active:
            raise HTTPException(409, "Bağlantıyı değiştirmek için önce kanalı duraklatın.")
        platform = account.settings["managed_platform"]
    token, channels = _discover(platform)
    candidates = [c for c in channels if c["id"] == data.channel_id]
    if len(candidates) != 1:
        raise HTTPException(409, "Seçilen Buffer kanalı doğrulanamadı.")
    selected = candidates[0]
    if reason := channel_problem(selected, platform):
        raise HTTPException(409, reason)
    with SessionLocal.begin() as session:
        # Same global lock as channel/OAuth creation: duplicate identities cannot
        # race between two SMF accounts. No network call is made under this lock.
        gate = session.scalar(select(SystemState).where(SystemState.id == "cycle-config").with_for_update())
        if gate is None:
            raise HTTPException(409, "SMF sunucu kurulumu tamamlanmalı.")
        account = session.scalar(select(Account).where(Account.id == account_id).with_for_update())
        if not account or (account.settings or {}).get("managed_platform") not in ("tiktok", "youtube"):
            raise HTTPException(404, "Bağımsız TikTok/YouTube kanalı bulunamadı.")
        if account.settings["managed_platform"] != platform:
            raise HTTPException(409, "Hesabın platformu değişti.")
        if account.active:
            raise HTTPException(409, "Bağlantıyı değiştirmek için önce kanalı duraklatın.")
        pending = session.scalar(select(PublishJob.id).where(
            PublishJob.account_id == account_id, PublishJob.status.not_in(("PUBLISHED", "CANCELLED"))).limit(1))
        if pending:
            raise HTTPException(409, "Bekleyen yayın sonuçlanmadan sağlayıcı değiştirilemez.")
        duplicate = session.scalar(select(Connection.id).where(
            Connection.platform == platform, Connection.account_id != account_id,
            Connection.external_id == selected["serviceId"]).limit(1))
        if duplicate:
            raise HTTPException(409, "Bu platform hesabı başka bir SMF kanalına bağlı.")
        connection = session.scalar(select(Connection).where(
            Connection.account_id == account_id, Connection.platform == platform).with_for_update())
        if connection and connection.external_id and connection.external_id != selected["serviceId"]:
            if session.scalar(select(PublishJob.id).where(PublishJob.account_id == account_id).limit(1)):
                raise HTTPException(409, "Bu kanalın yayın geçmişi var; farklı platform hesabı için yeni kanal oluşturun.")
        if connection is None:
            connection = Connection(account_id=account_id, platform=platform)
            session.add(connection)
            session.flush()
        binding = {"external_id": selected["serviceId"], "buffer_channel_id": selected["id"],
                   "buffer_organization_id": selected["organizationId"], "buffer_account_id": selected["buffer_account_id"]}
        connection.external_id = selected["serviceId"]
        connection.status = "CONNECTED"
        connection.scopes = ["buffer:youtube:publish" if platform == "youtube" else SCOPE]
        connection.settings = {**(connection.settings or {}), **binding,
                               "username": selected.get("name", ""), "publish_via": "buffer",
                               "auto_publish_enabled": False}
        row = session.scalar(select(OAuthToken).where(OAuthToken.connection_id == connection.id).with_for_update())
        prior = decrypt_json(row.encrypted_payload) if row else {}
        previous = prior.get("previous_direct_oauth")
        if prior and not prior.get("buffer_channel_id"):
            previous = prior
        payload = {"access_token": token, **binding}
        if previous:
            payload["previous_direct_oauth"] = previous
        encrypted = encrypt_json(payload)
        if row:
            row.encrypted_payload = encrypted
        else:
            session.add(OAuthToken(connection_id=connection.id, encrypted_payload=encrypted))
        account.username = selected.get("name", "")
        account.active = False
        return {"connected": True, "username": account.username, "platform": platform, "enabled": False}
