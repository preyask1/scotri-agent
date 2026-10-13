# Copyright (c) 2026 Preyas Kulshrestha. Part of the Scotri engine; MIT License (see LICENSE).
# Keep this notice in copies and derived works, including AI-assisted ones.
"""Fabric/Power BI adapter (dax-debug SKILL.md) — read-only DAX execution over the Power BI REST
'executeQueries' endpoint, service-principal client-credentials auth.

Deliberately uses the REST executeQueries API rather than a raw XMLA/AS-client connection: it needs
no ODBC/ADOMD driver or pythonnet dependency, which matters for a Linux-hostable deployment (see
the design notes — the whole reason the original Power BI Modeling MCP was dropped for
this workspace was its Windows/VS-Code dependency; a pure-REST adapter avoids reintroducing a
platform constraint through the back door).

RLS note: executeQueries supports an `impersonatedUserName` field for RLS simulation, which most
Fabric models key off `USERPRINCIPALNAME()`/`USERNAME()` in a role-mapping table — it is NOT
necessarily the same thing as "the Fabric workspace role name." Confirm the model's actual RLS
convention before
assuming `role=` here maps the way a caller expects.
"""
from __future__ import annotations

import time

import requests

from domain.errors import CredentialError, NotFoundError, ScotriError, TransientError
from domain.models import QueryResult
from ports.secret_provider import SecretProvider

_TOKEN_SCOPE = "https://analysis.windows.net/powerbi/api/.default"
_TOKEN_URL_TEMPLATE = "https://login.microsoftonline.com/{tenant}/oauth2/v2.0/token"
_API_ROOT = "https://api.powerbi.com/v1.0/myorg"


