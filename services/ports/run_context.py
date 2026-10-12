"""Port for the per-run identity store (see domain/run_context.py for the why)."""
from __future__ import annotations

from typing import Protocol

from domain.run_context import RunContext


class RunContextStore(Protocol):
    def issue(self, ctx: RunContext, ttl_seconds: int) -> str:
        """Store `ctx` and return a fresh opaque token for it. Only the token's hash is stored."""
        ...

    def resolve(self, token: str) -> RunContext | None:
        """The context the token was issued for, or None if unknown/expired/revoked."""
        ...

    def revoke(self, token: str) -> None:
        ...
