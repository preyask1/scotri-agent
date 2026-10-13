# Copyright (c) 2026 Preyas Kulshrestha. Part of the Scotri engine; MIT License (see LICENSE).
# Keep this notice in copies and derived works, including AI-assisted ones.
"""The Scotri MCP server — exposes the same engagement lifecycle as app/api.py, but as MCP tools for
a Claude/agent client (Claude Desktop, another Claude Code session, ...) instead of as a plain HTTP
API for a Teams/web/Slack frontend. This is a second *transport* for the same domain logic, not a
second engine: every tool here calls straight into domain/engagement_policy.py and the same
`container`, exactly like app/api.py's /v1/chat and /v1/chat/{job_id} routes do.

Deliberately outward-facing only: this is Scotri exposing ITSELF as tools for a caller, never Scotri
consuming an MCP server for its own Jira/Databricks/Fabric integrations. That inbound direction is
still hard-ruled out — see agent-instructions.md's "No MCP servers back this agent's automation" and
CLAUDE.md's "No .mcp.json" — because it doesn't change how Scotri does its own read-only work,
doesn't add an .mcp.json to this workspace's own config, and doesn't give the underlying Agent SDK
run a new MCP-backed integration to depend on. See CLAUDE.md and agent-instructions.md for the
recorded distinction (added alongside this file).

One free-form tool pair, not one tool per skill — mirrors api.py's own documented choice: no
per-capability endpoint, no API-level prompt template. Scotri decides what to do by reading the
prompt, the same way an interactive session would. Two tools, not one, because MCP tool calls are
request/response and a Scotri engagement can run long (multi-stage skill chains, governed queries) —
`scotri_start` returns immediately with a job_id; `scotri_status` polls it. Same shape as
POST /v1/chat + GET /v1/chat/{job_id}, just over MCP instead of HTTP.

Auth: reuses container.identity_provider (the same Azure AD token validation api.py uses) rather
than trusting whatever process spawned this server — this runs headless for multiple callers, so
"whoever can start the process" is not a safe stand-in for a principal.

Targets `mcp` 2.x (`mcp.server.mcpserver.MCPServer` — v1's `FastMCP` was renamed and its API changed;
confirmed against the installed 2.2.0 by inspecting the package directly, not guessed). A tool
requests the current `Context` by adding a same-named parameter annotated `Context`; `Context.headers`
returns the transport's raw request headers (`None` on stdio, populated under streamable-http) —
see mcp/server/mcpserver/context.py's own docstring: "Headers are client-supplied input — never
treat one as an identity assertion" on their own, which is exactly why they're passed through
`identity_provider.validate` below rather than trusted directly.

Run with: uvicorn app.mcp_server:asgi_app --host 0.0.0.0 --port 8100  (from inside services/, venv
active) — a separate port from app/api.py's 8000, since these are two independent transports for the
same engine and either should be able to run without the other.
"""
from __future__ import annotations

import functools
import logging
from datetime import datetime, timezone

from mcp.server.mcpserver import Context, MCPServer
from mcp.server.mcpserver.exceptions import ToolError

from app.container import SERVICES_ROOT, build_container
from app.engagement_runner import EngagementRunner
from app.logging_setup import configure_logging
from domain.access_policy import require_owner_or_role
from domain.diagrams import extract_diagrams
from domain.engagement import Engagement, EngagementStatus, is_terminal
from domain.engagement_policy import cancel_engagement, resolve_turn, start_engagement
from domain.engagement_queue import queue_view
from domain.errors import NotFoundError, PolicyError, ScotriError
from domain.models import Principal
from domain.shared_files import (
    decode_upload_content, list_engagement_files, read_engagement_file, resolve_uploads, store_upload,
)

WORKSPACE_ROOT = str(SERVICES_ROOT.parent)  # C:\Scotri — where CLAUDE.md/.claude/skills load from

logger = logging.getLogger("scotri.mcp")

configure_logging(log_file=SERVICES_ROOT / "logs" / "scotri-mcp.log")
container = build_container()

mcp = MCPServer("scotri")

runner = EngagementRunner(lambda: container, WORKSPACE_ROOT, "mcp")
"""Starts jobs through the admission queue shared with app/api.py (OPEN_ITEMS.md #32)."""

FILE_ACCESS_ROLES = container.role_set("HISTORY_ADMIN_ROLES") | container.role_set("FILE_READER_ROLES")
"""Same rule as app/api.py's file routes: the engagement's owner, or one of these roles."""


def _as_tool_error(fn):
    """Confirmed by running a real client against this server (2026-09-24): MCPServer.call_tool
    wraps any exception that ISN'T a ToolError into UnexpectedToolError, whose message is always
    the generic "Error executing tool <name>" — the original ScotriError.user_message never reaches
    the caller, only the server log (see mcp/server/mcpserver/exceptions.py's own docstrings). A
    plain `raise ScotriError(...)` from inside a tool body, the pattern every other adapter in this
    codebase uses, would silently swallow the actual reason (missing token, unknown job_id, ...)
    right when a caller most needs it. This re-raises as ToolError so `str(exc)` (== user_message)
    reaches the client, matching what app/api.py's HTTP error handler already does for the same
    domain.errors.ScotriError today."""

    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except ScotriError as exc:
            raise ToolError(exc.user_message) from exc

    return wrapper


