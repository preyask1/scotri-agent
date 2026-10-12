"""Ports for the admin surface (OPEN_ITEMS.md #43): the caller-app register and the admin audit log.

Both are Scotri's own state. Who may call them is decided at the route (`ADMIN_ROLES`); these ports
trust their arguments, like ports/engagement_history.py.
"""
from __future__ import annotations

from typing import Protocol

from domain.admin import AdminAuditEntry, CallerApp


class CallerRegistry(Protocol):
    def list(self) -> list[CallerApp]:
        """Every registered caller app, by app_id."""
        ...

    def get(self, app_id: str) -> CallerApp | None:
        ...

    def upsert(self, app: CallerApp) -> None:
        """Insert or replace one record. `registered_by`/`registered_at` of an existing record are
        kept; the caller passes the new `updated_at`."""
        ...

    def delete(self, app_id: str) -> bool:
        """True if a record was removed."""
        ...


class AdminAuditLog(Protocol):
    def append(self, entry: AdminAuditEntry) -> None:
        """Record one admin action. Raises if the line cannot be written: an admin action that
        cannot be audited must not go ahead."""
        ...

    def tail(self, limit: int) -> list[dict]:
        """The most recent `limit` entries, newest last."""
        ...
