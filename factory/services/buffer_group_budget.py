"""One conservative request ledger for both platforms of the new Buffer group.
Free plan, checked 2026-09-28: https://developers.buffer.com/guides/api-limits.html
Old groups retain their existing ledgers. Rotation does not reset counters.
"""
import hashlib,re
from datetime import datetime,timedelta,timezone
from sqlalchemy import select
from factory.db import SessionLocal
from factory.models import SystemLog,SystemState
from factory.providers.base import ProviderError
EVENT='buffer_mixed_request_v1'
LIMITS=((900,90),(86400,225),(2592000,2700))
def reserve_request(account_id):
    if not isinstance(account_id,str) or not re.fullmatch(r'[A-Za-z0-9_-]{8,100}',account_id):
        raise ProviderError('Buffer hesap kimliği doğrulanmalı.','buffer_identity')
    with SessionLocal.begin() as s:
        dialect=s.get_bind().dialect.name
        if dialect=='postgresql':
            from sqlalchemy.dialects.postgresql import insert
        elif dialect=='sqlite':
            from sqlalchemy.dialects.sqlite import insert
        else:raise ProviderError('İstek sayacı veritabanı desteklenmiyor.','configuration_missing')
        lock='bg:'+hashlib.sha256(account_id.encode()).hexdigest()[:30]
        s.execute(insert(SystemState).values(id=lock,paused=False).on_conflict_do_nothing(index_elements=['id']))
        s.scalar(select(SystemState).where(SystemState.id==lock).with_for_update())
        now=datetime.now(timezone.utc)
        times=s.scalars(select(SystemLog.created_at).where(SystemLog.event==EVENT,
            SystemLog.data['buffer_account_id'].as_string()==account_id,
            SystemLog.created_at>now-timedelta(days=30)).order_by(SystemLog.created_at)).all()
        waits=[]
        for seconds,limit in LIMITS:
            recent=[dt for dt in times if dt>now-timedelta(seconds=seconds)]
            if len(recent)>=limit:waits.append((recent[0]+timedelta(seconds=seconds)-now).total_seconds())
        if waits:raise ProviderError('Buffer grubunun istek bütçesi doldu; süre bekleniyor.','rate_limit',True,max(120,int(max(waits))+1))
        s.add(SystemLog(event=EVENT,level='debug',message='Buffer grup istek rezervasyonu.',
            data={'buffer_account_id':account_id},created_at=now))
