"""Durable manual requests and daily wall-clock plans. No new database schema."""
from datetime import datetime, timedelta, timezone
import math
from zoneinfo import ZoneInfo
from sqlalchemy import select, func
from factory.models import Account, Connection, Job, PublishJob, PublishedPost, Video
from factory.services.jobs import enqueue_job
from factory.services.cadence import pending_video

KIND = 'PUBLICATION_REQUEST'
OPEN = ('WAITING_CONTENT', 'TRACKING')
TERMINAL = ('PUBLISHED', 'CANCELLED', 'SKIPPED', 'BLOCKED')


def next_slot(account, now):
    cfg = account.settings or {}
    if cfg.get('publication_mode') != 'planner':
        return None
    hours = cfg.get('planner_interval_hours', 0)
    if hours:
        anchor = datetime.fromisoformat(cfg['planner_anchor']).astimezone(timezone.utc)
        base = anchor + timedelta(minutes=cfg.get('planner_offset', 0))
        step = timedelta(hours=hours)
        return base + max(0, math.ceil((now-base)/step)) * step
    zone = ZoneInfo(account.timezone)
    local = now.astimezone(zone)
    candidates = []
    for day in range(-1, 3):
        date = (local + timedelta(days=day)).date()
        for clock in cfg.get('planner_times', []):
            hour, minute = map(int, clock.split(':'))
            dt = datetime(date.year, date.month, date.day, hour, minute, tzinfo=zone)
            dt = dt.astimezone(timezone.utc) + timedelta(minutes=cfg.get('planner_offset', 0))
            if dt >= now: candidates.append(dt)
    return min(candidates) if candidates else None


def refresh_request(session, request):
    if request.status != 'TRACKING': return
    pubs = session.scalars(select(PublishJob).where(PublishJob.id.in_(request.result.get('publication_ids', [])))).all()
    if not pubs: return
    states = {p.status for p in pubs}
    if states == {'PUBLISHED'}: request.status = 'PUBLISHED'
    elif states <= {'CANCELLED', 'PUBLISHED'}: request.status = 'CANCELLED'
    elif states.intersection({'UNKNOWN','BLOCKED','POLICY_BLOCKED','FAILED'}):
        request.status = 'BLOCKED'
        request.error_message = 'Önceki yayının sonucu kontrol edilmeli; tekrar gönderilmedi.'


def open_request(session, account_id):
    requests = session.scalars(select(Job).where(Job.account_id == account_id, Job.kind == KIND,
        Job.status.in_(OPEN)).order_by(Job.created_at)).all()
    for request in requests:
        refresh_request(session, request)
        if request.status in OPEN: return request
    return None


def make_request(session, account, key, due, mode, **payload):
    old = session.scalar(select(Job).where(Job.idempotency_key == key))
    if old: return old
    from factory.services.resilient_hourly import enabled, FLAG
    if enabled(account): payload = {**payload, FLAG: True}
    request = Job(account_id=account.id, kind=KIND, status='WAITING_CONTENT', idempotency_key=key,
        run_after=due, payload={'mode':mode, 'expires_at':(due+timedelta(minutes=30) if mode=='manual' else due+timedelta(minutes=10)).isoformat(), **payload}, result={})
    session.add(request);session.flush()
    return request


def reserve(session, account, now):
    if (account.settings or {}).get('managed_platform') == 'tiktok':
        from factory.providers.buffer import ready as buffer_ready
        connection=session.scalar(select(Connection).where(Connection.account_id==account.id,Connection.platform=='tiktok'))
        if not connection or not buffer_ready(connection):return 0
    request = open_request(session, account.id)
    if request and request.status == 'TRACKING': return 0
    if pending_video(session, account.id): return 0
    cfg = account.settings or {}
    if not request and cfg.get('publication_mode') == 'planner':
        # Reserve only the next future slot; downtime never causes catch-up bursts.
        from factory.services.resilient_hourly import enabled, next_due
        due = next_due(session, account, now) if enabled(account) else next_slot(account, now)
        if due is None: return 0
        request = make_request(session, account, f'planner:{account.id}:{due.isoformat()}', due, 'planner')
        if request.status != 'WAITING_CONTENT': return 0
    if not request: return 0
    if now > datetime.fromisoformat(request.payload['expires_at']):
        from factory.services.resilient_hourly import renewable, extend_window
        if renewable(account, request):
            extend_window(request, now)
        else:
            request.status='SKIPPED';request.error_message='Zaman aralığında uygun içerik hazırlanamadı.'
            return 0
    connections = session.scalars(select(Connection).where(Connection.account_id==account.id,
        Connection.platform.in_(['instagram','youtube','x','facebook','tiktok']), Connection.status.in_(['CONNECTED','connected']))).all()
    from factory.providers.x import ready
    connections = [c for c in connections if c.platform != "x" or ready(c)]
    from factory.providers.buffer import ready as buffer_ready
    connections = [c for c in connections if c.platform != 'tiktok' or
                   ((account.settings or {}).get('managed_platform')=='tiktok' and buffer_ready(c))]
    from factory.services.channels import eligible
    connections = [c for c in connections if eligible(account, c)]
    if not connections: return 0
    from factory.services.drive_repeat import ready_order
    video = session.scalar(select(Video).where(Video.account_id==account.id, Video.source_type=='DRIVE',
        Video.status.in_(['READY','AWAITING_APPROVAL']), Video.qa['passed'].as_boolean().is_(True),
        ~Video.id.in_(select(PublishJob.video_id)), ~Video.id.in_(select(PublishedPost.video_id)))
        .order_by(ready_order(account), func.random()).limit(1).with_for_update(skip_locked=True))
    if not video: return 0
    # User explicitly approved unattended Drive publication; QA is still mandatory.
    video.metadata_json = {**(video.metadata_json or {}), 'approved': True}
    ids=[]
    for c in connections:
        key=f'publish:{video.id}:{c.id}'
        pub=PublishJob(account_id=account.id,video_id=video.id,connection_id=c.id,platform=c.platform,
            status='SCHEDULED',scheduled_at=max(now,request.run_after),idempotency_key=key,checkpoint={})
        session.add(pub);session.flush();ids.append(pub.id)
        enqueue_job(session,'PUBLISH',account_id=account.id,video_id=video.id,idempotency_key=key,
            payload={'publish_job_id':pub.id,'publication_request_id':request.id},priority=100,run_after=pub.scheduled_at)
    video.status='SCHEDULED';request.video_id=video.id;request.status='TRACKING'
    request.result={'publication_ids':ids}
    return 1


