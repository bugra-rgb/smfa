"""Isolated Buffer GraphQL adapter for X text/news. Uses existing BufferClient.

Schema verified 2026-09-30: https://developers.buffer.com/reference.html
account.organizations and channels are complete arrays (no cursor arguments).
posts is a Relay connection. No idempotency key is documented for createPost.
"""
from datetime import timedelta
import hashlib
import json
import re
from urllib.parse import urlsplit

from factory.services.xnews_sources import NewsError, parse_time, utcnow, weighted_length

CHANNEL_FIELDS = 'id name organizationId service serviceId isDisconnected isLocked isQueuePaused allowedActions'
POST_FIELDS = '''id text status channelId channelService schedulingType externalLink sentAt createdAt allowedActions
channel { id organizationId service serviceId }'''
ACCOUNT_QUERY = 'query XNewsAccount { account { id organizations { id name } } }'
CHANNELS_QUERY = 'query XNewsChannels($input: ChannelsInput!) { channels(input: $input) { ' + CHANNEL_FIELDS + ' } }'
CHANNEL_QUERY = 'query XNewsChannel($input: ChannelInput!) { account { id } channel(input: $input) { ' + CHANNEL_FIELDS + ' } }'
POST_QUERY = 'query XNewsPost($input: PostInput!) { account { id } post(input: $input) { ' + POST_FIELDS + ' } }'
POSTS_QUERY = '''query XNewsReconcile($input: PostsInput!, $first: Int!, $after: String) {
account { id } posts(input: $input, first: $first, after: $after) {
edges { cursor node { ''' + POST_FIELDS + ''' } } pageInfo { hasNextPage endCursor } } }'''
CREATE_QUERY = '''mutation XNewsCreate($input: CreatePostInput!) { createPost(input: $input) {
__typename ... on PostActionSuccess { post { ''' + POST_FIELDS + ''' } } ... on MutationError { message } } }'''
DELETE_QUERY = '''mutation XNewsDelete($input: DeletePostInput!) { deletePost(input: $input) {
__typename ... on DeletePostSuccess { id } ... on MutationError { message } } }'''
BINDING_KEYS = ('buffer_account_id', 'buffer_organization_id', 'buffer_channel_id', 'external_id')


