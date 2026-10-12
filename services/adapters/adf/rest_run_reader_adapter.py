"""ADF pipeline/activity run-history adapter (adf-databricks-read SKILL.md's "ADF pipeline
failures" section; OPEN_ITEMS.md: decided 2026-09-27) — read-only ARM REST calls, service-principal
client-credentials auth.

Authenticates as its own app in the ADF tenant (ADF_TENANT_ID/ADF_CLIENT_ID, OPEN_ITEMS.md #16)
using a **federated identity credential, never a secret**: ADF can live in a separate data tenant, a
different Entra tenant from Fabric/Scotri, so the Fabric SP cannot be used. The app trusts a managed
identity on the Scotri host; adapters/azure_ad/federated_assertion.py gets that identity's token and
it is exchanged here as a `client_assertion`. The token scope is ARM's `.default` — deliberately **not** `az`: the runbook this reader is modeled on
(an earlier investigation runbook) used a personal `az login`, which this workspace
forbids (no interactive/browser auth of any kind — agent-instructions.md).

Both endpoints here are ARM's query-style POST endpoints (`queryPipelineRuns`,
`queryActivityruns`) — read-only by construction, no create/cancel/rerun operation exists in this
adapter. `domain.adf_run_policy` enforces the mandatory pipeline-name filter and the query-window
cap *before* either method is called; this adapter does not re-check those itself, so it must
always be reached through the governed CLI commands, never called directly.
"""
from __future__ import annotations

import time
from datetime import datetime

import requests

from adapters.azure_ad.federated_assertion import get_client_assertion
from domain.errors import CredentialError, ScotriError, TransientError
from domain.models import AdfFactoryRef
from ports.secret_provider import SecretProvider

_TOKEN_SCOPE = "https://management.azure.com/.default"
_TOKEN_URL_TEMPLATE = "https://login.microsoftonline.com/{tenant}/oauth2/v2.0/token"
_API_ROOT = "https://management.azure.com"
_API_VERSION = "2018-06-01"


def _iso(dt: datetime) -> str:
    # ARM wants a plain UTC-ish timestamp; isoformat() round-trips fine for both naive and
    # tz-aware datetimes the CLI layer passes in (argparse parses --start/--end as naive UTC).
    return dt.isoformat()


