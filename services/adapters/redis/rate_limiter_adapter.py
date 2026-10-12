"""Redis-backed RateLimiter (query-governor SKILL.md).

Fixed-window counting for the per-user rate limit; a simple INCR/DECR pair for the per-backend
concurrency semaphore. Good enough for the first build — if concurrency counting under heavy
contention ever needs stricter atomicity than INCR/DECR provides, that's the point to move to a Lua
script (EVAL), not before; no need to add that complexity ahead of evidence it's needed.
"""
from __future__ import annotations

import time

import redis

from adapters.redis.errors import translate_redis_errors
from domain.models import Backend
from ports.rate_limiter import GovernorState

# Purely a crash-safety net, not a normal-operation limit: far longer than any real governed query
# (a SELECT/DESCRIBE/SHOW or a DAX Execute) should ever take. If the process holding a concurrency
# slot is killed/crashes/loses its Redis connection before governed_call.py's `finally` can decrement
# it, this bounds how long the leaked slot survives instead of it staying stuck forever (confirmed
# live 2026-09-25: governor:concurrency:databricks was sitting at 1 with no TTL and no way for it to
# ever recover on its own). Set with NX so only the FIRST concurrent holder starts the clock — a
# second caller entering while one is already in flight must not reset how long the first has been
# counted, or a steady stream of overlapping calls could keep the TTL from ever elapsing.
CONCURRENCY_SLOT_TTL_SECONDS = 900


class RedisRateLimiter:
    """Implements ports.rate_limiter.RateLimiter."""

    def __init__(self, client: redis.Redis) -> None:
        self._client = client

    @translate_redis_errors
    def increment_and_get_count(self, user: str, window_seconds: int) -> int:
        window_start = int(time.time()) // window_seconds
        key = f"governor:rate:{user}:{window_start}"
        pipe = self._client.pipeline()
        pipe.incr(key)
        pipe.expire(key, window_seconds)
        count, _ = pipe.execute()
        return int(count)

    @translate_redis_errors
    def enter_concurrency_slot(self, backend: Backend) -> int:
        key = f"governor:concurrency:{backend.value}"
        pipe = self._client.pipeline()
        pipe.incr(key)
        pipe.expire(key, CONCURRENCY_SLOT_TTL_SECONDS, nx=True)
        count, _ = pipe.execute()
        return int(count)

    @translate_redis_errors
    def exit_concurrency_slot(self, backend: Backend) -> None:
        key = f"governor:concurrency:{backend.value}"
        new_value = self._client.decr(key)
        if new_value < 0:
            # Should never happen if callers always pair enter/exit — reset rather than let a
            # negative counter permanently under-report concurrency.
            self._client.set(key, 0)

    @translate_redis_errors
    def reset_concurrency_slot(self, backend: Backend) -> int:
        key = f"governor:concurrency:{backend.value}"
        pipe = self._client.pipeline()
        pipe.get(key)
        pipe.set(key, 0)
        previous, _ = pipe.execute()
        return int(previous) if previous is not None else 0

    @translate_redis_errors
    def peek_state(self, user: str, backend: Backend, window_seconds: int) -> GovernorState:
        """GET, never INCR — reporting only. Uses the same key scheme as the real counters above so
        the reported numbers reflect actual state, without perturbing it."""
        window_start = int(time.time()) // window_seconds
        rate_key = f"governor:rate:{user}:{window_start}"
        concurrency_key = f"governor:concurrency:{backend.value}"
        pipe = self._client.pipeline()
        pipe.get(rate_key)
        pipe.get(concurrency_key)
        rate_raw, concurrency_raw = pipe.execute()
        return GovernorState(
            rate_count_this_window=int(rate_raw) if rate_raw is not None else 0,
            concurrency_in_flight=int(concurrency_raw) if concurrency_raw is not None else 0,
        )
