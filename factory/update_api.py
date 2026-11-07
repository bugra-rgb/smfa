"""Authenticated additive Instagram features using existing sessions and CSRF."""
from __future__ import annotations
from datetime import datetime,timedelta,timezone
import httpx
from typing import Literal
from fastapi import APIRouter,Depends,HTTPException,UploadFile,Query
from fastapi.responses import FileResponse,Response
from pydantic import BaseModel,ConfigDict,Field,StrictBool
from sqlalchemy import select
from factory.config import settings
from factory.db import SessionLocal
from factory.models import Account,Connection,DriveConnection,OAuthToken,PublishJob,utcnow
from factory.update_models import ReelsCoverAsset,PublicationCover,InstagramCapability
from factory.security import current_user,operator,admin,decrypt_json,digest
from factory.services import covers,insight_ranges

router=APIRouter(prefix='/api')

def enabled(feature):
    if not getattr(settings,'smfa_'+feature+'_enabled'):
        raise HTTPException(503,'Bu özellik henüz etkinleştirilmedi; mevcut yayın akışı çalışmaya devam eder.')

class CoverPatch(BaseModel):
    model_config=ConfigDict(extra='forbid')
    revision:int=Field(ge=0)
    enabled:StrictBool|None=None
    asset_id:str|None=Field(default=None,max_length=36)

class PublicationPatch(BaseModel):
    model_config=ConfigDict(extra='forbid')
    revision:int=Field(ge=0)
    mode:Literal['default','none','asset']
    asset_id:str|None=Field(default=None,max_length=36)

class RefreshInput(BaseModel):
    model_config=ConfigDict(extra='forbid')
    account_ids:list[str]=Field(max_length=100)
    start:str
    end:str

@router.get('/instagram/features')
def features(user=Depends(current_user)):
    return {'version':'smfa-report-days-20260922-v1','covers':settings.smfa_covers_enabled,'analytics':settings.smfa_analytics_enabled,'oauth':settings.smfa_oauth_hardening_enabled,'login':'instagram_login','api_version':settings.graph_api_version,'activity_boundaries_verified':settings.smfa_insights_boundaries_verified,'activity_period_basis':'instagram_report_end_date'}

@router.get('/accounts/{account_id}/reels-cover')
def get_cover(account_id:str,user=Depends(current_user)):
    enabled('covers')
    with SessionLocal() as session:return covers.get_setting(session,account_id)

@router.post('/accounts/{account_id}/reels-cover-assets')
async def upload_cover(account_id:str,file:UploadFile,x:float=Query(.5,ge=0,le=1),y:float=Query(.5,ge=0,le=1),zoom:float=Query(1,ge=1,le=4),user=Depends(operator)):
    enabled('covers')
    raw=await file.read(covers.MAX_BYTES+1)
    from fastapi.concurrency import run_in_threadpool
    def save():
        with SessionLocal.begin() as session:
            asset=covers.create_asset(session,account_id,raw,x,y,zoom)
            return covers.asset_data(asset)
    return await run_in_threadpool(save)

@router.patch('/accounts/{account_id}/reels-cover')
def patch_cover(account_id:str,data:CoverPatch,user=Depends(operator)):
    enabled('covers')
    if 'enabled' in data.model_fields_set and data.enabled is None:
        raise HTTPException(422,'Sabit kapak seçimi boş olamaz.')
    with SessionLocal.begin() as session:
        return covers.patch_setting(session,account_id,data.model_dump(exclude_unset=True))

@router.get('/reels-covers/{asset_id}.jpg')
def public_cover(asset_id:str):
    # Intentionally public immutable image: Meta must fetch it without cookies.
    # Not tied to feature flag: files referenced by in-flight publications survive rollback.
    with SessionLocal() as session:
        asset=session.get(ReelsCoverAsset,asset_id)
        if not asset:raise HTTPException(404,'Kapak bulunamadı.')
        path=covers.asset_path(asset)
        if not path.is_file():raise HTTPException(404,'Kapak dosyası bulunamadı.')
        return FileResponse(path,media_type='image/jpeg',headers={'Cache-Control':'public, max-age=31536000, immutable','X-Content-Type-Options':'nosniff'})

@router.get('/publishing/{publication_id}/cover')
def publication_cover(publication_id:str,user=Depends(current_user)):
    enabled('covers')
    with SessionLocal() as session:
        pub=session.get(PublishJob,publication_id)
        if not pub or pub.platform!='instagram':raise HTTPException(404,'Instagram yayını bulunamadı.')
        row=session.get(PublicationCover,publication_id)
        return {'publication_id':publication_id,'mode':row.mode if row else 'default','asset_id':row.asset_id if row else None,'revision':row.revision if row else 0,'frozen_at':row.frozen_at if row else None,'cover_sent':bool(row and row.sent_at),'published':pub.status=='PUBLISHED','visually_verified':bool(row and row.visually_verified_at)}

