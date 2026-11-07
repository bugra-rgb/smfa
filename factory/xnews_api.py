"""Admin-managed, authenticated X news controls; no direct X publishing.

Same shared workspace boundary as existing SMFA. Read routes require a signed-in
user; every mutable route requires admin + existing same-origin CSRF dependency.
"""
from datetime import datetime, timezone, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
import re
from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.encoders import jsonable_encoder
from pydantic import BaseModel, ConfigDict, Field, StrictStr, field_validator
from sqlalchemy import select, func
from factory.db import SessionLocal
from factory.models import Account, Connection, SystemLog
from factory.security import admin, current_user
from factory.xnews_models import XNewsAccount, XNewsSource, XNewsStory, XNewsTrendSnapshot, XNewsJob, XNewsConfig
from factory.services import xnews_settings as secrets

router = APIRouter(prefix='/api/control/xnews')
VERSION = 'smfa-xnews-20260930.1'

class Input(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)

class GlobalInput(Input):
    groq_api_key: StrictStr | None = Field(default=None, max_length=512)
    trend_api_key: StrictStr | None = Field(default=None, max_length=512)
    groq_model: StrictStr = Field(default='', max_length=160)
    trend_url: StrictStr = Field(default='', max_length=2048)
    trend_provider: StrictStr = Field(default='', max_length=160)
    trend_permission_confirmed: bool = False
    trend_freshness_seconds: int = Field(default=300, ge=30, le=900)
    global_daily_calls_cap: int = Field(default=100, ge=1, le=100000)
    account_daily_calls_cap: int = Field(default=20, ge=1, le=10000)
    global_daily_tokens_cap: int = Field(default=100000, ge=1000, le=10000000)
    account_daily_tokens_cap: int = Field(default=20000, ge=1000, le=1000000)

class SourceInput(Input):
    api_key: StrictStr | None = Field(default=None,max_length=512)
    name: StrictStr = Field(min_length=1, max_length=200)
    url: StrictStr = Field(min_length=1, max_length=2048)
    kind: StrictStr = 'rss'
    enabled: bool = False
    category: StrictStr = Field(default='general', max_length=80)
    language: StrictStr = Field(default='tr', max_length=16)
    turkey_terms: list[StrictStr] = Field(default_factory=list, max_length=100)
    permission_confirmed: bool = False
    @field_validator('kind')
    @classmethod
    def valid_kind(cls, value):
        if value not in ('rss','json'): raise ValueError('Kaynak türü RSS veya JSON olmalı.')
        return value

class QuietHours(Input):
    enabled: bool = False
    start: StrictStr = Field(default='23:00',pattern=r'^(?:[01]\d|2[0-3]):[0-5]\d$')
    end: StrictStr = Field(default='07:00',pattern=r'^(?:[01]\d|2[0-3]):[0-5]\d$')

class AccountSettings(Input):
    news_enabled: bool = False
    categories: list[StrictStr] = Field(default_factory=list,max_length=30)
    allowed_sources: list[StrictStr] = Field(default_factory=list,max_length=50)
    excluded_keywords: list[StrictStr] = Field(default_factory=list,max_length=100)
    excluded_hashtags: list[StrictStr] = Field(default_factory=list,max_length=100)
    interval_minutes: int = Field(default=180,ge=15,le=10080)
    timezone: StrictStr = Field(default='Europe/Istanbul',max_length=100)
    start_at: StrictStr | None = Field(default=None,max_length=80)
    daily_publish_cap: int = Field(default=8,ge=1,le=100)
    quiet_hours: QuietHours = Field(default_factory=QuietHours)
    ai_model: StrictStr = Field(default='',max_length=160)
    writing_style: StrictStr = Field(default='',max_length=500)
    insufficient_hashtag_behavior: StrictStr = 'skip'
    review_mode: bool = False
    revision: int | None = None
    @field_validator('timezone')
    @classmethod
    def timezone_valid(cls,value):
        try:ZoneInfo(value)
        except (ZoneInfoNotFoundError,ValueError):raise ValueError('Saat dilimi geçersiz.')
        return value
    @field_validator('start_at')
    @classmethod
    def start_valid(cls,value):
        if value:
            dt=datetime.fromisoformat(value.replace('Z','+00:00'))
            if dt.tzinfo is None:raise ValueError('Başlangıç saatinin saat dilimi gerekli.')
        return value or None
    @field_validator('insufficient_hashtag_behavior')
    @classmethod
    def behavior_valid(cls,value):
        if value not in ('skip','use_available'):raise ValueError('Etiket davranışı geçersiz.')
        return value
    @field_validator('categories','allowed_sources','excluded_keywords','excluded_hashtags')
    @classmethod
    def values_valid(cls,values):
        if any(not x.strip() or len(x)>160 or any(ord(c)<32 for c in x) for x in values):raise ValueError('Liste değeri geçersiz.')
        return list(dict.fromkeys(x.strip() for x in values))

