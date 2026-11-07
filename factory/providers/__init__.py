"""Configuration-based adapter registry. Production never falls back to mock."""
from pathlib import Path

from .base import ProviderError


def _settings(settings_obj=None):
    if settings_obj is None:
        from factory.config import settings
        return settings
    return settings_obj


def _pick(options, field, default):
    return (options or {}).get(field) or default


def get_llm(settings_obj=None, account_settings=None):
    from .ai import OpenAILLMProvider
    from .mock import MockLLMProvider
    s = _settings(settings_obj)
    if s.mode == "mock":
        return MockLLMProvider()
    if _pick(account_settings, "llm_provider", "openai") != "openai":
        raise ProviderError("Seçilen metin sağlayıcısı kurulu değil.", "unsupported_provider")
    return OpenAILLMProvider(s.openai_api_key, _pick(account_settings, "llm_model", s.openai_model))


def get_image(settings_obj=None, account_settings=None):
    from .ai import OpenAIImageProvider
    from .mock import MockImageProvider
    s = _settings(settings_obj)
    if s.mode == "mock":
        return MockImageProvider()
    if _pick(account_settings, "image_provider", "openai") != "openai":
        raise ProviderError("Seçilen görsel sağlayıcısı kurulu değil.", "unsupported_provider")
    return OpenAIImageProvider(s.openai_api_key, _pick(account_settings, "image_model", getattr(s, "openai_image_model", "")))


def get_video(settings_obj=None, account_settings=None):
    from .ai import ImageMotionVideoProvider, RunwayVideoProvider
    from .mock import MockVideoProvider
    s = _settings(settings_obj)
    if s.mode == "mock":
        return MockVideoProvider()
    provider = _pick(account_settings, "video_provider", "image_motion")
    if provider == "image_motion":
        return ImageMotionVideoProvider(get_image(s, account_settings))
    if provider == "runway":
        return RunwayVideoProvider(s.video_provider_api_key, _pick(account_settings, "video_model", getattr(s, "runway_video_model", "")))
    raise ProviderError("Video sağlayıcısı desteklenmiyor. image_motion veya runway seçin; Sora API kullanımdan kaldırılıyor.", "unsupported_provider")


def get_voice(settings_obj=None, account_settings=None):
    from .ai import OpenAIVoiceProvider
    from .mock import MockVoiceProvider
    s = _settings(settings_obj)
    if s.mode == "mock":
        return MockVoiceProvider()
    if _pick(account_settings, "voice_provider", "openai") != "openai":
        raise ProviderError("Seçilen ses sağlayıcısı kurulu değil.", "unsupported_provider")
    return OpenAIVoiceProvider(s.voice_provider_api_key or s.openai_api_key,
        _pick(account_settings, "voice_model", getattr(s, "openai_voice_model", "")),
        _pick(account_settings, "transcription_model", getattr(s, "openai_transcription_model", "") or "whisper-1"))


def get_music(settings_obj=None, account_settings=None):
    from .ai import LicensedMusicProvider
    return LicensedMusicProvider()


def get_drive(settings_obj=None, credentials=None):
    from .drive import GoogleDriveProvider
    from .mock import MockStorageProvider
    s = _settings(settings_obj)
    if s.mode == "mock":
        return MockStorageProvider(Path(s.data_dir) / "mock-drive")
    return GoogleDriveProvider(credentials or {})


def get_publisher(platform, settings_obj=None, *, publish_via=None):
    from .mock import MockPublisher
    from .social import InstagramPublisher, TikTokPublisher, YouTubePublisher
    s = _settings(settings_obj)
    platform = platform.lower()
    if platform not in {"youtube", "instagram", "tiktok", "x", "facebook"}:
        raise ProviderError("Bilinmeyen yayın platformu.", "unsupported_provider")
    if s.mode == "mock":
        return MockPublisher(platform)
    if platform == "youtube":
        if publish_via == "buffer":
            from .buffer_youtube import BufferYouTubePublisher
            return BufferYouTubePublisher()
        return YouTubePublisher()
    if platform == "instagram":
        if publish_via == "buffer":
            from .buffer_instagram import BufferInstagramPublisher
            return BufferInstagramPublisher()
        if not s.graph_api_version:
            raise ProviderError("Instagram API sürümü gerekli.", "configuration_missing")
        return InstagramPublisher(s.graph_api_version)
    if platform == "x":
        from .x import XPublisher
        return XPublisher()
    if platform == "facebook":
        from .facebook import FacebookPublisher
        return FacebookPublisher(s.graph_api_version)
    if getattr(s,'buffer_api_key',''):
        from .buffer import BufferTikTokPublisher
        return BufferTikTokPublisher()
    return TikTokPublisher()
