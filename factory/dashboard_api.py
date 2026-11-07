"""Authenticated, side-effect-free dashboard data. Collection is a separate timer."""
from fastapi import APIRouter, Depends, Response
from factory.db import SessionLocal
from factory.security import current_user
from factory.services.dashboard_metrics import VERSION, report

router = APIRouter(prefix='/api/control')

@router.get('/metrics-summary')
def metrics_summary(response: Response, user=Depends(current_user)):
    response.headers["Cache-Control"] = "private, no-store"
    from factory.control_api import overview
    current = overview(user)
    with SessionLocal() as session:
        return report(session, current)

@router.get('/metrics-version')
def metrics_version(user=Depends(current_user)):
    return {'version': VERSION}