class SettingsInput(Input):
    settings: AccountSettings
    revision: int = Field(ge=0)

class ToggleInput(Input):
    enabled: bool
    revision: int = Field(ge=0)

class DiscoverInput(Input):
    connection_id: StrictStr = Field(min_length=1, max_length=64)

class BindInput(DiscoverInput):
    organization_id: StrictStr = Field(min_length=1, max_length=100)
    channel_id: StrictStr = Field(min_length=1, max_length=100)

class TestInput(Input):
    provider: StrictStr


def now():
    return datetime.now(timezone.utc)


def problem(exc):
    from factory.services.xnews_sources import NewsError
    from factory.providers.base import ProviderError
    from factory.services.xnews_runtime import NewsRuntimeError
    if isinstance(exc, (NewsError, ProviderError, NewsRuntimeError)):
        # Both adapters own sanitized messages, never HTTP response bodies.
        return HTTPException(409, str(exc))
    return HTTPException(409, 'İşlem tamamlanamadı. Sağlayıcı ayarını, kaynağı ve bağlantıyı kontrol edin.')


def required(s, model, key, lock=False):
    q=select(model).where((model.account_id if model is XNewsAccount else model.id) == key)
    item=s.scalar(q.with_for_update() if lock else q)
    if item is None: raise HTTPException(404, 'Kayıt bulunamadı.')
    return item


def defaults(a):
    return {'news_enabled':False, 'automation_enabled':False, 'revision':0,
            'interval_minutes':180,'timezone':a.timezone or 'Europe/Istanbul','start_at':None,
            'daily_publish_cap':8,'quiet_hours':{'enabled':False,'start':'23:00','end':'07:00'},
            'categories':[],'allowed_sources':[],'excluded_keywords':[],'excluded_hashtags':[],
            'ai_model':'','writing_style':'','insufficient_hashtag_behavior':'skip','review_mode':False}


def describe(s,a):
    cfg=s.get(XNewsAccount,a.id)
    current=defaults(a)
    if cfg:
        current.update(cfg.config or {})
        current.update(news_enabled=cfg.news_enabled,automation_enabled=cfg.automation_enabled,
                       revision=cfg.schedule_version,start_at=cfg.start_at)
    links=s.scalars(select(Connection).where(Connection.account_id==a.id)).all()
    platforms=sorted({c.platform for c in links if c.platform in ('x','twitter','instagram','youtube','tiktok','facebook')})
    health={'status':'not_bound','message':'Buffer’dan X kanalı seçilmedi.'}
    if current.get('buffer_channel_id'):
        health={'status':'bound','message':'Kayıtlı eşleşme var. Canlı bağlantı kontrolü ayrıca yapılır.',
                'channel_id':current['buffer_channel_id'],'checked_at':current.get('binding_checked_at')}
    latest=s.scalar(select(XNewsJob).where(XNewsJob.account_id==a.id).order_by(XNewsJob.created_at.desc()).limit(1))
    return {'id':a.id,'name':a.username or a.name,'platforms':platforms,'settings':current,
            'connection_health':health,'next_run_at':cfg.next_run_at if cfg else None,
            'last_attempt_at':latest.updated_at if latest else None}


