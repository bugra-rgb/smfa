"""Read-only provider analytics, bounded daily collection; never publishes.

Buffer: documented aggregatedPostMetrics, normalized metrics, 30-day post cohort.
Instagram: own profile followers_count with the existing Instagram Login token.
Only sanitized values are persisted, under a shared-volume atomic cache.
"""
from __future__ import annotations
import argparse
from datetime import datetime, timedelta, timezone
import hashlib
import json
import math
from pathlib import Path
import os
import re
import tempfile

VERSION = 'smfa-analytics-20260929.1'
TTL = 24 * 3600
RETRY_TTL = 6 * 3600
PLATFORMS = ('instagram', 'youtube', 'tiktok', 'facebook', 'x')
NAMES = ('views', 'likes', 'comments', 'shares')


def number(value):
    return value if type(value) in (int, float) and math.isfinite(value) and value >= 0 else None


def timestamp(value):
    if not isinstance(value, str): return None
    try:
        dt = datetime.fromisoformat(value.replace('Z', '+00:00'))
        return dt.astimezone(timezone.utc) if dt.tzinfo else None
    except ValueError: return None


def window(now=None):
    now = now or datetime.now(timezone.utc)
    end = now.replace(hour=0, minute=0, second=0, microsecond=0)
    return end - timedelta(days=29), end


def normalize_aggregate(data):
    values = {}
    for item in data.get('metrics') or []:
        if not isinstance(item, dict) or item.get('unit') != 'count': continue
        if item.get('type') in ('views', 'reactions', 'likes', 'comments', 'shares', 'reposts', 'postCount', 'impressions'):
            value = number(item.get('value'))
            if value is not None: values[item['type']] = value
    updated = timestamp(data.get('metricsUpdatedAt'))
    count = values.get('postCount')
    # Buffer's baseline zeroes are not evidence of a measured post.
    measured = updated is not None and count is not None and count > 0
    return {'status': 'ok' if measured else 'awaiting_provider',
            'totals': {'views': values.get('views') if measured else None,
                       'likes': values.get('likes', values.get('reactions')) if measured else None,
                       'comments': values.get('comments') if measured else None,
                       'shares': values.get('shares', values.get('reposts')) if measured else None},
            'posts': count, 'updated_at': updated.isoformat() if updated else None}


def identity(c):
    cfg = c.settings or {}
    return {'connection_id': c.id, 'account_id': c.account_id, 'platform': c.platform,
            'external_id': c.external_id, 'buffer_account_id': cfg.get('buffer_account_id'),
            'buffer_organization_id': cfg.get('buffer_organization_id'),
            'buffer_channel_id': cfg.get('buffer_channel_id'), 'publish_via': cfg.get('publish_via', 'direct')}


def cache_path():
    from factory.config import settings
    return Path(settings.data_dir) / 'smfa-analytics-v1' / 'cache.json'


def read_cache():
    p = cache_path()
    try:
        if p.stat().st_size > 8 * 1024 * 1024: return {}
        value = json.loads(p.read_text())
        return value if isinstance(value, dict) and value.get('version') == VERSION else {}
    except (OSError, ValueError): return {}


def save_cache(value):
    p = cache_path()
    p.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, tmp = tempfile.mkstemp(prefix='.metrics-', dir=p.parent)
    try:
        with os.fdopen(fd, 'w') as out:
            os.chmod(tmp, 0o600)
            json.dump(value, out, ensure_ascii=False, allow_nan=False)
        os.replace(tmp, p)
    finally:
        if os.path.exists(tmp): os.unlink(tmp)


def due(entry, now):
    at = timestamp((entry or {}).get('attempted_at'))
    wait = TTL if (entry or {}).get('status') == 'ok' else RETRY_TTL
    return at is None or now - at >= timedelta(seconds=wait)


def reserve(creds, platform):
    if creds.get('smfa_mixed_buffer_budget_v1') is True or platform == 'instagram':
        from factory.services.buffer_group_budget import reserve_request
        reserve_request(creds['buffer_account_id']); return
    from factory.services.buffer_budget import reserve_request
    reserve_request()


