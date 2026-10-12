"""One Scotri engagement — a single submitted prompt, from whichever frontend, tracked from QUEUED
through a terminal state.

Pure data + pure predicates only. The actual sequencing (call the agent runtime, write the log
entry) lives in domain/engagement_policy.py; the actual storage lives behind ports/job_store.py.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import Enum

from domain.models import Principal


class EngagementStatus(str, Enum):
    QUEUED = "queued"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"
    """Stopped on the caller's request (POST /v1/chat/{job_id}/cancel) — distinct from FAILED so a
    frontend can say "stopped" rather than "something went wrong"."""
    INCOMPLETE = "incomplete"
    """Ran without erroring, but stopped before finishing the task (turn limit, mid-execution death).
    Distinct from both SUCCEEDED and FAILED because it is neither: `result` may hold real partial
    work, so calling it FAILED throws that away, and calling it SUCCEEDED is the false green this
    status exists to prevent — a frontend must not present this as an answer."""


def is_terminal(status: EngagementStatus) -> bool:
    return status in (
        EngagementStatus.SUCCEEDED,
        EngagementStatus.FAILED,
        EngagementStatus.CANCELLED,
        EngagementStatus.INCOMPLETE,
    )


@dataclass(frozen=True)
class Engagement:
    job_id: str
    prompt: str
    principal: Principal
    status: EngagementStatus
    created_at: datetime
    result: str | None = None
    error: str | None = None
    resume_session_id: str | None = None
    """The agent-runtime session this engagement continues, if it's a follow-up turn in an existing
    conversation (see app/api.py's CreateChatRequest.resume_job_id, resolved to a session_id there).
    None for the first turn of a conversation."""
    session_id: str | None = None
    """The agent-runtime session this run itself ended up in, once known (set on completion) — the
    value a *later* follow-up turn would pass back in as resume_session_id."""
    origin: str | None = None
    """Which entry point started this job ("api" for app/api.py; None for anything else, e.g. the
    MCP server, which runs its jobs in its own process). Lets app/api.py's startup sweep fail only
    the jobs whose worker was *its own* previous process, not another process's live ones."""
    conversation_id: str | None = None
    """The server-issued conversation this turn belongs to (OPEN_ITEMS.md #25). Assigned on the first
    turn and propagated to follow-ups (resolved in app/api.py from resume_job_id or an explicit
    conversation_id), so a conversation exists server-side rather than only in the browser's
    localStorage. None for a job from a path that doesn't track conversations."""
    attachment_ids: tuple[str, ...] = ()
    """Uploads the caller attached to this prompt (domain/shared_files.py). The run stages them into
    the workspace for the agent to read. Empty for a prompt with no attachments."""
    cancel_reason: str | None = None
    """Why a CANCELLED job was cancelled, for a frontend to word it and offer to send the prompt
    again (OPEN_ITEMS.md #32): `user` (the caller), `admin` (an admin removed it from the queue),
    `queue_timeout` (it waited too long for a slot) or `busy` (refused at the door: the queue was
    full or the caller already had the most waiting jobs allowed). None for other statuses."""

    def to_json(self) -> str:
        """Round-trips through ports.job_store.JobStore (a string-keyed store, same shape as
        QueryResult's cache round-trip in models.py)."""
        import json
        return json.dumps(
            {
                "job_id": self.job_id,
                "prompt": self.prompt,
                "subject": self.principal.subject,
                "display_name": self.principal.display_name,
                "platform": self.principal.platform,
                "roles": self.principal.roles,
                "status": self.status.value,
                "created_at": self.created_at.isoformat(),
                "result": self.result,
                "error": self.error,
                "resume_session_id": self.resume_session_id,
                "session_id": self.session_id,
                "origin": self.origin,
                "conversation_id": self.conversation_id,
                "attachment_ids": list(self.attachment_ids),
                "cancel_reason": self.cancel_reason,
            }
        )

    @staticmethod
    def from_json(s: str) -> "Engagement":
        import json
        data = json.loads(s)
        return Engagement(
            job_id=data["job_id"],
            prompt=data["prompt"],
            principal=Principal(
                subject=data["subject"],
                display_name=data["display_name"],
                platform=data["platform"],
                roles=data.get("roles", []),
            ),
            status=EngagementStatus(data["status"]),
            created_at=datetime.fromisoformat(data["created_at"]),
            result=data.get("result"),
            error=data.get("error"),
            resume_session_id=data.get("resume_session_id"),
            session_id=data.get("session_id"),
            origin=data.get("origin"),
            conversation_id=data.get("conversation_id"),
            attachment_ids=tuple(data.get("attachment_ids") or ()),
            cancel_reason=data.get("cancel_reason"),
        )

    def with_status(
        self,
        status: EngagementStatus,
        *,
        result: str | None = None,
        error: str | None = None,
        session_id: str | None = None,
        cancel_reason: str | None = None,
    ) -> "Engagement":
        """Frozen dataclass -> every transition returns a new value rather than mutating in place,
        so a JobStore.update caller always has an explicit, complete new record to persist."""
        return Engagement(
            job_id=self.job_id,
            prompt=self.prompt,
            principal=self.principal,
            status=status,
            created_at=self.created_at,
            result=result if result is not None else self.result,
            error=error if error is not None else self.error,
            resume_session_id=self.resume_session_id,
            session_id=session_id if session_id is not None else self.session_id,
            origin=self.origin,
            conversation_id=self.conversation_id,
            attachment_ids=self.attachment_ids,
            cancel_reason=cancel_reason if cancel_reason is not None else self.cancel_reason,
        )