def audit(s,event,user,account_id=None,data=None):
    s.add(SystemLog(account_id=account_id,event='xnews.'+event,message='X haber modülü: '+event,
                    level='info',data={'user_id':user['id'],**(data or {})}))


def reserve_for(account_id=None):
    from factory.services.xnews_runtime import make_reserver
    return make_reserver(SessionLocal, account_id)


def validate_url(url):
    from factory.services.xnews_sources import safe_url, NewsError
    try:safe_url(url)
    except (NewsError,ValueError):raise HTTPException(422,'Kaynak HTTPS olmalı; yerel adres veya URL içinde gizli anahtar kullanılamaz.') from None


def check_revision(s,aid,revision):
    required(s,Account,aid,lock=True)
    row=s.scalar(select(XNewsAccount).where(XNewsAccount.account_id==aid).with_for_update())
    if (row.schedule_version if row else 0) != revision:
        raise HTTPException(409,'Ayarlar başka bir oturumda değişti. Hesabı yeniden yükleyin.')
    return row


@router.get('')
def overview(user=Depends(current_user)):
    with SessionLocal() as s:
        rows=s.scalars(select(Account).order_by(Account.name,Account.id)).all()
        cfg=secrets.public_settings(s)
        missing=[]
        if not cfg['groq_key_configured']:missing.append('Groq anahtarı yapılandırılmadı')
        if not cfg['groq_model']:missing.append('Groq modeli seçilmedi')
        if not cfg['trend_configured']:missing.append('Gündem veri kaynağı yapılandırılmadı')
        if not s.scalar(select(XNewsSource.id).where(XNewsSource.enabled.is_(True)).limit(1)):missing.append('Etkin haber kaynağı yok')
        return jsonable_encoder({'version':VERSION,'can_edit':user['role'].lower()=='admin','can_admin':user['role'].lower()=='admin',
            'accounts':[describe(s,a) for a in rows],'config':cfg,'status':{'missing_configuration':missing}})


@router.get('/config')
def get_config(user=Depends(admin)):
    with SessionLocal() as s:return secrets.public_settings(s)


@router.put('/config')
def save_config(data:GlobalInput,user=Depends(admin)):
    if data.trend_url:validate_url(data.trend_url)
    if data.account_daily_calls_cap>data.global_daily_calls_cap or data.account_daily_tokens_cap>data.global_daily_tokens_cap:
        raise HTTPException(422,'Hesap tüketim sınırı uygulama sınırından büyük olamaz.')
    with SessionLocal.begin() as s:
        return secrets.write_settings(s,data.model_dump(),user['id'])


@router.get('/models')
def models(user=Depends(admin)):
    from factory.services.xnews_sources import GroqClient
    try:
        with SessionLocal() as s:key=secrets.read_secret(s,'groq'); model=secrets.read_settings(s).get('groq_model','')
        result=GroqClient(key,model,reserve=reserve_for()).models()
        return {'models':result}
    except Exception as exc:raise problem(exc) from None


@router.post('/config/test')
def test_config(data:TestInput,user=Depends(admin)):
    try:
        with SessionLocal() as s:
            if data.provider=='groq':
                client=secrets.ai_provider(s,{},reserve=reserve_for())
            elif data.provider=='trends':
                cfg={**secrets.read_settings(s)['trend'],'api_key':secrets.read_secret(s,'trend')}
            else:raise HTTPException(422,'Groq veya trends seçilmeli.')
        if data.provider=='groq':return client.test()
        from factory.services.xnews_sources import fetch_trends
        reserve_for()()
        snapshot=fetch_trends(cfg)
        with SessionLocal.begin() as s:
            row=XNewsTrendSnapshot(provider=snapshot['provider'],region=snapshot['region'],
                fetched_at=datetime.fromisoformat(str(snapshot['fetched_at']).replace('Z','+00:00')) if isinstance(snapshot['fetched_at'],str) else snapshot['fetched_at'],
                provider_timestamp=datetime.fromisoformat(str(snapshot['provider_timestamp']).replace('Z','+00:00')) if isinstance(snapshot.get('provider_timestamp'),str) else snapshot.get('provider_timestamp'),
                data=jsonable_encoder(snapshot))
            s.add(row)
        return {'ok':True,'snapshot':jsonable_encoder(snapshot),'publication_requests':0}
    except HTTPException:raise
    except Exception as exc:raise problem(exc) from None


