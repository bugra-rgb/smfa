"""GET-only bounded rechecks for transient Instagram container status errors.

Enabled by trusted account IDs in pipeline.py, only for top.sesi and demo.bird.
Never create a replacement container, clear publish_started, or retry publishing.
"""
from datetime import datetime, timezone
import hashlib
import math

from .base import checked, persist

KEY = 'smfa_container_probe_v1'
INTERVAL = 60
MAX_ERROR_CHECKS = 5
WINDOW = 300
REVIEW_WORDS = ('copyright', 'policy', 'restricted', 'permission', 'too many',
    'rate limit', 'quota', 'not allowed', 'ineligible', 'telif', 'ihlal', 'kisit',
    'kısıt', 'unsupported', 'codec', 'aspect ratio', 'invalid format',
    'invalid duration', 'frame rate', 'video too long')


def utc_seconds():
    return datetime.now(timezone.utc).timestamp()


def number(value):
    return type(value) in (int, float) and math.isfinite(value)


def container_status(client, base, headers, cp, save_checkpoint):
    """Return (provider state, defer without POST). Persist evidence before return."""
    now = utc_seconds()
    previous = cp.get(KEY) or {}
    if not isinstance(previous, dict) or previous.get('container_id') != str(cp['container_id']):
        previous = {}
    last = previous.get('checked_at')
    if number(last) and 0 <= now - last < INTERVAL:
        return {}, True
    state = checked(client.get(f"{base}/{cp['container_id']}", headers=headers,
        params={'fields': 'status_code,status'})).json()
    if not isinstance(state, dict):
        return {}, True
    status = state.get('status_code')
    detail = str(state.get('status') or '')
    evidence = {'container_id': str(cp['container_id']), 'checked_at': now,
        'status_code': status if status in ('FINISHED','ERROR','EXPIRED','IN_PROGRESS','PUBLISHED') else 'UNRECOGNIZED',
        'detail_sha256': hashlib.sha256(detail.encode()).hexdigest()}
    defer = False
    if status in ('ERROR', 'EXPIRED'):
        count = previous.get('error_checks', 0)
        count = count if type(count) is int and 0 <= count <= MAX_ERROR_CHECKS else MAX_ERROR_CHECKS
        first = previous.get('first_error_at', now)
        elapsed = now-first if number(first) else WINDOW
        evidence.update(error_checks=count+1, first_error_at=first)
        defer = (not any(word in detail.lower() for word in REVIEW_WORDS)
            and count+1 < MAX_ERROR_CHECKS and 0 <= elapsed < WINDOW)
    # Keep the original error budget through IN_PROGRESS transitions.
    elif status != 'FINISHED':
        for key in ('error_checks', 'first_error_at'):
            if key in previous:
                evidence[key] = previous[key]
    persist(cp, save_checkpoint, **{KEY: evidence})
    return state, defer