class AdfRestRunReader:
    """Implements ports.pipeline_run_reader.PipelineRunReader."""

    def __init__(self, secrets: SecretProvider) -> None:
        # ADF is in the OPS tenant, so it has its own app (OPEN_ITEMS.md #16); no secret exists.
        self._secrets = secrets
        self._tenant_id = secrets.get("ADF_TENANT_ID")
        self._client_id = secrets.get("ADF_CLIENT_ID")
        self._token: str | None = None
        self._token_expires_at: float = 0.0

    # --- auth -----------------------------------------------------------------------------

    def _get_token(self) -> str:
        if self._token and time.time() < self._token_expires_at - 60:
            return self._token
        resp = requests.post(
            _TOKEN_URL_TEMPLATE.format(tenant=self._tenant_id),
            data={
                "grant_type": "client_credentials",
                "client_id": self._client_id,
                "client_assertion_type": "urn:ietf:params:oauth:client-assertion-type:jwt-bearer",
                "client_assertion": get_client_assertion(self._secrets, "ADF"),
                "scope": _TOKEN_SCOPE,
            },
            timeout=15,
        )
        self._assert_json_response(resp, context="token request")
        payload = resp.json()
        self._token = payload["access_token"]
        self._token_expires_at = time.time() + int(payload.get("expires_in", 3600))
        return self._token

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self._get_token()}"}

    @staticmethod
    def _assert_json_response(resp: requests.Response, context: str) -> None:
        """Same shape as adapters/fabric/rest_semantic_model_adapter.py's check — fail loudly and
        specifically, never let a raw traceback reach the CLI layer.

        A 403 here most often means the service principal's role assignment on this factory
        (Data Factory Reader or equivalent — OPEN_ITEMS.md decision 1) hasn't actually been granted
        yet, not that the credential itself is wrong — the message says that explicitly so it isn't
        misreported as "missing credential"."""
        content_type = resp.headers.get("content-type", "")
        if resp.status_code == 200 and "application/json" in content_type:
            return
        detail = f"ADF {context} failed: HTTP {resp.status_code}, content-type={content_type!r}."
        if resp.status_code in (401, 403):
            detail += (
                " A 401/403 here usually means the app (ADF_CLIENT_ID) has not "
                "been granted a role (e.g. Data Factory Reader) on this specific factory yet — see "
                "OPEN_ITEMS.md's 'ADF pipeline run-history reader' decision 1. Report this as a "
                "missing role grant, not a broken adapter or a wrong credential."
            )
            detail += f" Body (truncated): {resp.text[:300]!r}"
            raise CredentialError(detail)
        if resp.status_code == 429 or resp.status_code >= 500:
            raise TransientError(detail + f" Body (truncated): {resp.text[:300]!r}")
        raise ScotriError(detail + f" Body (truncated): {resp.text[:300]!r}")

    def _factory_url(self, factory: AdfFactoryRef, *segments: str) -> str:
        base = (
            f"{_API_ROOT}/subscriptions/{factory.subscription_id}/resourceGroups/"
            f"{factory.resource_group}/providers/Microsoft.DataFactory/factories/{factory.factory_name}"
        )
        return "/".join([base, *segments]) + f"?api-version={_API_VERSION}"

    # --- PipelineRunReader port -------------------------------------------------------------

    def query_pipeline_runs(
        self, factory: AdfFactoryRef, pipeline_names: list[str], start: datetime, end: datetime
    ) -> list[dict]:
        """`Pipeline Runs - Query By Factory`, filtered by pipeline name — see domain/adf_run_policy.py
        for why the filter and window are both mandatory by the time this is called."""
        try:
            body = {
                "lastUpdatedAfter": _iso(start),
                "lastUpdatedBefore": _iso(end),
                "filters": [{"operand": "PipelineName", "operator": "In", "values": pipeline_names}],
            }
            resp = requests.post(
                self._factory_url(factory, "queryPipelineRuns"), headers=self._headers(), json=body, timeout=30
            )
            self._assert_json_response(resp, context="queryPipelineRuns")
            return resp.json().get("value", [])
        except (requests.ConnectionError, requests.Timeout) as e:
            raise TransientError(f"ADF request failed: {e}") from e

    def query_activity_runs(
        self, factory: AdfFactoryRef, run_id: str, start: datetime, end: datetime
    ) -> list[dict]:
        """`Activity Runs - Query By Pipeline Run` — note the group name really is `activity-run`
        (`az datafactory activity-run query-by-pipeline-run`), not a sub-command of `pipeline-run`;
        the equivalent REST path is nested under the run id, not a sibling of queryPipelineRuns."""
        try:
            body = {"lastUpdatedAfter": _iso(start), "lastUpdatedBefore": _iso(end)}
            resp = requests.post(
                self._factory_url(factory, "pipelineruns", run_id, "queryActivityruns"),
                headers=self._headers(),
                json=body,
                timeout=30,
            )
            self._assert_json_response(resp, context="queryActivityruns")
            return resp.json().get("value", [])
        except (requests.ConnectionError, requests.Timeout) as e:
            raise TransientError(f"ADF request failed: {e}") from e

    def query_trigger_runs(
        self, factory: AdfFactoryRef, trigger_names: list[str], start: datetime, end: datetime
    ) -> list[dict]:
        """`Trigger Runs - Query By Factory`, filtered by trigger name — the operand is `TriggerName`
        (a sibling query-style POST at the factory root, like queryPipelineRuns). Read-only; the
        mandatory filter and window cap are enforced by domain/adf_run_policy before this is called."""
        try:
            body = {
                "lastUpdatedAfter": _iso(start),
                "lastUpdatedBefore": _iso(end),
                "filters": [{"operand": "TriggerName", "operator": "In", "values": trigger_names}],
            }
            resp = requests.post(
                self._factory_url(factory, "queryTriggerRuns"), headers=self._headers(), json=body, timeout=30
            )
            self._assert_json_response(resp, context="queryTriggerRuns")
            return resp.json().get("value", [])
        except (requests.ConnectionError, requests.Timeout) as e:
            raise TransientError(f"ADF request failed: {e}") from e
