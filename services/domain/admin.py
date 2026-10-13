# Copyright (c) 2026 Preyas Kulshrestha. Part of the Scotri engine; MIT License (see LICENSE).
# Keep this notice in copies and derived works, including AI-assisted ones.
"""Pure rules for the admin surface (OPEN_ITEMS.md #43): what an admin may see, how a log tail is
cut and redacted, what a caller-app record must hold, and the shape of an admin audit line.

Why an admin surface fits a read-only workspace. Every admin route reads Scotri's own state (its
logs, job store, history, Redis counters, mirror status). It writes the caller-app register below,
which lives in Scotri's own database, the same footing as a verdict review, and (since 2026-10-06) can
start a manual mirror sync, which runs the sync service's own `refresh_registry_once` under the shared
sync lock: a human admin asking the sync service's code to run now, never the agent, which holds no
admin token. Nothing here reaches a product repo, Jira, Databricks or Fabric, so the hard rules in
agent-instructions.md are unchanged. What makes it a departure is that it crosses users: an admin
sees every caller's engagements and steps. So it gets its own gate and its own audit:

- **Gate.** `ADMIN_ROLES` (an Entra app role Scotri reads, never assigns). Empty by default, which
  means nobody: `require_role` fails closed on an empty set.
- **Audit.** Every admin call, allowed or refused, appends one line to the admin audit log: who,
  which action, which target, the outcome. Never a payload: no log text, no step contents, no
  prompt. A role gates; it never exempts (identity-model.md §5).

"Apps and API tokens" means recording which Entra-registered client apps call Scotri, keyed by the
token's `azp`/`appid` (`Principal.platform`). Scotri never mints, stores or shows a credential;
token issuance stays with Entra. The register is the place to say which caller is a person-facing
frontend and which is a service identity, which the webhook/sweep path (#7) will need for
`require_human_caller`.
"""
from __future__ import annotations

import re
from collections import deque
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from pathlib import Path

from domain.errors import PolicyError
from domain.redaction import redact_secrets

LOG_SOURCES: dict[str, str] = {
    "api": "scotri-api.log",
    "cli": "scotri-cli.log",
    "mcp": "scotri-mcp.log",
    "sync": "scotri-sync-service.log",
}
"""The only log files the admin log view will open, by short name. An allowlist, not a path
parameter, so no request can name a file outside `services/logs/`."""

DEFAULT_LOG_LINES = 200
MAX_LOG_LINES = 2000
MAX_LINE_CHARS = 2000
MAX_FILTER_CHARS = 200


def resolve_log_source(logs_dir: Path, source: str) -> Path:
    """The file for one allowlisted source name; PolicyError for anything else."""
    name = LOG_SOURCES.get(source)
    if name is None:
        raise PolicyError(f"unknown log source {source!r}; use one of: {', '.join(sorted(LOG_SOURCES))}")
    return logs_dir / name


def clamp_lines(lines: int | None) -> int:
    if lines is None:
        return DEFAULT_LOG_LINES
    return max(1, min(int(lines), MAX_LOG_LINES))


def tail_log(path: Path, lines: int, contains: str | None = None) -> tuple[list[str], bool]:
    """The last `lines` lines of `path` (after filtering by `contains`, case-insensitive), each
    secret-redacted and truncated. Returns (lines, exists). Reads the file once, streaming, keeping
    only a bounded window, so a 10 MB rotating log is not loaded whole.

    Redaction happens here even though the log handlers already redact env-var secret values: those
    only know secrets present in this process's environment, while `redact_secrets` is the pattern
    redactor (connection strings, bearer tokens, JWTs) the CLI boundary uses."""
    if not path.is_file():
        return [], False
    needle = (contains or "").strip().lower()[:MAX_FILTER_CHARS]
    window: deque[str] = deque(maxlen=lines)
    with path.open("r", encoding="utf-8", errors="replace") as handle:
        for raw in handle:
            line = raw.rstrip("\n")
            if needle and needle not in line.lower():
                continue
            window.append(line)
    out = []
    for line in window:
        line = redact_secrets(line)
        if len(line) > MAX_LINE_CHARS:
            line = line[:MAX_LINE_CHARS] + " …[truncated]"
        out.append(line)
    return out, True


