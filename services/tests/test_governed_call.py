# Copyright (c) 2026 Preyas Kulshrestha. Part of the Scotri engine; MIT License (see LICENSE).
# Keep this notice in copies and derived works, including AI-assisted ones.
"""domain/governed_call.py — the full governed sequence (cache -> concurrency -> rate -> execute),
tested with fake Cache/RateLimiter doubles (no Redis). Covers the OPEN_ITEMS.md #32c fix (a
concurrency-rejected call must NOT burn a rate-limit slot), slot release on every exit path, and the
#22 structured rejection fields (queue_position / wait_seconds)."""
from __future__ import annotations

import pytest

from domain.governed_call import GovernorRejection, run_governed, run_governed_write
from domain.governor_policy import GovernorPolicy
from domain.models import Backend


class FakeCache:
    def __init__(self, hit_value: str | None = None) -> None:
        self._hit_value = hit_value          # returned for any get() -> simulates a hit
        self.store: dict[str, str] = {}

    def get(self, key: str) -> str | None:
        if self._hit_value is not None:
            return self._hit_value
        return self.store.get(key)

    def set(self, key: str, value: str, ttl_seconds: int) -> None:
        self.store[key] = value


class FakeRateLimiter:
    def __init__(self, enter_return: int = 1, rate_return: int = 1) -> None:
        self._enter_return = enter_return
        self._rate_return = rate_return
        self.rate_calls = 0
        self.enter_calls = 0
        self.exit_calls = 0

    def increment_and_get_count(self, user: str, window_seconds: int) -> int:
        self.rate_calls += 1
        return self._rate_return

    def enter_concurrency_slot(self, backend: Backend) -> int:
        self.enter_calls += 1
        return self._enter_return

    def exit_concurrency_slot(self, backend: Backend) -> None:
        self.exit_calls += 1

    def reset_concurrency_slot(self, backend: Backend) -> int:  # unused here
        return 0


def _run(cache, limiter, execute):
    return run_governed(
        user="alice",
        backend=Backend.DATABRICKS,
        cache_key_input="acme_prod:SELECT 1",
        rate_limiter=limiter,
        cache=cache,
        policy=GovernorPolicy(),
        execute=execute,
        serialize=lambda r: r,
        deserialize=lambda s: s,
    )


def test_cache_hit_touches_no_counters():
    cache = FakeCache(hit_value="CACHED")
    limiter = FakeRateLimiter()
    result, meta = _run(cache, limiter, execute=lambda: pytest.fail("execute must not run on a cache hit"))
    assert result == "CACHED"
    assert meta.from_cache is True
    assert limiter.rate_calls == 0
    assert limiter.enter_calls == 0


def test_miss_then_success_balances_slot_and_caches():
    cache = FakeCache()
    limiter = FakeRateLimiter(enter_return=1, rate_return=1)
    calls = []
    result, meta = _run(cache, limiter, execute=lambda: calls.append(1) or "RESULT")
    assert result == "RESULT"
    assert meta.from_cache is False
    assert calls == [1]
    assert limiter.rate_calls == 1
    assert limiter.enter_calls == 1 and limiter.exit_calls == 1  # balanced
    assert cache.store  # result was cached


def test_concurrency_rejection_does_not_burn_rate_quota():
    """The #32c fix: a call the concurrency cap rejects was never attempted, so it must not consume
    one of the user's hourly rate slots."""
    cache = FakeCache()
    limiter = FakeRateLimiter(enter_return=5)  # cap is 5 -> in_flight 5 is rejected
    with pytest.raises(GovernorRejection) as ei:
        _run(cache, limiter, execute=lambda: pytest.fail("execute must not run when queued"))
    assert limiter.rate_calls == 0                       # <-- the bug this test locks down
    assert limiter.enter_calls == 1 and limiter.exit_calls == 1  # slot entered then released
    assert ei.value.queue_position == 1
    assert ei.value.wait_seconds is None                 # no invented ETA for a concurrency queue


def test_rate_rejection_releases_slot_and_reports_wait():
    cache = FakeCache()
    limiter = FakeRateLimiter(enter_return=1, rate_return=30)  # default cap 30 -> 30 is over
    with pytest.raises(GovernorRejection) as ei:
        _run(cache, limiter, execute=lambda: pytest.fail("execute must not run when rate-limited"))
    assert limiter.enter_calls == 1 and limiter.exit_calls == 1  # slot not leaked
    assert ei.value.queue_position is None
    assert ei.value.wait_seconds is not None and 0 < ei.value.wait_seconds <= 3600


def test_execute_exception_releases_slot_and_does_not_cache():
    cache = FakeCache()
    limiter = FakeRateLimiter()

    def boom():
        raise ValueError("boom")

    with pytest.raises(ValueError):
        _run(cache, limiter, execute=boom)
    assert limiter.exit_calls == 1   # released via finally
    assert not cache.store           # nothing cached on failure


# --- run_governed_write: audit-only path for mutating calls (OPEN_ITEMS.md #29) ---------------

def test_write_runs_execute_without_touching_any_governor_counter():
    """A Jira write is audit-only: no cache, no rate count, no concurrency slot — decided
    'audited but not rate-limited'. Locks down that run_governed_write never calls the limiter."""
    limiter = FakeRateLimiter()
    cache = FakeCache()
    calls = []
    result = run_governed_write(
        user="alice", backend=Backend.JIRA, action="comment:ACME-1",
        execute=lambda: calls.append(1) or "COMMENT-113081",
    )
    assert result == "COMMENT-113081"
    assert calls == [1]
    assert limiter.rate_calls == 0 and limiter.enter_calls == 0 and limiter.exit_calls == 0
    assert not cache.store  # a write is never cached — never served or deduplicated from cache


def test_write_propagates_execute_exception():
    """A failed write must still surface (and, though not asserted here, log an error audit line) —
    never swallowed into a false success."""
    def boom():
        raise ValueError("jira 403")

    with pytest.raises(ValueError):
        run_governed_write(user="alice", backend=Backend.JIRA, action="comment:ACME-1", execute=boom)