@router.patch('/publishing/{publication_id}/cover')
def patch_publication_cover(publication_id:str,data:PublicationPatch,user=Depends(operator)):
    enabled('covers')
    from factory.services.pipeline import reveal_checkpoint
    with SessionLocal.begin() as session:
        pub=session.scalar(select(PublishJob).where(PublishJob.id==publication_id).with_for_update())
        if not pub or pub.platform!='instagram':raise HTTPException(404,'Instagram yayını bulunamadı.')
        cp=reveal_checkpoint(pub.checkpoint)
        lease=(pub.checkpoint or {}).get('worker_lease',{})
        if pub.status not in ('SCHEDULED','RETRYING') or any(cp.get(k) for k in ('create_started','container_id','publish_started','remote_id')) or (lease.get('until') and datetime.fromisoformat(lease['until'])>utcnow()):
            raise HTTPException(409,'Yayın hazırlığı başlamış; kapak değiştirilemez.')
        row=session.get(PublicationCover,publication_id)
        if not row:row=PublicationCover(publication_id=publication_id,revision=0);session.add(row)
        if row.frozen_at or row.revision!=data.revision:raise HTTPException(409,'Kapak sabitlendi veya başka bir oturumda değişti.')
        if data.mode=='asset':
            asset=session.get(ReelsCoverAsset,data.asset_id)
            if not asset or asset.account_id!=pub.account_id:raise HTTPException(422,'Kapak bu hesaba ait değil.')
        row.mode=data.mode;row.asset_id=data.asset_id if data.mode=='asset' else None;row.revision+=1
        return {'revision':row.revision,'mode':row.mode,'asset_id':row.asset_id}

@router.get('/instagram/analytics')
def analytics(account_ids:str='',preset:str='7d',start:str|None=None,end:str|None=None,view:str='activity',compare:bool=False,format:Literal['json','csv']='json',user=Depends(current_user)):
    enabled('analytics')
    ids=list(dict.fromkeys(filter(None,account_ids.split(','))))
    if len(ids)>100:raise HTTPException(422,'En fazla 100 hesap seçilebilir.')
    with SessionLocal() as session: result=insight_ranges.build_report(session,ids,preset,start,end,view,compare)
    if format=='csv':return Response(insight_ranges.csv_report(result),media_type='text/csv; charset=utf-8',headers={'Content-Disposition':'attachment; filename="SMFA_Instagram_Analitik.csv"','Cache-Control':'no-store'})
    return result

@router.get('/instagram/analytics/capabilities')
def metric_capabilities(user=Depends(current_user)):
    return {'login':'instagram_login','api_version':settings.graph_api_version,'permission':'instagram_business_manage_insights','metrics':[{'metric':m,'label':insight_ranges.LABELS[m],'activity':'requires_verified_period_response' if m in insight_ranges.ADDITIVE else 'exact_scope_only_or_unsupported','cohort':'latest_lifetime' if m in ('views','reach','likes','comments','shares','saves') else 'unsupported','aggregation':'sum_nonoverlapping_periods' if m in insight_ranges.ADDITIVE else 'do_not_sum','resolution':'API returned period, stored in UTC','boundary_verified':settings.smfa_insights_boundaries_verified} for m in insight_ranges.METRICS]}

@router.post('/instagram/analytics/refresh')
def refresh_analytics(data:RefreshInput,user=Depends(operator)):
    enabled('analytics')
    from factory.services.instagram_insights import enqueue_collection
    with SessionLocal.begin() as session:
        result=insight_ranges.build_report(session,data.account_ids,'custom',data.start,data.end)
        return enqueue_collection(session,[a['account_id'] for a in result['accounts']],data.start,data.end)

@router.get('/accounts/{account_id}/instagram-status')
def instagram_status(account_id:str,user=Depends(current_user)):
    with SessionLocal() as session:
        account=covers.instagram_account(session,account_id)
        connection=session.scalar(select(Connection).where(Connection.account_id==account_id,Connection.platform=='instagram'))
        drive=session.scalar(select(DriveConnection).where(DriveConnection.account_id==account_id))
        cap=session.get(InstagramCapability,account_id) if settings.smfa_oauth_hardening_enabled else None
        return {'identity':cap.identity if cap else 'unchecked','connection':connection.status if connection else 'DISCONNECTED','publishing':cap.publishing if cap else 'unknown','insights':cap.insights if cap else 'unknown','drive':drive.status if drive else 'DISCONNECTED','active':account.active,'auto_publish':account.auto_publish,'checked_at':cap.checked_at if cap else None,'expires_at':cap.expires_at if cap else None,'login':'instagram_login','note':'Bağlantı kontrolü gerçek yayın testi değildir.'}

@router.post('/accounts/{account_id}/instagram-check')
def instagram_check(account_id:str,user=Depends(admin)):
    enabled('oauth_hardening')
    from factory.providers.oauth import resolve_identity,instagram_permissions
    from factory.services.instagram_connection import update_capability
    from factory.providers.base import ProviderError
    with SessionLocal() as session:
        covers.instagram_account(session,account_id)
        conn=session.scalar(select(Connection).where(Connection.account_id==account_id,Connection.platform=='instagram'))
        token=session.scalar(select(OAuthToken).where(OAuthToken.connection_id==conn.id)) if conn else None
        if not token:raise HTTPException(409,'Önce Instagram hesabını bağlayın.')
        credentials=decrypt_json(token.encrypted_payload);expected=conn.external_id;token_version=digest(token.encrypted_payload)
    try:
        identity=resolve_identity('instagram',credentials)
        if str(identity.get('external_id'))!=expected:raise HTTPException(409,'Instagram kimliği eşleşmiyor. Bağlantı değiştirilmedi.')
        credentials['scope']=instagram_permissions(credentials,force_refresh=True)
    except (ProviderError,httpx.TransportError):
        raise HTTPException(502,'Instagram kontrolü tamamlanamadı. Mevcut bağlantı korundu.') from None
    with SessionLocal.begin() as session:
        current=session.scalar(select(Connection).where(Connection.id==conn.id).with_for_update())
        current_token=session.scalar(select(OAuthToken).where(OAuthToken.connection_id==conn.id).with_for_update())
        if not current or current.external_id!=expected or not current_token or digest(current_token.encrypted_payload)!=token_version:raise HTTPException(409,'Bağlantı kontrol sırasında değişti.')
        update_capability(session,account_id,credentials)
    return instagram_status(account_id,user)
