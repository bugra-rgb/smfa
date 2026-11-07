"""Instagram report closing dates, never relabelled as Turkey midnight days."""
from datetime import datetime, timedelta, timezone
from sqlalchemy import select
from factory.config import settings
from factory.models import Connection
from factory.update_models import InsightMeasure

BASIS = 'instagram_report_end_date'
SCOPE = 'account_provider_day'
CORE = ('views', 'likes', 'comments', 'shares', 'saves')
NOTICE = ('Tarihler, Instagram günlük raporlarının Türkiye saatine çevrilmiş bitiş günüdür. '
          'Bunlar Türkiye saatiyle 00:00–24:00 ölçümleri değildir. '
          'Gün sınırları API yanıtından okunur; boş yanıt sıfır sayılmaz.')


def resolve_period(payload, window):
    """Use two returned endpoints; do not assume a timezone or a 24-hour DST day."""
    rows = [r for r in payload.get('data', []) if isinstance(r, dict)
            and r.get('name') == 'reach' and r.get('period') == 'day']
    if len(rows) != 1:
        return None
    ends = []
    for item in rows[0].get('values', []):
        try:
            end = datetime.fromisoformat(item['end_time'])
            if end.tzinfo is None:
                return None
            ends.append(end.astimezone(timezone.utc))
        except (KeyError, ValueError, TypeError):
            return None
    if len(set(ends)) != len(ends):
        return None
    ends.sort()
    matches = [e for e in ends if window['start_utc'] <= e < window['end_utc']]
    if len(matches) != 1:
        return None
    end = matches[0]
    index = ends.index(end)
    if index == 0:
        return None
    start = ends[index - 1]
    if not timedelta(hours=23) <= end - start <= timedelta(hours=25):
        return None
    return {'start_utc': start, 'end_utc': end, 'report_date': window['start']}


def activity(session, account, window):
    from factory.services.insight_ranges import METRICS, ZONE, cell, aggregate_cells, with_interactions
    connection = session.scalar(select(Connection).where(Connection.account_id == account.id,
                                                        Connection.platform == 'instagram'))
    measures = session.scalars(select(InsightMeasure).where(
        InsightMeasure.account_id == account.id, InsightMeasure.scope == SCOPE,
        InsightMeasure.period_end >= window['start_utc'],
        InsightMeasure.period_end < window['end_utc'])).all()
    valid = []
    for row in measures:
        p = row.provenance or {}
        if not (connection and p.get('external_id') == connection.external_id
                and p.get('api_version') == settings.graph_api_version
                and p.get('period_basis') == BASIS and p.get('boundary_verified') is True):
            continue
        # Older midnight-labelled observations and changed identities cannot leak in.
        if (p.get('provider_start_utc') != row.period_start.isoformat()
                or p.get('provider_end_utc') != row.period_end.isoformat()
                or p.get('report_date') != row.period_end.astimezone(ZONE).date().isoformat()):
            continue
        valid.append(row)
    result = {}; daily = []
    for metric in METRICS:
        if metric not in CORE:
            result[metric] = cell(status='unsupported')
            continue
        cells = []
        for i in range(window['days']):
            day = (window['start_utc'].astimezone(ZONE).date() + timedelta(days=i)).isoformat()
            matches = [r for r in valid if r.metric == metric and r.provenance['report_date'] == day]
            intervals = {(r.period_start, r.period_end) for r in matches}
            if len(intervals) == 1:
                row = max(matches, key=lambda r: r.fetched_at)
                value = cell(row.value, row.status, last_sync=row.fetched_at.isoformat(),
                             period_start=row.period_start.isoformat(), period_end=row.period_end.isoformat())
            else:
                value = cell(status='error' if len(intervals) > 1 else 'no_data')
            cells.append(value)
            if metric == 'views':
                daily.append({'date': day, **value})
        result[metric] = aggregate_cells(cells, metric=metric, unique_scope=True)
    return with_interactions(result), daily
