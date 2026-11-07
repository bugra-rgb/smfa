"""Durable, isolated news scheduler and at-most-one local create attempt.

No Celery/video jobs are created. A mutation with an uncertain outcome is never
resubmitted; reconciliation uses its frozen text and binding. Provider acceptance
is not publication. All mutation callbacks persist before the next network step.
"""
from __future__ import annotations
import hashlib
import json
import math
import re
import uuid
from datetime import datetime,timedelta,timezone
from zoneinfo import ZoneInfo
from sqlalchemy import select,update,or_,func
from sqlalchemy.exc import IntegrityError
from factory.models import Account,Job,PublishJob,SystemState,SystemLog,utcnow
from factory.xnews_models import (XNewsAccount,XNewsSource,XNewsStory,XNewsTrendSnapshot,
    XNewsJob,XNewsBudget,XNewsDedup)

UNSENT=('DISCOVERED','GENERATING','READY','RETRYABLE_FAILED','AWAITING_REVIEW')
INFLIGHT=('SUBMITTING','BUFFER_ACCEPTED','BUFFER_PENDING')
TERMINAL=('PUBLISHED','SKIPPED','FAILED','CANCELLED')
MAX_POLLS=10
MAX_TRACKING_SECONDS=3600
LEASE_SECONDS=240

class NewsRuntimeError(Exception):
    def __init__(self,code,message,retry_after=0):
        super().__init__(message);self.code=code;self.retry_after=retry_after

def instant(value):
    if isinstance(value,str):value=datetime.fromisoformat(value.replace('Z','+00:00'))
    if value is None:return None
    if value.tzinfo is None:raise NewsRuntimeError('invalid_time','Saat dilimi belirtilmeli.')
    return value.astimezone(timezone.utc)

def frozen_hash(payload):
    return hashlib.sha256(json.dumps(payload,ensure_ascii=False,sort_keys=True,separators=(',',':')).encode()).hexdigest()

def audit(session,event,account_id=None,**data):
    session.add(SystemLog(event='xnews.'+event,account_id=account_id,level='info',message='X haber otomasyonu: '+event,data=data))

def configure_account(session,account_id,config,now=None,actor_id=None):
    now=now or utcnow()
    if not session.get(Account,account_id):raise NewsRuntimeError('account_missing','Hesap bulunamadı.')
    row=session.scalar(select(XNewsAccount).where(XNewsAccount.account_id==account_id).with_for_update())
    if row is None:
        row=XNewsAccount(account_id=account_id,config={},schedule_version=0,news_enabled=False,automation_enabled=False)
        session.add(row);session.flush()
    cfg={**(row.config or {}),**config}
    minutes=int(cfg.get('interval_minutes',180))
    if not 15<=minutes<=10080:raise NewsRuntimeError('invalid_interval','Aralık 15–10080 dakika olmalı.')
    ZoneInfo(cfg.get('timezone','Europe/Istanbul'))
    # Saving changes never silently activates automation or emits a publication.
    cfg.pop('automation_enabled',None)
    row.buffer_channel_id=cfg.get('buffer_channel_id') or None
    row.config=cfg;row.news_enabled=bool(cfg.get('news_enabled',False))
    if not row.news_enabled:
        row.automation_enabled=False
        session.execute(update(XNewsJob).where(XNewsJob.account_id==account_id,XNewsJob.status.in_((*INFLIGHT,'UNKNOWN_RESULT'))).values(cancel_requested=True))
    row.schedule_version+=1
    start=instant(cfg.get('start_at'))
    row.start_at=start or now
    row.next_run_at=start if start and start>now else now+timedelta(minutes=minutes)
    session.execute(update(XNewsJob).where(XNewsJob.account_id==account_id,XNewsJob.status.in_(UNSENT),
        XNewsJob.submit_started_at.is_(None)).values(status='CANCELLED',error_code='schedule_changed',error_message='Plan değişti; eski yerel iş iptal edildi.'))
    audit(session,'settings_changed',account_id,actor_id=actor_id,schedule_version=row.schedule_version)
    return row

def set_automation(session,account_id,enabled,now=None,actor_id=None):
    now=now or utcnow()
    row=session.scalar(select(XNewsAccount).where(XNewsAccount.account_id==account_id).with_for_update())
    if row is None or not row.news_enabled:raise NewsRuntimeError('news_not_enabled','Önce hesap haber ayarlarını kaydedin.')
    if enabled:
        cfg=row.config or {}
        account=session.get(Account,account_id)
        required=('buffer_connection_id','buffer_account_id','buffer_organization_id','buffer_channel_id','external_id')
        if not account or not account.active or any(not cfg.get(k) for k in required):
            raise NewsRuntimeError('binding_missing','Etkin hesap ve doğrulanmış Buffer X eşleşmesi gerekiyor.')
        if not cfg.get('allowed_sources'):
            raise NewsRuntimeError('sources_not_configured','Önce bu hesap için haber kaynaklarını seçin.')
    row.automation_enabled=bool(enabled)
    if enabled:
        if row.next_run_at is None or row.next_run_at<=now:
            row.next_run_at=now+timedelta(minutes=int(row.config.get('interval_minutes',180)))
    else:
        session.execute(update(XNewsJob).where(XNewsJob.account_id==account_id,XNewsJob.status.in_(UNSENT),
            XNewsJob.submit_started_at.is_(None)).values(status='CANCELLED',error_code='automation_stopped',error_message='Haber otomasyonu durduruldu.'))
        session.execute(update(XNewsJob).where(XNewsJob.account_id==account_id,XNewsJob.status.in_((*INFLIGHT,'UNKNOWN_RESULT'))).values(cancel_requested=True))
    audit(session,'automation_changed',account_id,actor_id=actor_id,enabled=bool(enabled))
    return row

