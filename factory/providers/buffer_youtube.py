"""YouTube Shorts through Buffer. Queue acceptance is not a published video.

Uses Buffer's documented channel, createPost and post operations. An uncertain
createPost is never repeated. Visibility is reported by Buffer, and the source
of that evidence is retained for the panel instead of claiming a Google probe.
"""
from datetime import datetime
import hashlib
import math
from pathlib import Path
import re
import time
from urllib.parse import parse_qs, urlsplit

import httpx

from factory.media.engine import inspect_media
from .base import ProviderError, persist
from .buffer import BufferClient, _public_media_url, channel_problem
from .social import _result

SCOPE = "buffer:youtube:publish"
CHANNEL_FIELDS = """id name organizationId service serviceId isDisconnected isLocked isQueuePaused
    metadata { ... on YoutubeMetadata { defaultToReminders } }"""
POST_FIELDS = """id status channelId channelService schedulingType externalLink sentAt
    channel { id organizationId service serviceId }
    metadata { ... on YoutubePostMetadata { title privacy madeForKids isAiGenerated } }"""
CHANNEL_QUERY = "query SMFBufferYouTubeChannel($input: ChannelInput!) { account { id } channel(input: $input) { " + CHANNEL_FIELDS + " } }"
POST_QUERY = "query SMFBufferYouTubePost($input: PostInput!) { account { id } post(input: $input) { " + POST_FIELDS + " } }"
CREATE_QUERY = """mutation SMFBufferYouTubeCreate($input: CreatePostInput!) {
  createPost(input: $input) { __typename ... on PostActionSuccess { post { """ + POST_FIELDS + """ } }
    ... on MutationError { message }
  }
}"""


def youtube_url(value):
    """Accept only real YouTube video URL shapes, never a lookalike host."""
    parsed = urlsplit(value or "")
    if parsed.scheme != "https" or parsed.username or parsed.password or parsed.port not in (None, 443):
        return None
    if parsed.hostname == "youtu.be":
        video_id = parsed.path.strip("/")
    elif parsed.hostname in ("youtube.com", "www.youtube.com", "m.youtube.com"):
        if parsed.path == "/watch":
            values = parse_qs(parsed.query).get("v", [])
            video_id = values[0] if len(values) == 1 else ""
        else:
            match = re.fullmatch(r"/shorts/([A-Za-z0-9_-]{11})/?", parsed.path)
            video_id = match[1] if match else ""
    else:
        return None
    if not re.fullmatch(r"[A-Za-z0-9_-]{11}", video_id):
        return None
    return video_id, "https://www.youtube.com/watch?v=" + video_id


def create_error_detail(result, token):
    """Preserve the provider error, excluding credentials and signed URLs."""
    kind = result.get("__typename") if isinstance(result, dict) else None
    if not isinstance(kind, str) or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,100}", kind):
        kind = "UnrecognizedResponse"
    value = result.get("message", "") if isinstance(result, dict) else ""
    value = str(value).replace(token, "[secret]") if token else str(value)
    value = re.sub(r'https?://[^\s"<>]+', '[url]', value)
    value = re.sub(r'(?i)(bearer\s+|(?:token|signature|password|secret)\s*[=:]\s*)[^\s,;]+', r'\1[secret]', value)
    value = ''.join(c for c in value if c.isprintable())[:500]
    return {"type": kind, "message": value or "Buffer response was not a success."}


