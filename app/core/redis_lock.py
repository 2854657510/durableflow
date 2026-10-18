from __future__ import annotations

import uuid

_RELEASE_LUA = """
if redis.call('get', KEYS[1]) == ARGV[1] then
    return redis.call('del', KEYS[1])
end
return 0
"""

_EXTEND_LUA = """
if redis.call('get', KEYS[1]) == ARGV[1] then
    return redis.call('pexpire', KEYS[1], ARGV[2])
end
return 0
"""


class RedisLock:
    """基于 Redis 的轻量分布式锁。

    正确性提醒：Redis 锁可能因为主从切换/过期/网络分区失效，不能作为
    唯一正确性保证。核心任务状态仍然以 PostgreSQL 行锁和唯一约束为准。
    这个锁只用于“减少重复工作”这类可降级的场景。
    """

    def __init__(self, redis_client, key: str, *, ttl_ms: int = 10_000):
        self.redis = redis_client
        self.key = f"durableflow:lock:{key}"
        self.token = uuid.uuid4().hex
        self.ttl_ms = ttl_ms

    def acquire(self) -> bool:
        return bool(self.redis.set(self.key, self.token, nx=True, px=self.ttl_ms))

    def release(self) -> bool:
        return bool(self.redis.eval(_RELEASE_LUA, 1, self.key, self.token))

    def extend(self) -> bool:
        return bool(self.redis.eval(_EXTEND_LUA, 1, self.key, self.token, self.ttl_ms))

    def __enter__(self) -> "RedisLock":
        if not self.acquire():
            raise TimeoutError(f"获取锁失败: {self.key}")
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.release()
