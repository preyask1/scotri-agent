# Copyright (c) 2026 Preyas Kulshrestha. Part of the Scotri engine; MIT License (see LICENSE).
# Keep this notice in copies and derived works, including AI-assisted ones.
"""Port for the query governor's rate-limit + concurrency tracking (query-governor SKILL.md)."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from domain.models import Backend


@dataclass(frozen=True)
class GovernorState:
    """Read-only snapshot for reporting/debugging — see `RateLimiter.peek_state`."""
    rate_count_this_window: int
    concurrency_in_flight: int


class RateLimiter(Protocol):
    def increment_and_get_count(self, user: str, window_seconds: int) -> int:
        """Atomically increment this user's counter for the current window and return the new count."""
        ...

    def enter_concurrency_slot(self, backend: Backend) -> int:
        """Atomically increment the in-flight counter for `backend` and return the new count.

        Caller is responsible for calling `exit_concurrency_slot` when the query finishes, even on
        error — see adapters/redis/rate_limiter_adapter.py's context-manager helper.
        """
        ...

    def exit_concurrency_slot(self, backend: Backend) -> None:
        """Atomically decrement the in-flight counter for `backend`."""
        ...

    def reset_concurrency_slot(self, backend: Backend) -> int:
        """Force the in-flight counter for `backend` back to 0 and return its prior value.

        Manual escape hatch for `governor-reset` — for a slot leaked by a killed/crashed process
        (enter_concurrency_slot's TTL is a self-healing backstop, not something an operator should
        have to wait out once they've already confirmed nothing is actually in flight)."""
        ...

    def peek_state(self, user: str, backend: Backend, window_seconds: int) -> GovernorState:
        """Read-only: report the current rate count (this user, this window) and the current
        in-flight count (this backend), WITHOUT incrementing either counter. For reporting/debugging
        only — never a substitute for the real accept/reject decision `run_governed` makes, since
        this never acquires a slot or consumes rate-limit quota.
        """
        ...