def _authenticate(ctx: Context) -> Principal:
    """Same contract as app/api.py's _authenticate: a missing/invalid Bearer token never reaches a
    tool body. `ctx.headers` is None on a transport that doesn't carry HTTP headers (stdio) — that
    is treated as "cannot authenticate," never as "auth not required.\""""
    headers = ctx.headers
    authorization = headers.get("authorization") if headers is not None else None
    if authorization is None or not authorization.lower().startswith("bearer "):
        raise ScotriError("Missing Bearer token")
    token = authorization.split(" ", 1)[1]
    return container.identity_provider.validate(token)


@mcp.tool()
@_as_tool_error
def scotri_start(prompt: str, ctx: Context, attachment_ids: list[str] | None = None,
                 conversation_id: str | None = None, resume_job_id: str | None = None) -> dict:
    """Start a Scotri engagement for a free-form prompt (a Jira ticket key, a debugging question, a
    change request — anything an interactive Scotri session could be asked). Returns immediately
    with a job_id; call scotri_status to poll for the result. Scotri decides what to do by reading
    the prompt itself, the same way an interactive session would — there is no per-capability tool
    here on purpose, matching app/api.py's POST /v1/chat.

    attachment_ids: optional upload_ids from scotri_upload (or the HTTP API's POST /v1/uploads), to
    hand to Scotri with this prompt. Only your own uploads can be attached.

    conversation_id / resume_job_id: for a follow-up turn (OPEN_ITEMS.md #25). Omit both on the first
    turn; the result carries a conversation_id. Pass it back to group the next turn with this one,
    and pass the previous turn's job_id as resume_job_id to continue its session (Scotri then sees
    what it saw). Both must be your own. Same rules as POST /v1/chat.
    """
    principal = _authenticate(ctx)
    conversation_id, resume_session_id = resolve_turn(
        container.job_store, container.engagement_history, principal,
        conversation_id=conversation_id, resume_job_id=resume_job_id,
    )
    attachment_ids = list(dict.fromkeys(attachment_ids or []))
    if attachment_ids:
        resolve_uploads(container.artifact_store, attachment_ids, principal.subject)

    job_id = start_engagement(container.job_store, principal, prompt, resume_session_id=resume_session_id,
                              conversation_id=conversation_id, attachment_ids=tuple(attachment_ids))
    # Returns at once either way: the run starts now on a thread if a slot is free, else it waits in
    # the queue shared with the HTTP API (a busy refusal comes back as a tool error that says so).
    admission = runner.submit(container.job_store.get(job_id))

    logger.info("mcp chat %s %s by %s@%s (conversation %s)%s", job_id, admission.outcome.value,
                principal.subject, principal.platform, conversation_id,
                " (continuing a prior turn)" if resume_session_id else "")
    response = {"job_id": job_id, "resumed": resume_session_id is not None, "conversation_id": conversation_id,
                "queued": admission.position is not None}
    if admission.position is not None:
        response["queue_position"] = admission.position
    return response


@mcp.tool()
@_as_tool_error
def scotri_upload(filename: str, content: str, ctx: Context, encoding: str = "text") -> dict:
    """Store one file to attach to a prompt (OPEN_ITEMS.md #6(f)), the MCP twin of POST /v1/uploads.
    Pass the returned upload_id in scotri_start's attachment_ids. `content` is the file as UTF-8 text
    (encoding="text", the default) or base64 (encoding="base64", for PDFs, images and other binary
    files). Same rules as the HTTP route: the size limit (10 MB by default), credential files refused,
    text secret-redacted before it is stored, and only you can attach it. An upload is Scotri's own
    storage, never a write to a product repo or Jira.
    """
    principal = _authenticate(ctx)
    policy = container.upload_policy
    data = decode_upload_content(content, encoding, policy.max_bytes)
    upload = store_upload(container.artifact_store, principal, filename, data, policy)
    logger.info("mcp upload %s stored for %s (%s, %d bytes, redacted=%s)",
                upload.upload_id, principal.subject, upload.filename, upload.size, upload.redacted)
    return {
        "upload_id": upload.upload_id,
        "filename": upload.filename,
        "size": upload.size,
        "content_type": upload.content_type,
        "created_at": upload.created_at.isoformat(),
        "redacted": upload.redacted,
    }


@mcp.tool()
@_as_tool_error
def scotri_status(job_id: str, ctx: Context) -> dict:
    """Poll a previously-started Scotri engagement. status is one of
    queued/running/succeeded/failed/cancelled/incomplete; result is populated once succeeded, error
    once failed. While status is queued, Scotri is busy and the job waits for a free slot: `queue`
    gives its position in line; cancel it with scotri_cancel. A cancelled job carries cancel_reason
    (user, admin, queue_timeout after waiting too long, or busy) and the original prompt, so it can
    be sent again. While it runs, progress says which stage it is in (phase, phase_label, phases_seen),
    how many tool calls it has made, and when it last did something; poll every 5-15 seconds. It
    never says what the run is looking at. Same v1 simplification as
    GET /v1/chat/{job_id}: any authenticated caller may poll any job_id (job_ids are opaque uuid4
    hex, not guessable/enumerable in practice) — restricting to the engagement's own principal is a
    deliberate follow-up, not an oversight.
    """
    _authenticate(ctx)

    engagement: Engagement | None = container.job_store.get(job_id)
    if engagement is None:
        raise ScotriError(f"No engagement with job_id {job_id}")

    return _status(engagement)


