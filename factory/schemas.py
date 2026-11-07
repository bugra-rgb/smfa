import re
from datetime import datetime
from urllib.parse import urlsplit
from pathlib import Path
from typing import Any, Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import BaseModel, ConfigDict, Field, field_validator

from factory.config import settings


DEFAULT_ACCOUNT_SETTINGS = {
    "schedule": ["09:00", "13:00", "18:00"],
    "inventory_min": 2, "inventory_target": 5, "inventory_max": 10,
    "duration": 30, "width": 1080, "height": 1920,
    "llm_provider": "mock", "video_provider": "mock",
    "voice": {"provider": "mock", "voice": "alloy", "speed": 1.0, "pitch": 0},
    "subtitle": {"font_size": 56, "color": "#FFFFFF", "outline": 3},
    "branding": {"position": "top-right", "width": 140, "opacity": 0.85, "margin": 40},
    "categories": [], "research_interval_minutes": 180,
}


def validate_account_settings(value: dict[str, Any]):
    def check_no_secrets(data):
        if isinstance(data, dict):
            for key, child in data.items():
                if str(key).lower() in {"api_key", "access_token", "refresh_token", "client_secret", "password", "secret_key"}:
                    raise ValueError("API anahtarları hesap ayarına yazılamaz; güvenli sunucu ayarlarını kullanın.")
                check_no_secrets(child)
        elif isinstance(data, list):
            for child in data:
                check_no_secrets(child)
    check_no_secrets(value)
    if value.get("caption_policy") == "manual_five_hashtags":
        from factory.services.hashtags import valid_pool
        if not valid_pool(value.get("manual_hashtag_pool")):
            raise ValueError("En az 5 farklı geçerli hashtag gerekli.")
    if "cycle_index" in value and (type(value["cycle_index"]) is not int or not 0 <= value["cycle_index"] < 180):
        raise ValueError("Yayın sırası 0–179 arasında olmalı.")
    if "cycle_v2" in value and type(value["cycle_v2"]) is not bool:
        raise ValueError("Yayın döngüsü açık/kapalı olmalı.")
    mode = value.get("publication_mode")
    if mode is not None and mode not in ("manual", "planner"):
        raise ValueError("Yayın modu manual veya planner olmalı.")
    times = value.get("planner_times", [])
    if not isinstance(times, list) or len(times)>12 or any(not isinstance(t,str) or not re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d",t) for t in times) or len(set(times))!=len(times):
        raise ValueError("Plan saatleri benzersiz HH:MM değerleri olmalı.")
    offset = value.get("planner_offset", 0)
    if type(offset) is not int or not 0 <= offset < 360:
        raise ValueError("Plan sıra farkı 0–359 dakika olmalı.")
    interval = value.get("planner_interval_hours", 0)
    if type(interval) is not int or not 0 <= interval <= 168:
        raise ValueError("Plan aralığı 0–168 saat olmalı.")
    if interval:
        try:
            anchor = datetime.fromisoformat(value.get("planner_anchor", ""))
            if anchor.tzinfo is None: raise ValueError()
        except (ValueError, TypeError):
            raise ValueError("Aralıklı planın başlangıcı saat dilimi içermeli.") from None
    hours = value.get("publish_interval_hours", 0)
    if not isinstance(hours, int) or isinstance(hours, bool) or not 0 <= hours <= 168:
        raise ValueError("Yayın aralığı 1–168 saat olmalı; saat listesi için 0 kullanın.")
    anchor = value.get("publish_not_before")
    if anchor:
        try:
            parsed = datetime.fromisoformat(anchor.replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                raise ValueError()
        except (ValueError, TypeError, AttributeError):
            raise ValueError("İlk yayın zamanı saat dilimi içeren ISO tarihi olmalı.") from None
    folder = value.get("drive_input_folder_id", "")
    if not isinstance(folder, str):
        raise ValueError("Drive klasör bağlantısı veya kimliği metin olmalı.")
    folder = folder.strip()
    if folder.startswith("https://"):
        url = urlsplit(folder)
        match = re.fullmatch(r"/drive/(?:u/\d+/)?folders/([A-Za-z0-9_-]+)(?:/)?", url.path)
        if url.netloc != "drive.google.com" or not match:
            raise ValueError("Geçerli bir Google Drive klasör bağlantısı girin.")
        folder = match.group(1)
    if folder and not re.fullmatch(r"[A-Za-z0-9_-]{10,200}", folder):
        raise ValueError("Geçerli bir Google Drive klasör kimliği girin.")
    if "drive_input_folder_id" in value:
        value = {**value, "drive_input_folder_id": folder}
    publishing = value.get("publishing", {})
    if not isinstance(publishing, dict) or not isinstance(publishing.get("instagram", {}), dict):
        raise ValueError("Platform yayın ayarları nesne olmalı.")
    caption = publishing.get("instagram", {}).get("caption", "")
    if not isinstance(caption, str) or len(caption) > 2200:
        raise ValueError("Instagram açıklaması en fazla 2200 karakter olabilir.")
    schedule = value.get("schedule", [])
    if not isinstance(schedule, list) or any(not isinstance(x, str) or not re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", x) for x in schedule):
        raise ValueError("Yayın saatlerini HH:MM biçiminde girin.")
    if len(schedule) != len(set(schedule)):
        raise ValueError("Aynı yayın saati iki kez eklenemez.")
    levels = [value.get(key, DEFAULT_ACCOUNT_SETTINGS[key]) for key in ("inventory_min", "inventory_target", "inventory_max")]
    if any(not isinstance(x, int) or isinstance(x, bool) or x < 0 for x in levels) or levels != sorted(levels):
        raise ValueError("Stok değerleri 0 ≤ minimum ≤ hedef ≤ maksimum olmalı.")
    duration = value.get("duration", 30)
    if not isinstance(duration, (int, float)) or not 3 <= duration <= 600:
        raise ValueError("Video süresi 3–600 saniye arasında olmalı.")
    for key in ("width", "height"):
        number = value.get(key, DEFAULT_ACCOUNT_SETTINGS[key])
        if not isinstance(number, int) or number < 128 or number > 3840 or number % 2:
            raise ValueError("Çözünürlük 128–3840 arasında çift sayı olmalı.")
    for key, path in value.items():
        if key.endswith("_path") and path:
            try:
                Path(path).resolve().relative_to(Path(settings.data_dir).resolve())
            except (ValueError, TypeError):
                raise ValueError("Medya dosyası önce panele yüklenmeli.") from None
    return value


class AccountCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=1, max_length=200)
    username: str = Field(default="", max_length=200)
    niche: str = Field(min_length=1, max_length=500)
    language: str = Field(default="tr", min_length=2, max_length=40)
    target_audience: str = Field(default="", max_length=4000)
    content_style: str = Field(default="", max_length=200)
    tone: str = Field(default="", max_length=200)
    daily_target: int = Field(default=3, ge=0)
    timezone: str = "Europe/Istanbul"
    active: bool = True
    auto_publish: bool = False
    approval_mode: Literal["AUTO_PUBLISH", "MANUAL_APPROVAL"] = "MANUAL_APPROVAL"
    source_mode: Literal["AI_ONLY", "DRIVE_ONLY", "AI_AND_DRIVE"] = "AI_ONLY"
    settings: dict[str, Any] = Field(default_factory=dict)

    @field_validator("timezone")
    @classmethod
    def timezone_valid(cls, value):
        try:
            ZoneInfo(value)
        except (ZoneInfoNotFoundError, ValueError):
            raise ValueError("Geçerli bir saat dilimi seçin.") from None
        return value

    @field_validator("settings")
    @classmethod
    def settings_valid(cls, value):
        return validate_account_settings(value)

class LoginInput(BaseModel):
    email: str = Field(min_length=3, max_length=320)
    password: str = Field(min_length=1, max_length=1024)


class OAuthStartInput(BaseModel):
    account_id: str


class RescheduleInput(BaseModel):
    scheduled_at: str


class UserCreate(BaseModel):
    email: str = Field(min_length=3, max_length=320)
    password: str = Field(min_length=12, max_length=128)
    role: Literal["admin", "operator", "viewer"] = "viewer"