@router.get('/accounts/{account_id}')
def account_get(account_id:str,user=Depends(current_user)):
    with SessionLocal() as s:
        value=describe(s,required(s,Account,account_id))
        return jsonable_encoder({'account':value,**value})


@router.put('/accounts/{account_id}')
def account_put(account_id:str,data:SettingsInput,user=Depends(admin)):
    from factory.services.xnews_runtime import configure_account
    try:
        with SessionLocal.begin() as s:
            check_revision(s,account_id,data.revision)
            cfg=data.settings.model_dump();cfg.pop('revision',None)
            # Binding is trusted server-verified metadata; never editable JSON.
            allowed={'news_enabled','categories','allowed_sources','excluded_keywords','excluded_hashtags','interval_minutes','timezone','start_at',
                     'daily_publish_cap','quiet_hours','ai_model','writing_style','insufficient_hashtag_behavior','review_mode'}
            if set(cfg)-allowed:raise HTTPException(422,'Desteklenmeyen hesap ayarı.')
            sourceids=cfg.get('allowed_sources',[])
            if not isinstance(sourceids,list) or any(not s.get(XNewsSource,str(v)) for v in sourceids):raise HTTPException(422,'Haber kaynağı seçimi geçersiz.')
            configure_account(s,account_id,cfg,actor_id=user['id'])
            s.flush();value=describe(s,s.get(Account,account_id))
            return jsonable_encoder({'saved':True,'account':value,**value})
    except HTTPException:raise
    except (ValueError,TypeError,ZoneInfoNotFoundError):raise HTTPException(422,'Aralık, saat dilimi veya hesap ayarları geçersiz.') from None
    except Exception as exc:raise problem(exc) from None


@router.post('/accounts/{account_id}/automation')
def automation(account_id:str,data:ToggleInput,user=Depends(admin)):
    from factory.services.xnews_runtime import set_automation
    try:
        with SessionLocal.begin() as s:
            row=check_revision(s,account_id,data.revision)
            if data.enabled:
                if not row:raise HTTPException(409,'Önce hesap ayarlarını kaydedin.')
                cfg=row.config or {}; globalcfg=secrets.public_settings(s)
                if not globalcfg['groq_key_configured'] or not (cfg.get('ai_model') or globalcfg['groq_model']):raise HTTPException(409,'Groq anahtarı ve modeli yapılandırılmalı.')
                if not globalcfg['trend_configured']:raise HTTPException(409,'Gündem veri kaynağı yapılandırılmadı.')
                if not all(cfg.get(k) for k in ('buffer_connection_id','buffer_account_id','buffer_organization_id','buffer_channel_id','external_id')):raise HTTPException(409,'Önce Buffer X kanalını eşleştirin.')
                if not any((r:=s.get(XNewsSource,key)) and r.enabled and (r.config or {}).get('permission_confirmed') for key in cfg.get('allowed_sources',[])):raise HTTPException(409,'Etkin ve kullanım izni doğrulanmış bir haber kaynağı seçin.')
                secrets.credential_info(s,cfg['buffer_connection_id'])
            set_automation(s,account_id,data.enabled,actor_id=user['id'])
            s.flush();value=describe(s,s.get(Account,account_id))
            return jsonable_encoder({'saved':True,'account':value,**value})
    except HTTPException:raise
    except Exception as exc:raise problem(exc) from None


@router.get('/buffer/connections')
def connections(user=Depends(admin)):
    from factory.config import settings
    with SessionLocal() as s:
        rows=[]
        if settings.buffer_api_key:rows.append({'id':'server','label':'Mevcut sunucu Buffer anahtarı','organization_name':''})
        for c in s.scalars(select(Connection).order_by(Connection.created_at)).all():
            if (c.settings or {}).get('publish_via')=='buffer' and c.status.upper()=='CONNECTED':
                a=s.get(Account,c.account_id)
                rows.append({'id':c.id,'label':(a.username or a.name)+' · Buffer',
                             'organization_name':str((c.settings or {}).get('buffer_organization_id') or '')})
        return {'connections':rows}