def approve_job(session,job_id,now=None,actor_id=None):
    now=now or utcnow();job=session.scalar(select(XNewsJob).where(XNewsJob.id==job_id).with_for_update())
    if not job or job.status!='AWAITING_REVIEW':raise NewsRuntimeError('invalid_state','İş onay beklemiyor.')
    job.status='READY';job.next_attempt_at=now;job.audit={**job.audit,'approved_by':actor_id,'approved_at':now.isoformat()}
    audit(session,'review_approved',job.account_id,job_id=job.id,actor_id=actor_id)
    return job

def request_cancel(session,job_id,now=None,actor_id=None):
    job=session.scalar(select(XNewsJob).where(XNewsJob.id==job_id).with_for_update())
    if not job:raise NewsRuntimeError('job_missing','İş bulunamadı.')
    if job.status in UNSENT and not job.submit_started_at:job.status='CANCELLED'
    elif job.status not in TERMINAL:job.cancel_requested=True;job.cancellation_checked=False;job.next_attempt_at=now or utcnow()
    audit(session,'cancel_requested',job.account_id,job_id=job.id,actor_id=actor_id)
    return job

def quiet(cfg,now):
    value=cfg.get('quiet_hours') or {}
    if not value.get('enabled'):return False
    local=now.astimezone(ZoneInfo(cfg.get('timezone','Europe/Istanbul'))).strftime('%H:%M')
    start,end=value['start'],value['end']
    return (start<=local<end) if start<end else (local>=start or local<end)

