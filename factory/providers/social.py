"""Official publishers. Network uncertainty is never treated as permission to repost."""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
from pathlib import Path
import re
from typing import Any
from urllib.parse import urlparse

import httpx

from .base import ProviderError, bearer, checked, google_session, persist


def _result(status, cp, remote_id=None, url=None, **extra):
    if extra.get("error"):
        extra.setdefault("message", extra["error"])
    return {"status": status, "remote_id": remote_id or cp.get("remote_id"), "url": url,
            "checkpoint": dict(cp), **extra}


class YouTubePublisher:
    def __init__(self, client=None):
        self.client = client or httpx.Client(timeout=120, follow_redirects=False)

    def _status(self, cp, headers, save_checkpoint):
        data = checked(self.client.get("https://www.googleapis.com/youtube/v3/videos", headers=headers,
            params={"part": "status,processingDetails", "id": cp["remote_id"]})).json()
        items = data.get("items", [])
        if not items:
            return _result("UNKNOWN", cp, error="YouTube video kimliği bulunamadı; yeniden yükleme engellendi.")
        item = items[0]
        if item.get("id") and item["id"] != cp["remote_id"]:
            return _result("UNKNOWN", cp, error="YouTube farklı bir video kimliği döndürdü; yayın doğrulanmadı.")
        state = item.get("status", {}).get("uploadStatus")
        processing = item.get("processingDetails", {}).get("processingStatus")
        if state in ("failed", "rejected", "deleted"):
            return _result("FAILED", cp, error=f"YouTube işleme durumu: {state}")
        if processing == "failed":
            return _result("FAILED", cp, error="YouTube videoyu işleyemedi; yeniden yükleme yapılmadı.")
        privacy = item.get("status", {}).get("privacyStatus")
        if state == "processed" and processing != "processing":
            if cp.get("requested_privacy", "private") != privacy:
                return _result("POLICY_BLOCKED", cp, url=f"https://www.youtube.com/watch?v={cp['remote_id']}",
                               error="Video yüklendi ancak istenen görünürlük uygulanmadı. YouTube API denetimi/kanal ayarlarını kontrol edin.", visibility=privacy)
            persist(cp, save_checkpoint, complete=True, visibility=privacy)
            return _result("PUBLISHED", cp, url=f"https://www.youtube.com/watch?v={cp['remote_id']}", visibility=privacy)
        return _result("PROCESSING", cp)

    @staticmethod
    def _offset(response, size):
        """308 without Range means no bytes are confirmed, not a missing header error."""
        value = response.headers.get("Range")
        if not value:
            return 0
        match = re.fullmatch(r"(?:bytes=)?0-(\d+)", value.strip())
        if not match or int(match[1]) >= size:
            return None
        return int(match[1]) + 1

    @staticmethod
    def _text(value, *, title=False):
        if not isinstance(value, str):
            raise ProviderError("YouTube başlık/açıklama alanı metin olmalı.", "invalid_metadata")
        # Drive filenames may contain characters YouTube explicitly disallows.
        value = value.replace("<", "").replace(">", "").strip()
        if title:
            value = value[:100].strip()
            if not value:
                raise ProviderError("YouTube başlığı gerekli; Drive video adını kontrol edin.", "invalid_metadata")
            return value
        # YouTube limits the description to UTF-8 bytes, not Python characters.
        return value.encode("utf-8")[:5000].decode("utf-8", errors="ignore")

    def publish(self, video_path, metadata, credentials, checkpoint=None, save_checkpoint=None):
        cp = dict(checkpoint or {})
        if cp.get("external_id") and cp["external_id"] != credentials.get("external_id"):
            return _result("BLOCKED", cp, error="YouTube kanalı değişti; eski yükleme kullanılmadı.")
        headers = bearer(credentials)
        if cp.get("complete"):
            return _result("PUBLISHED", cp, url=f"https://www.youtube.com/watch?v={cp['remote_id']}", visibility=cp.get("visibility"))
        if cp.get("remote_id"):
            return self._status(cp, headers, save_checkpoint)
        if cp.get("initiation_started") and not cp.get("session_url"):
            return _result("UNKNOWN", cp, error="YouTube yükleme başlangıcı belirsiz; yeniden başlatılmadı.")
        video = Path(video_path)
        size = video.stat().st_size
        offset = 0
        try:
            if cp.get("session_url"):
                url = google_session(cp["session_url"])
                # Query the original upload size even if the local file changed.
                # A completed upload can still be reconciled without sending bytes.
                recorded_size = cp.get("upload_size")
                query_size = recorded_size if type(recorded_size) is int and recorded_size > 0 else size
                response = self.client.put(url, headers={**headers, "Content-Length": "0", "Content-Range": f"bytes */{query_size}"}, content=b"")
                if response.status_code in (200, 201):
                    persist(cp, save_checkpoint, remote_id=response.json()["id"])
                    return self._status(cp, headers, save_checkpoint)
                if response.status_code in (404, 410):
                    return _result("UNKNOWN", cp, error="YouTube yükleme oturumu sona erdi. Kanal kontrol edilmeden yeniden yüklenmez.")
                if response.status_code != 308:
                    checked(response)
                    return _result("UNKNOWN", cp)
                if not cp.get("fingerprint") or type(recorded_size) is not int or recorded_size <= 0:
                    return _result("UNKNOWN", cp, error="Eski YouTube yüklemesinin video bütünlüğü doğrulanamıyor; inceleme olmadan dosya gönderilmedi.")
                with video.open("rb") as source:
                    current_fingerprint = hashlib.file_digest(source, "sha256").hexdigest()
                if size != recorded_size or current_fingerprint != cp["fingerprint"]:
                    return _result("BLOCKED", cp, error="YouTube yüklemesindeki yerel video değişti; eski oturuma yeni dosya gönderilmedi.")
                offset = self._offset(response, size)
                if offset is None:
                    return _result("UNKNOWN", cp, error="YouTube yükleme ilerlemesi doğrulanamadı; dosya tekrar gönderilmedi.")
            else:
                privacy = metadata.get("privacy_status", "private")
                if privacy not in {"private", "public", "unlisted"}:
                    raise ProviderError("YouTube görünürlüğü geçersiz.", "invalid_metadata")
                title = self._text(metadata.get("title", ""), title=True)
                description = self._text(metadata.get("description", ""))
                if size <= 0:
                    raise ProviderError("YouTube videosu boş olamaz.", "invalid_video")
                with video.open("rb") as source:
                    video_fingerprint = hashlib.file_digest(source, "sha256").hexdigest()
                body = {"snippet": {"title": title, "description": description, "tags": metadata.get("tags", [])[:30], "categoryId": str(metadata.get("category_id", "22"))},
                        "status": {"privacyStatus": privacy, "selfDeclaredMadeForKids": bool(metadata.get("made_for_kids", False)), "containsSyntheticMedia": bool(metadata.get("contains_synthetic_media", True))}}
                persist(cp, save_checkpoint, requested_privacy=privacy, initiation_started=True,
                        external_id=credentials.get("external_id"), upload_size=size, fingerprint=video_fingerprint)
                response = self.client.post("https://www.googleapis.com/upload/youtube/v3/videos",
                    params={"uploadType": "resumable", "part": "snippet,status"}, headers={**headers, "X-Upload-Content-Length": str(size), "X-Upload-Content-Type": "video/mp4"}, json=body)
                if 400 <= response.status_code < 500:
                    persist(cp, save_checkpoint, initiation_started=False)
                checked(response)
                # Must be committed before sending a single video byte.
                persist(cp, save_checkpoint, session_url=google_session(response.headers["Location"]))
            with video.open("rb") as stream:
                stream.seek(offset)
                while offset < size:
                    chunk = stream.read(8 * 1024 * 1024)
                    response = self.client.put(cp["session_url"], headers={**headers, "Content-Type": "video/mp4", "Content-Length": str(len(chunk)), "Content-Range": f"bytes {offset}-{offset + len(chunk) - 1}/{size}"}, content=chunk)
                    if response.status_code in (200, 201):
                        persist(cp, save_checkpoint, remote_id=response.json()["id"])
                        return self._status(cp, headers, save_checkpoint)
                    if response.status_code != 308:
                        checked(response)
                        return _result("UNKNOWN", cp)
                    next_offset = self._offset(response, size)
                    if next_offset is None:
                        return _result("UNKNOWN", cp, error="YouTube yükleme ilerlemesi doğrulanamadı; dosya tekrar gönderilmedi.")
                    if next_offset <= offset:
                        raise ProviderError("YouTube yüklemesi ilerlemiyor.", "upload_stalled", True, checkpoint=cp)
                    offset = next_offset
                    stream.seek(offset)
            return _result("PROCESSING", cp)
        except httpx.TransportError:
            # The persisted session is queried before any bytes are retried.
            return _result("PROCESSING", cp,
                           error="Bağlantı kesildi; aynı YouTube oturumu üzerinden durum sorgulanacak.")

    def analytics(self, remote_id, credentials, start_date=None, end_date=None):
        headers = bearer(credentials)
        response = checked(self.client.get("https://www.googleapis.com/youtube/v3/videos", headers=headers,
            params={"part": "statistics", "id": remote_id})).json()
        stats = (response.get("items") or [{}])[0].get("statistics", {})
        metrics: dict[str, Any] = {key: int(stats[source]) for key, source in {"views": "viewCount", "likes": "likeCount", "comments": "commentCount"}.items() if source in stats}
        if start_date:
            end_date = end_date or datetime.now(timezone.utc).date().isoformat()
            data = checked(self.client.get("https://youtubeanalytics.googleapis.com/v2/reports", headers=headers,
                params={"ids": "channel==MINE", "startDate": start_date, "endDate": end_date, "filters": f"video=={remote_id}", "metrics": "views,estimatedMinutesWatched,averageViewDuration,averageViewPercentage,likes,comments,shares"})).json()
            if data.get("rows"):
                metrics["period"] = {column["name"]: value for column, value in zip(data["columnHeaders"], data["rows"][0])}
                period = metrics["period"]
                if isinstance(period.get("estimatedMinutesWatched"), (int, float)):
                    metrics["watch_time_seconds"] = period["estimatedMinutesWatched"] * 60
                for target, source in {"average_view_duration": "averageViewDuration", "average_view_percentage": "averageViewPercentage", "shares": "shares"}.items():
                    if isinstance(period.get(source), (int, float)):
                        metrics[target] = period[source]
        return {**metrics, "unavailable": ["impressions_ctr", "saves"]}


