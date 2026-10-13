# Copyright (c) 2026 Preyas Kulshrestha. Part of the Scotri engine; MIT License (see LICENSE).
# Keep this notice in copies and derived works, including AI-assisted ones.
"""Port for Databricks SQL execution (databricks-query SKILL.md).

`domain.query_guard.assert_select_only` must be called (by the adapter, or by whoever composes the
call) before `execute` ever reaches a real statement — this port doesn't enforce that itself, so
don't call an adapter's `execute` directly without going through the governed path.
"""
from __future__ import annotations

from typing import Protocol

from domain.models import QueryResult


class QueryExecutor(Protocol):
    def execute(self, catalog: str, statement: str) -> QueryResult:
        ...

    def describe_table(self, catalog: str, schema: str, table: str) -> QueryResult:
        ...

    def current_user(self) -> str:
        """Confirm the service-principal credential is live — see databricks-query SKILL.md step 1."""
        ...
