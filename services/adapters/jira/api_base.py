# Copyright (c) 2026 Preyas Kulshrestha. Part of the Scotri engine; MIT License (see LICENSE).
# Keep this notice in copies and derived works, including AI-assisted ones.
"""Resolves the correct Jira REST base URL for the credential actually in use.

Atlassian API tokens come in two kinds, and they are NOT interchangeable at the URL level
(confirmed against a real 401 on 2026-09-22, then verified live against the gateway form):

- A **classic (unscoped) token** calls the site directly:
  https://<site>.atlassian.net/rest/api/3/...
- A **scoped ("API token with scopes") token** — which is what this workspace's credential
  actually is — must go through Atlassian's API gateway instead, addressed by cloudId, not by
  site name: https://api.atlassian.com/ex/jira/<cloudId>/rest/api/3/...

Both adapters in this package use a scoped token, so both resolve through JIRA_CLOUD_ID here
rather than JIRA_BASE_URL directly. JIRA_CLOUD_ID is looked up once, via the unauthenticated
`https://<site>.atlassian.net/_edge/tenant_info` endpoint, and cached in .env rather than
re-resolved on every call.
"""
from __future__ import annotations

from ports.secret_provider import SecretProvider

_GATEWAY_BASE = "https://api.atlassian.com/ex/jira"


def resolve_api_base(secrets: SecretProvider) -> str:
    cloud_id = secrets.get("JIRA_CLOUD_ID")
    return f"{_GATEWAY_BASE}/{cloud_id}"
