"""The Scotri agent-service — the long-running HTTP entry point every frontend (Teams, a web portal,
Slack once migrated, ...) calls, instead of each frontend embedding its own Agent SDK invocation.

Free-form prompt design: the frontend sends a raw prompt, Scotri (via the Agent SDK reading its own
CLAUDE.md/skills) decides what to do — no per-capability endpoint, no API-level prompt template.
Scope/behavior boundaries come entirely from Scotri's own agent-instructions.md/Scotri.md hard rules
(see domain/engagement_policy.py's module docstring).

Two entry points onto the SAME engine, not two designs: /v1/chat takes free text (also what
app/mcp_server.py's MCP tools mirror); /v1/engagements (added 2026-09-24, reconciled from the
earlier, since-abandoned api-service branch's ticket-shaped design) takes a bare `ticket_key` and is
purely a narrower INPUT shape, not a behaviorally-restricted one — it converts ticket_key into the
same free-form prompt Scotri already knows how to read (see CLAUDE.md: "Give it a Jira ticket, e.g.
PROJ-2041") and calls the exact same start_engagement/run_engagement /v1/chat does. No second
Engagement/InteractionLogEntry shape: `Engagement.prompt` stays the one canonical field so
app/mcp_server.py and app/cli.py, both already built against it, don't have to know this endpoint
exists. /v1/engagements additionally checks presence-claim and returns an advisory
`collision_notice` if someone else already has the ticket — that's the one piece worth keeping from
the abandoned branch, since it only makes sense once you actually have a ticket_key up front; it
does not generalize to arbitrary free text, so /v1/chat and the MCP tools deliberately don't carry it
(unchanged reasoning from the original 1d28155 commit).

Mirrors app/sync_service.py's shape for a long-running process: build ONE container at process
start, and route the actual work through domain-level functions (engagement_policy.*) so this file
stays thin — argument/response wiring only, exactly like app/cli.py's own rule for itself.

Run with: uvicorn app.api:app --host 0.0.0.0 --port 8000  (from inside services/, venv active).
"""
from __future__ import annotations

import logging
import uuid
from contextlib import asynccontextmanager
from dataclasses import asdict
from datetime import datetime, timezone

from urllib.parse import quote

from fastapi import FastAPI, File, Header, HTTPException, Response, UploadFile
from pydantic import BaseModel

from app.admin_routes import build_admin_router
from app.container import SERVICES_ROOT, build_container
from app.engagement_runner import EngagementRunner
from app.logging_setup import configure_logging
from domain.diagrams import extract_diagrams
from domain.engagement import Engagement, EngagementStatus, is_terminal
from domain.engagement_queue import ServerBusyError, queue_view
from domain.engagement_policy import (
    adopt_into_conversation, cancel_engagement, fail_interrupted_engagements, resolve_turn,
    start_engagement,
)
from domain.errors import ErrorCategory, PolicyError, ScotriError
from domain.learning_browse import browse_learnings, get_learning
from domain.presence_policy import format_collision_notice, is_claim_stale
from domain.shared_files import (
    SharedFile,
    list_engagement_files,
    read_engagement_file,
    is_active_content,
    resolve_uploads,
    store_upload,
)
from domain.access_policy import require_owner_or_role
from domain.trace_policy import get_job_trace
from domain.verdict_review import (
    REVIEWABLE_STATUSES,
    UNREVIEWED,
    is_reviewable,
    ReviewOutcome,
    VerdictReview,
    validate_review,
)

WORKSPACE_ROOT = str(SERVICES_ROOT.parent)  # C:\Scotri — where CLAUDE.md/.claude/skills live

logger = logging.getLogger("scotri.api")

configure_logging(log_file=SERVICES_ROOT / "logs" / "scotri-api.log")
container = build_container()

API_ORIGIN = "api"  # Engagement.origin for jobs started here — what the startup sweep is scoped to

runner = EngagementRunner(lambda: container, WORKSPACE_ROOT, "api")
"""Starts jobs through the shared admission queue (OPEN_ITEMS.md #32, app/engagement_runner.py)."""


@asynccontextmanager
async def lifespan(_app: FastAPI):
    # A job's worker is a background task in THIS process, so any "running" job left over from a
    # previous process can never finish. Fail them now so a polling frontend stops spinning on them.
    # Never let a Redis hiccup here keep the API from starting.
    try:
        count = fail_interrupted_engagements(container.job_store, origin=API_ORIGIN,
                                             progress_store=container.progress_store,
                                             queue=container.engagement_queue)
        if count:
            logger.warning("startup: marked %d engagement(s) interrupted by the previous restart as failed", count)
    except Exception:  # noqa: BLE001
        logger.exception("startup: could not sweep interrupted engagements")
    # Renews this process's slots, fails runs whose process died, expires jobs that waited too
    # long and starts waiting jobs when slots free up (OPEN_ITEMS.md #32).
    runner.start_ticking()
    # Generated lineage missing or behind the mirror (fresh deployment, failed post-swap step,
    # lost index): regenerate in the background. A no-op when everything is current.
    container.ensure_lineage_current_in_background()
    yield
    runner.stop_ticking()