class BufferYouTubePublisher:
    def __init__(self, client=None, reserve=None):
        self.api = BufferClient(client, reserve)
        self.client = self.api.client

    @staticmethod
    def _bindings(credentials):
        keys = ("external_id", "buffer_channel_id", "buffer_organization_id", "buffer_account_id")
        result = {key: credentials.get(key) for key in keys}
        if any(not isinstance(value, str) or not value for value in result.values()):
            raise ProviderError("YouTube hesabını SMF panelinde Buffer’dan seçin.", "buffer_identity")
        return result

    @staticmethod
    def _matches(channel, binding):
        return (isinstance(channel, dict) and channel.get("id") == binding["buffer_channel_id"]
                and channel.get("organizationId") == binding["buffer_organization_id"]
                and channel.get("service") == "youtube" and channel.get("serviceId") == binding["external_id"])

    def _record(self, post, cp, binding, save):
        if (not isinstance(post, dict) or post.get("id") != cp.get("buffer_post_id")
                or post.get("channelId") != binding["buffer_channel_id"] or post.get("channelService") != "youtube"
                or not self._matches(post.get("channel"), binding)):
            return _result("UNKNOWN", cp, error="Buffer hesap veya gönderi kimliği uyuşmuyor; yayın doğrulanmadı.")
        if post.get("schedulingType") != "automatic":
            return _result("BLOCKED", cp, error="Buffer bildirimi otomatik yayın sayılmaz; gönderiyi kontrol edin.")
        state = post.get("status")
        if state == "sent":
            try:
                link = youtube_url(post.get("externalLink"))
                sent_at = datetime.fromisoformat(str(post.get("sentAt", "")).replace("Z", "+00:00"))
                valid_time = sent_at.tzinfo is not None
            except (TypeError, ValueError):
                link, valid_time = None, False
            if not link or not valid_time:
                return _result("UNKNOWN", cp, error="Buffer gönderildi dedi ancak YouTube yayın bağlantısı/tarihi doğrulanamadı.")
            meta = post.get("metadata") or {}
            visibility = meta.get("privacy")
            if visibility != cp.get("requested_privacy") or visibility not in ("public", "private", "unlisted"):
                return _result("POLICY_BLOCKED", cp, url=link[1], visibility=visibility,
                               error="Buffer yayın görünürlüğü istenen ayarla uyuşmuyor; tekrar yükleme yapılmadı.")
            if (meta.get("madeForKids") is not cp.get("made_for_kids")
                    or meta.get("isAiGenerated") is not cp.get("contains_synthetic_media")):
                return _result("POLICY_BLOCKED", cp, url=link[1],
                               error="Buffer izleyici veya yapay içerik bildirimi uyuşmuyor; yayını kontrol edin.")
            persist(cp, save, remote_id=link[0], complete=True, published_url=link[1],
                    visibility=visibility, visibility_source="buffer", provider_published_at=sent_at.isoformat())
            return _result("PUBLISHED", cp, url=link[1], visibility=visibility,
                           visibility_source="buffer", published_at=sent_at.isoformat())
        if state in ("error", "draft", "needs_approval"):
            return _result("BLOCKED", cp, error="YouTube gönderisi Buffer içinde kontrol edilmeli; tekrar gönderilmedi.")
        if state not in ("scheduled", "sending"):
            return _result("UNKNOWN", cp, error="Buffer gönderi durumu tanınmadı; tekrar gönderilmedi.")
        count = int(cp.get("poll_count", 0)) + 1
        persist(cp, save, poll_count=count, poll_after=time.time() + min(3600, 120 * (2 ** min(count - 1, 5))))
        return _result("PROCESSING", cp)

    def publish(self, video_path, metadata, credentials, checkpoint=None, save_checkpoint=None):
        cp = dict(checkpoint or {})
        binding = self._bindings(credentials)
        if credentials.get('smfa_mixed_buffer_budget_v1') is True and not self.api._reserve_injected:
            from factory.services.buffer_group_budget import reserve_request as reserve_group
            self.api.reserve = lambda: reserve_group(binding['buffer_account_id'])
        has_work = any(cp.get(key) for key in ("publish_started", "buffer_post_id", "remote_id", "complete", "upload_url"))
        if any((has_work or cp.get(key)) and cp.get(key) != value for key, value in binding.items()):
            return _result("BLOCKED", cp, error="Yayın sağlayıcısı veya bağlı kanal değişti; eski işlem yeniden gönderilmedi.")
        if cp.get("complete") and cp.get("remote_id"):
            return _result("PUBLISHED", cp, url=cp.get("published_url"), visibility=cp.get("visibility"),
                           visibility_source="buffer", published_at=cp.get("provider_published_at"))
        if cp.get("publish_started") and not cp.get("buffer_post_id"):
            return _result("UNKNOWN", cp, error="Buffer oluşturma sonucu belirsiz; mükerrer yayın önlendi.")
        token = credentials.get("access_token")
        if not token:
            raise ProviderError("Buffer sunucu anahtarı eksik.", "oauth_missing")
        if cp.get("buffer_post_id"):
            if time.time() < cp.get("poll_after", 0):
                return _result("PROCESSING", cp)
            persist(cp, save_checkpoint, poll_after=time.time() + 300)
            try:
                data = self.api.request(token, POST_QUERY, {"input": {"id": cp["buffer_post_id"]}})
                if (data.get("account") or {}).get("id") != binding["buffer_account_id"]:
                    return _result("BLOCKED", cp, error="Buffer anahtarı başka hesaba ait; yayın doğrulanmadı.")
                return self._record(data.get("post"), cp, binding, save_checkpoint)
            except ProviderError as exc:
                if exc.code == "oauth_expired":
                    raise
                persist(cp, save_checkpoint, poll_after=time.time() + max(300, exc.retry_after or 300))
                return _result("PROCESSING", cp, error=str(exc))
            except (httpx.TransportError, ValueError, TypeError, KeyError):
                return _result("PROCESSING", cp, error="Buffer sonuç kontrolü bekliyor; yeni gönderi oluşturulmadı.")

        from factory.services.account_captions import provider_caption
        caption = provider_caption(metadata, "youtube")
        privacy = metadata.get("privacy_status")
        kids, synthetic = metadata.get("made_for_kids"), metadata.get("contains_synthetic_media")
        title = metadata.get("title")
        if privacy not in ("public", "private", "unlisted") or type(kids) is not bool or type(synthetic) is not bool:
            raise ProviderError("YouTube görünürlük, izleyici ve yapay içerik seçimi gerekli.", "invalid_metadata")
        if not isinstance(title, str) or not 1 <= len(title.strip()) <= 100 or "<" in title or ">" in title:
            raise ProviderError("YouTube video başlığı 1–100 karakter olmalı, açılı ayraç içermemeli.", "invalid_metadata")
        url = _public_media_url(metadata.get("public_video_url"))
        path = Path(video_path)
        if not 0 < path.stat().st_size <= 10 * 1024 ** 3:
            raise ProviderError("Buffer Shorts videosu 10 GB sınırını aşamaz.", "invalid_video")
        media = inspect_media(path)
        duration, width, height = media["duration"], media["width"], media["height"]
        if (not media["has_video"] or not math.isfinite(duration) or not 0 < duration <= 180
                or width <= 0 or height <= 0 or min(abs(width / height - 1), abs(width / height - 9 / 16)) > 0.005):
            raise ProviderError("Buffer Shorts için en fazla 3 dakika, kare veya 9:16 video gerekli; video kesilmedi.", "invalid_video")
        with path.open("rb") as stream:
            fingerprint = hashlib.file_digest(stream, "sha256").hexdigest()
        if cp.get("fingerprint") and cp["fingerprint"] != fingerprint:
            raise ProviderError("YouTube videosunun içeriği değişti.", "video_changed")
        data = self.api.request(token, CHANNEL_QUERY, {"input": {"id": binding["buffer_channel_id"]}})
        if (data.get("account") or {}).get("id") != binding["buffer_account_id"] or not self._matches(data.get("channel"), binding):
            raise ProviderError("Buffer hesabı/YouTube kanal kimliği değişti; yeniden bağlayın.", "buffer_identity")
        if reason := channel_problem(data["channel"], "youtube"):
            raise ProviderError(reason, "buffer_not_ready")
        self.api.reserve()
        persist(cp, save_checkpoint, publish_started=True, fingerprint=fingerprint, requested_privacy=privacy,
                made_for_kids=kids, contains_synthetic_media=synthetic, **binding)
        try:
            data = self.api.request(token, CREATE_QUERY, {"input": {
                "channelId": binding["buffer_channel_id"], "text": caption,
                "assets": [{"video": {"url": url}}], "mode": "shareNow", "schedulingType": "automatic",
                "needsApproval": False, "saveToDraft": False,
                "metadata": {"youtube": {"title": title.strip(), "categoryId": "24", "privacy": privacy,
                                         "madeForKids": kids, "isAiGenerated": synthetic}}
            }}, reserved=True)
            result = data.get("createPost", {})
            post = result.get("post")
            if result.get("__typename") != "PostActionSuccess" or not isinstance(post, dict) or not post.get("id"):
                detail = create_error_detail(result, token)
                persist(cp, save_checkpoint, buffer_create_error=detail)
                # No automatic retry, including for typed errors. Retain all
                # checkpoint fields and the provider's reason for human review.
                return _result("UNKNOWN", cp, error="Buffer: " + detail["type"] + ": " + detail["message"] + "; tekrar gönderilmedi.")
            persist(cp, save_checkpoint, buffer_post_id=str(post["id"]))
            return self._record(post, cp, binding, save_checkpoint)
        except (ProviderError, httpx.TransportError, ValueError, TypeError, KeyError):
            return _result("UNKNOWN", cp, error="Buffer oluşturma yanıtı alınamadı; otomatik tekrar engellendi.")

    def analytics(self, remote_id, credentials, start_date=None, end_date=None):
        return {"unavailable": ["views", "likes", "comments", "shares"],
                "note": "Buffer sonuç doğrulaması etkin; izlenme analitiği bu bağlantıda henüz etkin değil."}