def schedule_due(session,now=None):
    now=now or utcnow();created=[]
    rows=session.scalars(select(XNewsAccount).where(XNewsAccount.news_enabled.is_(True),
        XNewsAccount.automation_enabled.is_(True),XNewsAccount.next_run_at<=now).order_by(XNewsAccount.next_run_at)
        .with_for_update(skip_locked=True)).all()
    for row in rows:
        interval=timedelta(minutes=int(row.config.get('interval_minutes',180)))
        missed=max(0,int((now-row.next_run_at).total_seconds()//interval.total_seconds()))
        slot=row.next_run_at+missed*interval
        row.next_run_at=slot+interval
        existing=session.scalar(select(XNewsJob.id).where(XNewsJob.account_id==row.account_id,
            XNewsJob.scheduled_slot==slot,XNewsJob.schedule_version==row.schedule_version))
        if existing:continue
        account=session.get(Account,row.account_id)
        unresolved=session.scalar(select(XNewsJob.id).where(XNewsJob.account_id==row.account_id,
            XNewsJob.status.in_((*INFLIGHT,'UNKNOWN_RESULT'))).limit(1))
        global_state=session.get(SystemState,'global')
        global_paused=bool(global_state and global_state.paused)
        reason='global_paused' if global_paused else 'previous_news_unresolved' if unresolved else ('account_paused' if not account or not account.active else ('quiet_hours' if quiet(row.config,now) else None))
        job=XNewsJob(account_id=row.account_id,scheduled_slot=slot,schedule_version=row.schedule_version,
            status='SKIPPED' if reason else 'DISCOVERED',next_attempt_at=now,error_code=reason,
            error_message='Tur atlandı; sonraki plan normal aralıkta devam eder.' if reason else None,
            audit={'missed_slots_discarded':missed})
        session.add(job);session.flush();created.append(job.id)
    return created

def claim_job(session,job_id,now=None):
    now=now or utcnow();owner=str(uuid.uuid4())
    job=session.get(XNewsJob,job_id)
    if not job or job.status in TERMINAL or job.status=='AWAITING_REVIEW' or job.next_attempt_at>now:return None
    claim=session.execute(update(XNewsAccount).where(XNewsAccount.account_id==job.account_id,
        or_(XNewsAccount.lease_until.is_(None),XNewsAccount.lease_until<now))
        .values(lease_owner=owner,lease_until=now+timedelta(seconds=LEASE_SECONDS)))
    if claim.rowcount!=1:return None
    claim=session.execute(update(XNewsJob).where(XNewsJob.id==job_id,
        XNewsJob.status.not_in(TERMINAL),or_(XNewsJob.lease_until.is_(None),XNewsJob.lease_until<now))
        .values(lease_owner=owner,lease_until=now+timedelta(seconds=LEASE_SECONDS)))
    if claim.rowcount!=1:
        session.execute(update(XNewsAccount).where(XNewsAccount.account_id==job.account_id,XNewsAccount.lease_owner==owner).values(lease_owner=None,lease_until=None));return None
    return owner

def guard(session,job_id,owner):
    job=session.scalar(select(XNewsJob).where(XNewsJob.id==job_id).execution_options(populate_existing=True))
    if not job or job.lease_owner!=owner or not job.lease_until or job.lease_until<utcnow():
        raise NewsRuntimeError('lease_lost','İş kilidi başka çalışana geçti.')
    if job.status in TERMINAL:
        raise NewsRuntimeError('job_terminal','İş kapatılmış; yeni gönderim yapılmaz.')
    return job

def _insert(session,model,values):
    dialect=session.get_bind().dialect.name
    if dialect=='postgresql':from sqlalchemy.dialects.postgresql import insert
    elif dialect=='sqlite':from sqlalchemy.dialects.sqlite import insert
    else:raise NewsRuntimeError('database_unsupported','Veritabanı desteklenmiyor.')
    session.execute(insert(model).values(**values).on_conflict_do_nothing())

def reserve_budget(session_factory,account_id,resource='calls',units=1,now=None,limits=None):
    """Atomic conservative reservation. No refunds means failed/timeouts count too."""
    now=now or utcnow();units=int(units)
    if units<0:raise ValueError('negative reservation')
    from factory.services.xnews_settings import read_settings
    with session_factory.begin() as session:
        settings=limits or read_settings(session)
        row=session.get(XNewsAccount,account_id) if account_id else None
        cfg=(row.config or {}) if row else {}
        glob=int(settings.get('global_daily_'+resource+'_cap',{'calls':100,'tokens':100000,'publish':50}.get(resource,100)))
        acct=min(glob,int(cfg.get('daily_publish_cap',5) if resource=='publish' else cfg.get('daily_'+resource+'_cap',settings.get('account_daily_'+resource+'_cap',20 if resource=='calls' else 20000))))
        periods=[('global',now.date().isoformat(),glob)]
        if account_id:periods.append((account_id,now.astimezone(ZoneInfo(cfg.get('timezone','Europe/Istanbul'))).date().isoformat(),acct))
        for scope,day,limit in periods:
            key=f'{scope}:{resource}:{day}'
            _insert(session,XNewsBudget,{'key':key,'used':0,'updated_at':now})
            changed=session.execute(update(XNewsBudget).where(XNewsBudget.key==key,XNewsBudget.used+units<=limit).values(used=XNewsBudget.used+units,updated_at=now))
            if changed.rowcount!=1:raise NewsRuntimeError('quota_exceeded','Haber otomasyonu tüketim sınırına ulaştı.',3600)

def reserve_callback(session_factory,account_id,resource='calls'):
    def reserve(*args,**kwargs):
        units=kwargs.get('units',kwargs.get('tokens',1))
        if args and isinstance(args[0],(int,float)):units=args[0]
        reserve_budget(session_factory,account_id,resource,int(units))
    return reserve

def _gate(session,job,now,record=True):
    gate=session.scalar(select(SystemState).where(SystemState.id=='publish-gap').with_for_update())
    if gate is None:raise NewsRuntimeError('gate_missing','Mevcut yayın sırası kurulumu bulunamadı.')
    global_state=session.get(SystemState,'global')
    if global_state and global_state.paused:raise NewsRuntimeError('global_paused','Genel yayınlar duraklatılmış.',60)
    video=session.scalar(select(Job.id).join(PublishJob,Job.payload['publish_job_id'].as_string()==PublishJob.id).where(
        Job.kind=='PUBLISH',Job.result['cycle_admitted'].as_boolean().is_(True),
        PublishJob.status.not_in(('PUBLISHED','CANCELLED','BLOCKED','POLICY_BLOCKED','FAILED'))).limit(1))
    other=session.scalar(select(XNewsJob.id).where(XNewsJob.id!=job.id,XNewsJob.status.in_(INFLIGHT),
        XNewsJob.submit_started_at>now-timedelta(seconds=MAX_TRACKING_SECONDS)).limit(1))
    if video or other:raise NewsRuntimeError('publication_in_progress','Önceki platform işleminin tamamlanması bekleniyor.',30)
    previous=session.scalar(select(SystemLog).where(SystemLog.event=='control.publish.admitted').order_by(SystemLog.created_at.desc(),SystemLog.id.desc()).limit(1))
    if previous and previous.account_id!=job.account_id:
        seconds=(previous.created_at+timedelta(seconds=120)-now).total_seconds()
        if seconds>0:raise NewsRuntimeError('publication_spacing','Hesaplar arası 120 saniyelik aralık bekleniyor.',math.ceil(seconds))
    if record:
        gate.updated_at=now
        session.add(SystemLog(event='control.publish.admitted',account_id=job.account_id,level='info',
            message='X haber yayın işlemi başlatılıyor.',created_at=now,data={'content_type':'ai_news','job_id':job.id}))

def _tokens(text):return set(re.findall(r'\w+',text.casefold()))

def _dedup(session,job,payload,now):
    # Global lock protects both exact fingerprints and the similarity scan.
    _insert(session,XNewsBudget,{'key':'dedup-lock','used':0,'updated_at':now})
    session.scalar(select(XNewsBudget).where(XNewsBudget.key=='dedup-lock').with_for_update())
    keys=['story:'+payload['story_cluster_id'],'text:'+hashlib.sha256(' '.join(sorted(_tokens(payload['text']))).encode()).hexdigest()]
    if payload.get('source_content_fingerprint'):keys.append('body:'+payload['source_content_fingerprint'])
    for key in keys:
        old=session.get(XNewsDedup,key)
        if old and old.job_id!=job.id:raise NewsRuntimeError('duplicate_story','Bu haber başka bir haber hesabında kullanılmış.')
    recent=session.scalars(select(XNewsJob).where(XNewsJob.id!=job.id,XNewsJob.submit_started_at>=now-timedelta(hours=48))).all()
    own=_tokens(payload['text'])
    own_story=session.get(XNewsStory,job.story_id) if job.story_id else None
    own_body=_tokens(own_story.available_text) if own_story else set()
    for prev in recent:
        previous_story=session.get(XNewsStory,prev.story_id) if prev.story_id else None
        previous_body=_tokens(previous_story.available_text) if previous_story else set()
        if len(own_body)>12 and previous_body and len(own_body&previous_body)/len(own_body|previous_body)>=0.85:
            raise NewsRuntimeError('syndicated_duplicate','Aynı ajans haberi veya çok benzer kaynak başka hesapta kullanılmış.')
        theirs=_tokens((prev.payload or {}).get('text',''))
        if own and theirs and len(own&theirs)/len(own|theirs)>=0.80:
            raise NewsRuntimeError('similar_content','Başka hesaptaki haberle büyük ölçüde benzer içerik.')
    for key in keys:
        if not session.get(XNewsDedup,key):session.add(XNewsDedup(fingerprint=key,job_id=job.id,created_at=now))


def _validate_final(session,job,cfg,now):
    from factory.services.xnews_sources import validate_story,select_hashtags
    from factory.services.xnews_settings import read_settings
    from factory.xnews_models import XNewsConfig
    session.get(XNewsConfig,'global',populate_existing=True)
    global_cfg=read_settings(session)
    trend_cfg=global_cfg.get('trend') or {}
    if not global_cfg.get('global_enabled',False) or not trend_cfg.get('permission_confirmed') or not trend_cfg.get('url'):
        raise NewsRuntimeError('trend_unconfigured','Gündem kaynağının etkin yapılandırması ve izni gerekiyor.')
    if not job.payload or frozen_hash(job.payload)!=job.payload_hash:raise NewsRuntimeError('payload_changed','Sabit gönderi içeriği doğrulanamadı.')
    story=session.get(XNewsStory,job.story_id);trend=session.get(XNewsTrendSnapshot,job.trend_id)
    if not story or not trend:raise NewsRuntimeError('provenance_missing','Haber veya gündem kaydı bulunamadı.')
    if trend.provider!=trend_cfg.get('provider'):
        raise NewsRuntimeError('stale_draft','Gündem sağlayıcısı değişti; taslak yenilenmeli.')
    source=session.get(XNewsSource,story.source_id,populate_existing=True)
    if not source or not source.enabled or not (source.config or {}).get('permission_confirmed') or story.source_id not in cfg.get('allowed_sources',[]):
        raise NewsRuntimeError('source_disabled','Haber kaynağı artık bu hesap için etkin değil.')
    validate_story(story_dict(story),now=now)
    selected=select_hashtags(trend.data,story_dict(story),excluded=cfg.get('excluded_hashtags',[]),
        allow_fewer=cfg.get('insufficient_hashtag_behavior')=='use_available',target=5,now=now,
        max_age_seconds=int(trend_cfg.get('max_age_seconds',300)))
    if [x['tag'] for x in selected]!=job.payload.get('hashtags'):raise NewsRuntimeError('stale_draft','Gündem değişti; gönderilmemiş taslak yenilenmeli.')
    if quiet(cfg,now):raise NewsRuntimeError('quiet_hours','Sessiz saat nedeniyle tur atlandı.')


def story_dict(row):
    return _jsonable({**(row.data or {}),'story_id':row.id,'source_id':row.source_id,'title':row.title,'canonical_url':row.canonical_url,
        'available_text':row.available_text,'source_published_at':row.source_published_at,'source_updated_at':row.source_updated_at,
        'event_at':row.event_at,'retrieved_at':row.retrieved_at,'category':row.category,'language':row.language,
        'turkey_relevance':row.turkey_relevance,'story_cluster_id':row.story_cluster_id})

def _jsonable(value):
    return json.loads(json.dumps(value,default=lambda x:x.isoformat() if isinstance(x,datetime) else str(x),ensure_ascii=False))

class RuntimeServices:
    def __init__(self,session_factory=None):
        if session_factory is None:
            from factory.db import SessionLocal
            session_factory=SessionLocal
        self.session_factory=session_factory
    def provider(self,session,cfg):
        from factory.services.xnews_settings import buffer_provider
        return buffer_provider(session,cfg,reserve=reserve_callback(self.session_factory,cfg['account_id']))
    def prepare(self,session,row,now):
        from factory.services.xnews_settings import read_settings,read_secret,ai_provider
        from factory.services.xnews_sources import fetch_news,fetch_trends,validate_story,select_hashtags,compose_with_ai,parse_time
        cfg=row.config or {};settings=read_settings(session)
        sources=session.scalars(select(XNewsSource).where(XNewsSource.enabled.is_(True))).all()
        allowed=set(cfg.get('allowed_sources') or []) & {s.id for s in sources if (s.config or {}).get('permission_confirmed') is True}
        if not allowed:raise NewsRuntimeError('sources_not_configured','Bu hesap için haber kaynaklarını seçin.')
        refreshed=0
        for source in sources:
            if refreshed>=1:break
            if source.id not in allowed or source.last_fetched_at and source.last_fetched_at>now-timedelta(minutes=10):continue
            refreshed+=1
            source_data={**source.config,'id':source.id,'name':source.name,'url':source.url,'kind':source.kind,'api_key':read_secret(session,'source:'+source.id)}
            session.commit()
            try:
                reserve_budget(self.session_factory,row.account_id)
                found=fetch_news(source_data,now=now)
                for item in found[:200]:
                    if session.scalar(select(XNewsStory.id).where(XNewsStory.canonical_url==item['canonical_url'])):continue
                    session.add(XNewsStory(source_id=source.id,canonical_url=item['canonical_url'],title=item['title'],available_text=item.get('available_text',''),
                        source_published_at=parse_time(item.get('source_published_at')),source_updated_at=parse_time(item.get('source_updated_at')),
                        event_at=parse_time(item.get('event_at')),retrieved_at=now,category=item.get('category','general'),language=item.get('language','tr'),
                        turkey_relevance=bool(item.get('turkey_relevance')),story_cluster_id=item['story_cluster_id'],data=_jsonable(item)))
                source.last_error=None
            except Exception as exc:
                source.last_error=getattr(exc,'code','source_unavailable')
            source.last_fetched_at=now;session.commit()
        candidates=session.scalars(select(XNewsStory).where(XNewsStory.source_id.in_(allowed),XNewsStory.source_published_at>=now-timedelta(hours=48)).order_by(XNewsStory.source_published_at.desc()).limit(100)).all()
        chosen=None
        for story in candidates:
            if cfg.get('categories') and story.category not in cfg['categories']:continue
            if any(w.casefold() in (story.title+' '+story.available_text).casefold() for w in cfg.get('excluded_keywords',[]) if w):continue
            if session.get(XNewsDedup,'story:'+story.story_cluster_id):continue
            fingerprint=(story.data or {}).get('content_fingerprint')
            if fingerprint and session.get(XNewsDedup,'body:'+fingerprint):continue
            try:validate_story(story_dict(story),now=now)
            except Exception:continue
            chosen=story;break
        if chosen is None:raise NewsRuntimeError('no_eligible_news','Son 48 saatte uygun ve kullanılmamış haber bulunamadı.')
        tc=settings.get('trend') or {};age=int(tc.get('max_age_seconds',300))
        trend=session.scalar(select(XNewsTrendSnapshot).where(XNewsTrendSnapshot.provider==tc.get('provider',''),XNewsTrendSnapshot.region=='TR',XNewsTrendSnapshot.fetched_at>=now-timedelta(seconds=age),XNewsTrendSnapshot.provider_timestamp>=now-timedelta(seconds=age),XNewsTrendSnapshot.provider_timestamp<=now+timedelta(seconds=30)).order_by(XNewsTrendSnapshot.fetched_at.desc()).limit(1))
        if trend is None:
            session.commit();reserve_budget(self.session_factory,row.account_id)
            data=fetch_trends({**tc,'api_key':read_secret(session,'trend')},now=now)
            trend=XNewsTrendSnapshot(provider=data['provider'],region=data['region'],fetched_at=parse_time(data['fetched_at']),provider_timestamp=parse_time(data.get('provider_timestamp')),data=_jsonable(data))
            session.add(trend);session.commit()
        tags=select_hashtags(trend.data,story_dict(chosen),excluded=cfg.get('excluded_hashtags',[]),allow_fewer=cfg.get('insufficient_hashtag_behavior')=='use_available',target=5,now=now,max_age_seconds=age)
        session.commit()
        def ai_reserve(*args,**kwargs):
            reserve_budget(self.session_factory,row.account_id,'calls')
            # Count conservative full-input + bounded output before both model calls.
            reserve_budget(self.session_factory,row.account_id,'tokens',int(kwargs.get('tokens',kwargs.get('units',max(4096,len(chosen.available_text)+4096)))))
        ai=ai_provider(session,{**cfg,'account_id':row.account_id},reserve=ai_reserve)
        generated,payload=compose_with_ai(ai,story_dict(chosen),tags,style=cfg.get('writing_style',''))
        payload={**payload,'story_id':chosen.id,'story_cluster_id':chosen.story_cluster_id,'hashtags':[x['tag'] for x in tags],
            'hashtag_relations':tags,'trend_fetched_at':trend.fetched_at.isoformat(),'trend_max_age_seconds':age,'source_url':chosen.canonical_url,
            'source_content_fingerprint':chosen.data.get('content_fingerprint'),'generation':generated,'usage':generated.get('usage',{})}
        return _jsonable(payload),chosen.id,trend.id


def preview(session,account_id,services=None,now=None):
    now=now or utcnow();row=session.get(XNewsAccount,account_id)
    if not row:raise NewsRuntimeError('settings_missing','Haber ayarlarını önce kaydedin.')
    if services is None:
        from factory.db import SessionLocal
        services=RuntimeServices(SessionLocal)
    payload,_,_=services.prepare(session,row,now)
    return {**payload,'status':'PREVIEW','publication_requests':0,'ai_usage_possible':True}

def make_reserver(session_factory,account_id=None,cfg=None):
    """API-compatible call reservation; token reservations may pass tokens=N."""
    def reserve(*args,**kwargs):
        reserve_budget(session_factory,account_id,'calls')
        tokens=kwargs.get('tokens',0)
        if tokens:reserve_budget(session_factory,account_id,'tokens',int(tokens))
    return reserve


def _apply_result(session,job,result,now):
    state=result.get('state',result.get('status','UNKNOWN_RESULT'))
    if state not in (*INFLIGHT,'PUBLISHED','FAILED','UNKNOWN_RESULT','CANCELLED'):state='UNKNOWN_RESULT'
    job.status=state;job.last_checked_at=now
    post_id=result.get('post_id') or result.get('buffer_post_id')
    if post_id:
        if job.buffer_post_id and job.buffer_post_id!=post_id:raise NewsRuntimeError('identity_mismatch','Sağlayıcı gönderi kimliği değişti.')
        job.buffer_post_id=post_id
        if not job.accepted_at:job.accepted_at=now
    job.error_code=result.get('error_code') or result.get('code')
    job.error_message=result.get('error_message') or result.get('message')
    if state=='PUBLISHED':
        job.published_at=instant(result.get('sent_at') or result.get('sentAt'))
        job.published_url=result.get('external_link') or result.get('url') or result.get('externalLink')
        job.audit={**job.audit,'confirmed_sent_at':now.isoformat(),'publication_delay_seconds':
            (job.published_at-job.scheduled_slot).total_seconds() if job.published_at else None,
            'missing_external_link':not bool(job.published_url),'missing_sent_at':not bool(job.published_at)}
    if state in INFLIGHT:
        job.next_attempt_at=now+timedelta(seconds=min(300,30*2**min(job.poll_count,4)))
    elif state=='UNKNOWN_RESULT':
        job.next_attempt_at=now+timedelta(seconds=120)
    audit(session,'provider_result',job.account_id,job_id=job.id,state=state,post_id=job.buffer_post_id)


def process_job(session_factory,job_id,services=None,now=None):
    now=now or utcnow();services=services or RuntimeServices(session_factory)
    with session_factory.begin() as session:owner=claim_job(session,job_id,now)
    if not owner:return {'status':'not_claimed'}
    try:
        with session_factory() as session:
            job=guard(session,job_id,owner);row=session.get(XNewsAccount,job.account_id)
            cfg={**row.config,'account_id':row.account_id}
            # Polling a frozen binding never follows a newly selected channel.
            if job.buffer_binding:cfg={**cfg,**job.buffer_binding}
            # Crash after persistent intent: never call create again.
            if job.status=='SUBMITTING':
                job.status='UNKNOWN_RESULT';job.error_code='create_interrupted';job.error_message='Gönderim sonucu belirsiz; aynı içerik tekrar gönderilmeyecek.';session.commit()
            if job.status=='UNKNOWN_RESULT':
                if job.buffer_post_id and job.cancel_requested and not job.cancellation_checked:
                    provider=services.provider(session,cfg);job.cancellation_checked=True;session.commit()
                    result=provider.cancel(job.buffer_post_id)
                    job=guard(session,job_id,owner)
                    if result.get('state') in ('CANCELLED','PUBLISHED','FAILED'):
                        _apply_result(session,job,result,now);session.commit();return {'status':job.status}
                    job.audit={**job.audit,'cancel_result':result.get('state','UNKNOWN_RESULT')};session.commit()
                if job.reconcile_count>=2 or not job.submit_started_at:
                    return {'status':'UNKNOWN_RESULT','manual_review_required':True}
                provider=services.provider(session,cfg)
                job.reconcile_count+=1;session.commit()
                result=provider.reconcile(job.payload.get('text',''),job.submit_started_at,max_pages=3)
                job=guard(session,job_id,owner)
                candidates=result.get('candidates',[]) if isinstance(result,dict) else []
                if len(candidates)==1 and result.get('complete_scan') is True and result.get('unique_match') is True:
                    candidate=candidates[0]
                    post_id=candidate.get('post_id') or candidate.get('id')
                    if post_id and job.buffer_post_id and job.buffer_post_id!=post_id:
                        job.error_code='identity_mismatch';job.error_message='Bilinen Buffer gönderi kimliği farklı kayıtla değiştirilemez.';job.next_attempt_at=now+timedelta(minutes=5);session.commit()
                        return {'status':'UNKNOWN_RESULT','manual_review_required':True}
                    if post_id:
                        job.buffer_post_id=post_id;job.status='BUFFER_ACCEPTED';job.next_attempt_at=now;job.accepted_at=now;session.commit()
                    else:job.error_code='reconciliation_unresolved'
                else:
                    job.error_code='reconciliation_unresolved';job.error_message='Buffer sonucu kesinleşmedi; Buffer kanalındaki bu içeriği inceleyin. Yeniden gönderilmez.'
                    job.next_attempt_at=now+timedelta(minutes=5);session.commit()
                return {'status':job.status,'manual_review_required':job.status=='UNKNOWN_RESULT'}
            if job.status in ('BUFFER_ACCEPTED','BUFFER_PENDING'):
                if not job.buffer_post_id:
                    job.status='UNKNOWN_RESULT';job.error_code='missing_post_id';session.commit();return {'status':job.status}
                provider=services.provider(session,cfg)
                if job.cancel_requested and not job.cancellation_checked:
                    job.cancellation_checked=True;session.commit()
                    result=provider.cancel(job.buffer_post_id)
                    job=guard(session,job_id,owner)
                    if result.get('state') in ('CANCELLED','PUBLISHED','FAILED'):_apply_result(session,job,result,now);session.commit();return {'status':job.status}
                    job.audit={**job.audit,'cancel_result':result.get('state','UNKNOWN_RESULT')}
                if job.poll_count>=MAX_POLLS or (job.submit_started_at and (now-job.submit_started_at).total_seconds()>MAX_TRACKING_SECONDS):
                    job.status='UNKNOWN_RESULT';job.error_code='tracking_expired';job.error_message='Sınırlı yayın takibi tamamlandı; Buffer içinden kontrol gerekiyor.';job.reconcile_count=2;session.commit();return {'status':job.status}
                job.poll_count+=1;session.commit()
                result=provider.poll(job.buffer_post_id)
                job=guard(session,job_id,owner);_apply_result(session,job,result,now);session.commit();return {'status':job.status}
            pause=session.get(SystemState,'global')
            if pause and pause.paused:
                job.status='SKIPPED';job.error_code='global_paused';session.commit();return {'status':job.status}
            account=session.get(Account,job.account_id)
            if not row.news_enabled or not row.automation_enabled or row.schedule_version!=job.schedule_version or not account or not account.active:
                job.status='CANCELLED';job.error_code='automation_or_schedule_changed';session.commit();return {'status':job.status}
            if now>job.scheduled_slot+timedelta(minutes=30):
                job.status='SKIPPED';job.error_code='slot_expired';job.error_message='Yayın penceresi geçti; geçmiş tur telafi edilmeyecek.';session.commit();return {'status':job.status}
            if not job.payload:
                job.status='GENERATING';session.commit()
                payload,story_id,trend_id=services.prepare(session,row,now)
                job=guard(session,job_id,owner)
                job.payload=payload;job.payload_hash=frozen_hash(payload);job.story_id=story_id;job.trend_id=trend_id;job.status='READY'
                if cfg.get('review_mode') and not job.audit.get('approved_by'):
                    job.status='AWAITING_REVIEW';session.commit();return {'status':job.status}
                session.commit()
            try:_validate_final(session,job,cfg,now)
            except Exception as exc:
                # Unsent stale drafts can be refreshed exactly once per worker turn.
                if getattr(exc,'code','') not in ('stale_draft','stale_trends','trend_stale','trends_stale'):raise
                payload,story_id,trend_id=services.prepare(session,row,now)
                job=guard(session,job_id,owner);job.payload=payload;job.payload_hash=frozen_hash(payload);job.story_id=story_id;job.trend_id=trend_id
                if cfg.get('review_mode'):
                    job.status='AWAITING_REVIEW';job.audit={k:v for k,v in job.audit.items() if not k.startswith('approved_')};session.commit();return {'status':job.status}
                session.commit();_validate_final(session,job,cfg,now)
            # Fully revalidate settings at last local boundary before provider access.
            session.refresh(row)
            if not row.automation_enabled or row.schedule_version!=job.schedule_version:raise NewsRuntimeError('schedule_changed','Plan değişti.')
            provider=services.provider(session,cfg)
            session.commit()
            job=guard(session,job_id,owner)
            _gate(session,job,utcnow(),record=False)
            session.commit()  # Never hold the shared video gate during Buffer health checks.
            binding_keys=('buffer_connection_id','buffer_account_id','buffer_organization_id','buffer_channel_id','external_id')
            frozen_binding={k:cfg.get(k) for k in binding_keys}
            # Keep global gap and account locks until intent is persisted.
            def persist_intent(*args,**kwargs):
                nonlocal job
                current_account=session.scalar(select(Account).where(Account.id==job.account_id).with_for_update().execution_options(populate_existing=True))
                current=session.scalar(select(XNewsAccount).where(XNewsAccount.account_id==job.account_id).with_for_update().execution_options(populate_existing=True))
                job=session.scalar(select(XNewsJob).where(XNewsJob.id==job_id).with_for_update().execution_options(populate_existing=True))
                if not job or job.lease_owner!=owner or not job.lease_until or job.lease_until<utcnow():raise NewsRuntimeError('lease_lost','İş kilidi kaybedildi.')
                if not current_account or not current_account.active or job.cancel_requested or job.status in TERMINAL:
                    raise NewsRuntimeError('job_cancelled','İş veya hesap durdurulmuş.')
                if job.submit_started_at:raise NewsRuntimeError('already_submitted','Bu iş daha önce gönderim aşamasına girdi.')
                if not current.news_enabled or not current.automation_enabled or current.schedule_version!=job.schedule_version:raise NewsRuntimeError('schedule_changed','Plan değişti.')
                pause=session.get(SystemState,'global',populate_existing=True)
                if pause and pause.paused:raise NewsRuntimeError('global_paused','Genel yayınlar duraklatılmış.',60)
                _validate_final(session,job,current.config,utcnow())
                _gate(session,job,utcnow(),record=True)
                _dedup(session,job,job.payload,utcnow())
                reserve_budget(session_factory,job.account_id,'publish')
                job.buffer_binding=frozen_binding
                if args and isinstance(args[0],dict):
                    job.audit={**job.audit,'buffer_payload':args[0].get('payload'),'buffer_payload_hash':args[0].get('payload_hash')}
                job.status='SUBMITTING';job.submit_started_at=utcnow();job.attempts+=1
                audit(session,'submit_intent',job.account_id,job_id=job.id,payload_hash=job.payload_hash);session.commit()
            def persist_accepted(post_id,*args,**kwargs):
                nonlocal job
                job=guard(session,job_id,owner)
                if isinstance(post_id,dict):post_id=post_id.get('post_id') or post_id.get('id')
                if not post_id:raise NewsRuntimeError('missing_post_id','Buffer gönderi kimliği eksik.')
                job.buffer_post_id=str(post_id);job.accepted_at=utcnow();job.status='BUFFER_ACCEPTED';job.next_attempt_at=utcnow()+timedelta(seconds=30);session.commit()
            result=provider.create(job.payload['text'],persist_intent,persist_accepted)
            job=guard(session,job_id,owner);_apply_result(session,job,result,utcnow());session.commit();return {'status':job.status}
    except Exception as exc:
        with session_factory.begin() as session:
            job=session.get(XNewsJob,job_id)
            if job and job.lease_owner==owner:
                if job.status in TERMINAL:return {'status':job.status}
                job.error_code=getattr(exc,'code','news_operation_failed')
                job.error_message=str(exc)[:500] if type(exc).__module__.startswith('factory.') else 'Haber işlemi tamamlanamadı; sağlayıcı ve yapılandırmayı kontrol edin.'
                delay=int(getattr(exc,'retry_after',0) or 0)
                if job.submit_started_at:
                    if job.buffer_post_id:
                        job.status='BUFFER_PENDING';job.next_attempt_at=utcnow()+timedelta(seconds=max(60,delay))
                        if job.poll_count>=MAX_POLLS:job.status='UNKNOWN_RESULT';job.reconcile_count=2
                    else:job.status='UNKNOWN_RESULT';job.next_attempt_at=utcnow()+timedelta(seconds=120)
                elif delay and job.error_code in ('publication_spacing','publication_in_progress','global_paused'):
                    job.status='READY';job.next_attempt_at=utcnow()+timedelta(seconds=delay)
                else:job.status='SKIPPED'
                return {'status':job.status,'error_code':job.error_code}
        return {'status':'lease_lost'}
    finally:
        with session_factory.begin() as session:
            session.execute(update(XNewsJob).where(XNewsJob.id==job_id,XNewsJob.lease_owner==owner).values(lease_owner=None,lease_until=None))
            session.execute(update(XNewsAccount).where(XNewsAccount.lease_owner==owner).values(lease_owner=None,lease_until=None))


def tick(session_factory,services=None,now=None):
    now=now or utcnow()
    from factory.services.xnews_settings import read_settings
    with session_factory.begin() as session:
        cfg=read_settings(session)
        if not cfg.get('global_enabled',False):return {'status':'disabled','publication_requests':0}
        schedule_due(session,now)
    with session_factory() as session:
        # Existing accepted posts are tracked even if that account is now paused.
        job_id=session.scalar(select(XNewsJob.id).where(XNewsJob.status.in_((*UNSENT,*INFLIGHT,'UNKNOWN_RESULT')),
            XNewsJob.status!='AWAITING_REVIEW',XNewsJob.next_attempt_at<=now,
            or_(XNewsJob.status!='UNKNOWN_RESULT',XNewsJob.reconcile_count<2,
                (XNewsJob.cancel_requested.is_(True) & XNewsJob.cancellation_checked.is_(False) & XNewsJob.buffer_post_id.is_not(None))),
            or_(XNewsJob.lease_until.is_(None),XNewsJob.lease_until<now)).order_by(
                XNewsJob.submit_started_at.is_(None),XNewsJob.next_attempt_at,XNewsJob.created_at).limit(1))
    if job_id:return process_job(session_factory,job_id,services,now)
    return {'status':'idle','publication_requests':0}
