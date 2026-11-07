"""Low priority bounded account-period collection; never changes publication grants."""
from __future__ import annotations
from datetime import date,datetime,timedelta,timezone
import httpx
from sqlalchemy import select
from factory.config import settings
from factory.models import Account,Connection,OAuthToken,PublishJob,Job,utcnow
from factory.security import decrypt_json
from factory.services.insight_ranges import date_window,record_measure,valid_number
from factory.services.jobs import enqueue_job,RetryLater
from factory.update_models import InstagramCapability
from factory.services.instagram_report_days import BASIS,SCOPE,resolve_period

API_METRICS={'views':'views','likes':'likes','comments':'comments','shares':'shares','saves':'saves'}

def enqueue_collection(session,account_ids,start,end):
    window=date_window('custom',start,end)
    oldest=utcnow().date()-timedelta(days=89)
    left=max(date.fromisoformat(window['start']),oldest)
    right=date.fromisoformat(window['end'])
    accepted=0
    # One batch per six-hour cache period. The cursor continues after each bounded request.
    for account_id in account_ids:
        if left>right:continue
        account=session.get(Account,account_id)
        if not account or not account.active:continue
        connection=session.scalar(select(Connection).where(Connection.account_id==account_id,Connection.platform=='instagram'))
        if not connection or connection.status.upper()!='CONNECTED':continue
        key=f'ig-report-days-v1:{account_id}:{left}:{right}:{int(utcnow().timestamp())//21600}'
        enqueue_job(session,'INSTAGRAM_INSIGHTS',account_id=account_id,payload={'start':left.isoformat(),'end':right.isoformat(),'cursor':left.isoformat()},idempotency_key=key,priority=-20,max_attempts=3)
        accepted+=1
    return {'queued_accounts':accepted,'requested_start':start,'requested_end':end,'fetch_start':left.isoformat(),'note':'Geçmiş sorguları en fazla 90 günle sınırlı; API daha azını sağlarsa eksik günler doldurulmaz. Arşivlenen eski ölçümler korunur.'}

def classify(response):
    try:payload=response.json()
    except ValueError:return None,'error'
    error=payload.get('error') or {}
    if response.status_code==429 or error.get('code') in (4,17,32,613):return None,'rate_limit'
    if response.status_code>=500:return None,'error'
    if response.status_code in (401,403) or error.get('code') in (10,190,200):return None,'permission_required'
    if response.status_code>=400 or error:return None,'unsupported'
    return payload,'available'

def fetch_day(client,external,credentials,window,guard=lambda:None):
    rows=[]
    for key,metric in API_METRICS.items():
        guard()
        response=client.get(f'https://graph.instagram.com/{settings.graph_api_version}/{external}/insights',headers={'Authorization':'Bearer '+credentials['access_token']},params={'metric':metric,'period':'day','metric_type':'total_value','since':int(window['start_utc'].timestamp()),'until':int(window['end_utc'].timestamp())})
        payload,status=classify(response)
        if status=='rate_limit':raise RetryLater('Instagram analitik kotası bekleniyor; yayın kuyruğu korunuyor.',1800)
        value=None
        if payload is not None:
            items=[i for i in payload.get('data',[]) if i.get('name')==metric and i.get('period')=='day']
            if len(items)==1:
                value=(items[0].get('total_value') or {}).get('value')
                if not valid_number(value) or value<0:value=None;status='no_data'
            else: status='no_data'
        rows.append((key,value,status))
    return rows

def fetch_report_day(client,external,credentials,window,guard=lambda:None):
    guard()
    response=client.get(f'https://graph.instagram.com/{settings.graph_api_version}/{external}/insights',headers={'Authorization':'Bearer '+credentials['access_token']},params={'metric':'reach','period':'day','metric_type':'time_series','since':int((window['start_utc']-timedelta(days=2)).timestamp()),'until':int(window['end_utc'].timestamp())})
    payload,status=classify(response)
    if status=='rate_limit':raise RetryLater('Instagram analitik kotası bekleniyor; yayın kuyruğu korunuyor.',1800)
    if status!='available':return None,[],status
    period=resolve_period(payload,window)
    if period is None:return None,[],'period_unavailable'
    rows=fetch_day(client,external,credentials,window,guard)
    return period,rows,'available'

def collect(ctx):
    if not settings.smfa_analytics_enabled:return {'disabled':True}
    if not settings.smfa_insights_boundaries_verified:
        return {'skipped':'API tarih sınırı pilotu henüz doğrulanmadı; dönem verisi uydurulmadı.'}
    session=ctx.session
    account=session.get(Account,ctx.job.account_id)
    if not account or not account.active:return {'skipped':'account_paused','publishing_unchanged':True}
    connection=session.scalar(select(Connection).where(Connection.account_id==ctx.job.account_id,Connection.platform=='instagram'))
    if not connection or connection.status.upper()!='CONNECTED':return {'skipped':'connection_required'}
    # Read-only credentials here. Their normal publication refresh path remains authoritative.
    token=session.scalar(select(OAuthToken).where(OAuthToken.connection_id==connection.id))
    if not token:return {'skipped':'connection_required'}
    credentials=decrypt_json(token.encrypted_payload)
    cursor=(ctx.job.result or {}).get('cursor') or ctx.job.payload['cursor']
    window=date_window('custom',cursor,cursor)
    def guard():
        ctx.guard()
        due=session.scalar(select(PublishJob.id).where(PublishJob.status.in_(('SCHEDULED','RETRYING','PUBLISHING')),PublishJob.scheduled_at<=utcnow()+timedelta(minutes=2)).limit(1))
        if due:raise RetryLater('Yaklaşan yayınlara öncelik veriliyor.',180)
    guard()
    with httpx.Client(timeout=8,follow_redirects=False) as client:
        period,rows,period_status=fetch_report_day(client,connection.external_id,credentials,window,guard)
    for metric,value,status in rows:
        record_measure(session,ctx.job.account_id,connection.external_id,metric,period['start_utc'],period['end_utc'],value,status,scope=SCOPE,provenance={'source':'instagram_user_insights','api_version':settings.graph_api_version,'login':'instagram_login','external_id':connection.external_id,'period':'day','metric_type':'total_value','period_basis':BASIS,'report_date':cursor,'provider_start_utc':period['start_utc'].isoformat(),'provider_end_utc':period['end_utc'].isoformat(),'query_start_utc':window['start_utc'].isoformat(),'query_end_utc':window['end_utc'].isoformat(),'display_timezone':'Europe/Istanbul','boundary_verified':True})
    session.commit()
    if period_status=='permission_required' or any(s=='permission_required' for _,_,s in rows):
        cap=session.get(InstagramCapability,ctx.job.account_id)
        if cap:cap.insights='permission_required';cap.checked_at=utcnow();session.commit()
        return {'status':'permission_required','publishing_unchanged':True}
    next_day=date.fromisoformat(cursor)+timedelta(days=1)
    if next_day<=date.fromisoformat(ctx.job.payload['end']):
        ctx.checkpoint(cursor=next_day.isoformat())
        raise RetryLater('Sonraki analitik günü düşük öncelikli sırada.',600)
    return {'collected_through':cursor,'period_status':period_status,'period_basis':BASIS,'publication_unchanged':True}