def group_query(channels, start, end):
    variables = {'org': channels[0]['identity']['buffer_organization_id'],
                 'start': start.isoformat(), 'end': end.isoformat()}
    declarations = ['$org: OrganizationId!', '$start: DateTime!', '$end: DateTime!']
    fields = ['account { id }']
    for i, row in enumerate(channels):
        declarations.append('$c%d: ChannelId!' % i)
        variables['c%d' % i] = row['identity']['buffer_channel_id']
        fields.append('c%d: channel(input:{id:$c%d}) { id service serviceId organizationId }' % (i, i))
        fields.append('m%d: aggregatedPostMetrics(input:{organizationId:$org,channelIds:[$c%d],startDateTime:$start,endDateTime:$end}) { metrics { type value unit } metricsUpdatedAt }' % (i, i))
    return 'query SMFADashboardMetrics(' + ','.join(declarations) + ') {' + ' '.join(fields) + '}', variables


def safe_code(exc):
    code = getattr(exc, 'code', '')
    return code if code in ('rate_limit', 'oauth_expired', 'oauth_missing', 'buffer_identity', 'buffer_graphql_error') else 'request_failed'


def collect():
    try:
        import fcntl
    except ImportError:
        raise RuntimeError('The analytics collector requires Linux; use the Docker deployment.') from None
    import httpx
    from sqlalchemy import select
    from factory.config import settings
    from factory.db import SessionLocal
    from factory.models import Connection, OAuthToken
    from factory.security import decrypt_json
    from factory.providers.buffer import BufferClient
    now = datetime.now(timezone.utc)
    p = cache_path()
    p.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with (p.parent / 'collector.lock').open('a') as lock:
        try: fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError: return {'status': 'already_running', 'provider_requests': 0}
        cache = read_cache() or {'version': VERSION, 'connections': {}, 'groups': {}}
        records = cache.setdefault('connections', {})
        groups = {}
        direct = []
        with SessionLocal() as s:
            connections = s.scalars(select(Connection).where(Connection.platform.in_(PLATFORMS))).all()
            for c in connections:
                if c.status.upper() != 'CONNECTED': continue
                ident = identity(c)
                saved = records.get(c.id, {})
                if saved.get('identity') != ident: saved = {}; records.pop(c.id, None)
                token = s.scalar(select(OAuthToken).where(OAuthToken.connection_id == c.id))
                if not token: continue
                try: creds = decrypt_json(token.encrypted_payload)
                except Exception: continue
                if not creds.get('access_token'): continue
                row = {'identity': ident, 'creds': creds}
                if ident['publish_via'] == 'buffer':
                    keys = ('buffer_account_id', 'buffer_organization_id', 'buffer_channel_id', 'external_id')
                    if not all(ident.get(k) and creds.get(k) == ident[k] for k in keys): continue
                    key = (ident['buffer_account_id'], ident['buffer_organization_id'])
                    groups.setdefault(key, []).append(row)
                elif c.platform == 'instagram' and due(saved.get('followers'), now): direct.append(row)
        calls = 0
        for (account_id, organization_id), rows in groups.items():
            rows.sort(key=lambda r: r['identity']['connection_id'])
            binding_hash = hashlib.sha256(json.dumps([r['identity'] for r in rows], sort_keys=True).encode()).hexdigest()
            group_key = hashlib.sha256((account_id + ':' + organization_id).encode()).hexdigest()
            previous = cache['groups'].get(group_key, {})
            if previous.get('binding_hash') == binding_hash and not due(previous, now): continue
            if len({r['creds']['access_token'] for r in rows}) != 1:
                cache['groups'][group_key] = {'status': 'stored_key_conflict', 'attempted_at': now.isoformat(), 'binding_hash': binding_hash}
                continue
            # Current installation: six groups of three channels. Bound extra
            # groups/channels without allocating unbounded provider requests.
            if calls >= 12: break
            all_ok = True
            for offset in range(0, min(len(rows), 12), 3):
                if calls >= 12: all_ok = False; break
                batch = rows[offset:offset + 3]
                start, end = window(now)
                query, variables = group_query(batch, start, end)
                try:
                    client = httpx.Client(timeout=20, follow_redirects=False)
                    try:
                        api = BufferClient(client=client, reserve=lambda: reserve(batch[0]['creds'], batch[0]['identity']['platform']))
                        calls += 1
                        data = api.request(batch[0]['creds']['access_token'], query, variables)
                    finally: client.close()
                    if (data.get('account') or {}).get('id') != account_id: raise ValueError('identity')
                    for i, row in enumerate(batch):
                        ident = row['identity']; ch = data.get('c%d' % i) or {}
                        service = 'twitter' if ident['platform'] == 'x' else ident['platform']
                        if (ch.get('id') != ident['buffer_channel_id'] or ch.get('organizationId') != organization_id or ch.get('serviceId') != ident['external_id'] or ch.get('service') != service):
                            raise ValueError('channel identity')
                    for i, row in enumerate(batch):
                        ident = row['identity']; cid = ident['connection_id']
                        entry = records.setdefault(cid, {'identity': ident})
                        fresh = normalize_aggregate(data.get('m%d' % i) or {})
                        fresh.update({'source': 'buffer', 'window_start': start.isoformat(), 'window_end': end.isoformat(), 'attempted_at': now.isoformat()})
                        entry['performance'] = fresh
                        entry['followers'] = {'value': None, 'status': 'not_available_via_buffer'}
                except Exception as exc:
                    all_ok = False
                    for row in batch:
                        cid = row['identity']['connection_id']
                        entry = records.setdefault(cid, {'identity': row['identity']})
                        previous_metric = entry.get('performance', {})
                        entry['performance'] = {**previous_metric, 'status': safe_code(exc), 'attempted_at': now.isoformat()}
            cache['groups'][group_key] = {'status': 'ok' if all_ok and len(rows) <= 12 else 'retry', 'attempted_at': now.isoformat(), 'binding_hash': binding_hash}
        version = settings.graph_api_version
        if re.fullmatch(r'v\d+\.\d+', version or ''):
            for row in direct[:100]:
                ident = row['identity']; cid = ident['connection_id']
                entry = records.setdefault(cid, {'identity': ident})
                try:
                    with httpx.Client(timeout=15, follow_redirects=False) as client:
                        calls += 1
                        response = client.get('https://graph.instagram.com/' + version + '/me',
                            headers={'Authorization': 'Bearer ' + row['creds']['access_token']},
                            params={'fields': 'user_id,username,followers_count'})
                    if response.status_code != 200: raise ValueError('profile unavailable')
                    data = response.json()
                    if str(data.get('user_id') or data.get('id')) != ident['external_id']: raise ValueError('identity')
                    value = number(data.get('followers_count'))
                    entry['followers'] = {'value': value, 'status': 'ok' if value is not None else 'not_reported', 'updated_at': now.isoformat(), 'attempted_at': now.isoformat()}
                except Exception:
                    old = entry.get('followers', {})
                    entry['followers'] = {**old, 'status': 'profile_access_required', 'attempted_at': now.isoformat()}
        cache['updated_at'] = now.isoformat()
        save_cache(cache)
        return {'version': VERSION, 'status': 'complete', 'provider_request_attempts': calls,
                'cached_connections': len(records), 'publication_requests': 0}


