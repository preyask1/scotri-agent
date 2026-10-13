# Copyright (c) 2026 Preyas Kulshrestha. Part of the Scotri engine; MIT License (see LICENSE).
# Keep this notice in copies and derived works, including AI-assisted ones.
"""Per-run caller identity, issued and resolved outside the agent (OPEN_ITEMS.md #59).

Before this, the agent was never told who it was working for, so every `--user` it passed was a
guess (usually the `Scotri:<ticket-id>` convention): per-user rate limits were really per-ticket,
own-history retrieval matched nobody, and nothing stopped it passing another person's subject.

Mechanism: at run start the engine issues an opaque random token and stores the caller under the
token's SHA-256 in the shared store, expiring with the run. The agent sees only the token, in its
environment (RUN_TOKEN_ENV). Every CLI command resolves it and replaces `--user` with the real
caller. Chosen over a signed token because the CLI runs inside the agent's own process tree: any
signing key would have to be readable there, so the agent could forge one. The store only holds a
hash, so listing keys yields nothing usable. Ports to Azure as-is (Azure Cache for Redis).

An agent with an unrestricted shell could reach the store directly and write its own entry. Since
OPEN_ITEMS.md #28 / #60(f) (domain/tool_scope.py) an engagement's Bash may run only `cd` and the
services venv's `python -m app.cli`, and no app.cli command writes a run-context entry, so that path
is closed for engagements. A human running the CLI by hand is not an engagement and is unaffected.
"""
from __future__ import annotations

import hashlib
import json
import secrets
from dataclasses import asdict, dataclass, field

from domain.errors import PolicyError
from domain.models import Principal

RUN_TOKEN_ENV = "SCOTRI_RUN_TOKEN"


@dataclass(frozen=True)
class RunContext:
    subject: str
    display_name: str
    platform: str
    job_id: str
    roles: list[str] = field(default_factory=list)

    @staticmethod
    def for_principal(principal: Principal, job_id: str) -> "RunContext":
        return RunContext(subject=principal.subject, display_name=principal.display_name,
                          platform=principal.platform, job_id=job_id, roles=list(principal.roles))

    def to_json(self) -> str:
        return json.dumps(asdict(self))

    @staticmethod
    def from_json(s: str) -> "RunContext":
        d = json.loads(s)
        return RunContext(subject=d["subject"], display_name=d.get("display_name", ""),
                          platform=d.get("platform", ""), job_id=d["job_id"], roles=d.get("roles", []))


def new_token() -> str:
    return secrets.token_urlsafe(32)


def token_digest(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def resolve_user(token: str | None, claimed_user: str | None, lookup) -> tuple[str | None, RunContext | None]:
    """Return (user to attribute this command to, run context or None).

    No token (a human or script running the CLI directly) -> the claimed --user, unchanged.
    A token -> the caller it was issued for; the claimed --user is ignored. An unknown or expired
    token is refused rather than silently falling back to the claim, since falling back would turn
    the token into an optional suggestion."""
    if not token:
        return claimed_user, None
    ctx = lookup(token)
    if ctx is None:
        raise PolicyError(
            f"{RUN_TOKEN_ENV} is set but unknown or expired — this run's identity can't be resolved. "
            "The run may have outlived its time limit; it cannot continue under another identity."
        )
    return ctx.subject, ctx