def _client_config():
    """The deployment's client settings; engine defaults if config/client.yaml is missing or broken,
    so a bad branding file can never take the API down."""
    from domain.client_config import DEFAULT_CLIENT_CONFIG

    try:
        return container.client_config
    except Exception:  # noqa: BLE001
        logger.exception("config/client.yaml could not be read; using engine defaults")
        return DEFAULT_CLIENT_CONFIG


app = FastAPI(title=f"{_client_config().display_name} agent-service", lifespan=lifespan)

_CATEGORY_TO_STATUS = {
    ErrorCategory.CREDENTIAL: 401,
    ErrorCategory.NOT_FOUND: 404,
    ErrorCategory.POLICY: 403,
    ErrorCategory.TIMEOUT: 503,
    ErrorCategory.TRANSIENT: 503,
    ErrorCategory.CONFIGURATION: 500,
    ErrorCategory.UNKNOWN: 500,
}


@app.exception_handler(ServerBusyError)
async def _busy_handler(_request, exc: ServerBusyError):
    """Refused at the door (OPEN_ITEMS.md #32): 429 when the caller already has the most waiting
    jobs allowed, 503 when the queue is full. The body carries the prompt so a frontend can offer
    to send it again; nothing ran."""
    from fastapi.responses import JSONResponse

    status_code = 429 if exc.reason == "user_limit" else 503
    return JSONResponse(status_code=status_code, content=exc.to_dict(), headers={"Retry-After": "60"})


@app.exception_handler(ScotriError)
async def _scotri_error_handler(_request, exc: ScotriError):
    from fastapi.responses import JSONResponse

    status_code = _CATEGORY_TO_STATUS.get(exc.category, 500)
    return JSONResponse(
        status_code=status_code,
        content={"ok": False, "category": exc.category.value, "message": exc.user_message},
    )


HISTORY_ADMIN_ROLES = {
    r.strip() for r in container.secrets.get_optional("HISTORY_ADMIN_ROLES", "").split(",") if r.strip()
}
"""Roles allowed to read another user's conversation/history (OPEN_ITEMS.md #19 decision 1 — "relevant
users" beyond the owner). Empty by default: owner-only. Set HISTORY_ADMIN_ROLES=lead,admin to widen.
Configurable rather than hardcoded so the still-open policy decision stays a config change, not a
code change."""


FILE_READER_ROLES = container.role_set("FILE_READER_ROLES")
"""Roles allowed to read the deliverable files of an engagement someone else ran (OPEN_ITEMS.md #6),
in addition to HISTORY_ADMIN_ROLES. Narrower than HISTORY_ADMIN_ROLES on purpose: it lets an
implementer fetch a handoff doc without being able to read the whole conversation. Empty by default:
owner-only."""


LEARNINGS_READER_ROLES = container.role_set("LEARNINGS_READER_ROLES")
"""Roles allowed to browse every caller's learnings over GET /v1/learnings?scope=team (OPEN_ITEMS.md
#60(d), domain/learning_browse.py). Empty by default: each caller browses only their own. Doesn't
affect what the agent's `retrieve` searches, which stays team-wide."""


class CreateChatRequest(BaseModel):
    prompt: str
    attachment_ids: list[str] = []
    """upload_ids from POST /v1/uploads to hand to the agent with this prompt. Must be the caller's
    own uploads; an unknown id is a 404 and someone else's is a 403, both before a job is queued."""
    resume_job_id: str | None = None
    conversation_id: str | None = None
    """The server-issued conversation this turn belongs to (OPEN_ITEMS.md #25). Omit on the first
    turn — the server issues one and returns it. Pass it (or resume_job_id) on a follow-up so the
    turn is grouped server-side. A conversation_id that belongs to another principal is rejected;
    an unknown one is accepted (a client continuing an id the server hasn't recorded a turn for yet)."""
    """A prior /v1/chat job_id whose session this turn should continue — the caller's own way of
    saying "this is a follow-up in the same conversation, not a new one." Must be the caller's own
    job (403 otherwise). Resolved to that job's session_id (domain/engagement_policy.resolve_turn,
    never trusted as a session_id directly from the caller); if the referenced
    job has no session_id yet (not found, or hasn't completed), the turn just runs fresh instead
    of failing outright — losing the continuation is better than blocking the caller on it. The
    response's `resumed` flag says which happened, so the caller can tell the user."""


VERDICT_REVIEWER_ROLES = {
    r.strip() for r in container.secrets.get_optional("VERDICT_REVIEWER_ROLES", "").split(",") if r.strip()
}
"""Roles allowed to review an engagement someone else ran (OPEN_ITEMS.md #47). Empty by default:
only the human who ran the engagement may review it. Same configurable-not-hardcoded shape as
HISTORY_ADMIN_ROLES, so widening it to a lead/admin role is a config change."""


