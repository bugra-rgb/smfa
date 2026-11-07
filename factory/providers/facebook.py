"""Facebook Page Reels using Meta's documented start/upload/finish/status flow.

Reference: fbsamples/reels_publishing_apis/fb_reels_publishing_api_sample/index.js.
Acknowledgements are never treated as published posts. Ambiguous writes are
reconciled by the recorded video ID, never by starting a second upload.
"""
from pathlib import Path
import hashlib
import re
import time

import httpx

from .base import ProviderError, bearer, persist
from .social import _result
from factory.media.engine import MediaError, inspect_media

REQUIRED_SCOPES = {"pages_show_list", "pages_read_engagement", "pages_manage_posts"}


def validate_video(path):
    # Conservative SMF release profile from Meta's sample, not a claim about
    # Facebook's current maximum duration. Never silently trim source content.
    try:
        info = inspect_media(path)
    except MediaError:
        raise ProviderError("Facebook video dosyası incelenemedi; yükleme başlatılmadı.", "invalid_video") from None
    width, height = info.get("width", 0), info.get("height", 0)
    if (not info.get("has_video") or not 4 <= info.get("duration", 0) <= 60
            or width < 540 or height < 960 or width * 16 != height * 9):
        raise ProviderError("Bu SMF sürümünde Facebook Reels videosu 4–60 saniye, 9:16 ve en az 540×960 olmalı; video kesilmedi.", "invalid_video")


def graph(version):
    if not re.fullmatch(r"v\d+\.\d+", version or ""):
        raise ProviderError("Facebook Graph API sürümü gerekli.", "configuration_missing")
    return "https://graph.facebook.com/" + version


def checked(response):
    if 200 <= response.status_code < 300:
        return response
    error = {}
    try:
        error = response.json().get("error", {})
    except (ValueError, AttributeError):
        pass
    code = error.get("code") if isinstance(error, dict) else None
    if response.status_code == 401 or code == 190:
        raise ProviderError("Facebook bağlantısı yenilenmeli.", "oauth_expired")
    if response.status_code == 429 or code in (4, 17, 32, 613):
        delay = response.headers.get("retry-after", "")
        raise ProviderError("Facebook istek sınırı doldu; bekleme süresi uygulanıyor.", "rate_limit", True,
                            int(delay) if delay.isdigit() else 120)
    raise ProviderError(f"Facebook isteği başarısız (HTTP {response.status_code}).", "facebook_http_error",
                        response.status_code >= 500)


def pages(credentials, version, client):
    """Return server-only Page tokens, following cursors on one fixed endpoint."""
    if credentials.get("credential_kind") != "facebook_user":
        raise ProviderError("Facebook sayfa seçimi için yeniden giriş yapın.", "oauth_missing")
    if credentials.get("expires_at", 0) <= time.time():
        raise ProviderError("Facebook sayfa seçiminin süresi doldu; yeniden giriş yapın.", "oauth_expired")
    if not REQUIRED_SCOPES.issubset(set(credentials.get("scope", []))):
        raise ProviderError("Facebook sayfa listeleme ve yayın izinleri eksik.", "permission_missing")
    params = {"fields": "id,name,access_token,tasks", "limit": "100"}
    result = []
    seen = set()
    for _ in range(20):
        data = checked(client.get(graph(version) + "/me/accounts", headers=bearer(credentials), params=params)).json()
        for page in data.get("data", []):
            page_id = str(page.get("id", ""))
            if (page_id.isdigit() and page_id not in seen and page.get("access_token")
                    and set(page.get("tasks", [])).intersection({"CREATE_CONTENT", "MANAGE"})):
                seen.add(page_id)
                result.append({"id": page_id, "name": str(page.get("name") or page_id),
                               "access_token": page["access_token"]})
        paging = data.get("paging", {})
        if not paging.get("next"):
            return result
        cursor = paging.get("cursors", {}).get("after")
        if not isinstance(cursor, str) or not cursor or cursor == params.get("after"):
            break
        params["after"] = cursor
    raise ProviderError("Facebook sayfa listesi tamamlanamadı; seçim kaydedilmedi.", "pages_incomplete")


def select_page(credentials, page_id, version, client):
    page = next((p for p in pages(credentials, version, client) if p["id"] == page_id), None)
    if not page:
        raise ProviderError("Seçilen Facebook Sayfasında içerik yayınlama yetkisi bulunamadı.", "permission_missing")
    identity = checked(client.get(graph(version) + "/me", headers=bearer(page), params={"fields": "id,name"})).json()
    if str(identity.get("id")) != page_id:
        raise ProviderError("Facebook Sayfa kimliği eşleşmiyor.", "identity_mismatch")
    # Keep a conservative grant deadline. Reauthentication is required on expiry;
    # Facebook Page tokens have no OAuth refresh_token grant.
    return ({"access_token": page["access_token"], "credential_kind": "facebook_page", "page_id": page_id,
             "scope": credentials["scope"], "expires_at": credentials["expires_at"]},
            {"external_id": page_id, "username": str(identity.get("name") or page["name"]),
             "name": str(identity.get("name") or page["name"])})


