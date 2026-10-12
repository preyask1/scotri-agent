"""Pure role/ownership access checks — the single home for "may this Principal do/see this?".

Why this module exists (OPEN_ITEMS.md #29-adjacent, identity model — docs/ai/identity-model.md):
before this, every gate was an inline `if` in `app/api.py` (owner-or-admin on a conversation,
owner-only on `/steps`/cancel, the `HISTORY_ADMIN_ROLES` widen). That works, but it (a) can drift
between endpoints, and (b) only covers the HTTP transport — the MCP transport (`app/mcp_server.py`)
must enforce identically. Centralising the checks here as pure functions makes them uniform across
both transports and unit-testable with fake Principals (no FastAPI, no Redis, no token).

**What these enforce and what they do NOT.** These authorize access to *Scotri's own resources* —
an engagement, a conversation, a history list, an admin action — by the caller's `subject`
(ownership) and `roles` (Entra app-role claims Scotri reads, never assigns). They do **not** authorize
the *content of a data query*: nothing here (or anywhere in Scotri today) stops a caller from asking
for a row a business rule says isn't theirs — e.g. one salesman asking for a colleague's incentive.
That is data-plane row/column authorization and must live where the data lives (Databricks Unity
Catalog row filters / column masks, or Fabric RLS), keyed to a per-user identity Scotri does not yet
pass through to the warehouse (it queries as one shared service principal). See the "does NOT cover"
section of docs/ai/identity-model.md and the open item it points to. Do not try to bolt row-level
business rules onto these functions — that is the wrong layer for it.

Invariants (hold for every function here):
- **Fail closed.** Absent/empty/unreadable roles resolve to the least-privileged outcome (own-only,
  deny the privileged verb) — never the widest.
- **A role gates; it never exempts.** Passing a role check does not turn off the audit line (#29) or
  the query governor's rate limit — those are enforced elsewhere and unconditionally.
- **Enforce server-side.** These run at the request boundary in the engine, never in a frontend; a
  frontend hiding a button is UX, not a control.
"""
from __future__ import annotations

from collections.abc import Iterable

from domain.errors import PolicyError
from domain.models import Principal


def has_any_role(principal: Principal, allowed: Iterable[str]) -> bool:
    """True iff the principal holds at least one of `allowed`. Set-intersection semantics, matching
    the built `set(principal.roles) & HISTORY_ADMIN_ROLES` check in app/api.py. An empty `allowed`
    means "no role can satisfy this" → always False (fail closed), never "everyone passes"."""
    allowed_set = {r for r in allowed if r}
    if not allowed_set:
        return False
    return bool(set(principal.roles) & allowed_set)


def require_role(principal: Principal, allowed: Iterable[str], *, action: str = "this action") -> None:
    """Capability gate (identity-model.md category 2): raise unless the principal holds a role in
    `allowed`. Use at the top of an admin/mutating handler, before any work — cheapest gate first."""
    allowed_set = {r for r in allowed if r}
    if not has_any_role(principal, allowed_set):
        raise PolicyError(
            f"{action} requires one of these roles: {', '.join(sorted(allowed_set)) or '(none configured)'}"
        )


def can_read_owned(principal: Principal, owner_subject: str, admin_roles: Iterable[str]) -> bool:
    """Ownership-scoped read (identity-model.md category 1): the owner always may; anyone else may
    only if they hold a widening role. With an empty `admin_roles` this is strictly owner-only —
    the safe default (widening is a deliberate config act, e.g. HISTORY_ADMIN_ROLES)."""
    if principal.subject == owner_subject:
        return True
    return has_any_role(principal, admin_roles)


def require_owner_or_role(
    principal: Principal,
    owner_subject: str,
    admin_roles: Iterable[str],
    *,
    resource: str = "this resource",
) -> None:
    """Raising form of `can_read_owned` — the check + the 403-shaped PolicyError in one call, so a
    handler reads as one line."""
    if not can_read_owned(principal, owner_subject, admin_roles):
        raise PolicyError(f"not authorized to access {resource}")


def require_human_caller(principal: Principal, service_platforms: Iterable[str], *, action: str = "this action") -> None:
    """Caller-shape gate (identity-model.md category 4): refuse a service/automation identity even if
    it carries a role. `platform` is the token's azp/appid; a webhook- or sweep-triggered run has no
    user turn, so a human-approval action (e.g. a jira-write) must be refused there by construction,
    independent of roles. `service_platforms` is the set of platform ids that are service identities."""
    if principal.platform in {p for p in service_platforms if p}:
        raise PolicyError(
            f"{action} is not available to the automated caller '{principal.platform}' "
            "(no user is present to approve it)"
        )
