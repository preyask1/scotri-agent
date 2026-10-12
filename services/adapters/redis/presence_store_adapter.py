"""Redis-backed PresenceStore (presence-claim SKILL.md).

Each claim is a Redis hash (owner, started_at, last_heartbeat as ISO timestamps) with a TTL equal
to domain.presence_policy.CLAIM_INACTIVITY_TIMEOUT, refreshed on every heartbeat — so an abandoned
claim expires on its own without any adapter needing to run a cleanup sweep.
"""
from __future__ import annotations

from datetime import datetime, timezone

import redis

from adapters.redis.errors import translate_redis_errors
from domain.models import Claim
from domain.presence_policy import CLAIM_INACTIVITY_TIMEOUT


class RedisPresenceStore:
    """Implements ports.presence_store.PresenceStore."""

    def __init__(self, client: redis.Redis, key_prefix: str = "presence:claim:",
                 run_prefix: str = "presence:run:") -> None:
        self._client = client
        self._prefix = key_prefix
        self._run_prefix = run_prefix
        self._ttl_seconds = int(CLAIM_INACTIVITY_TIMEOUT.total_seconds())

    def _key(self, entity: str) -> str:
        return self._prefix + entity

    @translate_redis_errors
    def get_claim(self, entity: str) -> Claim | None:
        data = self._client.hgetall(self._key(entity))
        if not data:
            return None
        return Claim(
            entity=entity,
            owner=data["owner"],
            started_at=datetime.fromisoformat(data["started_at"]),
            last_heartbeat=datetime.fromisoformat(data["last_heartbeat"]),
        )

    @translate_redis_errors
    def set_claim(self, entity: str, owner: str) -> None:
        now = datetime.now(timezone.utc).isoformat()
        key = self._key(entity)
        self._client.hset(key, mapping={"owner": owner, "started_at": now, "last_heartbeat": now})
        self._client.expire(key, self._ttl_seconds)

    @translate_redis_errors
    def heartbeat(self, entity: str, owner: str) -> None:
        key = self._key(entity)
        current_owner = self._client.hget(key, "owner")
        if current_owner != owner:
            # Someone else's claim (or none exists) — don't refresh a claim that isn't yours.
            return
        self._client.hset(key, "last_heartbeat", datetime.now(timezone.utc).isoformat())
        self._client.expire(key, self._ttl_seconds)

    @translate_redis_errors
    def release_claim(self, entity: str, owner: str) -> None:
        key = self._key(entity)
        current_owner = self._client.hget(key, "owner")
        if current_owner == owner:
            self._client.delete(key)

    @translate_redis_errors
    def track_run_claim(self, job_id: str, entity: str) -> None:
        key = self._run_prefix + job_id
        self._client.sadd(key, entity)
        # Outlives any engagement (20-minute limit plus queueing); a run that dies leaves only this.
        self._client.expire(key, 4 * 3600)

    @translate_redis_errors
    def run_claims(self, job_id: str) -> list[str]:
        return sorted(self._client.smembers(self._run_prefix + job_id))

    @translate_redis_errors
    def forget_run(self, job_id: str) -> None:
        self._client.delete(self._run_prefix + job_id)