def discover(connection_id):
    with SessionLocal() as s:
        cfg={'buffer_connection_id':connection_id}
        c=s.get(Connection,connection_id)
        if c:cfg['buffer_account_id']=(c.settings or {}).get('buffer_account_id')
        provider=secrets.buffer_provider(s,cfg,reserve=reserve_for())
    result=provider.discover()
    # Provider returns native channel fields; UI gets a stable shape.
    return [{'id':c['id'],'name':c.get('name',''),'organization_id':c.get('organizationId'),
             'organization_name':c.get('organization_name',''),'service':c.get('service'),
             'health':c.get('problem') or ('Bağlantı kontrolü gerekli' if not c.get('can_publish',False) else 'Bağlı'),
             'can_publish':bool(c.get('can_publish')),'_binding':{
                 'buffer_account_id':c.get('buffer_account_id'),'buffer_organization_id':c.get('organizationId'),
                 'buffer_channel_id':c['id'],'external_id':c.get('serviceId')}} for c in result]


@router.post('/buffer/discover')
def discover_buffer(data:DiscoverInput,user=Depends(admin)):
    try:return {'channels':[{k:v for k,v in c.items() if k!='_binding'} for c in discover(data.connection_id)]}
    except Exception as exc:raise problem(exc) from None


@router.post('/accounts/{account_id}/bind')
def bind(account_id:str,data:BindInput,user=Depends(admin)):
    from factory.services.xnews_runtime import configure_account
    try:
        channels=discover(data.connection_id)
        selected=next((c for c in channels if c['id']==data.channel_id and c['organization_id']==data.organization_id),None)
        if not selected or not selected['can_publish']:raise HTTPException(409,'Seçilen X kanalı bu yetkilendirmeyle otomatik yayına uygun değil.')
        with SessionLocal.begin() as s:
            target=required(s,Account,account_id,lock=True)
            managed=(target.settings or {}).get('managed_platform')
            if managed and managed!='x':raise HTTPException(409,'Bu hesap başka bir platforma ait. X için ayrı SMFA hesabı seçin.')
            active=s.scalar(select(XNewsJob.id).where(XNewsJob.account_id==account_id,
                XNewsJob.status.in_(['SUBMITTING','UNKNOWN_RESULT','BUFFER_ACCEPTED','BUFFER_PENDING'])).limit(1))
            if active:raise HTTPException(409,'Önceki haber yayını sonuçlanmadan kanal eşleşmesi değiştirilemez.')
            duplicate=s.scalar(select(XNewsAccount).where(XNewsAccount.account_id!=account_id,
                XNewsAccount.config['buffer_channel_id'].as_string()==data.channel_id).limit(1))
            if duplicate:raise HTTPException(409,'Bu X kanalı başka bir SMFA haber hesabıyla eşleşmiş.')
            for c in s.scalars(select(Connection).where(Connection.account_id==account_id,Connection.platform.in_(['x','twitter']))).all():
                if c.external_id and c.external_id!=selected['_binding']['external_id']:
                    raise HTTPException(409,'Bu SMFA hesabındaki mevcut X bağlantısı farklı bir kanala ait.')
            configure_account(s,account_id,{**selected['_binding'],'buffer_connection_id':data.connection_id,
                'binding_checked_at':now().isoformat()},actor_id=user['id'])
            s.flush();value=describe(s,target);audit(s,'bound',user,account_id,{'channel_id':data.channel_id})
            return jsonable_encoder({'saved':True,'account':value,**value})
    except HTTPException:raise
    except Exception as exc:raise problem(exc) from None


@router.get('/sources')
def sources_get(user=Depends(current_user)):
    with SessionLocal() as s:return {'sources':[source_value(x) for x in s.scalars(select(XNewsSource).order_by(XNewsSource.name)).all()]}


