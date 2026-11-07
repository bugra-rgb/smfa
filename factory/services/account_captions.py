"""Account-authored text, appended to the existing five-tag publication copy.

No model/provider calls. Once a publication starts, its saved copy is immutable.
"""
import re
import unicodedata
from factory.services.hashtag_policy import length, valid_text, choose, topical, FIXED

KEY = 'manual_caption_v1'
VERSION = 'smfa-account-captions-20260929.1'
PLATFORMS = ('instagram', 'youtube', 'tiktok', 'facebook', 'x')
MAX_TEXT = 2000


def normalize(value):
    if not isinstance(value, str):
        raise ValueError('Açıklama metin olmalı.')
    value = unicodedata.normalize('NFC', value.replace('\r\n', '\n').replace('\r', '\n')).strip()
    if any(unicodedata.category(c) in ('Cc', 'Cs') and c not in '\n\t' for c in value):
        raise ValueError('Açıklamada geçersiz kontrol karakteri var.')
    if length(value) > MAX_TEXT:
        raise ValueError('Açıklama en fazla 2000 karakter olabilir; bazı emojiler iki karakter sayılır.')
    return value


def config(account):
    value = (account.settings or {}).get(KEY, {})
    if not isinstance(value, dict):
        raise ValueError('Kayıtlı açıklama ayarı geçersiz; yeniden kaydedin.')
    return {'text': value.get('text', ''), 'revision': value.get('revision', '0'),
            'updated_at': value.get('updated_at')}


def pool_for(account):
    from factory.services.hashtags import pool_for as default_pool
    return (account.settings or {}).get('manual_hashtag_pool') or default_pool(account.username or account.name)


def x_units(value):
    # Conservative upper bound: weighted code points, without emoji collapsing
    # or URL-shortening discounts. Short domain-like tokens reserve 23 units.
    def weight(c):
        n = ord(c)
        return 1 if n <= 0x10ff or 0x2000 <= n <= 0x200d or 0x2010 <= n <= 0x201f or 0x2032 <= n <= 0x2037 else 2
    total = sum(weight(c) for c in value)
    for token in re.findall(r'\S+', value):
        if '.' in token and not token.startswith('#'):
            total += max(0, 23 - sum(weight(c) for c in token))
    return total


def tag_budget(pool, unit):
    """Maximum possible units among the tag combinations the selector allows."""
    fixed = ' '.join(FIXED)
    allowance = 100 - length(fixed) - 3
    dp = {(0, 0): 0}
    for tag in topical(pool):
        for (count, used), score in list(dp.items()):
            key = count + 1, used + length(tag)
            if count < 3 and key[1] <= allowance:
                dp[key] = max(dp.get(key, -1), score + unit(tag))
    scores = [v for (count, _), v in dp.items() if count == 3]
    if not scores:
        raise ValueError('Hesabın hashtag havuzu geçersiz; önce hashtagleri düzenleyin.')
    return max(scores) + unit(fixed) + 3


def validate_for_account(value, account, platforms):
    value = normalize(value)
    if not value:
        return value
    if 'youtube' in platforms:
        if '<' in value or '>' in value:
            raise ValueError('YouTube açıklaması < veya > içeremez.')
        if len(value.encode('utf-8')) + 2 + tag_budget(pool_for(account), lambda s: len(s.encode('utf-8'))) > 5000:
            raise ValueError('Açıklama ve hashtagler YouTube açıklama sınırını aşıyor; metni kısaltın.')
    if 'x' in platforms:
        available = 280 - 2 - tag_budget(pool_for(account), x_units)
        if x_units(value) > available:
            raise ValueError('X için açıklamayı kısaltın. Hashtaglerden sonra en fazla %d metin birimi kalıyor; emoji ve bağlantılar farklı sayılır.' % max(0, available))
    return value


def validate_publication(metadata, platform):
    """Validate the original tag-only copy or an exact prefix + original tags."""
    caption = metadata.get('caption', '')
    if metadata.get('manual_caption_version') != VERSION:
        tags = caption.split() if isinstance(caption, str) else []
        legacy_ok = len(tags) == 5 and len({t.casefold() for t in tags}) == 5 and all(re.fullmatch(r'#\w{2,40}', t) for t in tags)
        if not (valid_text(caption) if platform == 'instagram' else legacy_ok):
            raise ValueError('Yayın için mevcut beş hashtag gerekli.')
        return caption
    tags = metadata.get('hashtags')
    if not isinstance(tags, list) or not all(isinstance(t, str) for t in tags) or not valid_text(' '.join(tags)):
        raise ValueError('Yayın hashtagleri doğrulanamadı.')
    body = normalize(metadata.get('manual_caption_text', ''))
    expected = body + '\n\n' + ' '.join(tags) if body else ' '.join(tags)
    if caption != expected or metadata.get('description') != expected:
        raise ValueError('Yayın açıklaması kayıtlı metinle eşleşmiyor.')
    if length(caption) > 2200:
        raise ValueError('Açıklama ve hashtagler platform sınırını aşıyor.')
    if platform == 'youtube' and (len(caption.encode('utf-8')) > 5000 or '<' in caption or '>' in caption):
        raise ValueError('YouTube açıklama sınırı aşıldı.')
    if platform == 'x' and x_units(caption) > 280:
        raise ValueError('X açıklama sınırı aşıldı; metni veya hashtagleri kısaltın.')
    return caption


