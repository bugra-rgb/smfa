"""Independent Drive channels; no implicit cross-platform publishing."""
from factory.config import settings
from factory.providers.x import REQUIRED_SCOPES
from factory.services.hashtags import valid_pool

PLATFORMS = ('x', 'youtube', 'tiktok', 'facebook', 'instagram')


def configured(platform):
    if platform == 'x':return bool(settings.x_client_id and settings.x_client_secret)
    if platform == 'youtube':return bool((settings.youtube_client_id or settings.google_client_id) and (settings.youtube_client_secret or settings.google_client_secret))
    if platform == 'tiktok':return bool(settings.buffer_api_key or (settings.tiktok_client_key and settings.tiktok_client_secret))
    if platform == 'facebook':return bool(settings.facebook_client_id and settings.facebook_client_secret and settings.graph_api_version)
    return False


def connection_ready(c):
    if not c or c.status.upper() != 'CONNECTED' or not c.external_id:return False
    scopes=set(c.scopes or [])
    if instagram_buffer(c):
        from factory.providers.buffer_instagram import ready
        return ready(c)
    if c.platform=='x':return REQUIRED_SCOPES.issubset(scopes)
    if youtube_buffer(c):
        return ('buffer:youtube:publish' in scopes and all((c.settings or {}).get(k)
                for k in ('buffer_channel_id','buffer_account_id','buffer_organization_id')))
    if c.platform=='youtube':return bool(scopes.intersection({'https://www.googleapis.com/auth/youtube.upload','https://www.googleapis.com/auth/youtube','https://www.googleapis.com/auth/youtube.force-ssl'}))
    if c.platform=='facebook':
        from factory.providers.facebook import REQUIRED_SCOPES as FACEBOOK_SCOPES
        return FACEBOOK_SCOPES.issubset(scopes) and (c.settings or {}).get('facebook_page_selected') is True
    if c.platform=='tiktok' and (c.settings or {}).get('publish_via')=='buffer':
        return ('buffer:tiktok:publish' in scopes and all((c.settings or {}).get(k)
                for k in ('buffer_channel_id','buffer_account_id','buffer_organization_id')))
    return c.platform=='tiktok' and 'user.info.basic' in scopes


def instagram_buffer(c):
    return bool(c and c.platform=='instagram' and (c.settings or {}).get('publish_via')=='buffer')


def youtube_buffer(c):
    return bool(c and c.platform=='youtube' and (c.settings or {}).get('publish_via')=='buffer')


def tiktok_buffer(c):
    return bool(c and c.platform=='tiktok' and (c.settings or {}).get('publish_via')=='buffer')


def tiktok_mode(links):
    c=next((c for c in links if c.platform=='tiktok'),None)
    if tiktok_buffer(c):return 'buffer_auto' if connection_ready(c) else 'connection_required'
    return 'manual_legacy' if connection_ready(c) else 'connection_required'


def eligible(account, connection):
    target=(account.settings or {}).get('managed_platform')
    if not target:return True  # Existing Instagram behavior is unchanged.
    if target=='instagram' and instagram_buffer(connection):
        from factory.providers.buffer_instagram import ready
        return ready(connection)
    return ((target in ('x','youtube','facebook') or (target=='tiktok' and tiktok_buffer(connection)))
            and connection.platform==target and connection_ready(connection)
            and (connection.settings or {}).get('auto_publish_enabled') is True)


def problems(account, links):
    cfg=account.settings or {};p=cfg.get('managed_platform');result=[]
    if p not in PLATFORMS:return ['Bağımsız platform hesabı değil']
    c=next((c for c in links if c.platform==p),None)
    if not youtube_buffer(c) and not instagram_buffer(c) and not configured(p):result.append('Platform uygulama bilgileri gerekli')
    if not connection_ready(c):result.append('Platform hesabı gerekli izinlerle bağlanmalı')
    if p=='instagram' and instagram_buffer(c) and cfg.get('buffer_instagram_cover_mode') not in ('intro_200ms','video_frame'):
        result.append('Buffer Instagram kapak davranisi secilmeli')
    if p=='tiktok' and tiktok_buffer(c):
        if not settings.buffer_api_key:result.append('Buffer sunucu anahtarı gerekli')
        if type(cfg.get('publishing',{}).get('tiktok',{}).get('contains_synthetic_media')) is not bool:
            result.append('TikTok yapay/değiştirilmiş içerik seçimi gerekli')
    if p=='facebook' and c and c.status=='PAGE_SELECTION_REQUIRED':result.append('Yayın yapılacak Facebook Sayfasını seçin')
    if not any(c.platform in ('drive','google') and c.status.upper()=='CONNECTED' for c in links):result.append('Google Drive bağlanmalı')
    if not cfg.get('drive_input_folder_id'):result.append('Video klasörü seçilmeli')
    if not valid_pool(cfg.get('manual_hashtag_pool')):result.append('En az 5 farklı hashtag gerekli')
    if p=='youtube':
        yt=cfg.get('publishing',{}).get('youtube',{})
        if not youtube_buffer(c) and yt.get('privacy_status')!='private' and not settings.youtube_audit_approved:result.append('Herkese açık/liste dışı yayın için YouTube uygulama denetimi gerekli')
        if type(yt.get('made_for_kids')) is not bool:result.append('YouTube çocuklara özel içerik seçimi gerekli')
        if type(yt.get('contains_synthetic_media')) is not bool:result.append('YouTube yapay/değiştirilmiş içerik seçimi gerekli')
    return result
