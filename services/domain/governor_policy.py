# Copyright (c) 2026 Preyas Kulshrestha. Part of the Scotri engine; MIT License (see LICENSE).
# Keep this notice in copies and derived works, including AI-assisted ones.
"""Rate-limit and concurrency policy for the query governor (query-governor SKILL.md).

Pure decision logic: given counters read from a store (any store — this module doesn't know or
care that it's Redis), decide whether a request is allowed, queued, or should wait. The Redis
adapter (adapters/redis/rate_limiter_adapter.py) owns the actual atomic increment/expiry mechanics;
this module owns the *policy* of what those numbers mean.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field

from domain.models import Backend, RateLimitDecision

# Defaults when the environment says nothing. Set them per deployment in services/.env (OPEN_ITEMS.md
# #3): GOVERNOR_RATE_LIMIT_PER_USER_PER_HOUR, GOVERNOR_MAX_CONCURRENT (every backend) and
# GOVERNOR_MAX_CONCURRENT_<BACKEND> (one backend, e.g. GOVERNOR_MAX_CONCURRENT_DATABRICKS).
DEFAULT_RATE_LIMIT_PER_USER_PER_HOUR = 30
DEFAULT_MAX_CONCURRENT_PER_BACKEND = {
    Backend.DATABRICKS: 5,
    Backend.FABRIC: 5,
    Backend.ADO: 5,
    Backend.ADF: 5,
    Backend.JIRA: 5,  # OPEN_ITEMS.md #29 — Jira reads are governed like any other backend. Writes
                      # don't take a slot (run_governed_write is audit-only), so this cap only ever
                      # bounds concurrent Jira READS (a backlog sweep issuing JQL + per-ticket reads).
}
RATE_LIMIT_WINDOW_SECONDS = 3600


def _env_int(name: str, default: int) -> int:
    raw = (os.environ.get(name) or "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        raise ValueError(f"{name}={raw!r} is not a whole number") from None
    if value < 1:
        raise ValueError(f"{name} must be at least 1, got {value}")
    return value


def configured_rate_limit() -> int:
    return _env_int("GOVERNOR_RATE_LIMIT_PER_USER_PER_HOUR", DEFAULT_RATE_LIMIT_PER_USER_PER_HOUR)


def configured_concurrency() -> dict[Backend, int]:
    every = _env_int("GOVERNOR_MAX_CONCURRENT", 0)  # 0: not set, keep each backend's default
    return {backend: _env_int(f"GOVERNOR_MAX_CONCURRENT_{backend.name}", every or default)
            for backend, default in DEFAULT_MAX_CONCURRENT_PER_BACKEND.items()}


@dataclass(frozen=True)
class GovernorPolicy:
    rate_limit_per_user_per_hour: int = field(default_factory=configured_rate_limit)
    max_concurrent_per_backend: dict[Backend, int] = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        if self.max_concurrent_per_backend is None:
            object.__setattr__(self, "max_concurrent_per_backend", configured_concurrency())


def decide_rate_limit(policy: GovernorPolicy, current_count_this_window: int) -> RateLimitDecision:
    """Given how many requests this user has already made in the current window, decide."""
    if current_count_this_window < policy.rate_limit_per_user_per_hour:
        return RateLimitDecision(allowed=True)
    return RateLimitDecision(
        allowed=False,
        wait_seconds=None,  # the caller knows the window reset time, not this pure function
        reason=(
            f"rate limit reached: {current_count_this_window}/"
            f"{policy.rate_limit_per_user_per_hour} this hour"
        ),
    )


def decide_concurrency(
    policy: GovernorPolicy, backend: Backend, current_in_flight: int
) -> RateLimitDecision:
    """Given how many requests are currently in flight against `backend`, decide."""
    limit = policy.max_concurrent_per_backend[backend]
    if current_in_flight < limit:
        return RateLimitDecision(allowed=True)
    return RateLimitDecision(
        allowed=False,
        queue_position=current_in_flight - limit + 1,
        reason=f"concurrency cap reached for {backend.value}: {current_in_flight}/{limit} in flight",
    )