class InstagramPublisher:
    def __init__(self, graph_api_version="v24.0", client=None):
        self.base = f"https://graph.instagram.com/{graph_api_version}"
        self.client = client or httpx.Client(timeout=120, follow_redirects=False)

    def publish(self, video_path, metadata, credentials, checkpoint=None, save_checkpoint=None):
        cp = dict(checkpoint or {})
        headers = bearer(credentials)
        account_id = credentials.get("external_id") or credentials.get("user_id")
        if not account_id:
            raise ProviderError("Instagram profesyonel hesap kimliği eksik.", "identity_missing")
        if cp.get("instagram_id") and cp["instagram_id"] != str(account_id):
            return _result("BLOCKED", cp, error="Instagram hesap kimliği değişti.")
        if cp.get("remote_id"):
            return _result("PUBLISHED", cp, url=cp.get("url"))
        if cp.get("publish_started"):
            # A successful media_publish response may have been lost. Never repeat it.
            return _result("UNKNOWN", cp, error="Instagram yayın isteğinin sonucu belirsiz. Hesabı kontrol ederek mevcut gönderiyi eşleyin; otomatik tekrar gönderilmez.")
        try:
            if not cp.get("container_id"):
                if cp.get("create_started"):
                    return _result("UNKNOWN", cp, error="Instagram kapsayıcı oluşturma sonucu belirsiz; yeniden gönderilmedi.")
                url = metadata.get("public_video_url", "")
                parsed = urlparse(url)
                if parsed.scheme != "https" or not parsed.hostname:
                    raise ProviderError("Instagram için HTTPS üzerinden erişilebilen süreli video adresi gerekli.", "public_media_url_missing")
                caption = metadata.get("caption") or metadata.get("description", "")
                body = {"media_type": "REELS", "video_url": url, "caption": caption[:2200], "share_to_feed": "true"}
                if metadata.get("cover_url"):
                    cover = urlparse(metadata["cover_url"])
                    if cover.scheme != "https" or not cover.hostname or cover.username or cover.password:
                        raise ProviderError("Kapak için geçerli HTTPS adresi gerekli.", "cover_unavailable")
                    body["cover_url"] = metadata["cover_url"]
                persist(cp, save_checkpoint, create_started=True, instagram_id=str(account_id), cover_asset_id=metadata.get("cover_asset_id"))
                response = self.client.post(f"{self.base}/{account_id}/media", headers=headers, data=body)
                if 400 <= response.status_code < 500:
                    persist(cp, save_checkpoint, create_started=False)
                result = checked(response).json()
                if not result.get("id"):
                    return _result("UNKNOWN", cp, error="Instagram kapsayıcı kimliği alınamadı.")
                persist(cp, save_checkpoint, container_id=result["id"], cover_sent=bool(body.get("cover_url")))
            if metadata.get("_smfa_wait_for_ready") is True:
                from .instagram_wait import container_status
                state, defer = container_status(self.client, self.base, headers, cp, save_checkpoint)
                if defer:
                    return _result("PROCESSING", cp)
            else:
                state = checked(self.client.get(f"{self.base}/{cp['container_id']}", headers=headers, params={"fields": "status_code,status"})).json()
            if state.get("status_code") in ("ERROR", "EXPIRED"):
                return _result("FAILED", cp, error="Instagram medya kapsayıcısı işlenemedi veya süresi doldu.")
            if state.get("status_code") == "PUBLISHED":
                return _result("UNKNOWN", cp, error="Instagram kapsayıcısı yayınlanmış; gönderi kimliği elle eşlenmeli.")
            if state.get("status_code") != "FINISHED":
                return _result("PROCESSING", cp)
            persist(cp, save_checkpoint, publish_started=True)
            response = self.client.post(f"{self.base}/{account_id}/media_publish", headers=headers, data={"creation_id": cp["container_id"]})
            if response.status_code >= 500:
                return _result("UNKNOWN", cp, error="Instagram yayın sonucu belirsiz; tekrar yayınlama durduruldu.")
            if response.status_code >= 400:
                # Known rejection is retryable only after rechecking this same container.
                persist(cp, save_checkpoint, publish_started=False)
                checked(response)
            result = response.json()
            if not result.get("id"):
                return _result("UNKNOWN", cp)
            persist(cp, save_checkpoint, remote_id=result["id"])
            return _result("PUBLISHED", cp)
        except httpx.TransportError:
            return _result("UNKNOWN" if cp.get("publish_started") else "PROCESSING", cp,
                           error="Instagram bağlantısı kesildi; kalıcı işlem durumu korundu.")

    def analytics(self, remote_id, credentials, start_date=None, end_date=None):
        headers = bearer(credentials)
        data = checked(self.client.get(f"{self.base}/{remote_id}/insights", headers=headers,
            params={"metric": "views,reach,likes,comments,saved,shares,total_interactions"})).json()
        result = {}
        for item in data.get("data", []):
            values = item.get("values", [])
            value = values[-1].get("value") if values else item.get("total_value", {}).get("value")
            if isinstance(value, (int, float)):
                result["saves" if item["name"] == "saved" else item["name"]] = value
        return {**result, "unavailable": ["retention_curve", "click_through_rate"]}