class AdoptConversationRequest(BaseModel):
    job_ids: list[str]
    conversation_id: str | None = None
    """Omit to get a fresh id; or pass one you own (e.g. the id a later turn of the same thread already
    got) to add the older jobs to it."""


class CreateEngagementRequest(BaseModel):
    ticket_key: str
    attachment_ids: list[str] = []


class VerdictReviewRequest(BaseModel):
    outcome: str
    """upheld | partially_upheld | overturned. "unreviewed" cannot be submitted — it is the absence
    of a review, not a value a human sets."""
    reason: str = ""
    superseded_by_job_id: str | None = None


def _authenticate(authorization: str | None):
    if authorization is None or not authorization.lower().startswith("bearer "):
        raise HTTPException(status_code=401, detail="Missing Bearer token")
    token = authorization.split(" ", 1)[1]
    return container.identity_provider.validate(token)


@app.get("/health")
def health() -> dict:
    try:
        container._redis_client.ping()  # noqa: SLF001 — health check is a deliberate, narrow exception
        redis_ok = True
    except Exception:  # noqa: BLE001 — a health check must never itself raise
        redis_ok = False
    return {"ok": redis_ok, "redis": redis_ok}


PORTAL_PAGE = SERVICES_ROOT.parent / "docs" / "developer" / "portal.html"


@app.get("/portal", include_in_schema=False)
def developer_portal() -> Response:
    """The developer portal for both transports (docs/developer/portal.html). Unauthenticated like
    /health and FastAPI's own /docs: it is static documentation and carries no data. Served from here
    so its "Try it" console calls this API same-origin, without a CORS allowance."""
    if not PORTAL_PAGE.is_file():
        raise HTTPException(status_code=404, detail="Developer portal page is not deployed")
    return Response(content=_branded(PORTAL_PAGE), media_type="text/html; charset=utf-8")


LOGO_TYPES = {".svg": "image/svg+xml", ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
              ".webp": "image/webp"}


def _logo_path() -> Path | None:
    """The configured logo, only if it is an image file under client/brand/ (never anything else on
    disk, whatever config/client.yaml says)."""
    logo = _client_config().logo
    if not logo:
        return None
    brand_dir = (SERVICES_ROOT.parent / "client" / "brand").resolve()
    path = (SERVICES_ROOT.parent / logo).resolve()
    if brand_dir not in path.parents or path.suffix.lower() not in LOGO_TYPES or not path.is_file():
        return None
    return path


def _branded(page: Path) -> bytes:
    """A static page with the deployment's display name and logo (domain/branding.py)."""
    from domain.branding import LOGO_ROUTE, brand_html

    logo = LOGO_ROUTE if _logo_path() else None
    return brand_html(page.read_text(encoding="utf-8"), _client_config().display_name, logo).encode("utf-8")


@app.get("/brand/logo", include_in_schema=False)
def brand_logo() -> Response:
    """The deployment's logo (client/brand/, set in client/profile.yaml). Unauthenticated, like the
    pages that show it."""
    path = _logo_path()
    if path is None:
        raise HTTPException(status_code=404, detail="No logo is configured")
    return Response(content=path.read_bytes(), media_type=LOGO_TYPES[path.suffix.lower()],
                    headers={"X-Content-Type-Options": "nosniff", "Cache-Control": "max-age=300"})


@app.post("/v1/chat", status_code=202)
def create_chat(
    body: CreateChatRequest,
    authorization: str | None = Header(default=None),
) -> dict:
    principal = _authenticate(authorization)

    conversation_id, resume_session_id = resolve_turn(
        container.job_store, container.engagement_history, principal,
        conversation_id=body.conversation_id, resume_job_id=body.resume_job_id,
    )
    attachment_ids = _checked_attachments(body.attachment_ids, principal)

    job_id = start_engagement(
        container.job_store, principal, body.prompt,
        resume_session_id=resume_session_id, origin=API_ORIGIN, conversation_id=conversation_id,
        attachment_ids=attachment_ids,
    )
    admission = runner.submit(container.job_store.get(job_id))  # ServerBusyError -> 429/503
    logger.info(
        "chat %s %s by %s@%s%s (conversation %s)",
        job_id, admission.outcome.value, principal.subject, principal.platform,
        " (continuing a prior turn)" if resume_session_id else "", conversation_id,
    )

    return {"job_id": job_id, "resumed": resume_session_id is not None, "conversation_id": conversation_id,
            **_admission_view(admission)}


def _admission_view(admission) -> dict:
    """`queued: true` and the place in line when the job has to wait for a slot."""
    if admission.position is None:
        return {"queued": False}
    return {"queued": True, "queue_position": admission.position}


def _checked_attachments(upload_ids: list[str], principal) -> tuple[str, ...]:
    """Fail the request, not the run, on a bad attachment: unknown -> 404, another user's -> 403
    (via the ScotriError handler)."""
    if not upload_ids:
        return ()
    resolve_uploads(container.artifact_store, upload_ids, principal.subject)
    return tuple(dict.fromkeys(upload_ids))


