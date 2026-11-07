"""Manually authored channel pools. No model, network call or generated prose."""
import hashlib
import random
import re

POLICY = 'manual_five_hashtags'
POOLS = {}  # Configure manual_hashtag_pool separately for each account.

def valid_pool(pool):
    from factory.services.hashtag_policy import validate
    try: validate(pool); return True
    except (ValueError,TypeError): return False

def pool_for(username):
    name = username.strip().lstrip('@').casefold()
    if name in POOLS:
        return list(POOLS[name])
    brand = re.sub(r'\W', '', name, flags=re.UNICODE)[:40]
    result = ['#KisaVideo', '#VideoGunlugu', '#GunlukPaylasim', '#ReelsVideo', '#KesfetVideolari', '#YeniVideo']
    if len(brand) >= 2 and ('#'+brand).casefold() not in {t.casefold() for t in result}:
        result.insert(0, '#'+brand)
    return result

def copy_for(account, identity):
    from factory.services.hashtag_policy import choose, make_copy
    pool=(account.settings or {}).get('manual_hashtag_pool') or pool_for(account.username or account.name)
    return make_copy(choose(pool,account.id,identity))

def publication_copy(ctx, account, video, pub):
    from factory.services.hashtag_policy import VERSION, started, stored_copy, revision, valid_text, make_copy
    from factory.services.pipeline import reveal_checkpoint
    from factory.services.jobs import PermanentFailure
    cp=reveal_checkpoint(pub.checkpoint or {})
    # The already-saved platform metadata is authoritative once any attempt has
    # started. Do not change a Buffer post or an uncertain direct upload.
    if started(pub,cp): return stored_copy(video,pub)
    pool=(account.settings or {}).get('manual_hashtag_pool') or pool_for(account.username or account.name)
    key='manual_hashtags:'+pub.id
    cached=(ctx.job.result or {}).get(key)
    fingerprint=revision(pool)
    if (isinstance(cached,dict) and cached.get('hashtag_policy_version')==VERSION
            and cached.get('hashtag_pool_revision')==fingerprint and valid_text(cached.get('caption'))
            and cached=={**make_copy(cached['caption'].split()),'hashtag_pool_revision':fingerprint}):
        result=dict(cached)
    else:
        try: result={**copy_for(account,pub.id),'hashtag_pool_revision':fingerprint}
        except ValueError as exc: raise PermanentFailure(str(exc)) from None
        ctx.checkpoint(**{key:result})
    video.metadata_json={**(video.metadata_json or {}),pub.platform:{**(video.metadata_json or {}).get(pub.platform,{}),**result}}
    ctx.session.commit()
    return dict(result)
