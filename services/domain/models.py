"""Shared value types for the domain layer.

Nothing in this module imports redis, databricks-sdk, requests, or any adapter-side library.
If you find yourself wanting to import one here, the type belongs in an adapter instead.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum

from domain.errors import ConfigurationError, PolicyError


class Environment(str, Enum):
    PROD = "prod"
    DEV = "dev"
    UAT = "uat"


class Backend(str, Enum):
    """The systems the query governor tracks separately, per-backend concurrency (agent-instructions.md)."""
    DATABRICKS = "databricks"
    ADO = "ado"  # live Dev/UAT reads (fabric-read/adf-databricks-read Path B) — still governed, per
                 # repo-registry.md: "a live Dev/UAT read still goes through the query governor"
    FABRIC = "fabric"
    ADF = "adf"  # ADF pipeline/activity run-history reads (OPEN_ITEMS.md, decided 2026-09-27) —
                 # a separate backend from ADO: this is an ARM management-plane call (pipeline-runs/
                 # activity-runs), not an ADO Git content read, so it gets its own concurrency cap.
    JIRA = "jira"  # Jira REST (OPEN_ITEMS.md #29, decided 2026-09-30) — reads are fully governed
                   # (rate + concurrency + cache + audit); writes are audit-only (run_governed_write:
                   # no cache — a write must never be served or deduplicated — and no rate count, a
                   # single user-approved write is not a load concern; the value is the audit trail,
                   # since a Jira write is this workspace's only mutating surface). Widening the
                   # governor to cover Jira makes "every outbound integration is governed" the rule.


@dataclass(frozen=True)
class RepoRef:
    """A resolved repo + branch + environment, as looked up from the repo registry."""
    repo: str
    org: str
    project: str
    branch: str
    environment: Environment
    mirrored: bool  # True => read from mirror-active/; False => on-demand live read
    catalog: str | None = None  # Databricks only — the Unity Catalog this branch corresponds to
    status: str | None = None  # e.g. "planned_not_live" — see repo_registry.yaml's comments


@dataclass(frozen=True)
class AdfFactoryRef:
    """A resolved Azure Data Factory identity for one environment, as looked up from the ADF
    factory registry (OPEN_ITEMS.md: ADF pipeline run-history reader, decided 2026-09-27).

    Deliberately separate from RepoRef: this identifies an Azure ARM resource (for run-history
    reads), not a git repo/branch (for content reads) — the two happen to cover the same physical
    ADF instance but are looked up through different registries for different purposes.
    """
    environment: Environment
    subscription_id: str
    resource_group: str
    factory_name: str


@dataclass(frozen=True)
class ModelRef:
    """A resolved LLM assignment for one lifecycle stage, as looked up from the model registry.

    Not used by anything yet — the host process that would actually call an LLM (Claude Agent SDK
    or equivalent) doesn't exist in this workspace yet. This exists so that host, once built, has a
    registry to resolve against on day one instead of hardcoding a vendor/model per stage."""
    stage_key: str
    vendor: str  # "anthropic" today; "openai"/"azure_openai"/etc. once a non-Claude stage exists
    model: str
    secret_name: str  # env var name (via SecretProvider) holding this vendor's API key


@dataclass(frozen=True)
class RateLimitDecision:
    allowed: bool
    wait_seconds: float | None = None
    queue_position: int | None = None
    reason: str | None = None


@dataclass(frozen=True)
class Claim:
    entity: str
    owner: str
    started_at: datetime
    last_heartbeat: datetime


@dataclass(frozen=True)
class QueryResult:
    columns: list[str]
    rows: list[tuple]
    row_count: int
    source: str = ""  # e.g. "<prod_catalog>.gold.sales_summary" or a DAX target — for audit logging

    def to_json(self) -> str:
        """Round-trips through the query-governor's cache (a string store) and is what CLI
        commands print to stdout for Claude to read — plain JSON, no Python repr."""
        import json
        return json.dumps(
            {"columns": self.columns, "rows": [list(r) for r in self.rows],
             "row_count": self.row_count, "source": self.source}
        )

    @staticmethod
    def from_json(s: str) -> "QueryResult":
        import json
        data = json.loads(s)
        return QueryResult(
            columns=data["columns"],
            rows=[tuple(r) for r in data["rows"]],
            row_count=data["row_count"],
            source=data.get("source", ""),
        )


@dataclass(frozen=True)
class MaintenanceStatus:
    active: bool
    eta_message: str | None = None


@dataclass(frozen=True)
class Principal:
    """Who is asking, as resolved from a validated identity token — never a self-reported header.

    OPEN_ITEMS.md (2026-09-22): platform is read from the token's own appid/azp claim, and roles
    carries Entra group/app-role claims from day one, so a future per-user restriction (RLS-style)
    is a filter over data already logged with this shape, not a re-plumb.
    """
    subject: str  # token's oid/sub — stable identity, not a display name
    display_name: str
    platform: str  # e.g. "teams", "web-portal", "slack" — from the token's appid/azp, not a header
    roles: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class InteractionLogEntry:
    """One record of a completed (or failed) engagement — the audit trail across every frontend.

    prompt_preview, not the full prompt: enough for an audit trail without storing arbitrarily
    long/sensitive prompt content verbatim in a plain JSONL file (this is the free-form-prompt
    branch — there's no ticket_key to log instead, since a request isn't necessarily about one
    ticket at all).
    """
    timestamp: datetime
    principal: Principal
    prompt_preview: str
    job_id: str
    duration_ms: int
    outcome: str  # "succeeded" | "failed" | "cancelled"

    def to_json(self) -> str:
        import json
        return json.dumps(
            {
                "timestamp": self.timestamp.isoformat(),
                "subject": self.principal.subject,
                "display_name": self.principal.display_name,
                "platform": self.principal.platform,
                "roles": self.principal.roles,
                "prompt_preview": self.prompt_preview,
                "job_id": self.job_id,
                "duration_ms": self.duration_ms,
                "outcome": self.outcome,
            }
        )


@dataclass(frozen=True)
class TraceStep:
    """One thing the agent did during an engagement turn, as read back from its session transcript.

    `kind` is "tool_call" (`tool` + `input_summary` + the paired `result_preview`) or "message"
    (assistant text, in `text`). Everything free-form here is already truncated and secret-redacted
    by domain/trace_policy.py by the time a caller sees it — never build one from raw transcript
    text without going through that.
    """
    kind: str
    timestamp: str | None = None
    tool: str | None = None
    input_summary: str | None = None
    result_preview: str | None = None
    text: str | None = None


class QueryNotAllowedError(PolicyError):
    """Raised by domain.query_guard when a statement contains a forbidden keyword.

    POLICY, not an error to retry or escalate as an incident — this is the guardrail working.
    """


class RepoNotInRegistryError(ConfigurationError):
    """Raised when a repo/environment isn't (yet) listed in the repo registry.

    CONFIGURATION — needs a human to add/confirm a registry row, not an urgent production issue.
    """
