"""X OAuth2 video publisher. Unknown post outcomes are never automatically repeated."""
from pathlib import Path
import hashlib
import re
import time
import httpx
from .base import ProviderError, bearer, persist
from .social import _result

BASE = "https://api.x.com/2"
REQUIRED_SCOPES = {"tweet.read", "tweet.write", "users.read", "media.write", "offline.access"}


def ready(connection):
    return (connection.platform == "x" and connection.status.upper() == "CONNECTED"
            and bool(connection.external_id)
            and (connection.settings or {}).get("auto_publish_enabled") is True
            and REQUIRED_SCOPES.issubset(set(connection.scopes or [])))


def checked(response):
    status = response.status_code
    if 200 <= status < 300:
        return response
    messages = {401: "X oturumu yenilenmeli.", 402: "X API erişimi veya kredisi kontrol edilmeli.",
                403: "X video/yayın izni reddedildi; uygulama erişimini kontrol edin.",
                429: "X istek sınırı doldu; bekleme süresi uygulanıyor."}
    delay = response.headers.get("retry-after", "")
    reset = response.headers.get("x-rate-limit-reset", "")
    wait = int(delay) if delay.isdigit() else max(1, int(reset)-int(time.time())) if reset.isdigit() else 120
    raise ProviderError(messages.get(status, f"X isteği başarısız (HTTP {status})."),
                        "oauth_expired" if status == 401 else "rate_limit" if status == 429 else "x_http_error",
                        retryable=status == 429 or status >= 500, retry_after=wait)


