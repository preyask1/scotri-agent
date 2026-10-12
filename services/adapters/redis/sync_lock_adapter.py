"""Redis-backed SyncLock. One key, `SET NX EX`; extend and release compare the token first, so a run
whose lock expired can never drop or extend the next run's lock."""
from __future__ import annotations

import uuid

import redis

from adapters.redis.errors import translate_redis_errors

_EXTEND = """
if redis.call('get', KEYS[1]) == ARGV[1] then return redis.call('expire', KEYS[1], ARGV[2]) end
return 0
"""
_RELEASE = """
if redis.call('get', KEYS[1]) == ARGV[1] then return redis.call('del', KEYS[1]) end
return 0
"""


class RedisSyncLock:
    """Implements ports.sync_lock.SyncLock. The stored value is `<token>|<holder>`."""

    def __init__(self, client: redis.Redis, key: str = "mirror:sync-lock") -> None:
        self._client = client
        self._key = key

    def _value(self, token: str) -> str | None:
        raw = self._client.get(self._key)
        if raw is None:
            return None
        raw = raw.decode() if isinstance(raw, bytes) else raw
        return raw if raw.split("|", 1)[0] == token else None

    @translate_redis_errors
    def acquire(self, holder: str, ttl_seconds: int) -> str | None:
        token = uuid.uuid4().hex
        if self._client.set(self._key, f"{token}|{holder}", nx=True, ex=ttl_seconds):
            return token
        return None

    @translate_redis_errors
    def extend(self, token: str, ttl_seconds: int) -> bool:
        value = self._value(token)
        return value is not None and bool(self._client.eval(_EXTEND, 1, self._key, value, ttl_seconds))

    @translate_redis_errors
    def release(self, token: str) -> None:
        value = self._value(token)
        if value is not None:
            self._client.eval(_RELEASE, 1, self._key, value)

    @translate_redis_errors
    def holder(self) -> str | None:
        raw = self._client.get(self._key)
        if raw is None:
            return None
        raw = raw.decode() if isinstance(raw, bytes) else raw
        return raw.split("|", 1)[1] if "|" in raw else "unknown"
