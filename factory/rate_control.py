"""Shared provider capacity across worker processes and hosts."""
import time
from contextlib import contextmanager
from uuid import uuid4

from factory.config import settings


class CapacityBusy(Exception):
    retryable = True
    retry_after = 15
    code = "provider_capacity"


ACQUIRE = """
local key, now, expiry, member, capacity = KEYS[1], tonumber(ARGV[1]), tonumber(ARGV[2]), ARGV[3], tonumber(ARGV[4])
redis.call('ZREMRANGEBYSCORE', key, '-inf', now)
if redis.call('ZCARD', key) >= capacity then return 0 end
redis.call('ZADD', key, expiry, member)
redis.call('EXPIRE', key, math.ceil(expiry-now)+60)
return 1
"""


@contextmanager
def provider_slot(provider: str):
    if settings.mode == "mock":
        yield
        return
    import redis
    client = redis.Redis.from_url(settings.redis_url, socket_connect_timeout=2, socket_timeout=2)
    token = str(uuid4())
    key = f"factory:capacity:{provider}"
    now = time.time()
    try:
        acquired = client.eval(ACQUIRE, 1, key, str(now), str(now + settings.provider_lease_seconds), token, str(settings.provider_concurrency))
    except redis.RedisError:
        raise CapacityBusy("Sağlayıcı kapasite kontrolüne ulaşılamadı; işlem bekletildi.") from None
    if not acquired:
        raise CapacityBusy("Sağlayıcının eşzamanlı işlem sınırı dolu; iş sırada bekliyor.")
    try:
        yield
    finally:
        try:
            client.zrem(key, token)
        except redis.RedisError:
            # Expiry reclaims the slot; never overwrite a completed remote effect.
            pass