class TikTokPublisher:
    """Policy boundary: private unattended team-account utilities are not approved use."""
    POLICY_URL = "https://developers.tiktok.com/docs/en/content-sharing-guidelines"

    def __init__(self, client=None):
        self.client = client or httpx.Client(timeout=45)

    def publish(self, video_path, metadata, credentials, checkpoint=None, save_checkpoint=None):
        return _result("POLICY_BLOCKED", checkpoint or {},
            error="TikTok Direct Post, ekip hesaplarına özel otonom yükleme aracını kabul etmiyor; her gönderide kullanıcı kontrolü ve onayı istiyor. Hazır videoyu indirip TikTok'ta yayınlayın.",
            handoff={"video_path": str(video_path), "metadata": metadata, "policy_url": self.POLICY_URL})

    def analytics(self, remote_id, credentials, start_date=None, end_date=None):
        response = checked(self.client.post("https://open.tiktokapis.com/v2/video/query/",
            headers=bearer(credentials), params={"fields": "id,view_count,like_count,comment_count,share_count"},
            json={"filters": {"video_ids": [str(remote_id)]}})).json()
        if response.get("error", {}).get("code") not in (None, "ok"):
            raise ProviderError("TikTok video metrikleri alınamadı.", "analytics_unavailable")
        videos = response.get("data", {}).get("videos", [])
        stats = videos[0] if videos else {}
        return {**{target: stats[source] for target, source in {"views": "view_count", "likes": "like_count", "comments": "comment_count", "shares": "share_count"}.items() if source in stats},
                "unavailable": ["retention", "watch_time", "saves", "click_through_rate"]}
