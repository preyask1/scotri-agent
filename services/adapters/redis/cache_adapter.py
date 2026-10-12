"""Redis-backed Cache (query-governor SKILL.md) — the result cache, keyed on the canonicalized query."""
from __future__ import annotations

import redis

from adapters.redis.errors import translate_redis_errors


class RedisCache:
    """Implements ports.cache.Cache."""

    def __init__(self, client: redis.Redis, key_prefix: str = "governor:cache:") -> None:
        self._client = client
        self._prefix = key_prefix

    @translate_redis_errors
    def get(self, key: str) -> str | None:
        return self._client.get(self._prefix + key)

    @translate_redis_errors
    def set(self, key: str, value: str, ttl_seconds: int) -> None:
        self._client.setex(self._prefix + key, ttl_seconds, value)
