"""TikTok automatic publishing through Buffer's documented public GraphQL API.

Reference: https://developers.buffer.com/reference.html (18 September 2026).
createPost has no documented idempotency key: an uncertain mutation is NEVER
repeated. Notifications, drafts and Buffer queue acceptance are not publication.
"""
from datetime import datetime
import hashlib
from pathlib import Path
import re
import time
from urllib.parse import parse_qs, urlsplit

import httpx

from factory.config import settings
from .base import ProviderError, persist
from .social import _result

ENDPOINT = "https://api.buffer.com"
SCOPE = "buffer:tiktok:publish"
CHANNEL_FIELDS = """id name organizationId service serviceId isDisconnected isLocked isQueuePaused
    metadata { ... on TiktokMetadata { defaultToReminders } }"""
POST_FIELDS = """id status channelId channelService schedulingType externalLink sentAt
    channel { id organizationId service serviceId }"""
ACCOUNT_QUERY = "query SMFBufferAccount { account { id organizations { id name } } }"
CHANNELS_QUERY = "query SMFBufferChannels($input: ChannelsInput!) { channels(input: $input) { " + CHANNEL_FIELDS + " } }"
CHANNEL_QUERY = "query SMFBufferChannel($input: ChannelInput!) { account { id } channel(input: $input) { " + CHANNEL_FIELDS + " } }"
POST_QUERY = "query SMFBufferPost($input: PostInput!) { account { id } post(input: $input) { " + POST_FIELDS + " } }"
CREATE_QUERY = """mutation SMFBufferCreate($input: CreatePostInput!) {
  createPost(input: $input) { __typename ... on PostActionSuccess { post { """ + POST_FIELDS + """ } }
    ... on MutationError { message }
  }
}"""


def ready(connection):
    cfg = connection.settings or {}
    return (connection.platform == "tiktok" and connection.status.upper() == "CONNECTED"
            and bool(connection.external_id) and cfg.get("publish_via") == "buffer"
            and cfg.get("auto_publish_enabled") is True and SCOPE in (connection.scopes or [])
            and bool(cfg.get("buffer_channel_id")) and bool(cfg.get("buffer_account_id")))


def channel_problem(channel, platform="tiktok"):
    label = "YouTube" if platform == "youtube" else "TikTok"
    if platform not in ("tiktok", "youtube") or not isinstance(channel, dict) or channel.get("service") != platform or not channel.get("serviceId"):
        return f"Buffer {label} hesap kimliği doğrulanamadı."
    if channel.get("isDisconnected") is not False:
        return f"{label} hesabını Buffer içinde yeniden bağlayın."
    if channel.get("isLocked") is not False:
        return "Buffer kanal sınırı veya hesap kilidi kontrol edilmeli."
    if channel.get("isQueuePaused") is not False:
        return "Buffer içindeki kanal duraklatılmış."
    if (channel.get("metadata") or {}).get("defaultToReminders") is not False:
        return f"Buffer içinden {label} otomatik yayınını açın; bildirimle yayın kullanılamaz."
    return ""


class BufferClient:
    def __init__(self, client=None, reserve=None):
        self.client = client or httpx.Client(timeout=90, follow_redirects=False)
        self._reserve_injected = reserve is not None
        if reserve is None:
            from factory.services.buffer_budget import reserve_request
            reserve = reserve_request
        self.reserve = reserve

    def request(self, token, query, variables=None, *, reserved=False):
        if not token:
            raise ProviderError("Buffer sunucu anahtarı eksik.", "oauth_missing")
        if not reserved:
            self.reserve()
        response = self.client.post(ENDPOINT, headers={"Authorization": "Bearer " + token},
                                    json={"query": query, "variables": variables or {}})
        if response.status_code == 429:
            retry = response.headers.get("retry-after", "")
            raise ProviderError("Buffer istek sınırına ulaşıldı.", "rate_limit", True,
                                max(120, int(retry)) if retry.isdigit() else 900)
        if response.status_code in (401, 403):
            raise ProviderError("Buffer anahtarı veya kanal izni yenilenmeli.", "oauth_expired")
        if response.status_code != 200:
            raise ProviderError("Buffer yanıtı doğrulanamadı.", "buffer_http_error", response.status_code >= 500)
        payload = response.json()
        if not isinstance(payload, dict) or payload.get("errors") or not isinstance(payload.get("data"), dict):
            raise ProviderError("Buffer API yanıtı kontrol edilmeli.", "buffer_graphql_error")
        return payload["data"]

    def discover(self, token, platform="tiktok"):
        if platform not in ("tiktok", "youtube"):
            raise ProviderError("Desteklenmeyen Buffer platformu.", "unsupported_provider")
        query = CHANNELS_QUERY
        if platform == "youtube":
            from .buffer_youtube import CHANNEL_FIELDS as YOUTUBE_CHANNEL_FIELDS
            query = "query SMFBufferYouTubeChannels($input: ChannelsInput!) { channels(input: $input) { " + YOUTUBE_CHANNEL_FIELDS + " } }"
        account = self.request(token, ACCOUNT_QUERY).get("account")
        if not isinstance(account, dict) or not account.get("id") or not isinstance(account.get("organizations"), list):
            raise ProviderError("Buffer hesap kimliği doğrulanamadı.", "buffer_identity")
        # Official account.organizations and channels are complete arrays, not
        # paginated connections. Query all accessible organizations; no first-page assumption.
        if len(account["organizations"]) > 10:
            raise ProviderError("Ücretsiz bağlantı için Buffer kuruluş kapsamı çok geniş.", "buffer_account_limit")
        found = []
        for org in account["organizations"]:
            if not isinstance(org, dict) or not org.get("id"):
                raise ProviderError("Buffer kuruluş kimliği doğrulanamadı.", "buffer_identity")
            channels = self.request(token, query, {"input": {"organizationId": org["id"]}}).get("channels")
            if not isinstance(channels, list):
                raise ProviderError("Buffer kanal listesi doğrulanamadı.", "buffer_identity")
            for channel in channels:
                if channel.get("service") != platform:
                    continue
                if not channel.get("id") or channel.get("organizationId") != org["id"]:
                    raise ProviderError("Buffer kanal kuruluşu doğrulanamadı.", "buffer_identity")
                found.append({**channel, "organization_name": org.get("name", ""), "buffer_account_id": account["id"]})
        return found


