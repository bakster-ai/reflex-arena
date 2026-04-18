"""
Rate-limit: Redis sliding-window при наличии REDIS_URL, иначе in-memory fallback.
In-memory fallback — не cluster-safe, но безопасен при single-instance deploy.
"""
import os
import time
import logging
from typing import Tuple

log = logging.getLogger("rate_limit")

REDIS_URL = os.environ.get("REDIS_URL", "")
_redis = None
_redis_initialized = False


def _get_redis():
    global _redis, _redis_initialized
    if _redis_initialized:
        return _redis
    _redis_initialized = True
    if not REDIS_URL:
        log.info("rate_limit: REDIS_URL not set — using in-memory fallback")
        return None
    try:
        import redis
        _redis = redis.from_url(REDIS_URL, socket_timeout=0.5, socket_connect_timeout=0.5, decode_responses=True)
        # Пробуем ping
        _redis.ping()
        log.info("rate_limit: connected to Redis")
    except Exception as e:
        log.warning(f"rate_limit: Redis connect failed, fallback to memory: {e}")
        _redis = None
    return _redis


# In-memory sliding window (dict-based bucket)
_mem_buckets: dict = {}


def check_and_incr(key: str, max_req: int, window_sec: int) -> Tuple[bool, int]:
    """
    Атомарно инкрементирует счётчик. Возвращает (allowed, current_count).
    allowed=False если count > max_req.
    """
    now = time.time()
    r = _get_redis()
    if r is not None:
        try:
            # Sliding window через sorted set + ZREMRANGEBYSCORE
            rkey = f"rl:{key}"
            pipe = r.pipeline()
            pipe.zremrangebyscore(rkey, 0, now - window_sec)
            pipe.zadd(rkey, {f"{now}:{os.getpid()}": now})
            pipe.zcard(rkey)
            pipe.expire(rkey, window_sec + 1)
            res = pipe.execute()
            count = res[2]
            return count <= max_req, count
        except Exception as e:
            log.warning(f"rate_limit: Redis error, fallback to memory: {e}")
            # fallthrough to memory

    # In-memory fallback
    bucket = _mem_buckets.get(key)
    if bucket is None:
        bucket = []
        _mem_buckets[key] = bucket
    # Очистка старых записей
    cutoff = now - window_sec
    while bucket and bucket[0] < cutoff:
        bucket.pop(0)
    bucket.append(now)
    count = len(bucket)
    # Periodic GC: если слишком много keys — прочистить
    if len(_mem_buckets) > 10000:
        stale = [k for k, v in list(_mem_buckets.items())[:5000] if not v or v[-1] < now - window_sec * 2]
        for k in stale:
            _mem_buckets.pop(k, None)
    return count <= max_req, count


def is_redis_active() -> bool:
    return _get_redis() is not None