def _progress(job_id: str, finished: bool = False) -> dict | None:
    """The job's live progress for a poller (OPEN_ITEMS.md #39): phase, counts and times only, never
    what the run is looking at, because any authenticated caller may poll any job. None when the run
    hasn't started or nothing was recorded. Best-effort: a store failure must not fail the poll."""
    try:
        progress = container.progress_store.get(job_id)
    except Exception:  # noqa: BLE001
        logger.exception("could not read progress for job %s", job_id)
        return None
    return progress.public_view(datetime.now(timezone.utc), finished) if progress else None


def _queue(engagement: Engagement) -> dict | None:
    """Place in line while the job waits for a slot (OPEN_ITEMS.md #32); None otherwise."""
    if engagement.status != EngagementStatus.QUEUED or getattr(container, "engagement_queue", None) is None:
        return None
    return queue_view(container.engagement_queue, engagement.job_id, container.queue_settings)


def _job_response(engagement: Engagement) -> dict:
    return {
        "job_id": engagement.job_id,
        "status": engagement.status.value,
        "result": engagement.result,
        "diagrams": extract_diagrams(engagement.result),
        "error": engagement.error,
        "cancel_reason": engagement.cancel_reason,
        "queue": _queue(engagement),
        "progress": _progress(engagement.job_id, is_terminal(engagement.status)),
    }


def _cancel(job_id: str, authorization: str | None) -> dict:
    principal = _authenticate(authorization)

    engagement: Engagement | None = container.job_store.get(job_id)
    if engagement is None:
        raise HTTPException(status_code=404, detail=f"No engagement with job_id {job_id}")
    # Reading a job is open to any authenticated caller (see get_chat), but stopping someone
    # else's work is not.
    if engagement.principal.subject != principal.subject:
        raise HTTPException(status_code=403, detail="Only the caller who started this job can cancel it")

    # A job still waiting in line is just taken out of it; otherwise stop it the ordinary way.
    result = runner.withdraw(job_id) or cancel_engagement(container.job_store, container.agent_runtime, job_id)
    return {**_job_response(result or engagement), "prompt": engagement.prompt}


@app.post("/v1/chat/{job_id}/cancel")
def cancel_chat(job_id: str, authorization: str | None = Header(default=None)) -> dict:
    """Stop a queued/running chat job. Idempotent — an already-finished job is returned as-is, so
    the caller can just read `status` to see what actually happened (a job that completed in the
    same instant comes back `succeeded`, not `cancelled`)."""
    return _cancel(job_id, authorization)


@app.get("/v1/chat/{job_id}")
def get_chat(job_id: str, authorization: str | None = Header(default=None)) -> dict:
    # v1 simplification: any authenticated caller may poll any job_id — restricting to the
    # engagement's own principal is a deliberate follow-up, not an oversight, once there's a real
    # need for it (job_ids are opaque uuid4 hex, not guessable/enumerable in practice).
    _authenticate(authorization)

    engagement: Engagement | None = container.job_store.get(job_id)
    if engagement is None:
        raise HTTPException(status_code=404, detail=f"No engagement with job_id {job_id}")

    return {**_job_response(engagement), "prompt": engagement.prompt}


def _steps(job_id: str, authorization: str | None) -> dict:
    principal = _authenticate(authorization)

    engagement: Engagement | None = container.job_store.get(job_id)
    if engagement is None:
        raise HTTPException(status_code=404, detail=f"No engagement with job_id {job_id}")
    # Stricter than get_chat: the steps carry tool inputs/outputs (queries, file contents), which
    # is more than the final answer a job_id-holder can already read — so owner only, like cancel.
    if engagement.principal.subject != principal.subject:
        raise HTTPException(status_code=403, detail="Only the caller who started this job can read its steps")

    trace = get_job_trace(container.job_store, container.trace_reader, job_id,  # NotFoundError -> 404
                          progress_store=container.progress_store)
    return {
        "job_id": job_id,
        "status": trace.engagement.status.value,
        "steps": [asdict(step) for step in trace.steps],
    }


@app.get("/v1/chat/{job_id}/steps")
def get_chat_steps(job_id: str, authorization: str | None = Header(default=None)) -> dict:
    """What the agent did for this job, in order: each tool call with a truncated, secret-redacted
    input and result preview, plus the agent's own narration. Read from the agent's session
    transcript on this host, so it works while the job is still running (once its session has
    started) as well as after; 404 if there is none (job never got a session, or it was cleaned up)."""
    return _steps(job_id, authorization)


@app.get("/v1/engagements/{job_id}/steps")
def get_engagement_steps(job_id: str, authorization: str | None = Header(default=None)) -> dict:
    """Same as GET /v1/chat/{job_id}/steps — both kinds share one job store."""
    return _steps(job_id, authorization)