def _public_media_url(value):
    from fastapi import HTTPException
    from factory.security import verify_media_signature
    origin = urlsplit(settings.media_base_url or settings.public_base_url)
    parsed = urlsplit(value or "")
    if (parsed.scheme != "https" or parsed.netloc != origin.netloc or parsed.username or parsed.password
            or parsed.fragment or not re.fullmatch(r"/api/media/[a-fA-F0-9-]{36}", parsed.path)):
        raise ProviderError("Buffer videosu SMF imzalı medya adresinden gelmeli.", "unsafe_remote_url")
    args = parse_qs(parsed.query)
    try:
        if set(args) != {"expires", "signature"} or any(len(v) != 1 for v in args.values()):
            raise ValueError()
        expiry = int(args["expires"][0])
        verify_media_signature(parsed.path.rsplit("/", 1)[1], expiry, args["signature"][0])
        if expiry < time.time() + 600:
            raise ValueError()
    except (HTTPException, KeyError, ValueError, TypeError):
        raise ProviderError("Buffer medya bağlantısının imzası veya süresi geçersiz.", "unsafe_remote_url") from None
    return value


class BufferTikTokPublisher:
    def __init__(self, client=None, reserve=None):
        self.api = BufferClient(client, reserve)
        self.client = self.api.client  # Pipeline eligibility/lease guards attach here.

    @staticmethod
    def _bindings(credentials):
        keys = ("external_id", "buffer_channel_id", "buffer_organization_id", "buffer_account_id")
        result = {key: credentials.get(key) for key in keys}
        if any(not isinstance(value, str) or not value for value in result.values()):
            raise ProviderError("Buffer TikTok hesabını SMF panelinden bağlayın.", "buffer_identity")
        return result

    @staticmethod
    def _matches(channel, binding):
        return (isinstance(channel, dict) and channel.get("id") == binding["buffer_channel_id"]
                and channel.get("organizationId") == binding["buffer_organization_id"]
                and channel.get("service") == "tiktok" and channel.get("serviceId") == binding["external_id"])

    def _record(self, post, cp, binding, save):
        if (not isinstance(post, dict) or post.get("id") != cp.get("buffer_post_id")
                or post.get("channelId") != binding["buffer_channel_id"] or post.get("channelService") != "tiktok"
                or not self._matches(post.get("channel"), binding)):
            return _result("UNKNOWN", cp, error="Buffer farklı hesap veya gönderi döndürdü; yayın doğrulanmadı.")
        if post.get("schedulingType") != "automatic":
            return _result("BLOCKED", cp, error="Buffer bildirimi otomatik yayın sayılmaz; gönderiyi Buffer içinde kontrol edin.")
        status = post.get("status")
        if status == "sent":
            parsed = urlsplit(post.get("externalLink") or "")
            match = re.fullmatch(r"/@[^/]+/video/([0-9]+)/?", parsed.path)
            try:
                sent_at = datetime.fromisoformat(str(post.get("sentAt", "")).replace("Z", "+00:00"))
                valid_time = sent_at.tzinfo is not None
            except ValueError:
                valid_time = False
            if (parsed.scheme != "https" or parsed.hostname not in ("tiktok.com", "www.tiktok.com")
                    or parsed.username or parsed.password or not match or not valid_time):
                return _result("UNKNOWN", cp, error="Buffer gönderildi dedi ancak TikTok yayın bağlantısı doğrulanamadı.")
            url = "https://www.tiktok.com" + parsed.path.rstrip("/")
            persist(cp, save, remote_id=match[1], complete=True, published_url=url)
            return _result("PUBLISHED", cp, url=url)
        if status in ("error", "draft", "needs_approval"):
            return _result("BLOCKED", cp, error="TikTok gönderisi Buffer içinde kontrol edilmeli; otomatik tekrar gönderilmedi.")
        if status not in ("scheduled", "sending"):
            return _result("UNKNOWN", cp, error="Buffer gönderi durumu tanınmadı; tekrar gönderilmedi.")
        attempts = int(cp.get("poll_count", 0)) + 1
        persist(cp, save, poll_count=attempts, poll_after=time.time() + min(3600, 120 * (2 ** min(attempts - 1, 5))))
        return _result("PROCESSING", cp)

    def publish(self, video_path, metadata, credentials, checkpoint=None, save_checkpoint=None):
        cp = dict(checkpoint or {})
        binding = self._bindings(credentials)
        if credentials.get('smfa_mixed_buffer_budget_v1') is True and not self.api._reserve_injected:
            from factory.services.buffer_group_budget import reserve_request as reserve_group
            self.api.reserve = lambda: reserve_group(binding['buffer_account_id'])
        has_remote_work = any(cp.get(key) for key in ("publish_started", "buffer_post_id", "remote_id", "complete"))
        if any((has_remote_work or cp.get(key)) and cp.get(key) != value for key, value in binding.items()):
            return _result("BLOCKED", cp, error="Bağlı Buffer/TikTok hesabı değişti; eski işlem kullanılmadı.")
        if cp.get("complete") and cp.get("remote_id"):
            return _result("PUBLISHED", cp, url=cp.get("published_url"))
        if cp.get("publish_started") and not cp.get("buffer_post_id"):
            return _result("UNKNOWN", cp, error="Buffer oluşturma sonucu belirsiz; mükerrer yayın önlendi.")
        token = credentials.get("access_token")
        if not token:
            raise ProviderError("Buffer sunucu anahtarı eksik.", "oauth_missing")
        if cp.get("buffer_post_id"):
            if time.time() < cp.get("poll_after", 0):
                return _result("PROCESSING", cp)
            # Mark the next poll before network I/O; a failing poll does not make
            # each worker tick consume a fresh Free request.
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
        caption = provider_caption(metadata, "tiktok")
        synthetic = metadata.get("contains_synthetic_media")
        if not isinstance(synthetic, bool):
            raise ProviderError("TikTok yapay zekâ içerik etiketi seçilmeli.", "invalid_metadata")
        url = _public_media_url(metadata.get("public_video_url"))
        path = Path(video_path)
        if not 0 < path.stat().st_size <= 1024 ** 3:
            raise ProviderError("TikTok videosu 1 GB sınırını aşamaz.", "invalid_video")
        with path.open("rb") as stream:
            fingerprint = hashlib.file_digest(stream, "sha256").hexdigest()
        if cp.get("fingerprint") and cp["fingerprint"] != fingerprint:
            raise ProviderError("TikTok videosunun içeriği değişti.", "video_changed")
        data = self.api.request(token, CHANNEL_QUERY, {"input": {"id": binding["buffer_channel_id"]}})
        if (data.get("account") or {}).get("id") != binding["buffer_account_id"] or not self._matches(data.get("channel"), binding):
            raise ProviderError("Buffer hesabı/TikTok kanal kimliği değişti; yeniden bağlayın.", "buffer_identity")
        reason = channel_problem(data["channel"])
        if reason:
            raise ProviderError(reason, "buffer_not_ready")
        self.api.reserve()  # Budget rejection happens before irreversible intent.
        persist(cp, save_checkpoint, publish_started=True, fingerprint=fingerprint, **binding)
        try:
            data = self.api.request(token, CREATE_QUERY, {"input": {
                "channelId": binding["buffer_channel_id"], "text": caption,
                "assets": [{"video": {"url": url}}], "mode": "shareNow", "schedulingType": "automatic",
                "needsApproval": False, "saveToDraft": False, "metadata": {"tiktok": {"isAiGenerated": synthetic}}
            }}, reserved=True)
            result = data.get("createPost", {})
            post = result.get("post")
            if result.get("__typename") != "PostActionSuccess" or not isinstance(post, dict) or not post.get("id"):
                return _result("UNKNOWN", cp, error="Buffer gönderi oluşturma sonucu doğrulanamadı; tekrar gönderilmedi.")
            persist(cp, save_checkpoint, buffer_post_id=str(post["id"]))
            return self._record(post, cp, binding, save_checkpoint)
        except (ProviderError, httpx.TransportError, ValueError, TypeError, KeyError):
            return _result("UNKNOWN", cp, error="Buffer oluşturma yanıtı alınamadı; otomatik tekrar engellendi.")

    def analytics(self, remote_id, credentials, start_date=None, end_date=None):
        return {"unavailable": ["views", "likes", "comments", "shares", "saves", "clicks"],
                "note": "Ücretsiz Buffer istek bütçesi yayın ve sonuç kontrolüne ayrıldı."}