def payload_hash(text, binding):
    return hashlib.sha256(json.dumps({'text': text, 'binding': {k: binding[k] for k in BINDING_KEYS},
        'mode': 'shareNow', 'schedulingType': 'automatic'}, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def channel_problem(channel):
    if not isinstance(channel, dict) or channel.get('service') != 'twitter' or not channel.get('serviceId'):
        return 'Buffer kanalının X kimliği doğrulanamadı.'
    if channel.get('isDisconnected') is not False:
        return 'X kanalını Buffer içinde yeniden bağlayın.'
    if channel.get('isLocked') is not False:
        return 'Buffer kanalı kilitli; hesap veya plan sınırını kontrol edin.'
    if channel.get('isQueuePaused') is not False:
        return 'Buffer kanalının yayın kuyruğu duraklatılmış.'
    actions = channel.get('allowedActions', [])
    if not isinstance(actions, list) or 'scheduleUpdates' not in actions or 'readUpdates' not in actions:
        return 'Bu Buffer kullanıcısının gönderi oluşturma veya sonuç okuma yetkisi eksik.'
    return ''


def _failure(exc):
    code = getattr(exc, 'code', 'buffer_network')
    mapping = {'quota_exceeded': ('quota_exceeded', 'SMFA haber modülünün tüketim sınırına ulaşıldı.', False),
               'rate_limit': ('buffer_rate_limit', 'Buffer istek sınırına ulaşıldı.', True),
               'oauth_expired': ('buffer_auth', 'Buffer anahtarı veya kanal izni yenilenmeli.', False),
               'oauth_missing': ('buffer_auth', 'Buffer anahtarı yapılandırılmadı.', False),
               'buffer_graphql_error': ('buffer_graphql', 'Buffer GraphQL yanıtı hata içeriyor.', False),
               'buffer_http_error': ('buffer_http', 'Buffer HTTP yanıtı doğrulanamadı.', True)}
    code, message, retryable = mapping.get(code, ('buffer_network', 'Buffer bağlantısı tamamlanamadı.', True))
    return NewsError(code, message, retryable, max(60, int(getattr(exc, 'retry_after', 0) or 300)))


class _ShortBufferHTTP:
    """Close every fixed-endpoint response pool; the legacy adapter stays intact."""
    def post(self, *args, **kwargs):
        import httpx
        with httpx.Client(timeout=20, follow_redirects=False, trust_env=False) as client:
            return client.post(*args, **kwargs)


class BufferXNews:
    def __init__(self, token, binding=None, api=None, reserve=None):
        self.token = token
        self.binding = dict(binding or {})
        if api is None:
            from factory.providers.buffer import BufferClient
            # Existing quota accounting is retained; callers can inject the
            # exact organization/account budget already used by this installation.
            api = BufferClient(client=_ShortBufferHTTP(), reserve=reserve)
        self.api = api
    def _call(self, query, variables=None, reserved=False):
        try:
            data = self.api.request(self.token, query, variables or {}, reserved=reserved)
            if not isinstance(data, dict) or data.get('errors'):
                raise NewsError('buffer_graphql', 'Buffer GraphQL yanıtı doğrulanamadı.')
            return data
        except NewsError:
            raise
        except Exception as exc:
            raise _failure(exc) from None
    def _binding(self):
        if any(not isinstance(self.binding.get(k), str) or not self.binding[k] for k in BINDING_KEYS):
            raise NewsError('buffer_identity', 'X hesabının Buffer eşleştirmesi tamamlanmadı.')
        return self.binding
    def _matches(self, channel):
        b = self._binding()
        return (isinstance(channel, dict) and channel.get('id') == b['buffer_channel_id']
                and channel.get('organizationId') == b['buffer_organization_id']
                and channel.get('service') == 'twitter' and channel.get('serviceId') == b['external_id'])
    def _account(self, data):
        if (data.get('account') or {}).get('id') != self._binding()['buffer_account_id']:
            raise NewsError('buffer_identity', 'Buffer anahtarı eşleşen hesaba ait değil.')
    def discover(self):
        account = self._call(ACCOUNT_QUERY).get('account')
        if not isinstance(account, dict) or not account.get('id') or not isinstance(account.get('organizations'), list):
            raise NewsError('buffer_identity', 'Buffer hesap ve kuruluş listesi doğrulanamadı.')
        found, seen_orgs, seen_channels = [], set(), set()
        for org in account['organizations']:
            if not isinstance(org, dict) or not isinstance(org.get('id'), str):
                raise NewsError('buffer_identity', 'Buffer kuruluş bilgisi doğrulanamadı.')
            if org['id'] in seen_orgs:
                continue
            seen_orgs.add(org['id'])
            channels = self._call(CHANNELS_QUERY, {'input': {'organizationId': org['id']}}).get('channels')
            if not isinstance(channels, list):
                raise NewsError('buffer_schema', 'Buffer kanal listesi biçimi değişmiş; eksik liste kullanılmadı.')
            for channel in channels:
                if not isinstance(channel, dict):
                    raise NewsError('buffer_schema', 'Buffer kanal yanıtı doğrulanamadı.')
                if channel.get('service') != 'twitter':
                    continue
                if channel.get('organizationId') != org['id'] or not channel.get('id'):
                    raise NewsError('buffer_identity', 'Buffer kanal kuruluşu doğrulanamadı.')
                if channel['id'] in seen_channels:
                    continue
                seen_channels.add(channel['id'])
                reason = channel_problem(channel)
                found.append({**channel, 'buffer_account_id': account['id'], 'organization_name': org.get('name', ''),
                              'ready': not bool(reason), 'can_publish': not bool(reason), 'problem': reason, 'platform_label': 'X'})
        return found
    def check(self):
        b = self._binding()
        data = self._call(CHANNEL_QUERY, {'input': {'id': b['buffer_channel_id']}})
        self._account(data)
        channel = data.get('channel')
        if not self._matches(channel):
            raise NewsError('buffer_identity', 'Buffer kuruluş veya X kanal eşleştirmesi değişmiş.')
        reason = channel_problem(channel)
        if reason:
            raise NewsError('buffer_not_ready', reason)
        return channel
    def _record(self, post, expected_id=None):
        b = self._binding()
        if (not isinstance(post, dict) or not post.get('id') or (expected_id and post['id'] != expected_id)
                or post.get('channelId') != b['buffer_channel_id'] or post.get('channelService') != 'twitter'
                or not self._matches(post.get('channel'))):
            return {'state': 'UNKNOWN_RESULT', 'error_code': 'buffer_identity', 'message': 'Buffer gönderi kimliği doğrulanamadı.'}
        base = {'post_id': post['id'], 'provider_status': post.get('status'), 'checked_at': utcnow().isoformat(),
                'sent_at': None, 'url': None, 'external_link': None, 'url_missing': True}
        if post.get('schedulingType') != 'automatic':
            return {**base, 'state': 'FAILED', 'error_code': 'buffer_reminder', 'message': 'Buffer bildirim/hatırlatma modu otomatik yayın sayılmaz.'}
        status = post.get('status')
        if status == 'sent':
            sent = parse_time(post.get('sentAt'))
            raw = post.get('externalLink')
            url = None
            if isinstance(raw, str):
                try:
                    p = urlsplit(raw)
                    if (p.scheme == 'https' and p.hostname in ('x.com', 'www.x.com', 'twitter.com', 'www.twitter.com')
                            and not p.username and not p.password and p.port in (None, 443)
                            and re.fullmatch(r'/[A-Za-z0-9_]+/status/[0-9]+/?', p.path)):
                        url = 'https://x.com' + p.path.rstrip('/')
                except ValueError:
                    pass
            return {**base, 'state': 'PUBLISHED', 'url': url, 'external_link': url, 'url_missing': not bool(url),
                    'sent_at': sent.isoformat() if sent else None,
                    'message': 'Yayın Buffer tarafından doğrulandı.' if url else 'Yayın doğrulandı; gerçek X bağlantısı henüz alınamadı.'}
        if status in ('scheduled', 'sending'):
            return {**base, 'state': 'BUFFER_PENDING', 'message': 'Buffer yayın sonucu bekleniyor.'}
        if status in ('error', 'draft', 'needs_approval'):
            return {**base, 'state': 'FAILED', 'error_code': 'buffer_' + status,
                    'message': 'Buffer gönderiyi yayımlamadı; ilgili gönderi Buffer içinde kontrol edilmeli.'}
        return {**base, 'state': 'UNKNOWN_RESULT', 'error_code': 'buffer_status', 'message': 'Buffer gönderi durumu tanınmadı.'}
    def create(self, text, persist_intent, persist_accepted):
        if not callable(persist_intent) or not callable(persist_accepted):
            raise NewsError('persistence_required', 'Gönderim niyeti ve Buffer kimliği kalıcılaştırılmalı.')
        if not isinstance(text, str) or not text.strip():
            raise NewsError('text_invalid', 'Boş haber gönderisi oluşturulamaz.')
        links = re.findall(r'https://[^\s]+', text)
        if weighted_length(text, links) > 280:
            raise NewsError('text_too_long', 'X gönderisi 280 ağırlıklı karakter sınırını aşıyor.')
        self.check()
        # Reserve before persistence: budget rejection means no mutation was sent.
        try:
            self.api.reserve()
        except Exception as exc:
            raise _failure(exc) from None
        payload = {'channelId': self.binding['buffer_channel_id'], 'text': text,
                   'schedulingType': 'automatic', 'mode': 'shareNow', 'saveToDraft': False, 'needsApproval': False}
        persist_intent({'payload': payload, 'payload_hash': payload_hash(text, self.binding),
                        'binding': {k: self.binding[k] for k in BINDING_KEYS}, 'started_at': utcnow().isoformat()})
        try:
            result = self._call(CREATE_QUERY, {'input': payload}, reserved=True).get('createPost')
        except NewsError as exc:
            # This includes timeout, HTTP 5xx, malformed GraphQL and 401/429:
            # conservative uncertainty preserves immutable intent. No retries.
            return {'state': 'UNKNOWN_RESULT', 'error_code': exc.code,
                    'message': 'Buffer oluşturma sonucu belirsiz; otomatik tekrar gönderilmedi.'}
        if not isinstance(result, dict):
            return {'state': 'UNKNOWN_RESULT', 'error_code': 'buffer_schema', 'message': 'Buffer oluşturma yanıtı eksik.'}
        kind = result.get('__typename')
        if kind != 'PostActionSuccess':
            # Known mutation rejection is distinguishable from a missing result.
            known = {'InvalidInputError', 'UnauthorizedError', 'LimitReachedError', 'NotFoundError', 'VoidMutationError'}
            if kind in known:
                code = {'UnauthorizedError': 'buffer_auth', 'LimitReachedError': 'buffer_limit',
                        'InvalidInputError': 'buffer_content_rejected'}.get(kind, 'buffer_rejected')
                return {'state': 'FAILED', 'error_code': code, 'message': 'Buffer oluşturma isteğini reddetti; otomatik tekrar gönderilmedi.'}
            return {'state': 'UNKNOWN_RESULT', 'error_code': 'buffer_mutation', 'message': 'Buffer oluşturma sonucu doğrulanamadı.'}
        post = result.get('post')
        if not isinstance(post, dict) or not isinstance(post.get('id'), str) or not post['id']:
            return {'state': 'UNKNOWN_RESULT', 'error_code': 'buffer_missing_id', 'message': 'Buffer gönderi kimliği alınamadı.'}
        # If the database callback fails, propagate: caller's persisted SUBMITTING
        # remains uncertain. Never swallow it and never create a replacement.
        persist_accepted({'post_id': post['id'], 'accepted_at': utcnow().isoformat()})
        state = self._record(post)
        if state['state'] == 'BUFFER_PENDING':
            state['state'] = 'BUFFER_ACCEPTED'
        return state
    def poll(self, post_id):
        data = self._call(POST_QUERY, {'input': {'id': post_id}})
        self._account(data)
        return self._record(data.get('post'), post_id)
    def reconcile(self, text, started_at, max_pages=10):
        """Read only. Exact text/channel/time match; absence never means retry."""
        b = self._binding()
        stamp = parse_time(started_at)
        if not stamp:
            raise NewsError('reconcile_time', 'Belirsiz gönderimin başlangıç zamanı eksik.')
        after, seen, candidates = None, set(), []
        end = stamp + timedelta(minutes=15)
        for page in range(min(20, max(1, max_pages))):
            data = self._call(POSTS_QUERY, {'input': {'organizationId': b['buffer_organization_id'],
                'filter': {'channelIds': [b['buffer_channel_id']], 'createdAt': {'start': (stamp-timedelta(minutes=2)).isoformat(), 'end': end.isoformat()}}},
                'first': 100, 'after': after})
            self._account(data)
            results = data.get('posts')
            if not isinstance(results, dict) or not isinstance(results.get('edges'), list) or not isinstance(results.get('pageInfo'), dict):
                raise NewsError('buffer_schema', 'Buffer geçmiş sayfalaması doğrulanamadı.')
            for edge in results['edges']:
                post = edge.get('node') if isinstance(edge, dict) else None
                created = parse_time(post.get('createdAt')) if isinstance(post, dict) else None
                if (isinstance(post, dict) and post.get('text') == text and self._matches(post.get('channel'))
                        and created and stamp-timedelta(minutes=2) <= created <= end):
                    rec = self._record(post)
                    if rec.get('post_id') and rec['post_id'] not in {x['post_id'] for x in candidates}:
                        candidates.append(rec)
            info = results['pageInfo']
            if info.get('hasNextPage') is False:
                return {'state': candidates[0]['state'] if len(candidates) == 1 else 'UNKNOWN_RESULT',
                        'candidates': candidates, 'complete_scan': True, 'unique_match': len(candidates) == 1,
                        'may_retry_create': False, 'pages': page + 1}
            after = info.get('endCursor')
            if info.get('hasNextPage') is not True or not isinstance(after, str) or not after or after in seen:
                raise NewsError('buffer_pagination', 'Buffer sayfalama imleci doğrulanamadı.')
            seen.add(after)
        return {'state': 'UNKNOWN_RESULT', 'candidates': candidates, 'complete_scan': False,
                'unique_match': False, 'may_retry_create': False, 'pages': page + 1}
    def cancel(self, post_id):
        """Caller MUST only pass its own module's persisted post ID and binding."""
        data = self._call(POST_QUERY, {'input': {'id': post_id}})
        self._account(data)
        post = data.get('post')
        state = self._record(post, post_id)
        if state['state'] == 'PUBLISHED':
            return state  # Never delete an already published social post.
        if (state['state'] != 'BUFFER_PENDING' or post.get('status') != 'scheduled'
                or 'deletePost' not in post.get('allowedActions', [])):
            return {**state, 'cancelled': False, 'message': 'Gönderi yayın sürecinde veya iptal yetkisi yok; Buffer içinde kontrol edin.'}
        try:
            result = self._call(DELETE_QUERY, {'input': {'id': post_id}}).get('deletePost')
        except NewsError:
            return {'state': 'UNKNOWN_RESULT', 'post_id': post_id, 'cancelled': False,
                    'message': 'Buffer iptal sonucu doğrulanamadı; gönderi ayrıca kontrol edilmeli.'}
        if isinstance(result, dict) and result.get('__typename') == 'DeletePostSuccess' and result.get('id') == post_id:
            return {'state': 'CANCELLED', 'post_id': post_id, 'cancelled': True, 'message': 'Buffer gönderinin silindiğini doğruladı.'}
        return {'state': 'UNKNOWN_RESULT', 'post_id': post_id, 'cancelled': False,
                'message': 'Buffer iptali doğrulamadı; kanalın diğer gönderileri değiştirilmedi.'}