@app.post("/v1/engagements", status_code=202)
def create_engagement(
    body: CreateEngagementRequest,
    authorization: str | None = Header(default=None),
) -> dict:
    principal = _authenticate(authorization)
    attachment_ids = _checked_attachments(body.attachment_ids, principal)

    # Presence-claim is advisory only (domain/presence_policy.py) — never blocks the engagement,
    # just surfaces that someone else may already be on this ticket. Checked here, not for
    # /v1/chat, because it only makes sense once a ticket_key is known up front.
    now = datetime.now(timezone.utc)
    existing = container.presence_store.get_claim(body.ticket_key)
    collision_notice = None
    if existing is not None and not is_claim_stale(existing, now):
        collision_notice = format_collision_notice(existing, now)
    container.presence_store.set_claim(body.ticket_key, owner=principal.subject)

    # ticket_key becomes the prompt Scotri reads — no second Engagement shape (see module docstring).
    conversation_id = uuid.uuid4().hex
    job_id = start_engagement(
        container.job_store, principal, body.ticket_key, origin=API_ORIGIN, conversation_id=conversation_id,
        attachment_ids=attachment_ids,
    )
    admission = runner.submit(container.job_store.get(job_id))  # ServerBusyError -> 429/503
    logger.info(
        "engagement %s %s for ticket %s by %s@%s", job_id, admission.outcome.value, body.ticket_key,
        principal.subject, principal.platform,
    )

    response: dict = {"job_id": job_id, "conversation_id": conversation_id, **_admission_view(admission)}
    if collision_notice:
        response["collision_notice"] = collision_notice
    return response


@app.post("/v1/engagements/{job_id}/cancel")
def cancel_engagement_job(job_id: str, authorization: str | None = Header(default=None)) -> dict:
    """Same as POST /v1/chat/{job_id}/cancel — both kinds share one job store."""
    return _cancel(job_id, authorization)


@app.get("/v1/engagements/{job_id}")
def get_engagement(job_id: str, authorization: str | None = Header(default=None)) -> dict:
    _authenticate(authorization)

    engagement: Engagement | None = container.job_store.get(job_id)
    if engagement is None:
        raise HTTPException(status_code=404, detail=f"No engagement with job_id {job_id}")

    return {
        "job_id": engagement.job_id,
        "ticket_key": engagement.prompt,  # this endpoint's callers only ever sent a ticket_key
        "status": engagement.status.value,
        "result": engagement.result,
        "diagrams": extract_diagrams(engagement.result),
        "error": engagement.error,
        "cancel_reason": engagement.cancel_reason,
        "queue": _queue(engagement),
        "progress": _progress(engagement.job_id, is_terminal(engagement.status)),
    }


def _history_summary(engagement: Engagement, review=None, verdict: str | None = None) -> dict:
    return {
        "job_id": engagement.job_id,
        "conversation_id": engagement.conversation_id,
        "prompt": engagement.prompt,
        "status": engagement.status.value,
        "created_at": engagement.created_at.isoformat(),
        "review_outcome": review.outcome.value if review else UNREVIEWED,
        "verdict": verdict,
        "reviewable": is_reviewable(engagement.status, verdict),
    }


def _review_dict(review: VerdictReview) -> dict:
    return {
        "job_id": review.job_id,
        "outcome": review.outcome.value,
        "reviewer": review.reviewer_subject,
        "reviewed_at": review.reviewed_at.isoformat(),
        "reason": review.reason,
        "superseded_by_job_id": review.superseded_by_job_id,
    }


@app.post("/v1/engagements/{job_id}/review", status_code=201)
def post_verdict_review(
    job_id: str, body: VerdictReviewRequest, authorization: str | None = Header(default=None)
) -> dict:
    """Record whether this engagement's verdict held up (OPEN_ITEMS.md #47). Human-only by
    construction: this authenticated route is the sole write path — no CLI command or MCP tool
    writes a review, so Scotri cannot grade its own work. Append-only; the latest review is current.
    Allowed for the engagement's owner, or a principal holding a VERDICT_REVIEWER_ROLES role."""
    principal = _authenticate(authorization)
    engagement = container.engagement_history.get(job_id)
    if engagement is None:
        raise HTTPException(status_code=404, detail=f"No engagement {job_id} in durable history")
    require_owner_or_role(principal, engagement.principal.subject, VERDICT_REVIEWER_ROLES,
                          resource=f"review of engagement {job_id}")
    if engagement.status not in REVIEWABLE_STATUSES:
        raise HTTPException(
            status_code=409,
            detail=f"engagement is '{engagement.status.value}' — only a succeeded run has a verdict to review",
        )
    verdict = container.engagement_history.learning_verdicts([job_id]).get(job_id)
    if not is_reviewable(engagement.status, verdict):
        raise HTTPException(
            status_code=409,
            detail="this run reached no reviewable conclusion (a bug verdict or a story's technical "
                   "design) — there is nothing to uphold or overturn",
        )
    try:
        outcome = ReviewOutcome(body.outcome)
    except ValueError:
        raise HTTPException(
            status_code=422,
            detail=f"outcome must be one of: {', '.join(o.value for o in ReviewOutcome)}",
        ) from None
    if body.superseded_by_job_id and container.engagement_history.get(body.superseded_by_job_id) is None:
        raise HTTPException(status_code=422, detail=f"superseded_by_job_id {body.superseded_by_job_id} not found")
    try:
        reason = validate_review(job_id, outcome, body.reason, body.superseded_by_job_id)
    except PolicyError as e:
        raise HTTPException(status_code=422, detail=e.user_message) from None
    review = VerdictReview(
        job_id=job_id, outcome=outcome, reviewer_subject=principal.subject,
        reviewed_at=datetime.now(timezone.utc), reason=reason,
        superseded_by_job_id=body.superseded_by_job_id,
    )
    container.engagement_history.add_review(review)
    logger.info("verdict review recorded job_id=%s outcome=%s reviewer=%s",
                job_id, outcome.value, principal.subject)
    return {"ok": True, "review": _review_dict(review)}


