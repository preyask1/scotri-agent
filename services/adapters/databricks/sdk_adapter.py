"""Databricks adapter (databricks-query SKILL.md) — service-principal M2M OAuth, SELECT-only.

Calls domain.query_guard.assert_select_only before every statement reaches the SDK — the
application-side half of "defense in depth"; the service principal's SELECT-only grant on both
catalogs is the platform-side half, and both must independently hold (see agent-instructions.md).
"""
from __future__ import annotations

from databricks.sdk import WorkspaceClient
from databricks.sdk.service.sql import StatementState

from domain.errors import ErrorCategory, ScotriError
from domain.models import QueryResult
from domain.query_guard import assert_select_only
from ports.secret_provider import SecretProvider


class DatabricksSdkQueryExecutor:
    """Implements ports.query_executor.QueryExecutor."""

    def __init__(self, secrets: SecretProvider) -> None:
        self._warehouse_id = secrets.get("DATABRICKS_SQL_WAREHOUSE_ID")
        self._client = WorkspaceClient(
            host=secrets.get("DATABRICKS_HOST"),
            client_id=secrets.get("DATABRICKS_CLIENT_ID"),
            client_secret=secrets.get("DATABRICKS_CLIENT_SECRET"),
        )

    def current_user(self) -> str:
        return self._client.current_user.me().user_name or "unknown"

    def execute(self, catalog: str, statement: str) -> QueryResult:
        assert_select_only(statement)
        response = self._client.statement_execution.execute_statement(
            warehouse_id=self._warehouse_id,
            catalog=catalog,
            statement=statement,
        )
        status = response.status

        # CRITICAL: a failed statement must never be reported as "0 rows" — confirmed 2026-09-18,
        # a PERMISSION_DENIED response was silently returned as an empty-but-successful QueryResult
        # before this check existed. "Never fabricate a result" (agent-instructions.md) applies to
        # this adapter's own error handling just as much as to the LLM layer above it.
        if status is not None and status.state in (StatementState.FAILED, StatementState.CANCELED):
            error = status.error
            message = error.message if error else f"statement {status.state.value} with no error detail"
            category = ErrorCategory.CREDENTIAL if "PERMISSION_DENIED" in (message or "") else ErrorCategory.UNKNOWN
            raise ScotriError(
                f"Databricks statement failed [{status.state.value}]: {message}",
                category=category,
            )

        result = response.result
        columns = [c.name for c in response.manifest.schema.columns] if response.manifest else []
        rows = [tuple(row) for row in (result.data_array or [])] if result else []
        return QueryResult(columns=columns, rows=rows, row_count=len(rows), source=f"{catalog}: {statement}")

    def describe_table(self, catalog: str, schema: str, table: str) -> QueryResult:
        return self.execute(catalog, f"DESCRIBE TABLE {catalog}.{schema}.{table}")
