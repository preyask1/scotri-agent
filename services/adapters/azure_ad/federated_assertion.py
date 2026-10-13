# Copyright (c) 2026 Preyas Kulshrestha. Part of the Scotri engine; MIT License (see LICENSE).
# Keep this notice in copies and derived works, including AI-assisted ones.
"""Gets the client assertion for an Entra app that trusts a federated identity credential
(workload identity federation) — no client secret anywhere. OPEN_ITEMS.md #16.

The ADF app registration (in the OPS tenant) trusts tokens issued by the Scotri host's tenant for a
user-assigned managed identity (audience `api://AzureADTokenExchange`). This module gets that token
from wherever the host provides it, with no extra dependency (plain `requests`):

1. `<prefix>_FEDERATED_TOKEN_FILE` — a file holding the assertion (AKS workload identity projects one;
   it is re-read on every call because the platform rotates it).
2. App Service / Functions / Container Apps — `IDENTITY_ENDPOINT` + `IDENTITY_HEADER`.
3. Azure VM / VMSS — the instance metadata service (IMDS) at 169.254.169.254.

On a machine with none of these (a laptop) there is nothing to obtain: it raises CredentialError
saying so and never falls back to another credential.
"""
from __future__ import annotations

import os
from pathlib import Path

import requests

from domain.errors import CredentialError
from ports.secret_provider import SecretProvider

AUDIENCE = "api://AzureADTokenExchange"
_IMDS_URL = "http://169.254.169.254/metadata/identity/oauth2/token"


def get_client_assertion(secrets: SecretProvider, prefix: str = "ADF") -> str:
    token_file = secrets.get_optional(f"{prefix}_FEDERATED_TOKEN_FILE")
    object_id = secrets.get_optional(f"{prefix}_MANAGED_IDENTITY_OBJECT_ID")
    if token_file:
        try:
            token = Path(token_file).read_text(encoding="utf-8").strip()
        except OSError as e:
            raise CredentialError(f"{prefix}_FEDERATED_TOKEN_FILE could not be read: {e}") from e
        if not token:
            raise CredentialError(f"{prefix}_FEDERATED_TOKEN_FILE is empty.")
        return token

    identity_endpoint, identity_header = os.environ.get("IDENTITY_ENDPOINT"), os.environ.get("IDENTITY_HEADER")
    try:
        if identity_endpoint and identity_header:
            params = {"api-version": "2019-08-01", "resource": AUDIENCE}
            headers = {"X-IDENTITY-HEADER": identity_header}
            url = identity_endpoint
        else:
            params = {"api-version": "2018-02-01", "resource": AUDIENCE}
            headers = {"Metadata": "true"}
            url = _IMDS_URL
        if object_id:
            params["object_id"] = object_id
        resp = requests.get(url, params=params, headers=headers, timeout=5)
    except (requests.ConnectionError, requests.Timeout) as e:
        raise CredentialError(
            "Federated ADF auth needs an Azure host with the managed identity attached (or "
            f"{prefix}_FEDERATED_TOKEN_FILE); no managed-identity endpoint is reachable from this "
            f"machine ({type(e).__name__}). A laptop cannot obtain this token."
        ) from e
    if resp.status_code != 200:
        raise CredentialError(
            f"Managed-identity token request failed: HTTP {resp.status_code}. Check that the identity "
            f"is attached to this host and {prefix}_MANAGED_IDENTITY_OBJECT_ID matches it."
        )
    try:
        return resp.json()["access_token"]
    except (ValueError, KeyError) as e:
        raise CredentialError("Managed-identity endpoint returned no access_token.") from e
