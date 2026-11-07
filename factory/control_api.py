"""Compact authenticated control panel API; no secrets are returned."""
from datetime import datetime, timezone
from uuid import UUID
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from factory.config import settings
from factory.db import SessionLocal
from factory.models import Account, Analytics, Connection, Idea, Job, PublishedPost, PublishJob, SystemState
from factory.security import admin, current_user, operator
from factory.services.control import aggregate_metrics, cycle_at
from factory.services.jobs import enqueue_job
from factory.services.publication_status import publication_status

router = APIRouter(prefix='/api/control')


def x_problems(account, connection, links):
    from factory.providers.x import REQUIRED_SCOPES
    from factory.services.hashtags import valid_pool
    cfg = account.settings or {}
    reasons = []
    if not settings.x_client_id or not settings.x_client_secret:
        reasons.append('X uygulama bilgileri sunucuya eklenmeli')
    if not connection or connection.status.upper() != 'CONNECTED' or not connection.external_id:
        reasons.append('X hesabı bağlanmalı')
    elif not REQUIRED_SCOPES.issubset(set(connection.scopes or [])):
        reasons.append('X video/yayın izinleri için yeniden bağlanmalı')
    if not cfg.get('drive_input_folder_id'):
        reasons.append('Video klasörü seçilmeli')
    if not any(c.platform in ('drive', 'google') and c.status.upper() == 'CONNECTED' for c in links):
        reasons.append('Google Drive bağlanmalı')
    if cfg.get('caption_policy') != 'manual_five_hashtags' or not valid_pool(cfg.get('manual_hashtag_pool')):
        reasons.append('Açıklamasız 5 hashtag havuzu hazırlanmalı')
    if cfg.get('publication_mode') != 'planner' or cfg.get('planner_interval_hours') != 5:
        reasons.append('Yayınlar sayfasında 5 saatlik plan seçilmeli')
    return reasons


@router.get('/x')
def x_overview(user=Depends(current_user)):
    with SessionLocal() as s:
        accounts = s.scalars(select(Account).order_by(Account.created_at)).all()
        links = s.scalars(select(Connection)).all()
        state = s.get(SystemState, 'global')
        result = []
        for a in accounts:
            if (a.settings or {}).get('smfa_archived_v1'): continue
            owned = [c for c in links if c.account_id == a.id]
            c = next((c for c in owned if c.platform == 'x'), None)
            cfg = a.settings or {}
            enabled = bool(c and (c.settings or {}).get('auto_publish_enabled'))
            reasons = x_problems(a, c, owned)
            result.append({'id': a.id, 'username': a.username, 'name': a.name,
                'x_username': (c.settings or {}).get('username') if c else None,
                'connected': bool(c and c.status.upper() == 'CONNECTED'),
                'enabled': enabled, 'active': a.active, 'blockers': reasons,
                'folder': cfg.get('drive_input_folder_id'),
                'next_at': planned_at(a, datetime.now(timezone.utc)) if enabled and a.active and not reasons and not (state and state.paused) else None,
                'hashtags': cfg.get('manual_hashtag_pool', [])})
        posts = s.scalars(select(PublishedPost).where(PublishedPost.platform == 'x').order_by(PublishedPost.published_at.desc()).limit(20)).all()
        queue = s.scalars(select(PublishJob).where(PublishJob.platform == 'x', PublishJob.status != 'PUBLISHED').order_by(PublishJob.scheduled_at.desc()).limit(20)).all()
        return {'configured': bool(settings.x_client_id and settings.x_client_secret),
            'callback_url': settings.public_base_url.rstrip('/') + '/api/oauth/x/callback',
            'paused': bool(state and state.paused), 'accounts': result,
            'posts': [{'id': p.id, 'account_id': p.account_id, 'url': p.url, 'time': p.published_at} for p in posts],
            'queue': [{'id': p.id, 'account_id': p.account_id, 'status': p.status, 'error': p.error_message, 'time': p.scheduled_at} for p in queue]}


