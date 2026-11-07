"""Authenticated, per-account manual caption settings; never creates a post."""
from datetime import datetime, timezone
from uuid import uuid4
from fastapi import APIRouter, Depends, HTTPException, Response
from pydantic import BaseModel, ConfigDict, Field, StrictStr
from sqlalchemy import select
from factory.db import SessionLocal
from factory.models import Account, Connection, SystemLog
from factory.security import current_user, operator
from factory.services.account_captions import (KEY, VERSION, PLATFORMS, config,
    validate_for_account, preview)

router = APIRouter(prefix='/api/control/captions')


class CaptionInput(BaseModel):
    model_config = ConfigDict(extra='forbid')
    text: StrictStr = Field(max_length=4000)
    revision: StrictStr = Field(min_length=1, max_length=64)


def platforms_for(account, links):
    managed = (account.settings or {}).get('managed_platform')
    return [managed] if managed in PLATFORMS else sorted({c.platform for c in links if c.account_id == account.id and c.platform in PLATFORMS}) or ['instagram']


def describe(account, links):
    saved = config(account)
    try:
        sample = preview(account, saved['text'])
        problem = None
    except (ValueError, TypeError):
        sample = {'hashtags': [], 'text': saved['text']}
        problem = 'Hashtag havuzunu kontrol edin.'
    return {'id': account.id, 'name': account.username or account.name,
            'platforms': platforms_for(account, links), **saved,
            'preview': sample, 'hashtag_warning': problem}


@router.get('')
def list_captions(response: Response, user=Depends(current_user)):
    response.headers['Cache-Control'] = 'no-store'
    with SessionLocal() as s:
        accounts = s.scalars(select(Account).order_by(Account.created_at, Account.id)).all()
        links = s.scalars(select(Connection)).all()
        return {'version': VERSION, 'can_edit': user.get('role', '').lower() in ('admin', 'operator'),
                'accounts': [describe(a, links) for a in accounts]}


@router.put('/{account_id}')
def save_caption(account_id: str, data: CaptionInput, response: Response, user=Depends(operator)):
    response.headers['Cache-Control'] = 'no-store'
    with SessionLocal.begin() as s:
        account = s.scalar(select(Account).where(Account.id == account_id).with_for_update())
        if not account:
            raise HTTPException(404, 'Hesap bulunamadı.')
        if config(account)['revision'] != data.revision:
            raise HTTPException(409, 'Bu hesabın açıklaması başka bir oturumda değişti. Metniniz korundu; kayıtlı metni yeniden yükleyin.')
        links = s.scalars(select(Connection).where(Connection.account_id == account_id)).all()
        try:
            text = validate_for_account(data.text, account, platforms_for(account, links))
        except (ValueError, TypeError, UnicodeError) as exc:
            raise HTTPException(422, str(exc)) from None
        saved = {'text': text, 'revision': str(uuid4()), 'updated_at': datetime.now(timezone.utc).isoformat()}
        account.settings = {**(account.settings or {}), KEY: saved}
        s.add(SystemLog(account_id=account.id, event='account.caption.saved', level='info',
                       message='Hesap açıklaması güncellendi.',
                       data={'user_id': user.get('id'), 'enabled': bool(text), 'revision': saved['revision']}))
        return {'saved': True, 'account': describe(account, links)}
