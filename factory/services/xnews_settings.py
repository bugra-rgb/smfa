"""X news configuration; secrets encrypted with SMFA's existing server key.

This deployment has one shared workspace and existing admin/operator/viewer roles.
Only admins can edit news settings or bind a Buffer credential. Existing video
connections, tokens and Account.settings are never updated here.
"""
import os
from sqlalchemy import select
from factory.models import Connection, OAuthToken, SystemLog
from factory.security import decrypt_json, encrypt_json
from factory.config import settings
from factory.xnews_models import XNewsConfig

DEFAULTS = {
    'global_enabled': True, 'groq_model': '',
    'trend': {'url': '', 'provider': '', 'region': 'TR', 'permission_confirmed': False, 'max_age_seconds': 300},
    'global_daily_calls_cap': 100, 'account_daily_calls_cap': 20,
    'global_daily_tokens_cap': 100000, 'account_daily_tokens_cap': 20000,
}


def read_settings(session):
    row = session.get(XNewsConfig, 'global')
    cfg = dict(row.config or {}) if row else {}
    return {**DEFAULTS, **cfg, 'trend': {**DEFAULTS['trend'], **cfg.get('trend', {})}}


def read_secret(session, key):
    row = session.get(XNewsConfig, 'global')
    if row and row.encrypted_secrets:
        value = decrypt_json(row.encrypted_secrets).get(key, '')
        if value:
            return value
    return os.environ.get('GROQ_API_KEY', '') if key == 'groq' else ''


def public_settings(session):
    cfg = read_settings(session)
    trend = cfg.pop('trend')
    return {**cfg, 'groq_key_configured': bool(read_secret(session, 'groq')),
            'groq_model': cfg.get('groq_model') or os.environ.get('GROQ_MODEL', ''),
            'trend_key_configured': bool(read_secret(session,'trend')), 'trend_url': trend['url'], 'trend_provider': trend['provider'],
            'trend_permission_confirmed': trend['permission_confirmed'],
            'trend_configured': bool(trend['url'] and trend['provider'] and trend['permission_confirmed']),
            'trend_freshness_seconds': trend['max_age_seconds']}


def write_settings(session, values, actor_id):
    row = session.scalar(select(XNewsConfig).where(XNewsConfig.id == 'global').with_for_update())
    if row is None:
        row = XNewsConfig(id='global', config={}, revision=1)
        session.add(row)
    old = read_settings(session)
    secrets = decrypt_json(row.encrypted_secrets) if row.encrypted_secrets else {}
    values = dict(values)
    trend_key = values.pop('trend_api_key', None)
    if trend_key and trend_key.strip():
        secrets['trend'] = trend_key.strip()
        row.encrypted_secrets = encrypt_json(secrets)
    key = values.pop('groq_api_key', None)
    if key is not None and key.strip():
        secrets['groq'] = key.strip()
        row.encrypted_secrets = encrypt_json(secrets)
    trend = dict(old['trend'])
    for external, internal in [('trend_url','url'), ('trend_provider','provider'),
                              ('trend_permission_confirmed','permission_confirmed'),
                              ('trend_freshness_seconds','max_age_seconds')]:
        if external in values:
            trend[internal] = values.pop(external)
    row.config = {**old, **values, 'trend': trend}
    row.revision = (row.revision or 0) + 1
    session.add(SystemLog(event='xnews.config.saved', message='X haber sağlayıcı ayarları kaydedildi.',
                          level='info', data={'user_id': actor_id, 'secret_changed': bool(key)}))
    session.flush()
    return public_settings(session)