class XPublisher:
    def __init__(self, client=None):
        self.client = client or httpx.Client(timeout=90, follow_redirects=False)

    def publish(self, video_path, metadata, credentials, checkpoint=None, save_checkpoint=None):
        cp = dict(checkpoint or {})
        if cp.get("external_id") and cp["external_id"] != credentials.get("external_id"):
            return _result("BLOCKED", cp, error="Bağlı X hesabı değişti; önceki hesaba ait yükleme kullanılmadı.")
        if cp.get("remote_id"):
            return _result("PUBLISHED", cp, url=f"https://x.com/i/web/status/{cp['remote_id']}")
        if cp.get("publish_started"):
            return _result("UNKNOWN", cp, error="X gönderisinin sonucu belirsiz; yeniden gönderilmedi.")
        if cp.get("initiation_started") and not cp.get("media_id"):
            return _result("UNKNOWN", cp, error="X yükleme kimliği alınamadı; işlem incelenmeli.")
        from factory.services.account_captions import provider_caption
        text = provider_caption(metadata, "x")
        headers = bearer(credentials)
        path = Path(video_path)
        size = path.stat().st_size
        if size <= 0 or size > 512 * 1024 * 1024:
            raise ProviderError("SMF X video yüklemesi 1 bayt–512 MB aralığında olmalı.", "invalid_video")
        with path.open("rb") as stream:
            fingerprint = hashlib.file_digest(stream, "sha256").hexdigest()
        if cp.get("fingerprint") and cp["fingerprint"] != fingerprint:
            raise ProviderError("Yüklenen video değişti; aynı işlem sürdürülemez.", "video_changed")
        if cp.get("expires_at") and time.time() >= cp["expires_at"]:
            return _result("BLOCKED", cp, error="X video yüklemesinin süresi doldu.")
        try:
            if not cp.get("media_id"):
                persist(cp, save_checkpoint, initiation_started=True, fingerprint=fingerprint,
                        external_id=credentials.get("external_id"))
                response = self.client.post(BASE + "/media/upload/initialize", headers=headers,
                    json={"media_type": "video/mp4", "total_bytes": size, "media_category": "tweet_video"})
                if 400 <= response.status_code < 500:
                    persist(cp, save_checkpoint, initiation_started=False)
                if response.status_code >= 500:
                    return _result("UNKNOWN", cp, error="X yükleme başlangıcı doğrulanamadı.")
                data = checked(response).json()["data"]
                media_id = str(data["id"])
                if not media_id.isdigit():
                    return _result("UNKNOWN", cp, error="X yükleme kimliği doğrulanamadı.")
                persist(cp, save_checkpoint, media_id=media_id, next_segment=0,
                        expires_at=time.time()+int(data.get("expires_after_secs", 86400)))
            media_id = cp["media_id"]
            if not str(media_id).isdigit():
                raise ProviderError("Geçersiz X medya kimliği.", "invalid_checkpoint")
            if not cp.get("finalize_started"):
                chunk_size = 4 * 1024 * 1024
                segment = cp.get("next_segment", 0)
                with path.open("rb") as stream:
                    stream.seek(segment * chunk_size)
                    while chunk := stream.read(chunk_size):
                        checked(self.client.post(f"{BASE}/media/upload/{media_id}/append", headers=headers,
                            data={"segment_index": str(segment)}, files={"media": ("video.mp4", chunk, "video/mp4")}))
                        segment += 1
                        persist(cp, save_checkpoint, next_segment=segment)
                persist(cp, save_checkpoint, finalize_started=True)
                response = self.client.post(f"{BASE}/media/upload/{media_id}/finalize", headers=headers)
                if response.status_code == 429:
                    persist(cp, save_checkpoint, finalize_started=False)
                checked(response)
                info = response.json()["data"].get("processing_info")
                if not info:
                    persist(cp, save_checkpoint, media_ready=True)
                elif info.get("state") == "failed":
                    return _result("BLOCKED", cp, error="X videoyu işleyemedi.")
                elif info.get("state") == "succeeded":
                    persist(cp, save_checkpoint, media_ready=True)
                else:
                    persist(cp, save_checkpoint, poll_after=time.time()+max(1, int(info.get("check_after_secs", 5))))
                    return _result("PROCESSING", cp)
            if not cp.get("media_ready"):
                if time.time() < cp.get("poll_after", 0):
                    return _result("PROCESSING", cp)
                data = checked(self.client.get(BASE + "/media/upload", headers=headers,
                    params={"command": "STATUS", "media_id": media_id})).json()["data"]
                info = data.get("processing_info", {})
                if info.get("state") == "failed":
                    return _result("BLOCKED", cp, error="X videoyu işleyemedi.")
                if info.get("state") != "succeeded":
                    persist(cp, save_checkpoint, poll_after=time.time()+max(1, int(info.get("check_after_secs", 5))))
                    return _result("PROCESSING", cp)
                persist(cp, save_checkpoint, media_ready=True)
            persist(cp, save_checkpoint, publish_started=True)
            response = self.client.post(BASE + "/tweets", headers=headers,
                json={"text": text, "media": {"media_ids": [media_id]}})
            if 400 <= response.status_code < 500:
                persist(cp, save_checkpoint, publish_started=False)
                checked(response)
            if response.status_code >= 300:
                return _result("UNKNOWN", cp, error="X yayın sonucu doğrulanamadı; tekrar gönderilmedi.")
            remote_id = str(response.json()["data"]["id"])
            if not remote_id.isdigit():
                return _result("UNKNOWN", cp, error="X gönderi kimliği doğrulanamadı.")
            persist(cp, save_checkpoint, remote_id=remote_id)
            return _result("PUBLISHED", cp, url=f"https://x.com/i/web/status/{remote_id}")
        except (httpx.TransportError, ValueError, KeyError, TypeError):
            uncertain = cp.get("publish_started") or (cp.get("initiation_started") and not cp.get("media_id"))
            return _result("UNKNOWN" if uncertain else "PROCESSING", cp,
                           error="X yanıtı doğrulanamadı; kayıtlı işlem durumu korundu.")

    def analytics(self, remote_id, credentials, start_date=None, end_date=None):
        # No automatic paid read calls. Publishing confirmation already carries the post ID.
        return {"unavailable": ["views", "likes", "comments", "shares", "saves", "clicks"],
                "note": "X ölçüm sorguları bu sürümde kapalı."}