class XAutoInput(BaseModel):
    enabled: bool


@router.put('/x/{account_id}/automation')
def x_automation(account_id: str, data: XAutoInput, user=Depends(admin)):
    with SessionLocal.begin() as s:
        a = s.scalar(select(Account).where(Account.id == account_id).with_for_update())
        if not a:
            raise HTTPException(404, 'Hesap bulunamadı.')
        owned = s.scalars(select(Connection).where(Connection.account_id == a.id).with_for_update()).all()
        c = next((c for c in owned if c.platform == 'x'), None)
        if not c:
            raise HTTPException(409, 'Önce X hesabını bağlayın.')
        if data.enabled:
            reasons = x_problems(a, c, owned)
            if reasons:
                raise HTTPException(409, ' · '.join(reasons))
            duplicate = s.scalar(select(Connection.id).where(Connection.platform == 'x',
                Connection.id != c.id, Connection.external_id == c.external_id,
                Connection.settings['auto_publish_enabled'].as_boolean().is_(True)))
            if duplicate:
                raise HTTPException(409, 'Bu X hesabı başka bir kanalda otomatik yayına açık.')
        c.settings = {**(c.settings or {}), 'auto_publish_enabled': data.enabled}
        return {'enabled': data.enabled}

def planned_at(account, now):
    from factory.services.planner import next_slot
    mode=(account.settings or {}).get('publication_mode')
    return next_slot(account,now) if mode else cycle_at(account,now)


def blockers(session, account):
    if (account.settings or {}).get('smfa_archived_v1'): return ['Hesap arşivlendi.']
    if (account.settings or {}).get('managed_platform'):
        from factory.services.channels import problems,tiktok_mode
        links = session.scalars(select(Connection).where(Connection.account_id == account.id)).all()
        reasons = problems(account, links)
        if account.settings.get("managed_platform") == "tiktok" and tiktok_mode(links) != 'buffer_auto':
            reasons.append("TikTok otomatik yayın için Buffer üzerinden bağlanmalı.")
        return reasons
    options = account.settings or {}
    links = session.scalars(select(Connection).where(Connection.account_id == account.id,
        Connection.status.in_(['CONNECTED', 'connected']))).all()
    platforms = {c.platform for c in links}
    problems = []
    if options.get('caption_policy') == 'manual_five_hashtags':
        from factory.services.hashtags import valid_pool
        if not valid_pool(options.get('manual_hashtag_pool')):
            problems.append('En az 5 farklı hashtag kaydedilmeli')
    elif not settings.openai_api_key or not settings.openai_model:
        problems.append('OpenAI ayarı eksik')
    if not options.get('drive_input_folder_id'):
        problems.append('Video klasörü seçilmedi')
    if not platforms.intersection({'drive', 'google'}):
        problems.append('Google Drive bağlanmalı')
    from factory.providers.x import ready
    if not platforms.intersection({'instagram', 'youtube'}) and not any(ready(c) for c in links):
        problems.append('Otomatik yayın yapılabilen hesap bağlanmalı')
    return problems


def platform_settings():
    return [
        {'id': 'instagram', 'name': 'Instagram', 'configured': bool(settings.instagram_client_id and settings.instagram_client_secret), 'mode': 'automatic'},
        {'id': 'youtube', 'name': 'YouTube', 'configured': bool((settings.youtube_client_id or settings.google_client_id) and (settings.youtube_client_secret or settings.google_client_secret)), 'mode': 'automatic', 'note': 'Denetlenmemiş API projelerinde yüklemeler gizli olabilir.'},
        {'id': 'tiktok', 'name': 'TikTok', 'configured': bool(settings.buffer_api_key), 'mode': 'automatic', 'note': 'Buffer Free: toplam 3 kanal; günlük API bütçesiyle otomatik yayın.'},
        {'id': 'x', 'name': 'X', 'configured': bool(settings.x_client_id and settings.x_client_secret), 'mode': 'automatic', 'note': 'Resmî X girişi; bağlantıdan sonra otomatik yayın ayrıca açılır.'},
        {'id': 'facebook', 'name': 'Facebook Reels', 'configured': bool(settings.facebook_client_id and settings.facebook_client_secret and settings.graph_api_version), 'mode': 'automatic', 'note': 'Facebook Sayfası seçilir; kişisel profile yayın yapılmaz.'},
        {'id': 'drive', 'name': 'Google Drive', 'configured': bool(settings.google_client_id and settings.google_client_secret), 'mode': 'source'},
    ]