def enforce_request(ctx, pub, now):
    """Continue checkpointed operations, but never start an expired scheduled post."""
    from factory.services.pipeline import reveal_checkpoint
    from factory.services.jobs import RetryLater
    req = ctx.session.get(Job, ctx.job.payload['publication_request_id'])
    if not req: raise RuntimeError('Yayın talebi bulunamadı.')
    cp=reveal_checkpoint(pub.checkpoint)
    if any(cp.get(k) for k in ('container_id','upload_url','session_url','initiation_started','remote_id','publish_started','create_started','media_id')):
        return True
    from factory.services.resilient_hourly import renewable, extend_window
    account = ctx.session.get(Account, pub.account_id)
    if (account and renewable(account, req)
            and now > datetime.fromisoformat(req.payload['expires_at'])):
        extend_window(req, now)
    if req.status == 'CANCELLED' or now > datetime.fromisoformat(req.payload['expires_at']):
        pub.status='CANCELLED';req.status='SKIPPED' if req.status!='CANCELLED' else 'CANCELLED'
        pub.error_message='Planın süresi doldu veya talep iptal edildi.'
        ctx.session.commit();return False
    # A batch proceeds in selected account order even if one video takes longer.
    batch=req.payload.get('batch_id')
    if batch:
        earlier=ctx.session.scalars(select(Job).where(Job.kind==KIND,
            Job.payload['batch_id'].as_string()==batch,
            Job.payload['position'].as_integer()<req.payload['position'])).all()
        for prior in earlier:
            refresh_request(ctx.session,prior)
            if prior.status in OPEN:
                prior_account = ctx.session.get(Account, prior.account_id)
                keep_waiting = bool(prior_account and renewable(prior_account, prior))
                if prior.status=='WAITING_CONTENT' and now>datetime.fromisoformat(prior.payload['expires_at']) and not keep_waiting:
                    prior.status='SKIPPED'
                else:
                    ctx.session.commit();raise RetryLater('Önceki seçili hesabın yayın sonucu bekleniyor.',30)
        ctx.session.commit()
    return True


def describe(session, request):
    refresh_request(session,request)
    return {'id':request.id,'account_id':request.account_id,'status':request.status,
        'mode':request.payload.get('mode'),'planned_at':request.run_after,
        'video_id':request.video_id,'error':request.error_message}


def promote_plan(session, request, key, due, mode="manual", **payload):
    """A manual click may bring forward one unstarted future planned video."""
    if request.payload.get('mode')!='planner': return None
    pubs=session.scalars(select(PublishJob).where(PublishJob.id.in_(request.result.get('publication_ids',[])))
        .order_by(PublishJob.id).with_for_update().execution_options(populate_existing=True)).all()
    work=session.scalars(select(Job).where(Job.kind=='PUBLISH',Job.payload['publication_request_id'].as_string()==request.id)
        .order_by(Job.id).with_for_update().execution_options(populate_existing=True)).all()
    if any(p.status!='SCHEDULED' or p.attempts or p.checkpoint for p in pubs):return None
    if any(j.status!='QUEUED' or j.lease_owner or j.result.get('cycle_admitted') for j in work):return None
    request.idempotency_key=key;request.run_after=due
    request.payload={'mode':mode,'expires_at':(due+timedelta(minutes=30 if mode=='manual' else 10)).isoformat(),**payload}
    for pub in pubs:pub.scheduled_at=due
    for job in work:job.run_after=due
    return request
