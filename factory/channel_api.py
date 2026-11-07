"""Authenticated independent platform channels and manual TikTok preparation."""
import hashlib
import re
from datetime import datetime,timedelta,timezone
from typing import Literal
from uuid import UUID
from fastapi import APIRouter,Depends,HTTPException
from pydantic import BaseModel,Field
from sqlalchemy import select
from factory.config import settings
from factory.db import SessionLocal
from factory.models import Account,Connection,Video,PublishedPost,PublishJob,SystemState,DriveFile
from factory.security import admin,current_user
from factory.schemas import validate_account_settings
from factory.services.channels import PLATFORMS,configured,problems,tiktok_buffer,tiktok_mode
from factory.services.hashtags import copy_for
from factory.services.planner import next_slot
from factory.services.publication_status import publication_status

router=APIRouter(prefix='/api/control/channels')

class ChannelInput(BaseModel):
    platform:Literal['x','youtube','tiktok','facebook']
    name:str=Field(min_length=1,max_length=100)
    folder:str=Field(default='',max_length=500)
    hashtags:list[str]=Field(min_length=5,max_length=100)
    privacy:Literal['public','private','unlisted']='private'
    made_for_kids:bool|None=None
    synthetic:bool|None=None
    request_id:UUID

class SettingsInput(BaseModel):
    folder:str=Field(default='',max_length=500)
    hashtags:list[str]=Field(min_length=5,max_length=100)
    privacy:Literal['public','private','unlisted']='private'
    made_for_kids:bool|None=None
    synthetic:bool|None=None

class EnableInput(BaseModel):
    enabled:bool


def managed(s,account_id,lock=False):
    q=select(Account).where(Account.id==account_id)
    a=s.scalar(q.with_for_update() if lock else q)
    if not a or (a.settings or {}).get('smfa_archived_v1') or (a.settings or {}).get('managed_platform') not in PLATFORMS:raise HTTPException(404,'Bağımsız kanal bulunamadı.')
    return a


def normalize(data,base):
    cfg={**base,'drive_input_folder_id':data.folder,'manual_hashtag_pool':data.hashtags,
         'publishing':{**base.get('publishing',{}),
             'youtube':{'privacy_status':data.privacy,'made_for_kids':data.made_for_kids,'contains_synthetic_media':data.synthetic},
             'tiktok':{'contains_synthetic_media':data.synthetic}}}
    try:return validate_account_settings(cfg)
    except ValueError as exc:raise HTTPException(422,str(exc)) from None


def unique_folder(s,folder,exclude=None):
    if not folder:return
    from factory.services.drive_shared import enabled as shared_input
    if exclude:
        owner=s.get(Account,exclude)
        if owner and shared_input(owner):return
    rows=s.scalars(select(Account).where(Account.id!=exclude if exclude else Account.id.is_not(None))).all()
    if any((a.settings or {}).get('drive_input_folder_id')==folder for a in rows if not shared_input(a)):
        raise HTTPException(409,'Bu klasör başka bir kanalda kullanılıyor. Bağımsız kanal için ayrı klasör seçin.')


def gate(s):
    row=s.scalar(select(SystemState).where(SystemState.id=='cycle-config').with_for_update())
    if not row:raise HTTPException(409,'SMF sunucu kurulumu tamamlanmalı.')