@router.get('/overview')
def overview(user=Depends(current_user)):
    with SessionLocal() as s:
        accounts = s.scalars(select(Account).order_by(Account.created_at, Account.id)).all()
        links = s.scalars(select(Connection)).all()
        # Last cumulative snapshot per post, not sum of every polling sample.
        from factory.services.latest_metrics import latest_metrics
        metrics = [metric for metric, _connection_id in latest_metrics(s)]
        state = s.get(SystemState, 'global')
        result = []
        for a in accounts:
            if (a.settings or {}).get('smfa_archived_v1'): continue
            options = a.settings or {}; plan = options.get('connection_plan') or {}
            reasons = blockers(s, a)
            next_plan = planned_at(a, datetime.now(timezone.utc))
            publication = publication_status(s, a, blockers=reasons, paused=bool(state and state.paused),
                planned_at=next_plan, manual=options.get('managed_platform') == 'tiktok' and
                not any(c.platform=='tiktok' and (c.settings or {}).get('publish_via')=='buffer' for c in links if c.account_id==a.id))
            result.append({'id': a.id, 'name': a.name, 'username': a.username, 'active': a.active,
                'niche': a.niche, 'tone': a.tone, 'settings': options,
                'folder_id': options.get('drive_input_folder_id') or plan.get('folder_id'),
                'connections': [{'id': c.id, 'platform': c.platform, 'status': c.status,
                    'username': (c.settings or {}).get('username')} for c in links if c.account_id == a.id],
                **publication, 'planned_at': next_plan,
                'blockers': reasons, 'performance': aggregate_metrics([m for m in metrics if m.account_id == a.id])})
        result.sort(key=lambda a: (a['settings'].get('cycle_index', 999), a['username']))
        return {'version': 4, 'capabilities': {'independent_channels': True, 'publication_status': True},
            'oauth_origin': settings.public_base_url, 'accounts': result, 'platforms': platform_settings(), 'paused': bool(state and state.paused),
            'performance': aggregate_metrics(metrics), 'tracked_posts': s.scalar(select(func.count(PublishedPost.id))),
            'cycle': {'hours': None, 'gap_minutes': 2, 'timezone': 'Europe/Istanbul', 'times': ['09:00', '15:00', '21:00', '03:00']}}


class IdeasInput(BaseModel):
    account_id: str
    brief: str = Field(default='', max_length=2000)
    request_id: UUID


@router.post('/ideas', status_code=202)
def ideas(data: IdeasInput, user=Depends(operator)):
    if settings.mode != 'mock' and not (settings.openai_api_key and settings.openai_model):
        raise HTTPException(409, 'OpenAI ayarı eksik.')
    with SessionLocal.begin() as s:
        state = s.get(SystemState, 'global')
        if state and state.paused: raise HTTPException(409, 'Fikir üretmek için sistemi devam ettirin; hesap yayınları duraklatılmış kalabilir.')
        account = s.scalar(select(Account).where(Account.id == data.account_id).with_for_update())
        if not account: raise HTTPException(404, 'Hesap bulunamadı.')
        if (account.settings or {}).get('ai_disabled'):
            raise HTTPException(409, 'Bu hesapta AI kapalı; hazır hashtagler kullanılıyor.')
        pending = s.scalar(select(Job).where(Job.kind == 'AI_IDEAS', Job.payload['account_id'].as_string() == account.id,
            Job.status.in_(['QUEUED', 'RUNNING', 'RETRYING'])).limit(1))
        job = pending or enqueue_job(s, 'AI_IDEAS', account_id=account.id, payload={'account_id': account.id, 'brief': data.brief},
            idempotency_key=f'ideas:{account.id}:{data.request_id}', max_attempts=3, priority=30)
        return {'job_id': job.id, 'status': job.status}


