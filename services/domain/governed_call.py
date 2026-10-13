# Copyright (c) 2026 Preyas Kulshrestha. Part of the Scotri engine; MIT License (see LICENSE).
# Keep this notice in copies and derived works, including AI-assisted ones.
"""Wraps a governed operation (a Databricks query, a DAX execute) in the full query-governor
sequence as ONE call — cache check, rate limit, concurrency slot, execute, release, cache store,
audit log — so a CLI command never has to orchestrate these steps by hand and risk leaking a stuck
concurrency slot if a step gets skipped partway through.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Callable, TypeVar

from domain.errors import ErrorCategory, ScotriError
from domain.governor_policy import (
    RATE_LIMIT_WINDOW_SECONDS,
    GovernorPolicy,
    decide_concurrency,
    decide_rate_limit,
)
from domain.models import Backend
from domain.query_guard import canonicalize_for_cache
from ports.cache import Cache
from ports.rate_limiter import RateLimiter

logger = logging.getLogger("scotri.governed_call")

T = TypeVar("T")


def _seconds_until_window_reset() -> int:
    """Honest wait for rate-limit relief: the fixed window buckets by `int(time()) // window`, so a
    fresh quota bucket begins at the next window boundary. This is derived from the clock, not
    invented — the query-governor skill forbids a guessed wait, and this is a real one."""
    now = int(time.time())
    return RATE_LIMIT_WINDOW_SECONDS - (now % RATE_LIMIT_WINDOW_SECONDS)


@dataclass(frozen=True)
class GovernedCallResult:
    """Metadata about how the result was obtained — a rejection is always an exception
    (GovernorRejection), never a value in this struct, so `allowed` isn't a field here."""
    from_cache: bool = False


class GovernorRejection(ScotriError):
    """Raised when the governor declines to run the call at all — rate-limited or queued.

    Not the same as the underlying operation failing; this means the operation was never attempted.
    TRANSIENT by default — the right response is to wait and retry, not escalate.

    Carries the structured facts a caller can surface honestly (OPEN_ITEMS.md #22): `queue_position`
    (set for a concurrency rejection) and `wait_seconds` (set for a rate-limit rejection, from the
    fixed window's own reset time — never invented for a concurrency rejection, whose true wait would
    depend on in-flight query durations this workspace doesn't track; the query-governor skill's
    "never invent a wait estimate" rule). Either may be None.
    """

    category = ErrorCategory.TRANSIENT

    def __init__(
        self,
        user_message: str,
        *,
        queue_position: int | None = None,
        wait_seconds: float | None = None,
    ) -> None:
        super().__init__(user_message)
        self.queue_position = queue_position
        self.wait_seconds = wait_seconds


