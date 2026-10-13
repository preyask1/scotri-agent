"""adapters/databricks/sdk_adapter.py: a statement still running when the server-side wait ends must
be polled to completion, never returned as an empty result (engagement ea629988d18a read seven slow
queries as "no rows"); anything other than SUCCEEDED is an error; every result chunk is read."""
from __future__ import annotations

from types import SimpleNamespace as NS

import pytest
from databricks.sdk.service.sql import StatementState

from adapters.databricks.sdk_adapter import DatabricksSdkQueryExecutor
from domain.errors import ErrorCategory, OperationTimeoutError, ScotriError


class _Secrets:
    def get(self, name):
        return "x"


def _response(state, *, columns=None, rows=None, next_chunk=None, error=None, truncated=False):
    manifest = None
    result = None
    if columns is not None:
        manifest = NS(schema=NS(columns=[NS(name=c) for c in columns]), truncated=truncated)
        result = NS(data_array=rows or [], next_chunk_index=next_chunk)
    return NS(statement_id="stmt-1", status=NS(state=state, error=error), manifest=manifest, result=result)


class _FakeStatements:
    def __init__(self, first, polls=(), chunks=None):
        self.first = first
        self.polls = list(polls)
        self.chunks = chunks or {}
        self.execute_kwargs = None
        self.get_calls = 0
        self.cancelled = []

    def execute_statement(self, **kwargs):
        self.execute_kwargs = kwargs
        return self.first

    def get_statement(self, statement_id):
        self.get_calls += 1
        return self.polls.pop(0) if self.polls else self.first

    def get_statement_result_chunk_n(self, statement_id, chunk_index):
        return self.chunks[chunk_index]

    def cancel_execution(self, statement_id):
        self.cancelled.append(statement_id)


def _executor(statements, **kwargs):
    client = NS(statement_execution=statements)
    return DatabricksSdkQueryExecutor(_Secrets(), client=client, poll_interval_seconds=0, **kwargs)


def test_running_statement_is_polled_until_it_succeeds():
    fake = _FakeStatements(
        _response(StatementState.PENDING),
        polls=[_response(StatementState.RUNNING),
               _response(StatementState.SUCCEEDED, columns=["n"], rows=[["42"]])],
    )
    result = _executor(fake).execute("acme_prod", "SELECT count(*) AS n FROM t")
    assert result.columns == ["n"] and result.rows == [("42",)]
    assert fake.get_calls == 2
    assert fake.execute_kwargs["on_wait_timeout"].value == "CONTINUE"
    assert fake.execute_kwargs["wait_timeout"]


def test_statement_past_the_deadline_is_cancelled_and_raises_timeout_not_empty():
    fake = _FakeStatements(_response(StatementState.RUNNING))
    with pytest.raises(OperationTimeoutError) as exc:
        _executor(fake, max_wait_seconds=0).execute("acme_prod", "SELECT 1")
    assert "not an empty result" in exc.value.user_message
    assert fake.cancelled == ["stmt-1"]


@pytest.mark.parametrize("state", [StatementState.FAILED, StatementState.CANCELED, StatementState.CLOSED])
def test_non_success_terminal_state_raises(state):
    fake = _FakeStatements(_response(state, error=NS(message="boom")))
    with pytest.raises(ScotriError) as exc:
        _executor(fake).execute("acme_prod", "SELECT 1")
    assert state.value in exc.value.user_message


def test_permission_denied_is_a_credential_error():
    fake = _FakeStatements(_response(StatementState.FAILED, error=NS(message="PERMISSION_DENIED: no")))
    with pytest.raises(ScotriError) as exc:
        _executor(fake).execute("acme_prod", "SELECT 1")
    assert exc.value.category == ErrorCategory.CREDENTIAL


def test_missing_status_is_an_error_not_zero_rows():
    fake = _FakeStatements(NS(statement_id="stmt-1", status=None, manifest=None, result=None))
    with pytest.raises(ScotriError):
        _executor(fake).execute("acme_prod", "SELECT 1")


def test_every_result_chunk_is_read():
    fake = _FakeStatements(
        _response(StatementState.SUCCEEDED, columns=["v"], rows=[["1"], ["2"]], next_chunk=1),
        chunks={1: NS(data_array=[["3"]], next_chunk_index=2), 2: NS(data_array=[["4"]], next_chunk_index=None)},
    )
    result = _executor(fake).execute("acme_prod", "SELECT v FROM t")
    assert [r[0] for r in result.rows] == ["1", "2", "3", "4"] and result.row_count == 4


def test_truncated_result_raises():
    fake = _FakeStatements(_response(StatementState.SUCCEEDED, columns=["v"], rows=[["1"]], truncated=True))
    with pytest.raises(ScotriError):
        _executor(fake).execute("acme_prod", "SELECT v FROM t")


def test_genuinely_empty_result_still_returns_columns():
    fake = _FakeStatements(_response(StatementState.SUCCEEDED, columns=["Id", "Name"], rows=[]))
    result = _executor(fake).execute("acme_prod", "SELECT Id, Name FROM d WHERE 1 = 0")
    assert result.columns == ["Id", "Name"] and result.rows == [] and result.row_count == 0
