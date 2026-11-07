"""Turkey calendar windows and truthful aggregation of bounded observations."""
from __future__ import annotations
import calendar
import csv
import hashlib
import io
import json
import math
from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo
from sqlalchemy import select, func
from fastapi import HTTPException
from factory.models import Account, Analytics, Connection, PublishedPost, Video, utcnow
from factory.update_models import InsightMeasure, InstagramCapability
from factory.config import settings

ZONE=ZoneInfo('Europe/Istanbul')
METRICS=('views','reach','likes','comments','shares','saves','profile_views','follower_gain','follower_loss','follower_net','watch_time_seconds','average_watch_time_seconds')
ADDITIVE={'views','likes','comments','shares','saves','profile_views','watch_time_seconds'}
LABELS={'views':'Görüntülenme','reach':'Erişim','likes':'Beğeni','comments':'Yorum','shares':'Paylaşım','saves':'Kaydetme','profile_views':'Profil ziyaretleri','follower_gain':'Takipçi kazanımı','follower_loss':'Takipçi kaybı','follower_net':'Takipçi net değişimi','watch_time_seconds':'Toplam izleme süresi (sn)','average_watch_time_seconds':'Ortalama izleme süresi (sn)'}

def shift_month(day,months):
    index=day.year*12+day.month-1+months
    year,month=divmod(index,12);month+=1
    return date(year,month,min(day.day,calendar.monthrange(year,month)[1]))

def date_window(preset='7d', start=None, end=None, *, now=None, earliest=None):
    now=now or utcnow();today=now.astimezone(ZONE).date()
    right=today
    if preset=='today': left=today
    elif preset in ('7d','14d'): left=today-timedelta(days=int(preset[:-1])-1)
    elif preset in ('1m','3m'): left=shift_month(today,-int(preset[:-1]))+timedelta(days=1)
    elif preset=='all': left=min(earliest,today) if earliest else today
    elif preset=='custom':
        try: left=date.fromisoformat(start or '');right=date.fromisoformat(end or '')
        except ValueError: raise HTTPException(422,'Başlangıç ve bitiş tarihlerini seçin.') from None
    else: raise HTTPException(422,'Tarih seçimi geçersiz.')
    if left.year<2000: raise HTTPException(422,'2000 yılından önceki tarihler desteklenmiyor.')
    if left>right or right>today: raise HTTPException(422,'Tarih aralığı ters veya gelecekte olamaz.')
    def utc(day): return datetime.combine(day,time.min,ZONE).astimezone(timezone.utc)
    return {'start':left.isoformat(),'end':right.isoformat(),'start_utc':utc(left),'end_utc':utc(right+timedelta(days=1)),'timezone':'Europe/Istanbul','partial_today':right==today,'days':(right-left).days+1,'history_available':earliest is not None,'month_rule':'Bugünün takvimden 1/3 ay önceki eş günü (yoksa ayın son günü) hariç, bugün dahil.'}

def previous_window(window):
    end=date.fromisoformat(window['start'])-timedelta(days=1)
    start=end-timedelta(days=window['days']-1)
    return date_window('custom',start.isoformat(),end.isoformat())

def valid_number(value): return isinstance(value,(int,float)) and not isinstance(value,bool) and math.isfinite(value)

def cell(value=None,status='no_data',**extra): return {'value':value,'status':status,**extra}

def aggregate_cells(cells,*,metric,unique_scope=False):
    valid=[c for c in cells if valid_number(c.get('value')) and c['status'] in ('available','partial','stale')]
    if metric in ('reach','average_watch_time_seconds') and not unique_scope:
        return cell(status='unsupported',note='Tekil erişim veya ortalamalar farklı kapsamlar arasında toplanmaz.')
    if not valid:
        statuses={c['status'] for c in cells}
        status='permission_required' if 'permission_required' in statuses else 'error' if 'error' in statuses else 'unsupported' if statuses=={'unsupported'} else 'no_data'
        return cell(status=status,measured=0,expected=len(cells))
    total=sum(c['value'] for c in valid)
    status='partial' if len(valid)!=len(cells) or any(c['status']=='partial' for c in valid) else 'stale' if any(c['status']=='stale' for c in valid) else 'available'
    return cell(total,status,measured=len(valid),expected=len(cells))

def with_interactions(metrics):
    components=[metrics[k] for k in ('likes','comments','shares','saves')]
    metrics['interactions']=aggregate_cells(components,metric='interactions',unique_scope=True)
    total=metrics['interactions'];reach=metrics['reach']
    if total['status']=='available' and reach['status']=='available' and valid_number(reach['value']) and reach['value']>0:
        metrics['engagement_rate']=cell(100*total['value']/reach['value'],'available')
    else: metrics['engagement_rate']=cell(status='no_data',note='Aynı kapsamda tam etkileşim ve pozitif tekil erişim gerekli.')
    return metrics

def key_for(account_id,external_id,metric,scope,start,end,version):
    return hashlib.sha256(json.dumps([account_id,external_id,metric,scope,start.isoformat(),end.isoformat(),version,'instagram_login'],separators=(',',':')).encode()).hexdigest()