@app.get("/v1/engagements/{job_id}/review")
def get_verdict_reviews(job_id: str, authorization: str | None = Header(default=None)) -> dict:
    """Current review outcome plus the full append-only review trail for one engagement. Readable by
    the owner, or by a HISTORY_ADMIN_ROLES / VERDICT_REVIEWER_ROLES holder."""
    principal = _authenticate(authorization)
    engagement = container.engagement_history.get(job_id)
    if engagement is None:
        raise HTTPException(status_code=404, detail=f"No engagement {job_id} in durable history")
    require_owner_or_role(principal, engagement.principal.subject,
                          HISTORY_ADMIN_ROLES | VERDICT_REVIEWER_ROLES,
                          resource=f"reviews of engagement {job_id}")
    trail = container.engagement_history.list_reviews(job_id)
    verdict = container.engagement_history.learning_verdicts([job_id]).get(job_id)
    return {
        "job_id": job_id,
        "verdict": verdict,
        "reviewable": is_reviewable(engagement.status, verdict),
        "current_outcome": trail[-1].outcome.value if trail else UNREVIEWED,
        "reviews": [_review_dict(r) for r in trail],
    }


@app.get("/v1/history")
def get_history(limit: int = 50, authorization: str | None = Header(default=None)) -> dict:
    """The caller's OWN durable engagement history (OPEN_ITEMS.md #19), most recent first — filtered
    server-side by the validated principal's subject, never by anything the frontend sends. Survives
    the 24h job-store TTL because it comes from the durable history store, not Redis."""
    principal = _authenticate(authorization)
    limit = max(1, min(limit, 200))
    items = container.engagement_history.list_for_subject(principal.subject, limit)
    ids = [e.job_id for e in items]
    reviews = container.engagement_history.latest_reviews(ids)
    verdicts = container.engagement_history.learning_verdicts(ids)
    return {"subject": principal.subject, "count": len(items),
            "engagements": [_history_summary(e, reviews.get(e.job_id), verdicts.get(e.job_id))
                            for e in items]}


@app.get("/v1/learnings")
def list_learnings(scope: str = "mine", q: str = "", verdict: str | None = None, limit: int = 50,
                   authorization: str | None = Header(default=None)) -> dict:
    """Browse stored learnings (OPEN_ITEMS.md #60(d)). scope=mine (default): from runs you started.
    scope=team: everyone's, only with a LEARNINGS_READER_ROLES role (403 otherwise; empty by default).
    `q` ranks by relevance (BM25, as the agent's `retrieve` does), else newest first; `verdict`
    filters (confirmed_bug, not_a_bug, inconclusive, story, question)."""
    principal = _authenticate(authorization)
    items = browse_learnings(container.engagement_history, principal, LEARNINGS_READER_ROLES,
                             scope=scope, query=q, verdict=verdict, limit=limit)
    return {"scope": scope, "count": len(items), "learnings": items}


@app.get("/v1/learnings/{job_id}")
def get_one_learning(job_id: str, authorization: str | None = Header(default=None)) -> dict:
    """One learning, by the job that recorded it: its owner, or a LEARNINGS_READER_ROLES holder."""
    principal = _authenticate(authorization)
    return get_learning(container.engagement_history, principal, LEARNINGS_READER_ROLES, job_id)


@app.post("/v1/conversations/adopt")
def adopt_conversation(body: AdoptConversationRequest, authorization: str | None = Header(default=None)) -> dict:
    """Group your own earlier jobs under one server conversation (OPEN_ITEMS.md #25): the one-time
    move of threads that predate server-side conversations out of a browser's localStorage. Never
    moves a job that is already in a conversation; 403 if any job or the conversation_id is someone
    else's. `conversation_id` in the response is null when none of the jobs could be grouped."""
    principal = _authenticate(authorization)
    result = adopt_into_conversation(container.engagement_history, principal, body.job_ids, body.conversation_id)
    logger.info("conversation adopt by %s: %d adopted into %s, %d unknown, %d in another",
                principal.subject, len(result["adopted"]), result["conversation_id"],
                len(result["unknown"]), len(result["in_other"]))
    return result


