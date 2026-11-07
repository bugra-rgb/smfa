"""Three account tags followed by the two user-selected fixed tags."""
import hashlib
import json
import random
import re
import unicodedata

VERSION = 'five_hashtags_fixed_tail_v2'
FIXED = ('#fyp', '#benionecıkart')
FIXED_KEYS = {t.casefold() for t in FIXED}

def clean_tag(value):
    if not isinstance(value, str):
        raise ValueError('Etiketler metin olmalı.')
    value = unicodedata.normalize('NFC', value.strip())
    if not value.startswith('#'):
        value = '#' + value
    if not re.fullmatch(r'#[\w]{2,40}', value, re.UNICODE):
        raise ValueError('Etiketler 2–40 harf, rakam veya alt çizgi içermeli; boşluk içeremez.')
    return value

def topical(pool):
    if not isinstance(pool, list) or not 3 <= len(pool) <= 102:
        raise ValueError('Havuza en az 3 farklı konu etiketi ekleyin.')
    result, seen = [], set()
    for value in pool:
        tag = clean_tag(value); key = tag.casefold()
        if key in FIXED_KEYS:
            continue
        if key not in seen:
            seen.add(key); result.append(tag)
    if len(result) < 3:
        raise ValueError('Sabit iki etiket dışında en az 3 farklı konu etiketi gerekli.')
    return result

def length(value):
    return len(value.encode('utf-16-le')) // 2

def validate(pool):
    tags = topical(pool)
    if length(' '.join(sorted(tags, key=length)[:3] + list(FIXED))) > 100:
        raise ValueError('Üç konu etiketi ve iki sabit etiket 100 karaktere sığmalı; daha kısa etiketler ekleyin.')
    return tags

def valid_text(value):
    if not isinstance(value, str) or value != ' '.join(value.split()) or length(value) > 100:
        return False
    tags=value.split()
    return (len(tags)==5 and tags[-2:]==list(FIXED)
        and len({t.casefold() for t in tags})==5
        and all(re.fullmatch(r'#[\w]{2,40}', t, re.UNICODE) for t in tags))

def choose(pool, account_id, identity, preferred=None):
    tags = validate(pool)
    if valid_text(preferred) and all(t in tags for t in preferred.split()[:3]):
        return preferred.split()
    candidates = list(tags)
    seed=hashlib.sha256((str(account_id)+'\0'+str(identity)).encode()).digest()
    random.Random(seed).shuffle(candidates)
    chosen=[]
    while len(chosen)<3:
        need=2-len(chosen)
        for i,candidate in enumerate(candidates):
            remaining=candidates[:i]+candidates[i+1:]
            shortest=sorted(remaining,key=length)[:need]
            if len(shortest)==need and length(' '.join(chosen+[candidate]+shortest+list(FIXED)))<=100:
                chosen.append(candidates.pop(i));break
        else:
            raise ValueError('Beş tam etiket seçilemedi.')
    return chosen+list(FIXED)

def make_copy(tags):
    value=' '.join(tags)
    if not valid_text(value): raise ValueError('Etiket sırası veya sayısı geçersiz.')
    return {'caption':value,'description':value,'title':value,'hashtags':list(tags),
        'tags':[t[1:] for t in tags],'caption_policy':'manual_five_hashtags',
        'hashtag_policy_version':VERSION}

def revision(pool):
    return hashlib.sha256(json.dumps([VERSION,pool],ensure_ascii=False,separators=(',',':')).encode()).hexdigest()

def started(pub, checkpoint):
    return (pub.status in ('PUBLISHED','CANCELLED','UNKNOWN','POLICY_BLOCKED','BLOCKED','FAILED')
        or bool(pub.attempts or pub.remote_id or set(checkpoint)-{'worker_lease'}))

def stored_copy(video, pub):
    """Restore the exact saved text on retries, even if account overrides exist."""
    metadata=(video.metadata_json or {}).get(pub.platform,{})
    fields=('caption','description','title','hashtags','tags','caption_policy',
        'hashtag_policy_version','hashtag_pool_revision','youtube_title_policy',
        'manual_caption_text','manual_caption_version','manual_caption_revision')
    return {k:metadata[k] for k in fields if k in metadata}
