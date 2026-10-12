"""Port for the mirror sync lock: at most one sync run at a time across every process that can start
one (the sync service's schedule, `sync-run-once`, the admin panel's manual sync)."""
from __future__ import annotations

from typing import Protocol


class SyncLock(Protocol):
    def acquire(self, holder: str, ttl_seconds: int) -> str | None:
        """Take the lock for `ttl_seconds`. Returns a token to pass back, or None if it is held."""
        ...

    def extend(self, token: str, ttl_seconds: int) -> bool:
        """Push the expiry out, only if `token` still holds the lock. False means it was lost."""
        ...

    def release(self, token: str) -> None:
        """Drop the lock, only if `token` still holds it."""
        ...

    def holder(self) -> str | None:
        """Who holds the lock now, or None."""
        ...
