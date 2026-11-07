"""Session-bound Instagram reauthorization without writing Account settings."""
from fastapi import HTTPException
from sqlalchemy import select
from factory.config import settings
from factory.models import Connection, OAuthToken, utcnow
from factory.security import digest, decrypt_json, encrypt_json
from factory.update_models import InstagramCapability

BASIC='instagram_business_basic'
PUBLISH='instagram_business_content_publish'
INSIGHTS='instagram_business_manage_insights'

def begin_context(session,account_id,auth_session_id):
    connection=session.scalar(select(Connection).where(Connection.account_id==account_id,Connection.platform=='instagram'))
    token=session.scalar(select(OAuthToken).where(OAuthToken.connection_id==connection.id)) if connection else None
    return {'auth_session_id':auth_session_id,'app_id':settings.instagram_client_id,'login':'instagram_login','external_id':connection.external_id if connection else None,'token_version':digest(token.encrypted_payload) if token else None}

def verify_context(context,auth_session_id):
    if not context or context.get('auth_session_id')!=auth_session_id or context.get('app_id')!=settings.instagram_client_id or context.get('login')!='instagram_login':
        raise HTTPException(400,'Bağlantı denemesi bu oturuma veya uygulamaya ait değil. Yeniden başlatın.')

def scope_list(value):
    if isinstance(value,str):return value.replace(',',' ').split()
    return [s for s in value if isinstance(s,str)] if isinstance(value,list) else []

def validate_candidate(session,target,credentials,identity,context):
    # Shared advisory lock serializes first connection / duplicate identity checks.
    if session.bind.dialect.name=='postgresql':
        from sqlalchemy import text
        session.execute(text('SELECT pg_advisory_xact_lock(7346819202)'))
    connection=session.scalar(select(Connection).where(Connection.account_id==target.id,Connection.platform=='instagram').with_for_update().execution_options(populate_existing=True))
    token=session.scalar(select(OAuthToken).where(OAuthToken.connection_id==connection.id).with_for_update().execution_options(populate_existing=True)) if connection else None
    if (digest(token.encrypted_payload) if token else None)!=context.get('token_version'):
        raise HTTPException(409,'Erişim bu sırada yenilendi. Mevcut bağlantı korundu; yeni bir bağlantı denemesi başlatın.')
    external=str(identity.get('external_id') or '')
    if not external: raise HTTPException(409,'Kalıcı Instagram kimliği doğrulanamadı.')
    expected=connection.external_id if connection else context.get('external_id')
    if expected:
        if expected!=external: raise HTTPException(409,'Farklı Instagram hesabı seçildi. Mevcut bağlantı korundu.')
    elif target.username and target.username.lstrip('@').lower()!=str(identity.get('username','')).lstrip('@').lower():
        raise HTTPException(409,'İlk bağlantı seçilen kullanıcı adıyla eşleşmiyor.')
    duplicate=session.scalar(select(Connection.id).where(Connection.platform=='instagram',Connection.external_id==external,Connection.account_id!=target.id).limit(1))
    if duplicate: raise HTTPException(409,'Bu Instagram kimliği zaten başka bir SMFA hesabına bağlı.')
    scopes=set(scope_list(credentials.get('scope')))
    if not {BASIC,PUBLISH} <= scopes:
        raise HTTPException(409,'Temel profil/yayın izinleri doğrulanamadı. Mevcut erişim korunuyor.')
    if connection:
        previous=(connection.settings or {}).get('instagram_context')
        if previous and (previous.get('app_id')!=context['app_id'] or previous.get('login')!=context['login']):
            raise HTTPException(409,'Uygulama veya giriş kapsamı değişmiş. Ayrı geçiş kontrolü gerekli.')
    return connection

def update_capability(session,account_id,credentials,*,identity='verified'):
    row=session.get(InstagramCapability,account_id)
    if not row: row=InstagramCapability(account_id=account_id);session.add(row)
    scopes=set(scope_list(credentials.get('scope')))
    row.identity=identity;row.publishing='granted' if PUBLISH in scopes else 'unknown'
    row.insights='granted' if INSIGHTS in scopes else 'permission_required'
    row.checked_at=utcnow();row.expires_at=str(credentials.get('expires_at') or '') or None
    row.context={'login':'instagram_login','app_id':settings.instagram_client_id,'api_version':settings.graph_api_version}
    return row

def current_request_credentials(connection_id,expected_external_id):
    from factory.db import SessionLocal
    from factory.security import decrypt_credentials
    from factory.providers.base import ProviderError
    with SessionLocal.begin() as session:
        connection=session.get(Connection,connection_id)
        if not connection or connection.external_id!=expected_external_id:
            raise ProviderError('Instagram kimliği değişti; devam eden yayın durduruldu.','identity_changed')
        credentials=decrypt_credentials(connection_id,session)
        return {**credentials,'external_id':connection.external_id}
