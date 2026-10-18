from __future__ import annotations

import threading
import time
import uuid
from collections import deque

import redis

from app.config import Settings


class RateLimiter:
    """滑动窗口限流。优先 Redis，Redis 不可用时退化为进程内。"""

    def __init__(self, settings: Settings):
        self.limit = settings.rate_limit_per_minute
        self.window = 60
        self._lock = threading.Lock()
        self._local: dict[str, deque[float]] = {}
        self._redis: redis.Redis | None = None
        self._script = None
        if settings.redis_enabled:
            try:
                client = redis.Redis.from_url(settings.redis_url, decode_responses=True, socket_timeout=1)
                client.ping()
                self._redis = client
                self._script = client.register_script(
                    """
                    local key = KEYS[1]
                    local now = tonumber(ARGV[1])
                    local window = tonumber(ARGV[2])
                    local limit = tonumber(ARGV[3])
                    local member = ARGV[4]
                    redis.call('ZREMRANGEBYSCORE', key, 0, now - window)
                    local count = redis.call('ZCARD', key)
                    if count >= limit then
                        return 0
                    end
                    redis.call('ZADD', key, now, member)
                    redis.call('EXPIRE', key, window)
                    return 1
                    """
                )
            except Exception:  # noqa: BLE001 - 限流不能阻塞主链路
                self._redis = None

    @property
    def backend(self) -> str:
        return "redis" if self._redis is not None else "memory"

    def allow(self, key: str) -> bool:
        if self.limit <= 0:
            return True
        now = time.time()
        if self._redis is not None and self._script is not None:
            try:
                allowed = self._script(
                    keys=[f"durableflow:rl:{key}"],
                    args=[now, self.window, self.limit, f"{now}:{uuid.uuid4().hex}"],
                )
                return bool(allowed)
            except Exception:  # noqa: BLE001 - Redis 故障降级到本地
                pass

        with self._lock:
            bucket = self._local.setdefault(key, deque())
            cutoff = now - self.window
            while bucket and bucket[0] < cutoff:
                bucket.popleft()
            if len(bucket) >= self.limit:
                return False
            bucket.append(now)
            return True