@app.get("/v1/conversations/{conversation_id}")
def get_conversation(conversation_id: str, authorization: str | None = Header(default=None)) -> dict:
    """Every turn of one conversation, oldest first (OPEN_ITEMS.md #25) — a shareable, device-
    independent view that no longer lives only in the browser's localStorage. Readable by the
    conversation's owner, or by a principal holding a role in HISTORY_ADMIN_ROLES (owner-only by
    default). Full result text is included, so this is owner/role-gated, not open like GET
    /v1/chat/{job_id}."""
    principal = _authenticate(authorization)
    turns = container.engagement_history.get_conversation(conversation_id)
    if not turns:
        raise HTTPException(status_code=404, detail=f"No conversation {conversation_id}")
    owner = turns[0].principal.subject
    if owner != principal.subject and not (set(principal.roles) & HISTORY_ADMIN_ROLES):
        raise HTTPException(status_code=403, detail="Not your conversation")
    ids = [t.job_id for t in turns]
    reviews = container.engagement_history.latest_reviews(ids)
    verdicts = container.engagement_history.learning_verdicts(ids)
    return {
        "conversation_id": conversation_id,
        "owner": owner,
        "turn_count": len(turns),
        "turns": [
            {**_history_summary(t, reviews.get(t.job_id), verdicts.get(t.job_id)),
             "result": t.result, "diagrams": extract_diagrams(t.result), "error": t.error}
            for t in turns
        ],
    }


# --- Shared files and uploads (OPEN_ITEMS.md #6, domain/shared_files.py) ---------------------------


def _require_file_access(principal, job_id: str) -> None:
    """Owner, or a HISTORY_ADMIN_ROLES / FILE_READER_ROLES holder. Stricter than GET /v1/chat/{id}
    (any caller): a deliverable can quote query results, like a conversation's full text."""
    engagement = container.job_store.get(job_id) or container.engagement_history.get(job_id)
    if engagement is None:
        raise HTTPException(status_code=404, detail=f"No engagement with job_id {job_id}")
    require_owner_or_role(principal, engagement.principal.subject, HISTORY_ADMIN_ROLES | FILE_READER_ROLES,
                          resource=f"files of engagement {job_id}")


def _file_dict(f: SharedFile) -> dict:
    return {
        "job_id": f.job_id,
        "path": f.path,
        "name": f.path.rsplit("/", 1)[-1],
        "size": f.size,
        "content_type": f.content_type,
        "created_at": f.created_at.isoformat(),
        "url": f"/v1/engagements/{f.job_id}/files/{quote(f.path)}",
    }


def _list_files(job_id: str, authorization: str | None) -> dict:
    principal = _authenticate(authorization)
    _require_file_access(principal, job_id)
    files = list_engagement_files(container.artifact_store, job_id)
    return {"job_id": job_id, "count": len(files), "files": [_file_dict(f) for f in files]}


def _download_file(job_id: str, path: str, download: bool, authorization: str | None) -> Response:
    principal = _authenticate(authorization)
    _require_file_access(principal, job_id)
    data, shared = read_engagement_file(container.artifact_store, job_id, path)  # NotFoundError -> 404
    media_type = shared.content_type
    if media_type.startswith("text/"):
        media_type += "; charset=utf-8"
    name = path.rsplit("/", 1)[-1]  # already restricted to a safe character set by read_engagement_file
    # Active content (HTML/SVG/XML) and unknown binaries are never rendered inline.
    download = download or is_active_content(media_type) or not media_type.startswith(
        ("text/", "image/", "application/pdf"))
    return Response(
        content=data,
        media_type=media_type,
        headers={
            "Content-Disposition": f'{"attachment" if download else "inline"}; filename="{name}"',
            "X-Content-Type-Options": "nosniff",
        },
    )


@app.get("/v1/engagements/{job_id}/files")
def get_engagement_files(job_id: str, authorization: str | None = Header(default=None)) -> dict:
    """The markdown deliverables this run wrote (intake, verdict, impact analysis, plan, test
    results, handoff, proposal patches), each as it stood when the run ended, secret-redacted."""
    return _list_files(job_id, authorization)


@app.get("/v1/chat/{job_id}/files")
def get_chat_files(job_id: str, authorization: str | None = Header(default=None)) -> dict:
    """Same as GET /v1/engagements/{job_id}/files — both kinds share one job store."""
    return _list_files(job_id, authorization)


@app.get("/v1/engagements/{job_id}/files/{path:path}")
def download_engagement_file(
    job_id: str, path: str, download: bool = False, authorization: str | None = Header(default=None)
) -> Response:
    """One file's content. `?download=true` asks the browser to save it instead of showing it."""
    return _download_file(job_id, path, download, authorization)


@app.get("/v1/chat/{job_id}/files/{path:path}")
def download_chat_file(
    job_id: str, path: str, download: bool = False, authorization: str | None = Header(default=None)
) -> Response:
    return _download_file(job_id, path, download, authorization)


