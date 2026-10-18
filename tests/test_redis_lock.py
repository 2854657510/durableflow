import fakeredis

from app.core.redis_lock import RedisLock


def test_lock_is_exclusive_and_released():
    redis = fakeredis.FakeRedis(decode_responses=True)
    lock1 = RedisLock(redis, "demo", ttl_ms=5000)
    lock2 = RedisLock(redis, "demo", ttl_ms=5000)

    assert lock1.acquire() is True
    assert lock2.acquire() is False
    assert lock1.extend() is True
    assert lock1.release() is True
    assert lock2.acquire() is True
    assert lock2.release() is True


def test_wrong_owner_cannot_release():
    redis = fakeredis.FakeRedis(decode_responses=True)
    lock1 = RedisLock(redis, "demo", ttl_ms=5000)
    lock2 = RedisLock(redis, "demo", ttl_ms=5000)
    assert lock1.acquire() is True
    assert lock2.release() is False
    assert lock1.release() is True