def credential_info(session, connection_id):
    """Read personal Buffer keys only; never run unrelated platform OAuth refresh."""
    from factory.services.xnews_sources import NewsError
    if connection_id == 'server':
        if not settings.buffer_api_key:
            raise NewsError('buffer_key_missing', 'Buffer sunucu anahtarı yapılandırılmadı.')
        return settings.buffer_api_key, None
    conn = session.get(Connection, connection_id)
    if not conn or (conn.settings or {}).get('publish_via') != 'buffer' or conn.status.upper() != 'CONNECTED':
        raise NewsError('buffer_connection', 'Kayıtlı Buffer bağlantısı etkin değil.')
    token = session.scalar(select(OAuthToken).where(OAuthToken.connection_id == conn.id))
    if not token:
        raise NewsError('buffer_key_missing', 'Buffer bağlantısının sunucu yetkilendirmesi eksik.')
    credentials = decrypt_json(token.encrypted_payload)
    # The installed Buffer adapters use personal API keys; never pretend that
    # their platform OAuth refresh function implements Buffer OAuth refresh.
    if credentials.get('refresh_token') or credentials.get('expires_at'):
        raise NewsError('buffer_oauth_unsupported', 'Bu sürümde kayıtlı kişisel Buffer API anahtarı seçilmeli; OAuth yenilemesi yapılandırılmadı.')
    value = credentials.get('access_token')
    if not isinstance(value, str) or not value:
        raise NewsError('buffer_key_missing', 'Buffer anahtarı bulunamadı.')
    return value, conn


def _shared_buffer_reserve(buffer_account_id, connection_id, *, mixed=False, instagram=False):
    from factory.services.buffer_budget import reserve_request as legacy
    from factory.services.buffer_group_budget import reserve_request as group
    if buffer_account_id and (mixed or instagram):
        return lambda: group(buffer_account_id)
    return legacy


def buffer_provider(session, cfg, reserve=None):
    from factory.providers.xnews import BufferXNews
    from factory.services.xnews_sources import NewsError
    connection_id = cfg.get('buffer_connection_id', '')
    token, conn = credential_info(session, connection_id)
    if conn:
        actual = (conn.settings or {}).get('buffer_account_id')
        if actual and cfg.get('buffer_account_id') and actual != cfg['buffer_account_id']:
            raise NewsError('buffer_identity', 'Buffer kuruluşu ile seçilen yetkilendirme eşleşmiyor.')
    credential_flags = {}
    if conn:
        saved = session.scalar(select(OAuthToken).where(OAuthToken.connection_id == conn.id))
        credential_flags = decrypt_json(saved.encrypted_payload) if saved else {}
    shared = _shared_buffer_reserve(cfg.get('buffer_account_id'), connection_id,
        mixed=credential_flags.get('smfa_mixed_buffer_budget_v1') is True,
        instagram=credential_flags.get('smfa_instagram_buffer_v1') is True)
    def reserve_both(*args, **kwargs):
        if reserve:
            reserve(*args, **kwargs)
        shared()
    return BufferXNews(token, cfg, reserve=reserve_both)


def ai_provider(session, cfg, reserve=None):
    from factory.services.xnews_sources import GroqClient, NewsError
    model = cfg.get('ai_model') or read_settings(session).get('groq_model') or os.environ.get('GROQ_MODEL', '')
    key = read_secret(session, 'groq')
    if not key or not model:
        raise NewsError('groq_not_configured', 'Groq anahtarı ve kullanılabilir Llama modeli seçilmeli.')
    return GroqClient(key, model, reserve=reserve)


def write_secret(session,key,value,actor_id):
    row=session.scalar(select(XNewsConfig).where(XNewsConfig.id=='global').with_for_update())
    if row is None:
        row=XNewsConfig(id='global',config={},revision=1);session.add(row)
    saved=decrypt_json(row.encrypted_secrets) if row.encrypted_secrets else {}
    saved[key]=value.strip();row.encrypted_secrets=encrypt_json(saved)
    session.add(SystemLog(event='xnews.secret.saved',level='info',message='Haber sağlayıcısı anahtarı güncellendi.',data={'user_id':actor_id,'provider_kind':key.split(':')[0]}))
    session.flush()