class SimpleAccountInput(BaseModel):
    username: str = Field(default='', max_length=100)
    niche: str = Field(default='Karaktere özel özgün içerikler', min_length=1, max_length=500)


@router.post('/accounts', status_code=201)
def new_account(data: SimpleAccountInput, user=Depends(admin)):
    from factory.schemas import validate_account_settings
    with SessionLocal.begin() as s:
        gate = s.scalar(select(SystemState).where(SystemState.id == 'cycle-config').with_for_update())
        if gate is None: raise HTTPException(409, 'Sunucu güncellemesi tamamlanmalı.')
        username = data.username.strip().lstrip('@')
        if username:
            old = s.scalar(select(Account).where(func.lower(Account.username).in_([username.lower(), '@'+username.lower()])))
            if old: return {'id': old.id, 'existing': True}
        rows = s.scalars(select(Account)).all()
        index = max([int((a.settings or {}).get('cycle_index', -1)) for a in rows] + [-1]) + 1
        from factory.services.hashtags import pool_for
        options = validate_account_settings({'cycle_v2': True, 'cycle_index': index,
            'publication_mode': 'planner', 'planner_interval_hours': 5,
            'planner_anchor': datetime.now(timezone.utc).isoformat(), 'planner_offset': index * 2,
            'caption_policy': 'manual_five_hashtags', 'manual_hashtag_pool': pool_for(username), 'ai_disabled': True, 'auto_discover': not bool(username),
            'inventory_min': 1, 'inventory_target': 2, 'inventory_max': 3, 'duration': 600, 'width': 720, 'height': 1280,
            'subtitle': {'enabled': False}, 'voice': {'replace_source': False},
            'optimize_schedule': False, 'optimize_format': False, 'optimize_duration': False})
        row = Account(name=username or 'Yeni hesap', username=username, niche=data.niche, active=False,
            daily_target=5, auto_publish=True, approval_mode='AUTO_PUBLISH', source_mode='DRIVE_ONLY', settings=options)
        s.add(row);s.flush()
        return {'id': row.id, 'existing': False}


@router.post('/accounts/{account_id}/start')
def start(account_id: str, user=Depends(operator)):
    with SessionLocal.begin() as s:
        a = s.scalar(select(Account).where(Account.id == account_id).with_for_update())
        if not a: raise HTTPException(404, 'Hesap bulunamadı.')
        reasons = blockers(s, a)
        if reasons: raise HTTPException(409, ' · '.join(reasons))
        a.active = True
        return {'active': True}


class FolderInput(BaseModel):
    folder: str = Field(max_length=500)
    niche: str = Field(min_length=1, max_length=500)
    tone: str = Field(default='', max_length=200)


@router.put('/accounts/{account_id}/source')
def source(account_id: str, data: FolderInput, user=Depends(operator)):
    from factory.schemas import validate_account_settings
    with SessionLocal.begin() as s:
        a = s.scalar(select(Account).where(Account.id == account_id).with_for_update())
        if not a: raise HTTPException(404, 'Hesap bulunamadı.')
        if a.active: raise HTTPException(409, 'Kaynak değişikliği için hesabı önce duraklatın.')
        try: a.settings = validate_account_settings({**a.settings, 'drive_input_folder_id': data.folder})
        except ValueError as exc: raise HTTPException(422, str(exc)) from None
        a.niche, a.tone = data.niche, data.tone
        return {'saved': True}