@app.get("/v1/conversations/{conversation_id}/files")
def get_conversation_files(conversation_id: str, authorization: str | None = Header(default=None)) -> dict:
    """Every turn's files in one list, oldest turn first. A later turn's copy of the same path is
    the newer version; both are kept, each under its own job_id."""
    principal = _authenticate(authorization)
    turns = container.engagement_history.get_conversation(conversation_id)
    if not turns:
        raise HTTPException(status_code=404, detail=f"No conversation {conversation_id}")
    require_owner_or_role(principal, turns[0].principal.subject, HISTORY_ADMIN_ROLES | FILE_READER_ROLES,
                          resource=f"files of conversation {conversation_id}")
    files = [f for t in turns for f in list_engagement_files(container.artifact_store, t.job_id)]
    return {"conversation_id": conversation_id, "count": len(files), "files": [_file_dict(f) for f in files]}


@app.post("/v1/uploads", status_code=201)
def upload_file(file: UploadFile = File(...), authorization: str | None = Header(default=None)) -> dict:
    """Store one file (multipart field `file`) for attaching to a prompt: send the returned
    upload_id in `attachment_ids` on POST /v1/chat or /v1/engagements. Text files are secret-redacted
    before they are stored. Only the uploader can attach it. An upload is Scotri's own storage, not a
    write to any product repo or Jira."""
    principal = _authenticate(authorization)
    policy = container.upload_policy
    data = file.file.read(policy.max_bytes + 1)
    if len(data) > policy.max_bytes:
        raise HTTPException(status_code=413, detail=f"File too large; the limit is {policy.max_bytes:,} bytes")
    try:
        upload = store_upload(container.artifact_store, principal, file.filename or "upload", data, policy)
    except PolicyError as e:
        raise HTTPException(status_code=422, detail=e.user_message) from None
    logger.info("upload %s stored for %s (%s, %d bytes, redacted=%s)",
                upload.upload_id, principal.subject, upload.filename, upload.size, upload.redacted)
    return {
        "upload_id": upload.upload_id,
        "filename": upload.filename,
        "size": upload.size,
        "content_type": upload.content_type,
        "created_at": upload.created_at.isoformat(),
        "redacted": upload.redacted,
    }


# --- Admin panel (OPEN_ITEMS.md #43, app/admin_routes.py) ------------------------------------------
# The lambdas look `container` and `_authenticate` up per request, so a test that monkeypatches either
# on this module reaches the admin routes too.
admin_router = build_admin_router(lambda: container, lambda authorization: _authenticate(authorization))
app.include_router(admin_router)

PLATFORM_MAP_SHELL = SERVICES_ROOT / "app" / "static" / "platform-map.html"
_PAGE_HEADERS = {"X-Content-Type-Options": "nosniff", "Cache-Control": "no-store", "X-Frame-Options": "DENY",
                 "Referrer-Policy": "no-referrer"}


@app.get("/platform-map", include_in_schema=False)
def platform_map_page() -> Response:
    """The onboarding platform map (tools/platform_map). Like /admin, an unauthenticated shell with no
    data in it: it asks for a bearer token, fetches /v1/platform-map and renders what comes back."""
    if not PLATFORM_MAP_SHELL.is_file():
        raise HTTPException(status_code=404, detail="Platform map page is not deployed")
    return Response(content=_branded(PLATFORM_MAP_SHELL), media_type="text/html; charset=utf-8", headers=_PAGE_HEADERS)


@app.get("/v1/platform-map")
def platform_map(authorization: str | None = Header(default=None)) -> Response:
    """The generated platform map for any authenticated caller. It names internal workspaces,
    clusters, subscriptions and tenants (no data rows, no credentials), hence the token. Rebuilt at
    the end of every mirror sync (Container.after_mirror_sync)."""
    _authenticate(authorization)
    path = container.path_setting("PLATFORM_MAP_PATH", SERVICES_ROOT / "data" / "platform-map.html")
    if not path.is_file():
        raise HTTPException(status_code=404, detail="The platform map hasn't been built yet. It is rebuilt after "
                            "each mirror sync, or run tools/platform_map/gen_platform_map.py.")
    built = datetime.fromtimestamp(path.stat().st_mtime, timezone.utc).isoformat(timespec="seconds")
    return Response(content=path.read_bytes(), media_type="text/html; charset=utf-8",
                    headers={**_PAGE_HEADERS, "X-Platform-Map-Built": built})


ADMIN_PAGE = SERVICES_ROOT / "app" / "static" / "admin.html"


@app.get("/admin", include_in_schema=False)
def admin_page() -> Response:
    """The admin panel page. Unauthenticated like /portal: it is a static shell with no data in it.
    It asks for a bearer token and calls /v1/admin/*, where every route checks ADMIN_ROLES itself."""
    if not ADMIN_PAGE.is_file():
        raise HTTPException(status_code=404, detail="Admin page is not deployed")
    return Response(content=_branded(ADMIN_PAGE), media_type="text/html; charset=utf-8",
                    headers={"X-Content-Type-Options": "nosniff", "Cache-Control": "no-store",
                             "X-Frame-Options": "DENY", "Referrer-Policy": "no-referrer"})
