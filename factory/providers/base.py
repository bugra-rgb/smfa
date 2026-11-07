"""Database-independent synchronous provider contracts and safe HTTP errors."""
from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any, Protocol
from urllib.parse import urlparse

import httpx

Checkpoint = Callable[[dict], None]


class ProviderError(Exception):
    def __init__(self, message: str, code: str = "provider_error", retryable: bool = False,
                 retry_after: int | None = None, checkpoint: dict | None = None):
        super().__init__(message)
        self.code, self.retryable = code, retryable
        self.retry_after, self.checkpoint = retry_after, checkpoint or {}


def checked(response: httpx.Response) -> httpx.Response:
    """Never include response bodies/URLs: both can contain OAuth credentials."""
    status = response.status_code
    if status < 400:
        return response
    detail = {}
    try:
        detail = response.json().get("error", {})
        reason = detail.get("errors", [{}])[0].get("reason", "") if isinstance(detail, dict) else str(detail)
    except (ValueError, AttributeError, IndexError, httpx.ResponseNotRead):
        reason = ""
    try:
        host = response.request.url.host
    except RuntimeError:
        host = ""
    provider = {"api.openai.com": "OpenAI", "graph.instagram.com": "Instagram",
                "www.googleapis.com": "Google", "oauth2.googleapis.com": "Google"}.get(host, "Sağlayıcı")
    code = "oauth_expired" if status == 401 else "provider_http_error"
    if host == "api.openai.com" and status == 401:
        raise ProviderError("OpenAI API anahtarı doğrulanamadı; sunucu anahtarı kontrol edilmeli.", "openai_authentication", False)
    if reason == "invalid_grant":
        code = "oauth_expired"
    if status == 429 or reason in {"rateLimitExceeded", "userRateLimitExceeded", "quotaExceeded", "dailyLimitExceeded"}:
        code = "rate_limit"
    retryable = status >= 500 or code == "rate_limit"
    quotas = {
        "credit_balance_exhausted": "OpenAI API kredisi bitmiş; API hesabına kredi eklenmeli.",
        "organization_spend_limit_exceeded": "OpenAI kuruluş harcama sınırına ulaşılmış.",
        "project_spend_limit_exceeded": "OpenAI proje harcama sınırına ulaşılmış.",
        "organization_usage_limit_exceeded": "OpenAI kuruluş kullanım sınırına ulaşılmış.",
        "insufficient_quota": "OpenAI API kotası kullanılamıyor; kredi ve kullanım sınırları kontrol edilmeli.",
        "billing_hard_limit_reached": "OpenAI API harcama sınırına ulaşılmış.",
    }
    if host == "api.openai.com" and isinstance(detail, dict):
        quota = detail.get("code")
        if not isinstance(quota, str):
            quota = ""
        if quota not in quotas and detail.get("type") == "insufficient_quota":
            quota = "insufficient_quota"
        if quota in quotas:
            raise ProviderError(quotas[quota], quota, False)
    delay = response.headers.get("retry-after", "")
    raise ProviderError(f"{provider} isteği başarısız (HTTP {status}, {code}).", code,
                        retryable, int(delay) if delay.isdigit() else None)


def bearer(credentials: Mapping[str, Any]) -> dict:
    token = credentials.get("access_token")
    if not token:
        raise ProviderError("Hesap bağlantısında erişim anahtarı yok.", "oauth_missing")
    return {"Authorization": f"Bearer {token}"}


def persist(checkpoint: dict, save: Checkpoint | None, **updates: Any) -> None:
    if save is None:
        raise ProviderError("Kalıcı işlem kontrol noktası kaydedicisi gerekli.", "checkpoint_required")
    checkpoint.update(updates)
    save(dict(checkpoint))


def google_session(url: str) -> str:
    parsed = urlparse(url)
    if parsed.scheme != "https" or parsed.hostname not in {"www.googleapis.com", "upload.googleapis.com"}:
        raise ProviderError("Geçersiz yükleme oturumu adresi.", "unsafe_remote_url")
    return url


class LLMProvider(Protocol):
    def research(self, account: dict, history: list) -> dict: ...
    def script(self, idea: dict, account: dict) -> dict: ...
    def metadata(self, script: dict, account: dict) -> dict: ...


class VideoProvider(Protocol):
    def generate(self, prompt: str, output_path: str, options: dict | None = None) -> dict: ...


class ImageProvider(Protocol):
    def generate(self, prompt: str, output_path: str, options: dict | None = None) -> dict: ...


class VoiceProvider(Protocol):
    def synthesize(self, text: str, output_path: str, options: dict | None = None) -> dict: ...
    def transcribe(self, path: str, options: dict | None = None) -> dict: ...


class MusicProvider(Protocol):
    def generate(self, prompt: str, output_path: str, options: dict | None = None) -> dict: ...


class StorageProvider(Protocol):
    def upload(self, path: str, folder_id: str, idempotency_key: str, **kwargs: Any) -> dict: ...
    def download(self, file_id: str, path: str) -> dict: ...


class PublisherProvider(Protocol):
    def publish(self, video_path: str, metadata: dict, credentials: dict,
                checkpoint: dict | None = None, save_checkpoint: Checkpoint | None = None) -> dict: ...
    def analytics(self, remote_id: str, credentials: dict, start_date: str | None = None,
                  end_date: str | None = None) -> dict: ...