def source_value(x):
    return jsonable_encoder({'id':x.id,'name':x.name,'url':x.url,'kind':x.kind,'enabled':x.enabled,**(x.config or {}),
                             'last_fetched_at':x.last_fetched_at,'last_error':x.last_error})


def save_source(s,row,data,user):
    validate_url(data.url)
    if data.enabled and not data.permission_confirmed:raise HTTPException(422,'Kaynağın kullanım izni doğrulanmadan etkinleştirilemez.')
    values=data.model_dump()
    key=values.pop('api_key',None)
    for k in ('name','url','kind','enabled'):setattr(row,k,values.pop(k))
    if any(len(term)>100 for term in values['turkey_terms']):raise HTTPException(422,'Türkiye ilişkisi sözcükleri çok uzun.')
    row.config=values;s.add(row);s.flush()
    if key:secrets.write_secret(s,'source:'+row.id,key,user['id'])
    audit(s,'source.saved',user,data={'source_id':row.id})
    return {'saved':True,'source':source_value(row)}


@router.post('/sources')
def sources_post(data:SourceInput,user=Depends(admin)):
    with SessionLocal.begin() as s:return save_source(s,XNewsSource(),data,user)


@router.put('/sources/{source_id}')
def sources_put(source_id:str,data:SourceInput,user=Depends(admin)):
    with SessionLocal.begin() as s:return save_source(s,required(s,XNewsSource,source_id,lock=True),data,user)


@router.get('/stories')
def stories(account_id:str|None=None,user=Depends(current_user)):
    with SessionLocal() as s:
        q=select(XNewsStory).where(XNewsStory.source_published_at>=now()-timedelta(hours=48))
        if account_id:
            required(s,Account,account_id)
            cfg=s.get(XNewsAccount,account_id)
            ids=(cfg.config or {}).get('allowed_sources',[]) if cfg else []
            if ids:q=q.where(XNewsStory.source_id.in_(ids))
        rows=s.scalars(q.order_by(XNewsStory.source_published_at.desc()).limit(100)).all()
        return jsonable_encoder({'stories':[{'id':r.id,'title':r.title,'source_id':r.source_id,'canonical_url':r.canonical_url,
            'source_published_at':r.source_published_at,'category':r.category,'story_cluster_id':r.story_cluster_id,
            'status':'available' if r.turkey_relevance else 'not_relevant','source_name':(s.get(XNewsSource,r.source_id).name)} for r in rows]})


@router.get('/trends')
def trends(user=Depends(current_user)):
    with SessionLocal() as s:
        row=s.scalar(select(XNewsTrendSnapshot).order_by(XNewsTrendSnapshot.fetched_at.desc()).limit(1))
        cfg={**secrets.read_settings(s)['trend'],'api_key':secrets.read_secret(s,'trend')}
        if not row:return {'snapshot':None,'stale':True,'message':'Gündem veri kaynağı yapılandırılmadı veya henüz veri alınmadı.'}
        base=row.provider_timestamp or row.fetched_at
        stale=(now()-base).total_seconds()>cfg['max_age_seconds'] or row.provider!=cfg['provider']
        return jsonable_encoder({'snapshot':row.data,'stale':stale})


def job_value(j):
    return jsonable_encoder({'id':j.id,'account_id':j.account_id,'status':j.status,'scheduled_slot':j.scheduled_slot,
        'next_attempt_at':j.next_attempt_at,'last_checked_at':j.last_checked_at,'buffer_post_id':j.buffer_post_id,
        'submitted_at':j.submit_started_at,'accepted_at':j.accepted_at,'published_at':j.published_at,'published_url':j.published_url,
        'error_code':j.error_code,'error_message':j.error_message,'text':(j.payload or {}).get('text',''),
        'publication_delay_seconds':(j.published_at-j.scheduled_slot).total_seconds() if j.published_at else None,
        'cancel_requested':j.cancel_requested,'cancellation_checked':j.cancellation_checked})