def report(session, overview):
    from sqlalchemy import select, func
    from factory.models import Analytics, PublishedPost, Connection
    now = datetime.now(timezone.utc); start, end = window(now)
    cache = read_cache(); saved = cache.get('connections', {})
    conns = session.scalars(select(Connection).where(Connection.platform.in_(PLATFORMS))).all()
    from factory.services.latest_metrics import latest_metrics
    snapshots = latest_metrics(session, published_from=start, published_before=end + timedelta(days=1))
    counts = dict(session.execute(select(PublishedPost.connection_id, func.count(PublishedPost.id)).where(PublishedPost.published_at >= start, PublishedPost.published_at < end + timedelta(days=1)).group_by(PublishedPost.connection_id)).all())
    local = {}
    for metric, cid in snapshots:
        item = local.setdefault(cid, {'totals': {k: None for k in NAMES}, 'coverage': {k: 0 for k in NAMES}, 'posts': 0, 'updated_at': None})
        item['posts'] += 1
        for k in NAMES:
            value = number((metric.metrics or {}).get(k))
            if value is not None: item['totals'][k] = (item['totals'][k] or 0) + value; item['coverage'][k] += 1
        at = metric.bucket_at.isoformat()
        item['updated_at'] = max(item['updated_at'] or at, at)
    account_info = {a['id']: a for a in overview['accounts']}
    rows = []
    for c in conns:
        a = account_info.get(c.account_id)
        if not a: continue
        ident = identity(c); entry = saved.get(c.id, {})
        if entry.get('identity') != ident: entry = {}
        via_buffer = ident['publish_via'] == 'buffer'
        perf = entry.get('performance', {}) if via_buffer else local.get(c.id, {})
        values = {k: number((perf.get('totals') or {}).get(k)) for k in NAMES}
        updated = timestamp(perf.get('updated_at'))
        state = perf.get('status') if via_buffer else ('ok' if any(v is not None for v in values.values()) else 'awaiting_provider')
        if not state: state = 'awaiting_collection'
        collected = timestamp(perf.get('attempted_at'))
        stale = bool(via_buffer and collected and now - collected > timedelta(hours=36))
        followers = entry.get('followers', {})
        follower_at = timestamp(followers.get('updated_at'))
        follower_stale = bool(follower_at and now - follower_at > timedelta(hours=36))
        parts = [values[k] for k in ('likes', 'comments', 'shares')]
        rows.append({'connection_id': c.id, 'account_id': a['id'], 'name': a['username'] or a['name'],
                     'platform': c.platform, 'connected': c.status.upper() == 'CONNECTED',
                     'active': a['active'], 'totals': values, 'engagement_total': sum(parts) if all(v is not None for v in parts) else None,
                     'followers': number(followers.get('value')), 'followers_status': followers.get('status', 'not_available_via_buffer' if via_buffer else 'awaiting_collection'),
                     'followers_stale': follower_stale, 'followers_updated_at': followers.get('updated_at'),
                     'status': state, 'stale': stale, 'source': 'buffer' if via_buffer else 'smfa',
                     'updated_at': updated.isoformat() if updated else None, 'window_start': perf.get('window_start', start.isoformat()),
                     'window_end': perf.get('window_end', end.isoformat()),
                     'post_count': perf.get('posts') if via_buffer else counts.get(c.id, 0), 'coverage': perf.get('coverage'),
                     'publication_state': a.get('publication_state'), 'pending_status': a.get('pending_status'),
                     'next_at': a.get('next_at'), 'planned_at': a.get('planned_at'), 'blockers': a.get('blockers', [])})
    rows.sort(key=lambda r: (r['name'].casefold(), r['platform']))
    return {'version': VERSION, 'window_days': 30, 'rows': rows, 'paused': overview['paused'],
            'updated_at': cache.get('updated_at'),
            'scope_note': 'Son 30 günde yayımlanan gönderilerin birikmiş ölçümleri. Buffer bağlantılarında Buffer kanalındaki gönderiler, doğrudan bağlantılarda SMFA kayıtları kullanılır. Günlük aktivite değildir.',
            'latency_note': 'Buffer ölçümleri günlük güncellenir. Henüz ölçülmeyen veya desteklenmeyen değerler — olarak görünür.'}


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--sync', action='store_true', required=True)
    parser.parse_args()
    try: print(json.dumps(collect(), ensure_ascii=False))
    except Exception: print(json.dumps({'status': 'collection_failed', 'credentials_exposed': False})); raise SystemExit(1)