@router.post('',status_code=201)
def create_channel(data:ChannelInput,user=Depends(admin)):
    with SessionLocal.begin() as s:
        gate(s)
        identity=str(data.request_id)
        fingerprint=hashlib.sha256(data.model_dump_json().encode()).hexdigest()
        if not data.name.strip():raise HTTPException(422,'Kanal adı boş olamaz.')
        prior=s.get(Account,identity)
        if prior:
            if (prior.settings or {}).get('channel_request')!=identity or prior.settings.get('managed_platform')!=data.platform or prior.settings.get('channel_request_hash')!=fingerprint:
                raise HTTPException(409,'İstek kimliği farklı bir kayda ait.')
            return {'id':prior.id,'existing':True}
        rows=s.scalars(select(Account)).all()
        index=max([int((a.settings or {}).get('cycle_index',-1)) for a in rows]+[-1])+1
        anchor=(datetime.now(timezone.utc)+timedelta(minutes=1)).isoformat()
        cfg=normalize(data,{'managed_platform':data.platform,'channel_request':identity,'channel_request_hash':fingerprint,'auto_discover':True,
            'cycle_v2':True,'cycle_index':index,'publication_mode':'planner','planner_interval_hours':5,
            'planner_anchor':anchor,'planner_offset':index*2,'caption_policy':'manual_five_hashtags','ai_disabled':True,
            'inventory_min':1,'inventory_target':2,'inventory_max':3,'selection_policy':'random_unused',
            'duration':600,'width':720,'height':1280,'subtitle':{'enabled':False},'voice':{'replace_source':False},
            'optimize_schedule':False,'optimize_format':False,'optimize_duration':False})
        unique_folder(s,cfg.get('drive_input_folder_id'))
        a=Account(id=identity,name=data.name.strip(),username='',niche=data.name.strip(),active=False,auto_publish=True,
                  approval_mode='AUTO_PUBLISH',source_mode='DRIVE_ONLY',daily_target=5,settings=cfg)
        s.add(a);s.flush()
        return {'id':a.id,'existing':False}


@router.put('/{account_id}/settings')
def update_settings(account_id:str,data:SettingsInput,user=Depends(admin)):
    with SessionLocal.begin() as s:
        gate(s);a=managed(s,account_id,True)
        if a.active:raise HTTPException(409,'Ayar değiştirmek için önce kanalı duraklatın.')
        cfg=normalize(data,a.settings);unique_folder(s,cfg.get('drive_input_folder_id'),a.id)
        if cfg.get('drive_input_folder_id') != a.settings.get('drive_input_folder_id') and s.scalar(select(DriveFile.id).where(DriveFile.account_id==a.id).limit(1)):
            raise HTTPException(409,'Bu kanala içerik alınmış. Farklı kaynak için yeni kanal oluşturun.')
        # Reserved content belongs to its original source and caption configuration.
        if s.scalar(select(PublishJob.id).where(PublishJob.account_id==a.id,PublishJob.status.not_in(('PUBLISHED','CANCELLED'))).limit(1)):
            raise HTTPException(409,'Bekleyen yayını sonuçlandırın veya iptal edin; kaynak henüz değiştirilemez.')
        a.settings=cfg;return {'saved':True}


@router.put('/{account_id}/enabled')
def set_enabled(account_id:str,data:EnableInput,user=Depends(admin)):
    with SessionLocal.begin() as s:
        gate(s);a=managed(s,account_id,True)
        links=s.scalars(select(Connection).where(Connection.account_id==a.id).with_for_update()).all()
        p=a.settings['managed_platform'];c=next((c for c in links if c.platform==p),None)
        if data.enabled:
            reasons=problems(a,links)
            if reasons:raise HTTPException(409,' · '.join(reasons))
            duplicate=s.scalar(select(Connection.id).where(Connection.platform==p,Connection.id!=c.id,
                Connection.external_id==c.external_id,Connection.settings['auto_publish_enabled'].as_boolean().is_(True)))
            if duplicate:raise HTTPException(409,'Bu platform hesabı başka bir kanalda etkin.')
        automatic=p!='tiktok' or tiktok_buffer(c)
        if c:c.settings={**(c.settings or {}),'auto_publish_enabled':data.enabled and automatic}
        a.active=data.enabled
        return {'enabled':a.active,'mode':'automatic' if automatic else 'manual_preparation'}