@router.get('/posts')
def posts(user=Depends(current_user)):
    with SessionLocal() as s:
        return [{'id': p.id, 'account_id': p.account_id, 'platform': p.platform, 'published_at': p.published_at, 'url': p.url}
            for p in s.scalars(select(PublishedPost).order_by(PublishedPost.published_at.desc()).limit(100))]

class BatchInput(BaseModel):
    account_ids: list[UUID] = Field(min_length=1, max_length=180)
    request_id: UUID


class PlannerInput(BaseModel):
    account_ids: list[UUID] = Field(min_length=1, max_length=180)
    times: list[str] = Field(default_factory=lambda:['09:00','15:00','21:00','03:00'], max_length=12)
    enabled: bool = True
    interval_hours: int = Field(default=0, ge=0, le=168, strict=True)


def selected_accounts(s, ids):
    unique=list(dict.fromkeys(str(i) for i in ids))
    gate=s.scalar(select(SystemState).where(SystemState.id=='cycle-config').with_for_update())
    if not gate: raise HTTPException(409,'Sunucu güncellemesi tamamlanmalı.')
    rows=s.scalars(select(Account).where(Account.id.in_(unique)).order_by(Account.id).with_for_update()).all()
    by_id={a.id:a for a in rows}
    if len(by_id)!=len(unique): raise HTTPException(404,'Seçilen hesaplardan biri bulunamadı.')
    return [by_id[i] for i in unique]


@router.post('/publish-selected',status_code=202)
def publish_selected(data: BatchInput,user=Depends(operator)):
    from datetime import timedelta
    from factory.services.planner import make_request,open_request,describe,promote_plan,KIND
    from factory.services.cadence import pending_video
    now=datetime.now(timezone.utc)
    with SessionLocal.begin() as s:
        accounts=selected_accounts(s,data.account_ids)
        ids=[a.id for a in accounts];batch=str(data.request_id)
        previous=s.scalar(select(Job).where(Job.kind==KIND,Job.payload['batch_id'].as_string()==batch).limit(1))
        if previous and previous.payload.get('selection')!=ids:
            raise HTTPException(409,'Bu işlem kimliği başka bir hesap seçimiyle kullanılmış.')
        state=s.get(SystemState,'global')
        if state and state.paused: raise HTTPException(409,'Sistem duraklatılmış. Önce sistemi devam ettirin.')
        accepted=[];skipped=[]
        for position,a in enumerate(accounts):
            key=f'manual:{batch}:{a.id}'
            old=s.scalar(select(Job).where(Job.idempotency_key==key))
            if old: accepted.append(describe(s,old));continue
            reasons=blockers(s,a)
            pending=open_request(s,a.id)
            if not reasons and pending:
                promoted=promote_plan(s,pending,key,now+timedelta(minutes=2*len(accepted)),
                    batch_id=batch,position=position,selection=ids)
                if promoted:
                    a.active=True;accepted.append(describe(s,promoted));continue
            if pending or pending_video(s,a.id): reasons.append('Önceki yayın/talep tamamlanmalı; yeni kopya eklenmedi')
            if reasons:
                skipped.append({'account_id':a.id,'reasons':reasons});continue
            a.active=True;a.auto_publish=True;a.approval_mode='AUTO_PUBLISH';a.source_mode='DRIVE_ONLY'
            a.daily_target=max(1,a.daily_target)
            # A click does not turn on an unrequested recurring cycle.
            a.settings={**a.settings,'publication_mode':a.settings.get('publication_mode','manual'),
                'selection_policy':'random_unused','cycle_v2':True}
            request=make_request(s,a,key,now+timedelta(minutes=2*len(accepted)),'manual',
                batch_id=batch,position=position,selection=ids)
            accepted.append(describe(s,request))
        return {'accepted':accepted,'skipped':skipped,'gap_minutes':2}