class CallerKind(str, Enum):
    INTERACTIVE = "interactive"
    """A frontend a person uses (scotri-ui, Teams, an MCP client someone drives)."""
    SERVICE = "service"
    """An automated caller with no user present (webhook receiver, sweep). A human-approval action
    must be refused for these (`require_human_caller`)."""


_APP_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
MAX_APP_NAME = 120
MAX_APP_OWNER = 200
MAX_APP_NOTES = 1000


@dataclass(frozen=True)
class CallerApp:
    """One registered client app. `app_id` is the token's azp/appid claim as Scotri sees it in
    `Principal.platform`, e.g. an Entra application (client) id."""
    app_id: str
    name: str
    kind: CallerKind
    owner: str
    notes: str
    registered_by: str
    registered_at: datetime
    updated_at: datetime


def validate_caller_app(app_id: str, name: str, kind: str, owner: str, notes: str) -> CallerKind:
    """Check a caller-app record before it is stored; returns the parsed kind. PolicyError (→ 400
    at the route) on anything malformed.

    Refuses notes that look like they carry a credential. This register is metadata only; a secret
    pasted into it would be stored, shown to every admin, and kept in history."""
    if not _APP_ID.match(app_id or ""):
        raise PolicyError("app_id must be 1-128 characters: letters, digits, '.', '_', ':' or '-'")
    if not (name or "").strip() or len(name) > MAX_APP_NAME:
        raise PolicyError(f"name is required and at most {MAX_APP_NAME} characters")
    if len(owner or "") > MAX_APP_OWNER:
        raise PolicyError(f"owner is at most {MAX_APP_OWNER} characters")
    if len(notes or "") > MAX_APP_NOTES:
        raise PolicyError(f"notes are at most {MAX_APP_NOTES} characters")
    for field, value in (("name", name), ("owner", owner), ("notes", notes)):
        if redact_secrets(value or "") != (value or ""):
            raise PolicyError(f"{field} looks like it contains a credential; this register holds "
                              "metadata only, never a secret")
    try:
        return CallerKind(kind)
    except ValueError:
        raise PolicyError(f"kind must be one of: {', '.join(k.value for k in CallerKind)}") from None


@dataclass(frozen=True)
class PlatformActivity:
    """How one caller platform has actually used Scotri, from engagement history."""
    platform: str
    engagements: int
    subjects: int
    last_seen: datetime | None


def merge_callers(registered: list[CallerApp], activity: list[PlatformActivity]) -> list[dict]:
    """One row per app id that is registered, seen, or both. A seen-but-unregistered caller is the
    thing worth an admin's attention: some client is calling with a valid token that nobody has
    said what it is."""
    seen = {a.platform: a for a in activity if a.platform}
    rows = []
    for app in registered:
        act = seen.pop(app.app_id, None)
        rows.append({
            "app_id": app.app_id,
            "registered": True,
            "name": app.name,
            "kind": app.kind.value,
            "owner": app.owner,
            "notes": app.notes,
            "registered_by": app.registered_by,
            "registered_at": app.registered_at.isoformat(),
            "updated_at": app.updated_at.isoformat(),
            **_activity_fields(act),
        })
    for act in seen.values():
        rows.append({"app_id": act.platform, "registered": False, "name": None, "kind": None,
                     **_activity_fields(act)})
    rows.sort(key=lambda r: (r["registered"], r["last_seen"] or ""), reverse=True)
    return rows


def _activity_fields(act: PlatformActivity | None) -> dict:
    if act is None:
        return {"engagements": 0, "subjects": 0, "last_seen": None}
    return {"engagements": act.engagements, "subjects": act.subjects,
            "last_seen": act.last_seen.isoformat() if act.last_seen else None}


class AuditOutcome(str, Enum):
    OK = "ok"
    DENIED = "denied"
    ERROR = "error"


@dataclass(frozen=True)
class AdminAuditEntry:
    """One admin action. Who, what, which target, how it ended. Never the payload."""
    at: datetime
    subject: str
    display_name: str
    platform: str
    action: str
    target: str | None
    outcome: AuditOutcome
    detail: str | None = None
    """A short, non-payload note, e.g. which fields an app upsert changed or an error category."""

    def to_dict(self) -> dict:
        return {
            "at": self.at.isoformat(),
            "subject": self.subject,
            "display_name": self.display_name,
            "platform": self.platform,
            "action": self.action,
            "target": self.target,
            "outcome": self.outcome.value,
            "detail": self.detail,
        }
