"""Authenticated pool-only editing; no account, provider or schedule mutations."""
from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import HTMLResponse, Response
from pydantic import BaseModel, Field, ConfigDict
from sqlalchemy import select
from factory.db import SessionLocal
from factory.models import Account, Connection, SystemLog
from factory.security import current_user, operator
from factory.services.hashtags import pool_for
from factory.services.hashtag_policy import FIXED, VERSION, make_copy, choose, topical, validate, revision

router=APIRouter(prefix='/api/control/hashtags')

class PoolInput(BaseModel):
    model_config=ConfigDict(extra='forbid')
    tags:list[str]=Field(min_length=3,max_length=100)
    expected_revision:str=Field(pattern=r'^[a-f0-9]{64}$')

def account_pool(a):
    return (a.settings or {}).get('manual_hashtag_pool') or pool_for(a.username or a.name)

def describe(a,links):
    pool=account_pool(a);error=None;sample=None
    try:
        tags=topical(pool);sample=make_copy(choose(pool,a.id,'panel-preview'))['caption']
    except ValueError as exc:
        tags=[t for t in pool if isinstance(t,str)] if isinstance(pool,list) else []
        error=str(exc)
    cfg=a.settings or {};platform=cfg.get('managed_platform')
    platforms=[platform] if platform else sorted({c.platform for c in links if c.account_id==a.id and c.platform in ('instagram','youtube','tiktok','x','facebook')})
    return {'id':a.id,'name':a.name,'username':a.username,'platforms':platforms or ['instagram'],
        'active':a.active,'auto_publish':a.auto_publish,'tags':tags,'fixed_tags':list(FIXED),
        'revision':revision(pool),'preview':sample,'error':error,'policy':VERSION}

@router.get('/accounts')
def accounts(user=Depends(current_user)):
    with SessionLocal() as s:
        links=s.scalars(select(Connection)).all()
        return {'fixed_tags':list(FIXED),'count_per_post':5,'topic_count_per_post':3,
            'can_edit':user.get('role','').lower() in ('admin','operator'),
            'accounts':[describe(a,links) for a in s.scalars(select(Account).order_by(Account.name,Account.id))]}

@router.put('/accounts/{account_id}')
def save(account_id:str,data:PoolInput,user=Depends(operator)):
    try:
        tags=validate(data.tags)
        if len(tags)>98:raise ValueError('En fazla 98 konu etiketi kaydedilebilir.')
    except ValueError as exc:raise HTTPException(422,str(exc)) from None
    with SessionLocal.begin() as s:
        a=s.scalar(select(Account).where(Account.id==account_id).with_for_update())
        if not a:raise HTTPException(404,'Hesap bulunamadı.')
        before=account_pool(a)
        if revision(before)!=data.expected_revision:
            raise HTTPException(409,'Havuz başka bir pencerede değişti. Hesabı yeniden yükleyip değişikliğini tekrar uygula.')
        # Retain the legacy five-tag minimum for existing platform edit forms.
        stored=tags+list(FIXED)
        if before!=stored:
            a.settings={**(a.settings or {}),'manual_hashtag_pool':stored}
            s.add(SystemLog(account_id=a.id,event='hashtags.pool.updated',level='INFO',
                message='Hesabın hashtag havuzu güncellendi.',data={'user_id':user.get('id'),
                'before_revision':revision(before),'after_revision':revision(stored),'topic_count':len(tags)}))
        s.flush()
        links=s.scalars(select(Connection).where(Connection.account_id==a.id)).all()
        return {'saved':True,'account':describe(a,links)}

@router.get('/panel',response_class=HTMLResponse)
def panel():
    # Public shell only; account data and changes use authenticated endpoints.
    from factory.hashtag_panel import HTML
    return HTMLResponse(HTML)

@router.get('/panel.js')
def script():
    from factory.hashtag_panel import JS
    return Response(JS,media_type='application/javascript')

@router.get('/panel.css')
def css():
    from factory.hashtag_panel import CSS
    return Response(CSS,media_type='text/css')