@router.get('/history')
def history(account_id:str|None=None,user=Depends(current_user)):
    with SessionLocal() as s:
        q=select(XNewsJob)
        if account_id:required(s,Account,account_id);q=q.where(XNewsJob.account_id==account_id)
        return {'jobs':[job_value(j) for j in s.scalars(q.order_by(XNewsJob.created_at.desc()).limit(100)).all()]}


@router.post('/accounts/{account_id}/preview')
@router.post('/accounts/{account_id}/dry-run')
def preview(account_id:str,user=Depends(admin)):
    from factory.services.xnews_runtime import preview as prepare, RuntimeServices
    try:
        with SessionLocal() as s:
            required(s,Account,account_id)
            result=prepare(s,account_id,RuntimeServices(SessionLocal))
            s.commit()
        return jsonable_encoder({'publication_requests':0,**result})
    except HTTPException:raise
    except Exception as exc:raise problem(exc) from None


@router.post('/jobs/{job_id}/approve')
def approve(job_id:str,user=Depends(admin)):
    from factory.services.xnews_runtime import approve_job
    try:
        with SessionLocal.begin() as s:
            approve_job(s,job_id,actor_id=user['id']);s.flush()
            return {'job':job_value(required(s,XNewsJob,job_id))}
    except Exception as exc:raise problem(exc) from None


@router.post('/jobs/{job_id}/cancel')
def cancel(job_id:str,user=Depends(admin)):
    from factory.services.xnews_runtime import request_cancel
    try:
        with SessionLocal.begin() as s:
            request_cancel(s,job_id,actor_id=user['id']);s.flush()
            return {'job':job_value(required(s,XNewsJob,job_id))}
    except Exception as exc:raise problem(exc) from None

class NewAccountInput(Input):
    name: StrictStr = Field(min_length=1,max_length=160)

@router.post('/accounts')
def new_account(data:NewAccountInput,user=Depends(admin)):
    """Create an explicitly selected news-only target, with video automation off."""
    with SessionLocal.begin() as s:
        a=Account(name=data.name.strip(),username=data.name.strip().lstrip('@'),daily_target=0,
                  active=True,auto_publish=False,source_mode='DRIVE_ONLY',approval_mode='MANUAL_APPROVAL',
                  settings={'managed_platform':'x','publication_mode':'manual'})
        s.add(a);s.flush();audit(s,'account.created',user,a.id)
        return jsonable_encoder({'account':describe(s,a),'saved':True})

@router.post('/sources/{source_id}/test')
def test_source(source_id:str,user=Depends(admin)):
    from factory.services.xnews_sources import fetch_news,parse_time
    try:
        with SessionLocal() as s:
            row=required(s,XNewsSource,source_id)
            cfg={**row.config,'id':row.id,'name':row.name,'url':row.url,'kind':row.kind,
                 'api_key':secrets.read_secret(s,'source:'+row.id)}
        reserve_for()()
        rows=fetch_news(cfg)
        with SessionLocal.begin() as s:
            source=required(s,XNewsSource,source_id,lock=True)
            for item in rows[:200]:
                if s.scalar(select(XNewsStory.id).where(XNewsStory.canonical_url==item['canonical_url'])):continue
                s.add(XNewsStory(source_id=source.id,canonical_url=item['canonical_url'],title=item['title'],available_text=item.get('available_text',''),
                    source_published_at=parse_time(item.get('source_published_at')),source_updated_at=parse_time(item.get('source_updated_at')),
                    event_at=parse_time(item.get('event_at')),retrieved_at=now(),category=item.get('category','general'),language=item.get('language','tr'),
                    turkey_relevance=bool(item.get('turkey_relevance')),story_cluster_id=item['story_cluster_id'],data=jsonable_encoder(item)))
            source.last_fetched_at=now();source.last_error=None
            audit(s,'source.test',user,data={'source_id':source_id,'eligible_count':len(rows)})
        return {'ok':True,'eligible_count':len(rows),'publication_requests':0,'ai_calls':0}
    except HTTPException:raise
    except Exception as exc:raise problem(exc) from None
