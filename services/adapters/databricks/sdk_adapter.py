"""Databricks adapter (databricks-query SKILL.md) — service-principal M2M OAuth, SELECT-only.

Calls domain.query_guard.assert_select_only before every statement reaches the SDK — the
application-side half of "defense in depth"; the service principal's SELECT-only grant on both
catalogs is the platform-side half, and both must independently hold (see agent-instructions.md).
"""
from __future__ import annotations

import time

from databricks.sdk import WorkspaceClient
from databricks.sdk.service.sql import ExecuteStatementRequestOnWaitTimeout, StatementResponse, StatementState

from domain.errors import ErrorCategory, OperationTimeoutError, ScotriError
from domain.models import QueryResult
from domain.query_guard import assert_select_only
from ports.secret_provider import SecretProvider

_SERVER_WAIT = "30s"           # how long one execute/poll call blocks server-side (API allows 5-50s)
_POLL_INTERVAL_SECONDS = 2.0
# Total budget per statement. Stays under the agent's default 120 s Bash timeout, so a slow
# statement comes back as a clear timeout error rather than a killed command.
_DEFAULT_MAX_WAIT_SECONDS = 100.0
_IN_FLIGHT = (StatementState.PENDING, StatementState.RUNNING)


class DatabricksSdkQueryExecutor:
    """Implements ports.query_executor.QueryExecutor."""

    def __init__(self, secrets: SecretProvider, *, client: WorkspaceClient | None = None,
                 max_wait_seconds: float = _DEFAULT_MAX_WAIT_SECONDS,
                 poll_interval_seconds: float = _POLL_INTERVAL_SECONDS) -> None:
        self._warehouse_id = secrets.get("DATABRICKS_SQL_WAREHOUSE_ID")
        self._client = client or WorkspaceClient(
            host=secrets.get("DATABRICKS_HOST"),
            client_id=secrets.get("DATABRICKS_CLIENT_ID"),
            client_secret=secrets.get("DATABRICKS_CLIENT_SECRET"),
        )
        self._max_wait_seconds = max_wait_seconds
        self._poll_interval_seconds = poll_interval_seconds

    def current_user(self) -> str:
        return self._client.current_user.me().user_name or "unknown"

    def execute(self, catalog: str, statement: str) -> QueryResult:
        assert_select_only(statement)
        api = self._client.statement_execution
        deadline = time.monotonic() + self._max_wait_seconds
        response = api.execute_statement(
            warehouse_id=self._warehouse_id,
            catalog=catalog,
            statement=statement,
            wait_timeout=_SERVER_WAIT,
            on_wait_timeout=ExecuteStatementRequestOnWaitTimeout.CONTINUE,
        )
        # A statement still PENDING/RUNNING when the server-side wait ends comes back with no
        # manifest and no result. Before 2026-10-13 that was returned as an empty-but-successful
        # QueryResult (columns [], rows []), and engagement ea629988d18a read seven slow queries
        # as "no rows". Poll until the statement reaches a terminal state.
        while self._state(response) in _IN_FLIGHT:
            if time.monotonic() >= deadline:
                self._cancel(response.statement_id)
                raise OperationTimeoutError(
                    f"Databricks statement did not finish within {self._max_wait_seconds:.0f}s and was "
                    "cancelled. Narrow the query (filters, fewer columns, aggregates) and run it again; "
                    "this is not an empty result."
                )
            time.sleep(self._poll_interval_seconds)
            response = api.get_statement(response.statement_id)

        state = self._state(response)
        # CRITICAL: anything other than SUCCEEDED must never be reported as "0 rows" — confirmed
        # 2026-09-18, a PERMISSION_DENIED response was silently returned as an empty-but-successful
        # QueryResult before this check existed. "Never fabricate a result" (agent-instructions.md)
        # applies to this adapter's own error handling just as much as to the LLM layer above it.
        if state != StatementState.SUCCEEDED:
            error = response.status.error if response.status else None
            label = state.value if state else "no status"
            message = error.message if error and error.message else f"statement {label} with no error detail"
            category = ErrorCategory.CREDENTIAL if "PERMISSION_DENIED" in message else ErrorCategory.UNKNOWN
            raise ScotriError(f"Databricks statement failed [{label}]: {message}", category=category)

        manifest = response.manifest
        if manifest is not None and manifest.truncated:
            raise ScotriError(
                "Databricks truncated this result. Add filters, aggregate, or add a LIMIT and say so "
                "in the answer; a partial result is never reported as the whole.",
                category=ErrorCategory.POLICY,
            )
        columns = [c.name for c in manifest.schema.columns] if manifest and manifest.schema else []
        rows = self._all_rows(response)
        return QueryResult(columns=columns, rows=rows, row_count=len(rows), source=f"{catalog}: {statement}")

    @staticmethod
    def _state(response: StatementResponse) -> StatementState | None:
        return response.status.state if response.status else None

    def _all_rows(self, response: StatementResponse) -> list[tuple]:
        """Every chunk, not just the first: a large inline result arrives in several chunks."""
        result = response.result
        rows = [tuple(row) for row in (result.data_array or [])] if result else []
        next_index = result.next_chunk_index if result else None
        while next_index is not None:
            chunk = self._client.statement_execution.get_statement_result_chunk_n(
                response.statement_id, next_index)
            rows.extend(tuple(row) for row in (chunk.data_array or []))
            next_index = chunk.next_chunk_index
        return rows

    def _cancel(self, statement_id: str | None) -> None:
        if not statement_id:
            return
        try:
            self._client.statement_execution.cancel_execution(statement_id)
        except Exception:  # best effort: the timeout error is what the caller needs to see
            pass

    def describe_table(self, catalog: str, schema: str, table: str) -> QueryResult:
        return self.execute(catalog, f"DESCRIBE TABLE {catalog}.{schema}.{table}")