class FacebookPublisher:
    def __init__(self, version, client=None):
        self.version, self.base = version, graph(version)
        self.client = client or httpx.Client(timeout=90, follow_redirects=False)

    def publish(self, video_path, metadata, credentials, checkpoint=None, save_checkpoint=None):
        cp = dict(checkpoint or {})
        page_id = str(credentials.get("external_id") or "")
        if (not page_id.isdigit() or credentials.get("credential_kind") != "facebook_page"
                or credentials.get("page_id") != page_id):
            raise ProviderError("Doğrulanmış Facebook Sayfa bağlantısı gerekli.", "identity_missing")
        if cp and (cp.get("video_id") or cp.get("remote_id") or cp.get("initiation_started")) and cp.get("external_id") != page_id:
            return _result("BLOCKED", cp, error="Facebook Sayfası değişti; önceki yükleme kullanılmadı.")
        if cp.get("remote_id"):
            return _result("PUBLISHED", cp, url=f"https://www.facebook.com/reel/{cp['remote_id']}")
        if cp.get("initiation_started") and not cp.get("video_id"):
            return _result("UNKNOWN", cp, error="Facebook yükleme kimliği alınamadı; yeniden başlatılmadı.")
        if cp.get("video_id") and (not cp.get("fingerprint") or not cp.get("total_bytes")):
            return _result("BLOCKED", cp, error="Facebook yüklemesinin kaynak dosyası doğrulanamıyor; yeniden gönderilmedi.")
        from factory.services.account_captions import provider_caption
        caption = provider_caption(metadata, "facebook")
        path = Path(video_path)
        size = path.stat().st_size
        if size <= 0:
            raise ProviderError("Facebook videosu boş.", "invalid_video")
        with path.open("rb") as stream:
            fingerprint = hashlib.file_digest(stream, "sha256").hexdigest()
        if cp.get("fingerprint") and (cp["fingerprint"] != fingerprint or cp.get("total_bytes") != size):
            return _result("BLOCKED", cp, error="Facebook yüklemesinin video dosyası değişti.")
        if not cp.get("video_id"):
            validate_video(path)
        headers = bearer(credentials)
        try:
            if not cp.get("video_id"):
                persist(cp, save_checkpoint, initiation_started=True, external_id=page_id, fingerprint=fingerprint,
                        total_bytes=size, started_at=time.time())
                response = self.client.post(f"{self.base}/{page_id}/video_reels", headers=headers, data={"upload_phase": "start"})
                if 400 <= response.status_code < 500:
                    persist(cp, save_checkpoint, initiation_started=False)
                if response.status_code >= 500:
                    return _result("UNKNOWN", cp, error="Facebook yükleme başlangıcı doğrulanamadı.")
                video_id = str(checked(response).json()["video_id"])
                if not video_id.isdigit():
                    return _result("UNKNOWN", cp, error="Facebook video kimliği doğrulanamadı.")
                persist(cp, save_checkpoint, video_id=video_id)
            video_id = str(cp["video_id"])
            if not video_id.isdigit():
                raise ProviderError("Facebook yükleme kaydı geçersiz.", "invalid_checkpoint")
            # Reconcile any prior write before another mutating request.
            if cp.get("upload_started"):
                state = checked(self.client.get(f"{self.base}/{video_id}", headers=headers, params={"fields": "status"})).json().get("status", {})
                if not isinstance(state, dict) or not state:
                    return _result("UNKNOWN", cp, error="Facebook video durumu doğrulanamadı; yeniden gönderilmedi.")
                if state.get("video_status") == "error" or any(
                        (state.get(phase) or {}).get("status") == "error"
                        for phase in ("uploading_phase", "processing_phase", "publishing_phase")):
                    return _result("BLOCKED", cp, error="Facebook video işleme veya yayın aşamasını reddetti.")
                if (state.get("publishing_phase") or {}).get("status") == "complete" and cp.get("publish_started"):
                    persist(cp, save_checkpoint, remote_id=video_id)
                    return _result("PUBLISHED", cp, url=f"https://www.facebook.com/reel/{video_id}")
                if time.time() - cp.get("started_at", 0) > 1800:
                    return _result("UNKNOWN", cp, error="Facebook işlemi süresinde doğrulanamadı; yeniden gönderilmedi.")
                if cp.get("publish_started"):
                    return _result("PROCESSING", cp)
                if (state.get("uploading_phase") or {}).get("status") == "complete":
                    persist(cp, save_checkpoint, uploaded=True)
                elif not cp.get("uploaded"):
                    return _result("PROCESSING", cp)
            if not cp.get("uploaded"):
                persist(cp, save_checkpoint, upload_started=True)
                with path.open("rb") as stream:
                    response = self.client.post(f"https://rupload.facebook.com/video-upload/{self.version}/{video_id}",
                        headers={"Authorization": "OAuth " + credentials["access_token"], "offset": "0", "file_size": str(size),
                                 "Content-Type": "application/octet-stream"}, content=stream)
                if response.status_code == 429:
                    persist(cp, save_checkpoint, upload_started=False)
                if response.status_code >= 500:
                    return _result("PROCESSING", cp)
                if checked(response).json().get("success") is not True:
                    return _result("UNKNOWN", cp, error="Facebook video aktarımı doğrulanamadı.")
                persist(cp, save_checkpoint, uploaded=True)
            persist(cp, save_checkpoint, publish_started=True)
            response = self.client.post(f"{self.base}/{page_id}/video_reels", headers=headers,
                data={"upload_phase": "finish", "video_id": video_id, "video_state": "PUBLISHED", "description": caption})
            if response.status_code == 429:
                persist(cp, save_checkpoint, publish_started=False)
                checked(response)
            if 400 <= response.status_code < 500:
                checked(response)
            # Even success:true is only acknowledgement. The next invocation polls.
            return _result("PROCESSING", cp)
        except (httpx.TransportError, ValueError, KeyError, TypeError):
            return _result("PROCESSING" if cp.get("video_id") else "UNKNOWN", cp,
                           error="Facebook yanıtı doğrulanamadı; kayıtlı işlem korundu.")

    def analytics(self, remote_id, credentials, start_date=None, end_date=None):
        return {"unavailable": ["views", "likes", "comments", "shares", "saves", "clicks"],
                "note": "Facebook Reels ölçümleri bu sürümde alınmıyor; sıfır olarak kaydedilmez."}