def provider_caption(metadata, platform):
    from factory.providers.base import ProviderError
    try:
        return validate_publication(metadata, platform)
    except (ValueError, TypeError, UnicodeError):
        raise ProviderError('Açıklama veya hashtagler platform sınırlarına uygun değil. Hesabın açıklama alanını kontrol edin.', 'invalid_metadata', False) from None


def apply_caption(ctx, account, video, pub, metadata):
    from factory.services.hashtag_policy import started
    from factory.services.jobs import PermanentFailure
    from factory.services.pipeline import reveal_checkpoint
    if pub.platform not in PLATFORMS or started(pub, reveal_checkpoint(pub.checkpoint or {})):
        return  # Existing uploads/retries keep the saved metadata, including legacy copy.
    try:
        settings = config(account)
        body = validate_for_account(settings['text'], account, [pub.platform])
        tags = metadata.get('hashtags')
        if not isinstance(tags, list) or not valid_text(' '.join(tags)):
            raise ValueError('Yayın hashtagleri doğrulanamadı.')
        caption = body + '\n\n' + ' '.join(tags) if body else ' '.join(tags)
        fields = {'caption': caption, 'description': caption,
                  'manual_caption_text': body, 'manual_caption_version': VERSION,
                  'manual_caption_revision': settings['revision']}
        validate_publication({**metadata, **fields}, pub.platform)
    except (ValueError, TypeError, UnicodeError) as exc:
        raise PermanentFailure(str(exc)) from None
    metadata.update(fields)
    video.metadata_json = {**(video.metadata_json or {}), pub.platform: {
        **(video.metadata_json or {}).get(pub.platform, {}), **fields}}
    ctx.session.commit()


def preview(account, text):
    tags = choose(pool_for(account), account.id, 'manual-caption-preview')
    return {'hashtags': tags, 'text': text + '\n\n' + ' '.join(tags) if text else ' '.join(tags)}

# SMFA explicit caption-only mode, 20261029-r2. Opt-in Instagram and TikTok caption-only.
CAPTION_ONLY_VERSION = 'smfa-instagram-caption-only-20261022.1'
CAPTION_ONLY_KEY = 'smfa_no_hashtags_v1'
_tagged_apply_caption = apply_caption
_tagged_validate_publication = validate_publication
_tagged_preview = preview


def caption_only(account):
    return (account.settings or {}).get(CAPTION_ONLY_KEY) is True


def validate_publication(metadata, platform):
    if metadata.get('manual_caption_version') != CAPTION_ONLY_VERSION:
        return _tagged_validate_publication(metadata, platform)
    body = normalize(metadata.get('manual_caption_text', ''))
    if platform not in ('instagram', 'tiktok') or not body or '#' in body or '＃' in body:
        raise ValueError('Etiketsiz açıklama geçersiz.')
    if (metadata.get('caption') != body or metadata.get('description') != body
            or metadata.get('hashtags') != [] or metadata.get('tags') != []
            or metadata.get('caption_policy') != 'manual_text_only'):
        raise ValueError('Etiketsiz açıklama kayıtlı metinle eşleşmiyor.')
    return body


def apply_caption(ctx, account, video, pub, metadata):
    if pub.platform not in ('instagram', 'tiktok') or not caption_only(account):
        return _tagged_apply_caption(ctx, account, video, pub, metadata)
    from factory.services.hashtag_policy import started
    from factory.services.pipeline import reveal_checkpoint
    from factory.services.jobs import PermanentFailure
    if started(pub, reveal_checkpoint(pub.checkpoint or {})):
        return  # Preserve exact content for already-started and uncertain uploads.
    try:
        saved = config(account)
        body = normalize(saved['text'])
        fields = {'caption': body, 'description': body, 'hashtags': [], 'tags': [],
                  'caption_policy': 'manual_text_only',
                  'manual_caption_text': body,
                  'manual_caption_version': CAPTION_ONLY_VERSION,
                  'manual_caption_revision': saved['revision']}
        validate_publication(fields, pub.platform)
    except (ValueError, TypeError, UnicodeError) as exc:
        raise PermanentFailure(str(exc)) from None
    metadata.update(fields)
    video.metadata_json = {**(video.metadata_json or {}), pub.platform: {
        **(video.metadata_json or {}).get(pub.platform, {}), **fields}}
    ctx.session.commit()


def preview(account, text):
    if caption_only(account):
        return {'hashtags': [], 'text': normalize(text)}
    return _tagged_preview(account, text)