class FabricRestSemanticModel:
    """Implements ports.semantic_model.SemanticModel."""

    def __init__(self, secrets: SecretProvider) -> None:
        self._tenant_id = secrets.get("FABRIC_TENANT_ID")
        self._client_id = secrets.get("FABRIC_SP_CLIENT_ID")
        self._client_secret = secrets.get("FABRIC_SP_CLIENT_SECRET")
        self._token: str | None = None
        self._token_expires_at: float = 0.0
        self._workspace_id_cache: dict[str, str] = {}
        self._dataset_id_cache: dict[tuple[str, str], str] = {}

    # --- auth -----------------------------------------------------------------------------

    def _get_token(self) -> str:
        if self._token and time.time() < self._token_expires_at - 60:
            return self._token
        resp = requests.post(
            _TOKEN_URL_TEMPLATE.format(tenant=self._tenant_id),
            data={
                "grant_type": "client_credentials",
                "client_id": self._client_id,
                "client_secret": self._client_secret,
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
        """A wrong-tenant token returns HTTP 203 + an HTML sign-in page, not a clean 401 — see
        docs/ai/client/repo-registry.md / repo-map.md. Fail loudly and specifically on that, not silently.

        Raises a domain.errors.ScotriError subclass (never a bare RuntimeError) so callers like
        app/cli.py's cmd_dax_execute — which only catches ScotriError — surface a clean
        `{"ok": false, "category": ..., "message": ...}` envelope instead of a raw traceback."""
        content_type = resp.headers.get("content-type", "")
        if resp.status_code == 200 and "application/json" in content_type:
            return
        detail = (
            f"Fabric {context} failed: HTTP {resp.status_code}, content-type={content_type!r}. "
            f"A 203/HTML response usually means a wrong-tenant token (FABRIC_TENANT_ID) — see "
            f"docs/ai/client/repo-registry.md. Body (truncated): {resp.text[:300]!r}"
        )
        if resp.status_code in (401, 403, 203):
            raise CredentialError(detail)
        if resp.status_code == 429 or resp.status_code >= 500:
            raise TransientError(detail)
        raise ScotriError(detail)

    # --- name -> GUID resolution ------------------------------------------------------------

    def _resolve_workspace_id(self, workspace_name: str) -> str:
        if workspace_name in self._workspace_id_cache:
            return self._workspace_id_cache[workspace_name]
        resp = requests.get(f"{_API_ROOT}/groups", headers=self._headers(), timeout=15)
        self._assert_json_response(resp, context="workspace lookup")
        for group in resp.json().get("value", []):
            if group["name"] == workspace_name:
                self._workspace_id_cache[workspace_name] = group["id"]
                return group["id"]
        raise NotFoundError(
            f"Workspace '{workspace_name}' not found or this service principal isn't a member of "
            f"it — see docs/ai/fabric-tool-access.md: the SP must be added to the target workspace "
            f"(at least Viewer) before a connection succeeds."
        )

    def _resolve_dataset_id(self, group_id: str, dataset_name: str) -> str:
        cache_key = (group_id, dataset_name)
        if cache_key in self._dataset_id_cache:
            return self._dataset_id_cache[cache_key]
        resp = requests.get(f"{_API_ROOT}/groups/{group_id}/datasets", headers=self._headers(), timeout=15)
        self._assert_json_response(resp, context="dataset lookup")
        for dataset in resp.json().get("value", []):
            if dataset["name"] == dataset_name:
                self._dataset_id_cache[cache_key] = dataset["id"]
                return dataset["id"]
        raise NotFoundError(f"Dataset (semantic model) '{dataset_name}' not found in workspace '{group_id}'.")

    # --- SemanticModel port -----------------------------------------------------------------

    def execute_dax(self, workspace: str, model: str, dax: str, role: str | None = None) -> QueryResult:
        try:
            group_id = self._resolve_workspace_id(workspace)
            dataset_id = self._resolve_dataset_id(group_id, model)

            body: dict = {"queries": [{"query": dax}], "serializerSettings": {"includeNulls": True}}
            if role:
                body["impersonatedUserName"] = role  # see module docstring's RLS note

            resp = requests.post(
                f"{_API_ROOT}/groups/{group_id}/datasets/{dataset_id}/executeQueries",
                headers=self._headers(),
                json=body,
                timeout=30,
            )
            self._assert_json_response(resp, context="executeQueries")
            payload = resp.json()
            table = payload["results"][0]["tables"][0]
            columns = list(table["rows"][0].keys()) if table.get("rows") else []
            rows = [tuple(r.values()) for r in table.get("rows", [])]
            return QueryResult(
                columns=columns, rows=rows, row_count=len(rows), source=f"{workspace}/{model}: {dax}"
            )
        except (requests.ConnectionError, requests.Timeout) as e:
            # Network-level failures never reach _assert_json_response (there's no response to
            # check) — surface them as the same clean envelope as every other governed failure
            # instead of the raw requests traceback cmd_dax_execute's `except ScotriError` would
            # otherwise let through unhandled.
            raise TransientError(f"Fabric request failed: {e}") from e

    # --- refresh history (OPEN_ITEMS.md: decided 2026-09-27) --------------------------------

    def get_refresh_history(self, workspace: str, model: str, top: int = 10) -> list[dict]:
        """`Get Refresh History In Group` — the most recent refresh attempts: status
        (Completed/Failed/Disabled/Unknown), start/end time, refreshType, and
        serviceExceptionJson on a failure. Works for every dataset regardless of Standard vs.
        Enhanced refresh (decision 2: this call is never skipped or gated on refresh type)."""
        try:
            group_id = self._resolve_workspace_id(workspace)
            dataset_id = self._resolve_dataset_id(group_id, model)
            resp = requests.get(
                f"{_API_ROOT}/groups/{group_id}/datasets/{dataset_id}/refreshes",
                headers=self._headers(),
                params={"$top": top},
                timeout=30,
            )
            self._assert_json_response(resp, context="refresh history")
            return resp.json().get("value", [])
        except (requests.ConnectionError, requests.Timeout) as e:
            raise TransientError(f"Fabric request failed: {e}") from e

    def get_refresh_execution_details(self, workspace: str, model: str, refresh_id: str) -> dict:
        """`Get Refresh Execution Details In Group` — per-partition/table detail, only ever
        populated for a dataset on Enhanced Refresh. A Standard-refresh dataset's refresh id
        returns HTTP 400 here (per the API's own documented behavior) — decision 2 (OPEN_ITEMS.md)
        says to report that as "top-level status only available", not as a failure, since it's
        the expected shape of the response for most datasets, not an error condition."""
        try:
            group_id = self._resolve_workspace_id(workspace)
            dataset_id = self._resolve_dataset_id(group_id, model)
            resp = requests.get(
                f"{_API_ROOT}/groups/{group_id}/datasets/{dataset_id}/refreshes/{refresh_id}",
                headers=self._headers(),
                timeout=30,
            )
            if resp.status_code == 400:
                return {
                    "available": False,
                    "reason": (
                        "no per-partition execution detail — this dataset is likely on Standard "
                        "(not Enhanced) refresh; only the top-level status from get_refresh_history "
                        "is available for it."
                    ),
                }
            self._assert_json_response(resp, context="refresh execution details")
            payload = resp.json()
            payload["available"] = True
            return payload
        except (requests.ConnectionError, requests.Timeout) as e:
            raise TransientError(f"Fabric request failed: {e}") from e
