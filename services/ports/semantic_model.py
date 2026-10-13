# Copyright (c) 2026 Preyas Kulshrestha. Part of the Scotri engine; MIT License (see LICENSE).
# Keep this notice in copies and derived works, including AI-assisted ones.
"""Port for read-only DAX execution and refresh-history reads against a published Fabric semantic
model (dax-debug SKILL.md; refresh-history added OPEN_ITEMS.md 2026-09-27, fabric-read SKILL.md's
"Fabric refresh failures" section)."""
from __future__ import annotations

from typing import Protocol

from domain.models import QueryResult


class SemanticModel(Protocol):
    def execute_dax(
        self, workspace: str, model: str, dax: str, role: str | None = None
    ) -> QueryResult:
        """Execute a read-only DAX query. `role` optionally scopes to an RLS role for an RLS check."""
        ...

    def get_refresh_history(self, workspace: str, model: str, top: int = 10) -> list[dict]:
        """The most recent `top` refresh attempts for this dataset — status, start/end time,
        refresh type, and the service-exception detail on a failure. Read-only ("Get Refresh
        History In Group"); never triggers a refresh."""
        ...

    def get_refresh_execution_details(self, workspace: str, model: str, refresh_id: str) -> dict:
        """Per-partition/table detail for one refresh id (from get_refresh_history). Only ever
        populated for a dataset on Enhanced Refresh — for a Standard-refresh dataset this returns
        `{"available": False, "reason": "..."}` rather than raising, per OPEN_ITEMS.md decision 2
        (the caller reports 'top-level status only' instead of treating that as a failure)."""
        ...