@router.put('/planner')
def save_planner(data: PlannerInput,user=Depends(operator)):
    import re
    from factory.services.planner import KIND,OPEN,next_slot,open_request,promote_plan
    from factory.services.cadence import pending_video
    times=sorted(set(data.times)) if not data.interval_hours else []
    if data.enabled and not data.interval_hours and (not times or len(times)!=len(data.times) or any(not re.fullmatch(r'(?:[01]\d|2[0-3]):[0-5]\d',t) for t in times)):
        raise HTTPException(422,'Benzersiz saatler girin (HH:MM).')
    with SessionLocal.begin() as s:
        accounts=selected_accounts(s,data.account_ids)
        if data.enabled and data.interval_hours and data.interval_hours*60 < 2*len(accounts):
            raise HTTPException(422,'Yayın aralığı hesap sırasına yetecek kadar uzun olmalı.')
        # Require enough space for the entire selected sequence, including midnight.
        minutes=[int(t[:2])*60+int(t[3:]) for t in times] if data.enabled else []
        if len(minutes)>1 and min((minutes[(i+1)%len(minutes)]-v)%1440 for i,v in enumerate(minutes))<2*len(accounts):
            raise HTTPException(422,'Saatler arasında seçili hesapların 2 dakikalık sırasına yetecek boşluk olmalı.')
        result=[];now=datetime.now(timezone.utc)
        for i,a in enumerate(accounts):
            current=open_request(s,a.id)
            if data.enabled and pending_video(s,a.id) and not current:
                raise HTTPException(409,f'@{a.username}: mevcut eski yayın tamamlanmalı.')
            if current and current.payload.get('mode')!='planner' and data.enabled:
                raise HTTPException(409,f'@{a.username}: tek seferlik yayın önce tamamlanmalı.')
            a.settings={**a.settings,'publication_mode':'planner' if data.enabled else 'manual',
                'planner_times':times,'planner_offset':i*2,'planner_interval_hours':data.interval_hours,
                'planner_anchor':now.isoformat(),'selection_policy':'random_unused','cycle_v2':True}
            a.timezone='Europe/Istanbul';a.auto_publish=True;a.approval_mode='AUTO_PUBLISH';a.source_mode='DRIVE_ONLY'
            a.daily_target=max(1, (24+data.interval_hours-1)//data.interval_hours) if data.interval_hours else max(1,len(times))
            if current and data.enabled:
                due=next_slot(a,now);key=f'planner:{a.id}:{due.isoformat()}'
                duplicate=s.scalar(select(Job.id).where(Job.idempotency_key==key,Job.id!=current.id))
                if duplicate or not promote_plan(s,current,key,due,mode='planner'):
                    raise HTTPException(409,f'@{a.username}: başlamış yayın tamamlanmadan saati değiştirilemez.')
            elif current and current.status=='WAITING_CONTENT': current.status='CANCELLED'
            reasons=blockers(s,a)
            a.active=data.enabled and not reasons
            result.append({'account_id':a.id,'active':a.active,'blockers':reasons,'next_at':next_slot(a,now)})
        return {'accounts':result,'timezone':'Europe/Istanbul','gap_minutes':2}


@router.get('/requests')
def requests(user=Depends(current_user)):
    from factory.services.planner import KIND,describe
    with SessionLocal.begin() as s:
        return [describe(s,r) for r in s.scalars(select(Job).where(Job.kind==KIND).order_by(Job.created_at.desc()).limit(180))]


@router.post('/requests/{request_id}/cancel')
def cancel_request(request_id:str,user=Depends(operator)):
    from factory.services.planner import KIND
    with SessionLocal.begin() as s:
        request=s.get(Job,request_id)
        if not request or request.kind!=KIND: raise HTTPException(404,'Talep bulunamadı.')
        a=s.scalar(select(Account).where(Account.id==request.account_id).with_for_update())
        s.refresh(request)
        if request.status!='WAITING_CONTENT': raise HTTPException(409,'Hazırlanmış yayın için yayın kuyruğundaki iptal işlemini kullanın.')
        request.status='CANCELLED'
        return {'cancelled':True}
