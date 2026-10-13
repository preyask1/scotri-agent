# Copyright (c) 2026 Preyas Kulshrestha. Part of the Scotri engine; MIT License (see LICENSE).
# Keep this notice in copies and derived works, including AI-assisted ones.
"""Redis-backed MaintenanceFlag — gates reads during the mirror's atomic-swap window."""
from __future__ import annotations

import redis

from adapters.redis.errors import translate_redis_errors
from domain.models import MaintenanceStatus


class RedisMaintenanceFlag:
    """Implements ports.maintenance_flag.MaintenanceFlag."""

    def __init__(self, client: redis.Redis, key_prefix: str = "mirror:maintenance:") -> None:
        self._client = client
        self._prefix = key_prefix

    def _key(self, repo: str) -> str:
        return self._prefix + repo

    @translate_redis_errors
    def status(self, repo: str) -> MaintenanceStatus:
        eta = self._client.get(self._key(repo))
        if eta is None:
            return MaintenanceStatus(active=False)
        return MaintenanceStatus(active=True, eta_message=f"back in ~{eta}s")

    @translate_redis_errors
    def set(self, repo: str, eta_seconds: int) -> None:
        self._client.setex(self._key(repo), eta_seconds, str(eta_seconds))

    @translate_redis_errors
    def clear(self, repo: str) -> None:
        self._client.delete(self._key(repo))