def run_governed(
    *,
    user: str,
    backend: Backend,
    cache_key_input: str,
    rate_limiter: RateLimiter,
    cache: Cache,
    policy: GovernorPolicy,
    execute: Callable[[], T],
    serialize: Callable[[T], str],
    deserialize: Callable[[str], T],
    cache_ttl_seconds: int = 300,
    use_cache: bool = True,
) -> tuple[T, GovernedCallResult]:
    """Run `execute()` under the full governed sequence. Returns (result, metadata).

    `serialize`/`deserialize` round-trip the result through the cache as a string (Redis stores
    strings) — both required, not just `serialize`, so a cache hit returns the same properly-typed
    result a cache miss would, never a raw string the caller has to guess how to interpret.

    `use_cache=False` keeps the rate limit, concurrency slot and audit line but skips the cache both
    ways: for a call whose real result is a side effect on local disk (a Jira attachment download),
    where a cache hit would report a file that may no longer be there.
    """
    # Namespaced by backend: every backend shares one Redis prefix, and the Databricks key
    # (`<catalog>:<sql>`) and the DAX key (`<workspace>:<model>:<dax>:<role>`) carry no tag of their own.
    cache_key = f"{backend.value}:{canonicalize_for_cache(cache_key_input)}"

    cached = cache.get(cache_key) if use_cache else None
    if cached is not None:
        logger.info("governed_call: cache hit for user=%s backend=%s", user, backend.value)
        return deserialize(cached), GovernedCallResult(from_cache=True)

    # Concurrency is checked BEFORE the rate counter is touched (OPEN_ITEMS.md #32c): a call the
    # concurrency cap rejects was never attempted, so it must not consume one of the user's hourly
    # rate-limit slots. Reordering (rather than adding a decrement/rollback) is the fix the open item
    # names — no new RateLimiter method needed. The slot is entered here and always released: on a
    # concurrency rejection immediately below, on a rate rejection just after, or in the finally.
    in_flight = rate_limiter.enter_concurrency_slot(backend)
    concurrency_decision = decide_concurrency(policy, backend, current_in_flight=in_flight)
    if not concurrency_decision.allowed:
        rate_limiter.exit_concurrency_slot(backend)
        # No honest wait_seconds for a concurrency queue — it depends on in-flight query durations
        # this workspace doesn't track; surface the position only, never an invented ETA.
        raise GovernorRejection(
            f"queued — {concurrency_decision.queue_position} ahead of you",
            queue_position=concurrency_decision.queue_position,
        )

    rate_count = rate_limiter.increment_and_get_count(user, window_seconds=RATE_LIMIT_WINDOW_SECONDS)
    rate_decision = decide_rate_limit(policy, current_count_this_window=rate_count)
    if not rate_decision.allowed:
        rate_limiter.exit_concurrency_slot(backend)  # release the slot we just acquired
        raise GovernorRejection(
            rate_decision.reason or "rate limited",
            wait_seconds=_seconds_until_window_reset(),
        )

    start = time.monotonic()
    try:
        result = execute()
    finally:
        rate_limiter.exit_concurrency_slot(backend)
    duration = time.monotonic() - start

    logger.info(
        "governed_call: user=%s backend=%s duration=%.2fs cache_key=%s",
        user, backend.value, duration, cache_key,
    )
    if use_cache:
        cache.set(cache_key, serialize(result), ttl_seconds=cache_ttl_seconds)
    return result, GovernedCallResult(from_cache=False)


def run_governed_write(
    *,
    user: str,
    backend: Backend,
    action: str,
    execute: Callable[[], T],
) -> T:
    """Run a mutating call with **audit logging only** — no cache, no rate-limit count, no
    concurrency slot (OPEN_ITEMS.md #29, decided 2026-09-30).

    Why not the full `run_governed` sequence for a write:
    - **No cache**: a write must never be served or deduplicated from cache — that would silently
      drop a real second write, or return a stale "success" for one that never happened.
    - **No rate count**: a write here is a single, deliberate, user-approved action (the `jira-write`
      confirm gate), never a burst — an unlogged write is the real hole this closes, not an
      unthrottled one (the decision on the row explicitly: "audited but not rate-limited").
    - **No concurrency slot**: same reason — a one-off approved write shouldn't be able to draw a
      spurious "queued" rejection between a user's approval and the post landing.

    What it DOES do is emit the same governor audit line reads/queries emit (logger `scotri.
    governed_call`, which propagates to `logs/scotri-cli.log`), so a Jira write finally leaves a
    trace on this host — the PROJ-2085 comment (id 113081) left none, which is what raised #29. The
    audit line records who, which backend, which action, the duration, and the outcome — never the
    written content itself (that would put the very payload the redactor strips back into the log)."""
    start = time.monotonic()
    try:
        result = execute()
    except Exception:
        logger.info(
            "governed_write: user=%s backend=%s action=%s outcome=error",
            user, backend.value, action,
        )
        raise
    duration = time.monotonic() - start
    logger.info(
        "governed_write: user=%s backend=%s action=%s duration=%.2fs outcome=ok",
        user, backend.value, action, duration,
    )
    return result
