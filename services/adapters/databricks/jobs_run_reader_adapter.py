# Copyright (c) 2026 Preyas Kulshrestha. Part of the Scotri engine; MIT License (see LICENSE).
# Keep this notice in copies and derived works, including AI-assisted ones.
"""Databricks job/notebook run-history adapter (OPEN_ITEMS.md item #30) — read-only Jobs API 2.1
reads via the same service-principal M2M OAuth the SELECT executor uses.

Read-only by construction: only `jobs.list_runs` / `jobs.get_run` / `jobs.get_run_output` are called,
never a submit/cancel/repair/reset. `domain.databricks_run_policy` enforces the mandatory,
capped window and the limit cap *before* either list method is called; this adapter does not
re-check those itself, so it must always be reached through the governed CLI commands, never called
directly.

Prod workspace only: the single configured connection (DATABRICKS_HOST — <databricks host>),
matching the ADF reader's Prod-factory-only decision. The second workspace
(<secondary workspace host>) is a pending, separately-signed-off access request (OPEN_ITEMS.md decision 4)
and is intentionally not reachable here.

The Jobs API is a different permission surface from Unity Catalog SELECT: the SP's SELECT grant on
<prod_catalog>/<dev_catalog> says nothing about whether it can read job runs. If a call comes back 403 /
PERMISSION_DENIED, that is a *missing Jobs-API grant on this SP for this workspace* — report it as
exactly that, not as a broken adapter or a missing credential (OPEN_ITEMS.md item #30 decision 1).
"""
from __future__ import annotations

from datetime import datetime, timezone

from databricks.sdk import WorkspaceClient
from databricks.sdk.errors import DatabricksError

from domain.errors import CredentialError, NotFoundError, ScotriError, TransientError
from ports.secret_provider import SecretProvider


def _epoch_ms(dt: datetime) -> int:
    """The Jobs API filters on epoch-millisecond timestamps. CLI datetimes are naive UTC
    (app/cli.py's _parse_iso8601) — treat a naive value as UTC rather than local time."""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return int(dt.timestamp() * 1000)


class DatabricksJobsRunReader:
    """Implements ports.databricks_run_reader.DatabricksRunReader."""

    def __init__(self, secrets: SecretProvider) -> None:
        # Same credential as adapters/databricks/sdk_adapter.py — no warehouse needed for the Jobs
        # API, so this constructs its own client rather than sharing the SELECT executor's.
        self._client = WorkspaceClient(
            host=secrets.get("DATABRICKS_HOST"),
            client_id=secrets.get("DATABRICKS_CLIENT_ID"),
            client_secret=secrets.get("DATABRICKS_CLIENT_SECRET"),
        )

    # --- error mapping --------------------------------------------------------------------

    @staticmethod
    def _map_error(exc: DatabricksError, context: str) -> ScotriError:
        code = str(getattr(exc, "error_code", "") or "")
        text = f"{code} {exc}".upper()
        if "PERMISSION_DENIED" in text or "403" in text or "UNAUTHORIZED" in text or "401" in text:
            return CredentialError(
                f"Databricks Jobs API {context} was denied. This means the service principal "
                f"(DATABRICKS_CLIENT_ID) lacks a Jobs-API read grant (View/CAN_VIEW) on this "
                f"workspace — its SELECT grant on Unity Catalog does not cover the Jobs API. "
                f"Report this as a missing per-job/workspace grant on a named endpoint, not a "
                f"missing or broken credential (OPEN_ITEMS.md item #30 decision 1). Detail: {exc}"
            )
        if "DOES_NOT_EXIST" in text or "DOES NOT EXIST" in text or "NOT_FOUND" in text or "404" in text:
            # The Jobs API answers "does not exist" both for a job/run in another workspace and for
            # one this SP may not view. A gold table's DESCRIBE HISTORY can name a writer job this
            # workspace then calls nonexistent (a job on a secondary workspace, OPEN_ITEMS.md #14).
            return NotFoundError(
                f"Databricks Jobs API {context}: not visible from this workspace (DATABRICKS_HOST). The "
                f"job may run on another workspace this agent can't read (docs/ai/client/client.md names "
                f"a secondary workspace if there is one, OPEN_ITEMS.md #18), or this service principal "
                f"lacks CAN_VIEW on it. It does not mean the job or run never existed. Detail: {exc}")
        if "TOO_MANY_REQUESTS" in text or "429" in text or "UNAVAILABLE" in text or "INTERNAL" in text:
            return TransientError(f"Databricks Jobs API {context} failed transiently. Detail: {exc}")
        return ScotriError(f"Databricks Jobs API {context} failed. Detail: {exc}")

    # --- DatabricksRunReader port ----------------------------------------------------------

    def list_runs(self, job_id: int | None, start: datetime, end: datetime, limit: int) -> list[dict]:
        try:
            runs: list[dict] = []
            for run in self._client.jobs.list_runs(
                job_id=job_id,
                start_time_from=_epoch_ms(start),
                start_time_to=_epoch_ms(end),
                expand_tasks=True,
            ):
                runs.append(run.as_dict())
                if len(runs) >= limit:
                    break
            return runs
        except DatabricksError as e:
            raise self._map_error(e, "list_runs") from e

    def get_run(self, run_id: int) -> dict:
        try:
            return self._client.jobs.get_run(run_id).as_dict()
        except DatabricksError as e:
            raise self._map_error(e, "get_run") from e

    def get_run_output(self, run_id: int) -> dict:
        try:
            return self._client.jobs.get_run_output(run_id).as_dict()
        except DatabricksError as e:
            raise self._map_error(e, "get_run_output") from e