@mcp.tool()
@_as_tool_error
def scotri_cancel(job_id: str, ctx: Context) -> dict:
    """Cancel one of your own engagements. A job still waiting for a free slot is taken out of the
    line and never runs; a running one is stopped. Returns the job as scotri_status would, with the
    prompt, so it can be sent again. Idempotent: a finished job comes back unchanged.
    """
    principal = _authenticate(ctx)
    engagement: Engagement | None = container.job_store.get(job_id)
    if engagement is None:
        raise NotFoundError(f"No engagement with job_id {job_id}")
    if engagement.principal.subject != principal.subject:
        raise PolicyError("Only the caller who started this job can cancel it")
    result = runner.withdraw(job_id) or cancel_engagement(container.job_store, container.agent_runtime, job_id)
    return _status(result or engagement)


def _status(engagement: Engagement) -> dict:
    queue = None
    if engagement.status == EngagementStatus.QUEUED and getattr(container, "engagement_queue", None) is not None:
        queue = queue_view(container.engagement_queue, engagement.job_id, container.queue_settings)
    return {
        "job_id": engagement.job_id,
        "status": engagement.status.value,
        "prompt": engagement.prompt,
        "result": engagement.result,
        "diagrams": extract_diagrams(engagement.result),
        "error": engagement.error,
        "cancel_reason": engagement.cancel_reason,
        "queue": queue,
        "progress": _progress(engagement.job_id, is_terminal(engagement.status)),
    }


def _progress(job_id: str, finished: bool = False) -> dict | None:
    """Same as app/api.py's: phase, counts and times only (OPEN_ITEMS.md #39). Best-effort."""
    try:
        progress = container.progress_store.get(job_id)
    except Exception:  # noqa: BLE001
        logger.exception("could not read progress for job %s", job_id)
        return None
    return progress.public_view(datetime.now(timezone.utc), finished) if progress else None


def _require_file_access(principal: Principal, job_id: str) -> None:
    engagement = container.job_store.get(job_id) or container.engagement_history.get(job_id)
    if engagement is None:
        raise NotFoundError(f"No engagement with job_id {job_id}")
    require_owner_or_role(principal, engagement.principal.subject, FILE_ACCESS_ROLES,
                          resource=f"files of engagement {job_id}")


@mcp.tool()
@_as_tool_error
def scotri_list_files(job_id: str, ctx: Context) -> dict:
    """List the markdown files a finished Scotri engagement produced: requirement intake, bug
    verdict, impact analysis, implementation plan, test plan/results, the handoff document, and any
    proposal patch. Each is the version that run wrote, secret-redacted. Read one with
    scotri_get_file. Only the engagement's owner (or a configured reader role) may list them.
    """
    principal = _authenticate(ctx)
    _require_file_access(principal, job_id)
    files = list_engagement_files(container.artifact_store, job_id)
    return {
        "job_id": job_id,
        "files": [
            {"path": f.path, "size": f.size, "content_type": f.content_type, "created_at": f.created_at.isoformat()}
            for f in files
        ],
    }


@mcp.tool()
@_as_tool_error
def scotri_get_file(job_id: str, path: str, ctx: Context) -> dict:
    """Read one file from scotri_list_files, by its `path` (e.g. "scratch/PROJ-2107-handoff.md").
    Returns the full text. To find the handoff document, look for a path ending in "-handoff.md".
    """
    principal = _authenticate(ctx)
    _require_file_access(principal, job_id)
    data, shared = read_engagement_file(container.artifact_store, job_id, path)
    return {
        "job_id": job_id,
        "path": shared.path,
        "content_type": shared.content_type,
        "content": data.decode("utf-8", errors="replace"),
    }


_mcp_app = mcp.streamable_http_app()


async def asgi_app(scope, receive, send):
    """The MCP app, plus starting this process's queue ticker (OPEN_ITEMS.md #32) when the server
    starts. Started here rather than at import so tests that import this module start no thread."""
    if scope["type"] == "lifespan":
        runner.start_ticking()
        # Same catch-up as the API's start-up: on Azure this process runs in its own container,
        # so it can't rely on the API having brought generated lineage up to date.
        container.ensure_lineage_current_in_background()
    await _mcp_app(scope, receive, send)
# No app-level exception handler needed here, unlike app/api.py: every tool is wrapped in
# _as_tool_error, so a ScotriError never escapes a tool body uncaught in the first place — it's
# already a ToolError by the time MCPServer.call_tool sees it, which returns is_error=True with
# str(exc) (== user_message) in `content`. A ScotriError raised in a tool never reaches the ASGI
# layer at all, so a Starlette exception_handler here would be dead code.
