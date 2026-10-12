"""Redis-backed per-run identity store — implements ports.run_context.RunContextStore.

Keyed by the token's SHA-256, never the token itself, so listing `runctx:*` reveals no usable token.
Same client and pattern as the job store; on Azure this is Azure Cache for Redis, config only.
"""
from __future__ import annotations

import redis

from domain.run_context import RunContext, new_token, token_digest


class RedisRunContextStore:
    def __init__(self, client: redis.Redis, key_prefix: str = "runctx:") -> None:
        self._client = client
        self._prefix = key_prefix

    def _key(self, token: str) -> str:
        return self._prefix + token_digest(token)

    def issue(self, ctx: RunContext, ttl_seconds: int) -> str:
        token = new_token()
        self._client.set(self._key(token), ctx.to_json(), ex=max(1, int(ttl_seconds)))
        return token

    def resolve(self, token: str) -> RunContext | None:
        data = self._client.get(self._key(token))
        if data is None:
            return None
        return RunContext.from_json(data.decode("utf-8") if isinstance(data, bytes) else data)

    def revoke(self, token: str) -> None:
        self._client.delete(self._key(token))
