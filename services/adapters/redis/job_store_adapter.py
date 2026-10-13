# Copyright (c) 2026 Preyas Kulshrestha. Part of the Scotri engine; MIT License (see LICENSE).
# Keep this notice in copies and derived works, including AI-assisted ones.
"""Redis-backed JobStore (domain/engagement.py).

One string key per job_id, holding Engagement.to_json(), with a TTL so a finished job's record
expires on its own rather than needing a cleanup sweep — same shape as RedisPresenceStore's claim
TTL, same reasoning.
"""
from __future__ import annotations

import redis

from adapters.redis.errors import translate_redis_errors
from domain.engagement import Engagement, is_terminal

_DEFAULT_TTL_SECONDS = 24 * 60 * 60


class RedisJobStore:
    """Implements ports.job_store.JobStore."""

    def __init__(self, client: redis.Redis, key_prefix: str = "engagement:", ttl_seconds: int = _DEFAULT_TTL_SECONDS) -> None:
        self._client = client
        self._prefix = key_prefix
        self._ttl_seconds = ttl_seconds

    def _key(self, job_id: str) -> str:
        return self._prefix + job_id

    @translate_redis_errors
    def create(self, engagement: Engagement) -> None:
        self._client.set(self._key(engagement.job_id), engagement.to_json(), ex=self._ttl_seconds)

    @translate_redis_errors
    def get(self, job_id: str) -> Engagement | None:
        data = self._client.get(self._key(job_id))
        if data is None:
            return None
        return Engagement.from_json(data)

    @translate_redis_errors
    def list_active(self) -> list[Engagement]:
        active: list[Engagement] = []
        for key in self._client.scan_iter(match=self._prefix + "*"):
            data = self._client.get(key)
            if data is None:  # expired between the scan and the get
                continue
            engagement = Engagement.from_json(data)
            if not is_terminal(engagement.status):
                active.append(engagement)
        return active

    @translate_redis_errors
    def update(self, engagement: Engagement) -> None:
        # Re-set with a fresh TTL rather than a bare overwrite — a long-running engagement
        # shouldn't have its record expire mid-flight just because it was created a while ago.
        self._client.set(self._key(engagement.job_id), engagement.to_json(), ex=self._ttl_seconds)