@router.get('')
def overview(user=Depends(current_user)):
    with SessionLocal() as s:
        accounts=[a for a in s.scalars(select(Account).order_by(Account.created_at)) if (a.settings or {}).get('managed_platform') in PLATFORMS and not (a.settings or {}).get('smfa_archived_v1')]
        links=s.scalars(select(Connection)).all();state=s.get(SystemState,'global');result=[]
        for a in accounts:
            cfg=a.settings;p=cfg['managed_platform'];owned=[c for c in links if c.account_id==a.id]
            c=next((c for c in owned if c.platform==p),None);drive=next((c for c in owned if c.platform in ('drive','google') and c.status.upper()=='CONNECTED'),None)
            reasons=problems(a,owned)
            publication=publication_status(s,a,blockers=reasons,paused=bool(state and state.paused),
                planned_at=next_slot(a,datetime.now(timezone.utc)),manual=p=='tiktok' and not tiktok_buffer(c))
            result.append({'id':a.id,'name':a.name,'platform':p,'username':(c.settings or {}).get('username') if c else '',
                'connected':bool(c and c.status.upper()=='CONNECTED'),'drive_connected':bool(drive),'enabled':a.active,
                'folder':cfg.get('drive_input_folder_id',''),'hashtags':cfg.get('manual_hashtag_pool',[]),
                **publication,'blockers':reasons,'youtube':cfg.get('publishing',{}).get('youtube',{}),
                'tiktok':cfg.get('publishing',{}).get('tiktok',{}),
                'tiktok_publish_mode':tiktok_mode(owned) if p=='tiktok' else None,
                'publish_via':(c.settings or {}).get('publish_via','direct') if c else 'direct',
                'facebook_page_selection_required':bool(p=='facebook' and c and c.status=='PAGE_SELECTION_REQUIRED')})
        ids=[a.id for a in accounts]
        queue=[{'id':p.id,'account_id':p.account_id,'platform':p.platform,'status':p.status,'error':p.error_message,'time':p.scheduled_at}
            for p in s.scalars(select(PublishJob).where(PublishJob.account_id.in_(ids),PublishJob.status!='PUBLISHED').order_by(PublishJob.scheduled_at.desc()).limit(50))]
        posts=[]
        for p in s.scalars(select(PublishedPost).where(PublishedPost.account_id.in_(ids)).order_by(PublishedPost.published_at.desc()).limit(50)):
            video=s.get(Video,p.video_id)
            visibility=(video.metadata_json or {}).get('confirmed_visibility',{}).get(p.platform) if video else None
            evidence=(video.metadata_json or {}).get('youtube_publication_evidence',{}) if video else {}
            posts.append({'id':p.id,'account_id':p.account_id,'platform':p.platform,'url':p.url,'time':p.published_at,'visibility':visibility,
                'title':video.title if video else '', 'provider':evidence.get('provider'),
                'provider_published_at':evidence.get('published_at')})
        tiktok_ids=[a.id for a in accounts if a.settings['managed_platform']=='tiktok'
                    and not any(tiktok_buffer(c) for c in links if c.account_id==a.id)]
        prepared=[]
        for v in s.scalars(select(Video).where(Video.account_id.in_(tiktok_ids),Video.status.in_(('READY','AWAITING_APPROVAL','MANUAL_SHARED')),Video.qa['passed'].as_boolean().is_(True)).order_by(Video.created_at.desc()).limit(50)):
            a=next(a for a in accounts if a.id==v.account_id)
            prepared.append({'id':v.id,'account_id':v.account_id,'title':v.title,'caption':copy_for(a,v.id)['caption'],
                'status':'USER_REPORTED' if v.status=='MANUAL_SHARED' else 'READY','url':(v.metadata_json or {}).get('manual_tiktok_url')})
        return {'accounts':result,'queue':queue,'posts':posts,'prepared':prepared,'paused':bool(state and state.paused),
                'platforms':[{'id':p,'configured':bool(settings.buffer_api_key) if p=='tiktok' else configured(p),
                    'callback':None if p=='tiktok' else settings.public_base_url.rstrip('/')+'/api/oauth/'+p+'/callback'} for p in PLATFORMS],
                'youtube_audit_approved':settings.youtube_audit_approved,'oauth_origin':settings.public_base_url}


class ManualReport(BaseModel):
    url:str=Field(min_length=1,max_length=500)


class FacebookPageInput(BaseModel):
    page_id:str=Field(pattern=r'^\d{1,40}$')


def facebook_grant(s,account_id):
    from factory.models import OAuthToken
    from factory.security import decrypt_json
    a=managed(s,account_id,True)
    if a.settings['managed_platform']!='facebook':raise HTTPException(404,'Facebook kanalı bulunamadı.')
    c=s.scalar(select(Connection).where(Connection.account_id==a.id,Connection.platform=='facebook').with_for_update())
    if not c:raise HTTPException(409,'Önce Facebook ile giriş yapın.')
    token=s.scalar(select(OAuthToken).where(OAuthToken.connection_id==c.id))
    if not token:raise HTTPException(409,'Facebook bağlantısı yeniden yapılmalı.')
    return a,c,decrypt_json(token.encrypted_payload)


