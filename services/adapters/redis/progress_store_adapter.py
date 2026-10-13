# Copyright (c) 2026 Preyas Kulshrestha. Part of the Scotri engine; MIT License (see LICENSE).
# Keep this notice in copies and derived works, including AI-assisted ones.
"""Redis-backed ProgressStore (domain/engagement_progress.py). One key per job, same TTL as the job
record (RedisJobStore) so the two expire together."""
from __future__ import annotations

import redis

from adapters.redis.errors import translate_redis_errors
from domain.engagement_progress import EngagementProgress

_DEFAULT_TTL_SECONDS = 24 * 60 * 60


class RedisProgressStore:
    """Implements ports.progress_store.ProgressStore."""

    def __init__(self, client: redis.Redis, key_prefix: str = "engagement-progress:",
                 ttl_seconds: int = _DEFAULT_TTL_SECONDS) -> None:
        self._client = client
        self._prefix = key_prefix
        self._ttl_seconds = ttl_seconds

    @translate_redis_errors
    def put(self, progress: EngagementProgress) -> None:
        self._client.set(self._prefix + progress.job_id, progress.to_json(), ex=self._ttl_seconds)

    @translate_redis_errors
    def get(self, job_id: str) -> EngagementProgress | None:
        data = self._client.get(self._prefix + job_id)
        return None if data is None else EngagementProgress.from_json(data)
