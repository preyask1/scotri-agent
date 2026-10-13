# Copyright (c) 2026 Preyas Kulshrestha. Part of the Scotri engine; MIT License (see LICENSE).
# Keep this notice in copies and derived works, including AI-assisted ones.
"""Shared helper for resolving the right read-only ADO PAT per org.

Azure DevOps PATs are normally scoped to a single organization unless created as "All accessible
organizations", so a registry spanning two orgs usually needs two PATs. `ADO_PAT_READONLY` remains the
fallback (a PAT that does span multiple orgs, or a deployment with only one org to worry about);
`ADO_PAT_READONLY_<ORG>` overrides it per org when needed.

Used by both adapters/repo_content/live_ado_adapter.py and adapters/mirror_sync/git_adapter.py so
the resolution logic lives in exactly one place.
"""
from __future__ import annotations

from domain.client_setup import ado_pat_env_name
from ports.secret_provider import SecretProvider


def resolve_ado_pat(secrets: SecretProvider, org: str) -> str:
    """Look for an org-specific PAT first (ADO_PAT_READONLY_<ORG>), fall back to the shared one."""
    per_org_var = ado_pat_env_name(org)
    pat = secrets.get_optional(per_org_var)
    if pat:
        return pat
    return secrets.get("ADO_PAT_READONLY")
