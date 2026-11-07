"""Persistent shared Buffer Free budget; request reservations survive worker crashes.

The limits cover SMF requests across every key/channel on this installation.
Other tools using the same Buffer account are outside this ledger; Buffer remains
authoritative. Keep 10% headroom and never reset the ledger on key rotation.
"""
from datetime import datetime, timedelta, timezone

from sqlalchemy import delete, select

from factory.db import SessionLocal
from factory.models import SystemLog, SystemState
from factory.providers.base import ProviderError

EVENT = "buffer_api_request"
LOCK = "buffer-api-budget"
DAILY_LIMIT = 90
MONTHLY_LIMIT = 2700


def _counts(session, now):
    rows = session.scalars(select(SystemLog.created_at).where(
        SystemLog.event == EVENT, SystemLog.created_at > now - timedelta(days=30)
    ).order_by(SystemLog.created_at)).all()
    daily = [date for date in rows if date > now - timedelta(days=1)]
    return rows, daily


def budget_status():
    with SessionLocal() as session:
        monthly, daily = _counts(session, datetime.now(timezone.utc))
        return {"daily_remaining": max(0, DAILY_LIMIT - len(daily)),
                "monthly_remaining": max(0, MONTHLY_LIMIT - len(monthly)),
                "daily_limit": DAILY_LIMIT, "monthly_limit": MONTHLY_LIMIT}


def reserve_request():
    """Commit the reservation BEFORE the external call. No refunds on timeout."""
    with SessionLocal.begin() as session:
        dialect = session.get_bind().dialect.name
        if dialect == "postgresql":
            from sqlalchemy.dialects.postgresql import insert
        elif dialect == "sqlite":
            from sqlalchemy.dialects.sqlite import insert
        else:
            raise ProviderError("Buffer istek bütçesi için veritabanı desteği gerekli.", "buffer_budget_unavailable")
        session.execute(insert(SystemState).values(id=LOCK, paused=False).on_conflict_do_nothing(index_elements=["id"]))
        session.scalar(select(SystemState).where(SystemState.id == LOCK).with_for_update())
        now = datetime.now(timezone.utc)
        monthly, daily = _counts(session, now)
        waits = []
        if len(daily) >= DAILY_LIMIT:
            waits.append((daily[0] + timedelta(days=1) - now).total_seconds())
        if len(monthly) >= MONTHLY_LIMIT:
            waits.append((monthly[0] + timedelta(days=30) - now).total_seconds())
        if waits:
            raise ProviderError("Buffer ücretsiz istek bütçesi doldu; işlem bekliyor.",
                                "rate_limit", True, max(120, int(max(waits)) + 1))
        session.add(SystemLog(event=EVENT, level="debug", message="Buffer API istek rezervasyonu.", data={}, created_at=now))
        session.execute(delete(SystemLog).where(SystemLog.event == EVENT,
                                               SystemLog.created_at < now - timedelta(days=31)))