def record_measure(session,account_id,external_id,metric,start,end,value,status,*,provenance,now=None,scope='account_period'):
    now=now or utcnow();key=key_for(account_id,external_id,metric,scope,start,end,settings.graph_api_version)
    row=session.scalar(select(InsightMeasure).where(InsightMeasure.cache_key==key).with_for_update())
    if not row:
        row=InsightMeasure(account_id=account_id,cache_key=key,metric=metric,scope=scope,period_start=start,period_end=end,value=None,status=status,provenance=provenance)
        session.add(row)
    row.last_attempt_at=now
    if status=='available' and valid_number(value):
        row.value=value;row.status=status;row.fetched_at=now;row.last_error=None;row.provenance=provenance
    elif row.value is not None:
        row.last_error=status;row.status='stale'
    else: row.value=None;row.status=status;row.fetched_at=now;row.last_error=status;row.provenance=provenance
    session.flush()
    return row

def account_activity(session,account,window):
    connection=session.scalar(select(Connection).where(Connection.account_id==account.id,Connection.platform=='instagram'))
    measures=session.scalars(select(InsightMeasure).where(InsightMeasure.account_id==account.id,InsightMeasure.scope=='account_period',InsightMeasure.period_start>=window['start_utc'],InsightMeasure.period_end<=window['end_utc'])).all()
    result={};daily=[]
    cap=session.get(InstagramCapability,account.id)
    for metric in METRICS:
        matching=[r for r in measures if r.metric==metric and r.provenance.get('external_id')==(connection.external_id if connection else None) and r.provenance.get('api_version')==settings.graph_api_version]
        by_interval={}
        for r in matching:
            if r.provenance.get('boundary_verified'):
                key=(r.period_start,r.period_end)
                if key not in by_interval or r.fetched_at>by_interval[key].fetched_at: by_interval[key]=r
        exact=[r for r in matching if r.period_start==window['start_utc'] and r.period_end==window['end_utc'] and r.provenance.get('boundary_verified')]
        if exact:
            r=max(exact,key=lambda r:r.fetched_at);result[metric]=cell(r.value,r.status,last_sync=r.fetched_at.isoformat())
            if metric=='views' and window['days']==1: daily.append({'date':window['start'],**result[metric]})
            if metric!='views' or window['days']==1: continue
        if metric not in ADDITIVE:
            result[metric]=cell(status='unsupported');continue
        cells=[]
        for i in range(window['days']):
            start=window['start_utc']+timedelta(days=i);end=start+timedelta(days=1)
            r=by_interval.get((start,end))
            if r:
                value=cell(r.value,r.status,last_sync=r.fetched_at.isoformat())
            else: value=cell(status='permission_required' if cap and cap.insights=='permission_required' else 'no_data')
            cells.append(value)
            if metric=='views': daily.append({'date':start.astimezone(ZONE).date().isoformat(),**value})
        if not exact: result[metric]=aggregate_cells(cells,metric=metric,unique_scope=True)
    return with_interactions(result),daily

def cohort(session,account,window):
    posts=session.scalars(select(PublishedPost).where(PublishedPost.account_id==account.id,PublishedPost.platform=='instagram',PublishedPost.published_at>=window['start_utc'],PublishedPost.published_at<window['end_utc']).order_by(PublishedPost.published_at.desc())).all()
    snapshots=session.scalars(select(Analytics).where(Analytics.post_id.in_([p.id for p in posts])).order_by(Analytics.bucket_at.desc(),Analytics.created_at.desc())).all() if posts else []
    latest={}
    for r in snapshots: latest.setdefault(r.post_id,r)
    details=[]
    for post in posts:
        snap=latest.get(post.id);metrics=snap.metrics if snap else {}
        video=session.get(Video,post.video_id)
        row={'post_id':post.id,'published_at':post.published_at.isoformat(),'url':post.url,'title':video.title if video else '', 'measurement_scope':'latest_observed_lifetime','last_sync':snap.bucket_at.isoformat() if snap else None,'metrics':{}}
        for key in METRICS:
            raw=metrics.get(key)
            row['metrics'][key]=cell(raw,'available' if valid_number(raw) else 'no_data') if key in {'views','reach','likes','comments','shares','saves'} else cell(status='unsupported')
        details.append(row)
    summary={key:aggregate_cells([p['metrics'][key] for p in details],metric=key,unique_scope=len(details)==1) for key in METRICS}
    return with_interactions(summary),details

