from ipaddress import ip_address
from pathlib import Path
import re
from typing import Literal
from urllib.parse import urlsplit

from pydantic import model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")
    smfa_covers_enabled: bool = False
    smfa_analytics_enabled: bool = False
    smfa_oauth_hardening_enabled: bool = False
    smfa_insights_boundaries_verified: bool = False
    mode: Literal["mock", "production"] = "mock"
    database_url: str = "sqlite:///./data/factory.db"
    redis_url: str = "redis://localhost:6379/0"
    data_dir: str = "./data"
    secret_key: str = ""
    encryption_key: str = ""
    public_base_url: str = "http://localhost:3000"
    media_base_url: str = ""
    frontend_origin: str = "http://localhost:3000"
    frontend_aliases: str = ""
    allowed_hosts: str = "localhost,127.0.0.1,backend,testserver"
    worker_concurrency: int = 2
    provider_concurrency: int = 2
    provider_lease_seconds: int = 2100
    scheduler_interval: int = 30
    job_timeout: int = 1800
    session_hours: int = 12
    google_client_id: str = ""
    google_client_secret: str = ""
    youtube_client_id: str = ""
    youtube_client_secret: str = ""
    instagram_client_id: str = ""
    instagram_client_secret: str = ""
    facebook_client_id: str = ""
    facebook_client_secret: str = ""
    tiktok_client_key: str = ""
    tiktok_client_secret: str = ""
    buffer_api_key: str = ""
    x_client_id: str = ""
    x_client_secret: str = ""
    graph_api_version: str = ""
    openai_api_key: str = ""
    openai_model: str = ""
    openai_image_model: str = ""
    openai_video_model: str = ""
    openai_voice_model: str = ""
    openai_transcription_model: str = ""
    video_provider_api_key: str = ""
    runway_video_model: str = ""
    voice_provider_api_key: str = ""
    youtube_audit_approved: bool = False
    instagram_app_approved: bool = False
    storage_config: str = "{}"
    notification_webhook_url: str = ""
    cost_rates_json: str = "{}"
    admin_email: str = ""
    admin_password: str = ""

    @model_validator(mode="after")
    def validate_production(self):
        if self.provider_concurrency < 1 or self.worker_concurrency < 1 or self.job_timeout < 120:
            raise ValueError("Eşzamanlılık en az 1 ve iş zaman aşımı en az 120 saniye olmalı.")
        if self.provider_lease_seconds < self.job_timeout + 300:
            raise ValueError("PROVIDER_LEASE_SECONDS, JOB_TIMEOUT + 300 değerinden küçük olamaz.")
        for alias in filter(None, (v.strip() for v in self.frontend_aliases.split(','))):
            url = urlsplit(alias)
            if url.scheme != 'https' or not url.hostname or url.username or url.password or url.path not in ('','/') or url.query or url.fragment or '*' in alias:
                raise ValueError('FRONTEND_ALIASES yalnız kesin HTTPS origin adresleri içerebilir.')
        if self.media_base_url:
            message = "MEDIA_BASE_URL kullanıcı bilgisi, yol veya sorgu içermeyen geçerli bir origin olmalı."
            if (any(character.isspace() or ord(character) < 32 for character in self.media_base_url)
                    or any(character in self.media_base_url for character in "\\?#")):
                raise ValueError(message)
            try:
                media = urlsplit(self.media_base_url)
                port = media.port
            except ValueError:
                raise ValueError(message) from None
            if (media.scheme not in {"http", "https"} or not media.hostname
                    or media.username is not None or media.password is not None
                    or media.path not in {"", "/"} or media.query or media.fragment
                    or any(character in media.hostname for character in "%/?#@")
                    or media.netloc.endswith(":") or port == 0):
                raise ValueError(message)
            try:
                ip_address(media.hostname)
            except ValueError:
                try:
                    hostname = media.hostname.encode("idna").decode("ascii").rstrip(".")
                except UnicodeError:
                    raise ValueError(message) from None
                if len(hostname) > 253 or any(not re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?", label)
                                             for label in hostname.split(".")):
                    raise ValueError(message)
            if self.mode == "production" and media.scheme != "https":
                raise ValueError("Production MEDIA_BASE_URL HTTPS olmalı.")
            self.media_base_url = self.media_base_url.rstrip("/")
        if self.mode == "production":
            if not self.database_url.startswith("postgresql"):
                raise ValueError("Production için PostgreSQL zorunludur.")
            if len(self.secret_key) < 32 or not self.encryption_key:
                raise ValueError("SECRET_KEY ve ENCRYPTION_KEY güvenli değerlerle yapılandırılmalı.")
            from cryptography.fernet import Fernet
            Fernet(self.encryption_key.encode())
            if not self.public_base_url.startswith("https://"):
                raise ValueError("Production PUBLIC_BASE_URL HTTPS olmalı.")
            if self.frontend_origin.rstrip("/") != self.public_base_url.rstrip("/"):
                raise ValueError("Panel ve API aynı güvenli origin üzerinden sunulmalı.")
        Path(self.data_dir).mkdir(parents=True, exist_ok=True)
        return self


settings = Settings()