@router.get('/{account_id}/facebook/pages')
def facebook_pages(account_id:str,user=Depends(admin)):
    import httpx
    from factory.providers.facebook import pages
    from factory.providers.base import ProviderError
    with SessionLocal.begin() as s:
        a,c,credentials=facebook_grant(s,account_id)
        if c.status=='CONNECTED' and (c.settings or {}).get('facebook_page_selected'):
            return {'pages':[{'id':c.external_id,'name':c.settings.get('username',a.name)}], 'selected_page_id':c.external_id}
        try:
            with httpx.Client(timeout=45,follow_redirects=False) as client:
                choices=pages(credentials,settings.graph_api_version,client)
        except (ProviderError,httpx.HTTPError,ValueError,KeyError):
            raise HTTPException(409,'Facebook Sayfaları alınamadı. Sayfa yayın izinlerini kontrol edip yeniden bağlayın.') from None
        return {'pages':[{'id':p['id'],'name':p['name']} for p in choices],'selected_page_id':None}


@router.post('/{account_id}/facebook/page')
def facebook_page(account_id:str,data:FacebookPageInput,user=Depends(admin)):
    import httpx
    from factory.providers.facebook import select_page
    from factory.providers.base import ProviderError
    from factory.main import save_connection,audit
    with SessionLocal.begin() as s:
        gate(s);a,c,credentials=facebook_grant(s,account_id)
        if c.external_id and c.external_id!=data.page_id:
            raise HTTPException(409,'Bu SMF kanalı başka bir Facebook Sayfasına bağlı. Yeni Sayfa için yeni kanal oluşturun.')
        if c.status=='CONNECTED' and c.external_id==data.page_id:
            return {'connected':True,'page_id':c.external_id,'name':c.settings.get('username',a.name)}
        if c.status!='PAGE_SELECTION_REQUIRED':raise HTTPException(409,'Facebook ile yeniden giriş yapın.')
        try:
            with httpx.Client(timeout=45,follow_redirects=False) as client:
                selected,identity=select_page(credentials,data.page_id,settings.graph_api_version,client)
        except (ProviderError,httpx.HTTPError,ValueError,KeyError):
            raise HTTPException(409,'Facebook Sayfa kimliği veya yayın izni doğrulanamadı; bağlantı kaydedilmedi.') from None
        save_connection(s,a.id,'facebook',selected,identity)
        a.username=identity['username']
        if a.settings.get('auto_discover'):a.name=identity['name'];a.settings={**a.settings,'auto_discover':False}
        audit(s,'oauth.facebook_page_selected',user,a.id,{'page_id':data.page_id})
        return {'connected':True,'page_id':data.page_id,'name':identity['name']}

@router.post('/{account_id}/tiktok/{video_id}/reported')
def report_manual(account_id:str,video_id:str,data:ManualReport,user=Depends(admin)):
    from urllib.parse import urlsplit
    url=urlsplit(data.url)
    if url.scheme!='https' or url.hostname not in ('www.tiktok.com','tiktok.com') or url.username or url.password or not re.fullmatch(r'/@[^/]+/video/\d+/?',url.path):
        raise HTTPException(422,'TikTok gönderisinin tam https://www.tiktok.com/@hesap/video/… bağlantısını girin.')
    with SessionLocal.begin() as s:
        a=managed(s,account_id,True);v=s.get(Video,video_id)
        if a.settings['managed_platform']!='tiktok' or not v or v.account_id!=a.id:raise HTTPException(404,'TikTok videosu bulunamadı.')
        c=s.scalar(select(Connection).where(Connection.account_id==a.id,Connection.platform=='tiktok'))
        if tiktok_buffer(c):raise HTTPException(409,'Buffer otomatik yayın sonucu sistem tarafından doğrulanır; elle tamamlandı işaretlenemez.')
        if v.status=='MANUAL_SHARED':return {'status':'USER_REPORTED','verified':False}
        if v.status not in ('READY','AWAITING_APPROVAL') or not (v.qa or {}).get('passed'):raise HTTPException(409,'Video hazır değil.')
        v.status='MANUAL_SHARED';v.metadata_json={**(v.metadata_json or {}),'manual_tiktok_url':data.url,'manual_reported_at':datetime.now(timezone.utc).isoformat()}
        return {'status':'USER_REPORTED','verified':False}
