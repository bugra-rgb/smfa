"""The shared five-tag policy, including an intact YouTube title."""
from factory.services.hashtag_policy import (VERSION as SHARED_VERSION, length as title_length,
    valid_text as valid_title, validate as validate_pool, choose, make_copy as shared_copy, started, stored_copy)
VERSION=SHARED_VERSION+':youtube'
LIMIT=100

def valid_tags(tags):
    return isinstance(tags,list) and valid_title(' '.join(tags))

def make_copy(tags):
    return {**shared_copy(tags),'youtube_title_policy':VERSION}

def publication_copy(ctx,account,video,pub,metadata):
    if pub.platform!='youtube': return {}
    from factory.services.pipeline import reveal_checkpoint
    from factory.services.jobs import PermanentFailure
    from factory.services.hashtags import pool_for
    if started(pub,reveal_checkpoint(pub.checkpoint or {})): return stored_copy(video,pub)
    try:
        pool=(account.settings or {}).get('manual_hashtag_pool') or pool_for(account.username or account.name)
        result=make_copy(choose(pool,account.id,pub.id,metadata.get('caption')))
    except ValueError as exc: raise PermanentFailure(str(exc)) from None
    video.metadata_json={**(video.metadata_json or {}),'youtube':{**(video.metadata_json or {}).get('youtube',{}),**result}}
    ctx.session.commit()
    return result