def build_report(session,ids,preset,start=None,end=None,view='activity',compare=False):
    from factory.services.instagram_report_days import activity as provider_activity,BASIS,NOTICE,CORE
    query=select(Account).where((Account.settings['managed_platform'].as_string().is_(None)) | (Account.settings['managed_platform'].as_string()=='instagram'))
    accounts=session.scalars(query.order_by(Account.username)).all()
    if ids:
        allowed={a.id for a in accounts}
        if not set(ids)<=allowed: raise HTTPException(404,'Seçilen Instagram hesabı bulunamadı.')
        accounts=[a for a in accounts if a.id in ids]
    if view not in ('activity','cohort'): raise HTTPException(422,'Analitik görünümü geçersiz.')
    account_ids=[a.id for a in accounts]
    candidates=[]
    first=session.scalar(select(func.min(PublishedPost.published_at)).where(PublishedPost.account_id.in_(account_ids),PublishedPost.platform=='instagram'))
    if first:candidates.append(first)
    first_measure=session.scalar(select(func.min(InsightMeasure.period_start)).where(InsightMeasure.account_id.in_(account_ids)))
    if first_measure:candidates.append(first_measure)
    earliest=min(candidates).astimezone(ZONE).date() if candidates else None
    window=date_window(preset,start,end,earliest=earliest)
    result=[];details=[];sync=[];trend={}
    for account in accounts:
        if view=='cohort':
            metrics,posts=cohort(session,account,window)
            details.extend({**p,'account_id':account.id,'username':account.username} for p in posts)
            sync.extend(p['last_sync'] for p in posts if p['last_sync'])
        else:
            metrics,days=(provider_activity if settings.smfa_insights_boundaries_verified else account_activity)(session,account,window)
            sync.extend(d['last_sync'] for d in days if d.get('last_sync'))
            for day in days: trend.setdefault(day['date'],[]).append(day)
        count=session.scalar(select(func.count(PublishedPost.id)).where(PublishedPost.account_id==account.id,PublishedPost.platform=='instagram',PublishedPost.published_at>=window['start_utc'],PublishedPost.published_at<window['end_utc'])) or 0
        result.append({'account_id':account.id,'username':account.username,'metrics':metrics,'published_count':count})
    totals=with_interactions({m:aggregate_cells([r['metrics'][m] for r in result],metric=m,unique_scope=len(result)==1) for m in METRICS})
    sync_measure=session.scalar(select(func.max(InsightMeasure.fetched_at)).where(InsightMeasure.account_id.in_(account_ids),InsightMeasure.period_start>=window['start_utc'],InsightMeasure.period_end<=window['end_utc']))
    if sync_measure and view=='activity': sync.append(sync_measure.isoformat())
    output={'window':{k:v.isoformat() if isinstance(v,datetime) else v for k,v in window.items()},'view':view,'measurement_scope':'period_activity' if view=='activity' else 'latest_observed_lifetime','definition':'SMFA etkileşim toplamı = aynı kapsamdaki beğeni + yorum + paylaşım + kaydetme.','accounts':result,'totals':totals,'reels':details,'trend':[{'date':d,**aggregate_cells(v,metric='views')} for d,v in sorted(trend.items())],'published_count':sum(a['published_count'] for a in result),'last_sync':max(sync) if sync else None,'coverage_start':earliest.isoformat() if earliest else None,'partial_accounts':[r['username'] for r in result if any(v['status']!='available' for k,v in r['metrics'].items() if k in ADDITIVE)],'labels':LABELS}
    output['partial_accounts']=[r['username'] for r in result if any(r['metrics'][k]['status']!='available' for k in CORE)]
    if view=='activity' and settings.smfa_insights_boundaries_verified:
        output['measurement_scope']=BASIS
        output['window']['date_basis']=BASIS
        output['definition']=NOTICE+' '+output['definition']
        output['period_notice']=NOTICE
        output['published_count_note']='Yayın sayısı Türkiye takvim günlerine göredir; Instagram ölçüm aralıkları farklıdır.'
        # A legacy midnight row must not move the actual-report synchronization timestamp.
        measured=[d for days in trend.values() for d in days if d.get('last_sync')]
        output['last_sync']=max((d['last_sync'] for d in measured),default=None)
        output['report_periods']=[{'report_date':d, 'period_start':v.get('period_start'),'period_end':v.get('period_end')} for d,days in sorted(trend.items()) for v in days if v.get('period_end')]
    if compare:
        previous=previous_window(window)
        prev=build_report(session,account_ids,'custom',previous['start'],previous['end'],view,False)
        output['previous']={'window':prev['window'],'totals':prev['totals']}
        output['change_percent']={}
        for m,value in totals.items():
            old=prev['totals'][m]
            output['change_percent'][m]=100*(value['value']-old['value'])/old['value'] if value['status']=='available' and old['status']=='available' and old['value']>0 else None
    return output

def csv_report(report):
    out=io.StringIO();writer=csv.writer(out)
    def safe(v):
        s='' if v is None else str(v)
        return "'"+s if s.lstrip().startswith(('=','+','-','@','\t','\r','\n')) else s
    writer.writerow(['hesap','baslangic','bitis_dahil','saat_dilimi','olcum_kapsami','metrik','deger','durum','veri_baslangici','son_senkron','tanim'])
    for row in report['accounts']:
        for metric,value in row['metrics'].items():
            writer.writerow([safe(v) for v in [row['username'],report['window']['start'],report['window']['end'],report['window']['timezone'],report['measurement_scope'],metric,value['value'],value['status'],report['coverage_start'],report['last_sync'],report['definition']]])
    return '\ufeff'+out.getvalue()
