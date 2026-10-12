"""Prod-by-default environment resolution (agent-instructions.md, repo-registry.md).

This is the domain-level codification of the rule that's been stated in prose across half a dozen
skill files: absent an explicit environment, assume Prod; only reach into Dev/UAT when named. This
module resolves *which* repo/branch that means, via the RepoRegistry port — it never reads a file
or hits a network itself.
"""
from __future__ import annotations

from domain.models import Environment, RepoNotInRegistryError, RepoRef
from ports.repo_registry import RepoRegistry


def resolve(
    registry: RepoRegistry,
    repo_key: str,
    requested_environment: Environment | None,
) -> RepoRef:
    """Resolve a repo key + optional explicit environment to a concrete RepoRef.

    `requested_environment=None` means the caller didn't name one — Prod is assumed, per the hard
    rule in agent-instructions.md. This function does NOT check presence-claim or the maintenance
    flag; those are separate concerns, checked by the caller before/around this resolution.
    """
    environment = requested_environment or Environment.PROD
    candidates = registry.get(repo_key)
    if not candidates:
        raise RepoNotInRegistryError(
            f"'{repo_key}' is not listed in the repo registry — hand back to a human to confirm "
            f"the mapping rather than guessing at a name."
        )
    for ref in candidates:
        if ref.environment == environment:
            return ref
    known = ", ".join(sorted(c.environment.value for c in candidates))
    raise RepoNotInRegistryError(
        f"'{repo_key}' has no registered '{environment.value}' environment (known: {known})."
    )


def is_live_read_required(ref: RepoRef) -> bool:
    """True when this RepoRef must be read on-demand (Dev/UAT), never from the mirror."""
    return not ref.mirrored


def is_ready_to_sync(ref: RepoRef) -> bool:
    """True when the sync service should actually attempt to mirror this ref.

    Mirrored-but-`planned_not_live` refs (e.g. the new Fabric repo structure, confirmed by the user
    as not yet live) are excluded — attempting to clone a repo that may not exist yet in ADO would
    just produce noisy, expected failures. Flip repo_registry.yaml's `status` once it's confirmed
    live, and this starts including it with no other code change needed.
    """
    return ref.mirrored and ref.status != "planned_not_live"
