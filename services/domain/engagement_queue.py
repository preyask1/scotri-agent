"""The engagement admission queue (OPEN_ITEMS.md #32): at most N engagements run at once, the rest
wait in one FIFO queue shared by every process (API and MCP), and a waiting job can be cancelled by
its owner or an admin, or expires after a while.

Pure rules and the transitions that touch the job store. The queue itself lives behind
ports/engagement_queue.py (Redis); starting threads lives in app/engagement_runner.py.

Why whole engagements and not individual queries: a run that has started never stalls halfway
waiting for a slot, and a person waiting sees a position in line instead of a run that is slow for
no visible reason. The query governor still limits individual backend calls inside a run.

Decided with the user 2026-10-07: N=3, at most 2 waiting jobs per user, a waiting job expires after
30 minutes, the queue holds at most 20, admins may cancel anyone's waiting job. Each is a setting.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from enum import Enum

from domain.engagement import Engagement, EngagementStatus, is_terminal
from domain.errors import ErrorCategory, ScotriError
from ports.engagement_history import EngagementHistory
from ports.engagement_queue import EngagementQueue
from ports.job_store import JobStore

logger = logging.getLogger(__name__)

CANCEL_USER = "user"
CANCEL_ADMIN = "admin"
CANCEL_QUEUE_TIMEOUT = "queue_timeout"
CANCEL_BUSY = "busy"


@dataclass(frozen=True)
class QueueSettings:
    max_running: int = 3
    max_waiting_per_user: int = 2
    max_wait_seconds: int = 30 * 60
    max_length: int = 20
    lease_seconds: int = 90
    """How long a running job's slot lasts without being renewed. The process running it renews it
    every `tick_seconds`, so the slot only lapses when that process died, and then the job is failed
    and its slot freed instead of shrinking the limit for good."""
    tick_seconds: int = 15
    cancel_check_seconds: int = 3
    """How often a process looks for cancels of the runs it is executing (OPEN_ITEMS.md #33): a run
    cancelled through the other process (e.g. the web UI's Stop, via the API, on a run the MCP
    process picked up from the queue) is interrupted here within about this long."""

    @staticmethod
    def from_secrets(secrets) -> "QueueSettings":
        def num(name: str, default: int, minimum: int) -> int:
            raw = secrets.get_optional(name, str(default))
            try:
                return max(minimum, int(raw))
            except (TypeError, ValueError):
                logger.warning("engagement queue: %s=%r is not a whole number; using %d", name, raw, default)
                return default

        return QueueSettings(
            max_running=num("MAX_CONCURRENT_ENGAGEMENTS", 3, 1),
            max_waiting_per_user=num("MAX_QUEUED_ENGAGEMENTS_PER_USER", 2, 0),
            max_wait_seconds=num("ENGAGEMENT_QUEUE_MAX_WAIT_SECONDS", 30 * 60, 60),
            max_length=num("ENGAGEMENT_QUEUE_MAX_LENGTH", 20, 0),
        )


class AdmissionOutcome(str, Enum):
    START = "start"
    """A slot was free and nobody was waiting: the job holds a slot now and should start."""
    QUEUED = "queued"
    QUEUE_FULL = "queue_full"
    USER_LIMIT = "user_limit"


@dataclass(frozen=True)
class Admission:
    outcome: AdmissionOutcome
    position: int | None = None
    """1-based place in line when QUEUED."""


class ServerBusyError(ScotriError):
    """The request was refused at the door: the queue is full, or the caller already has the most
    waiting jobs allowed. Nothing ran. Carries the job (cancelled, reason `busy`) so a frontend can
    offer to send the prompt again."""

    category = ErrorCategory.TRANSIENT

    def __init__(self, message: str, job_id: str, reason: str, prompt: str) -> None:
        super().__init__(message)
        self.job_id = job_id
        self.reason = reason
        self.prompt = prompt

    def to_dict(self) -> dict:
        return {"ok": False, "category": self.category.value, "message": self.user_message,
                "reason": self.reason, "job_id": self.job_id, "prompt": self.prompt}


def busy_message(outcome: AdmissionOutcome, settings: QueueSettings) -> str:
    if outcome == AdmissionOutcome.USER_LIMIT:
        return (f"You already have {settings.max_waiting_per_user} requests waiting for a free slot. "
                "Wait for one to start, or cancel one, then send this again.")
    return "Scotri is busy and the queue is full. Nothing was started; send this again in a few minutes."


def admit(job_store: JobStore, queue: EngagementQueue, engagement: Engagement,
          settings: QueueSettings) -> Admission:
    """Admit a freshly created (QUEUED) engagement. START means it holds a slot and the caller
    starts it now. QUEUED means it waits in line. Refused at the door, the job is recorded as
    CANCELLED (reason `busy`) so it doesn't dangle as QUEUED, and ServerBusyError is raised."""
    admission = queue.admit(engagement.job_id, engagement.principal.subject, settings)
    if admission.outcome in (AdmissionOutcome.START, AdmissionOutcome.QUEUED):
        return admission
    message = busy_message(admission.outcome, settings)
    job_store.update(engagement.with_status(EngagementStatus.CANCELLED, error=message, cancel_reason=CANCEL_BUSY))
    raise ServerBusyError(message, engagement.job_id, admission.outcome.value, engagement.prompt)


def withdraw_waiting(job_store: JobStore, queue: EngagementQueue, history: EngagementHistory | None,
                     job_id: str, reason: str, error: str) -> Engagement | None:
    """Take a job out of the waiting line and record it CANCELLED with `reason`. Returns the new
    record, or None when the job wasn't waiting (already started, finished or gone), so the caller
    can fall back to the ordinary cancel. The prompt stays on the record for "send again"."""
    if not queue.remove_waiting(job_id):
        return None
    engagement = job_store.get(job_id)
    if engagement is None or is_terminal(engagement.status):
        return engagement
    cancelled = engagement.with_status(EngagementStatus.CANCELLED, error=error, cancel_reason=reason)
    job_store.update(cancelled)
    if history is not None:
        try:
            history.record(cancelled)
        except Exception:  # noqa: BLE001 — the cancel itself already happened
            logger.exception("withdraw_waiting: job_id %s history persistence failed", job_id)
    return cancelled


def cancel_message(reason: str, settings: QueueSettings) -> str:
    if reason == CANCEL_QUEUE_TIMEOUT:
        return (f"Cancelled: it waited {settings.max_wait_seconds // 60} minutes for a free slot without "
                "starting. Nothing ran. Send the same prompt again to retry.")
    if reason == CANCEL_ADMIN:
        return "Cancelled by an admin while it was waiting for a free slot. Nothing ran. Send it again to retry."
    return "Cancelled while it was waiting for a free slot. Nothing ran."


def expire_waiting(job_store: JobStore, queue: EngagementQueue, history: EngagementHistory | None,
                   settings: QueueSettings) -> list[str]:
    """Cancel every job that has waited longer than `max_wait_seconds` (reason `queue_timeout`)."""
    expired = []
    for job_id in queue.waiting_older_than(settings.max_wait_seconds):
        if withdraw_waiting(job_store, queue, history, job_id, CANCEL_QUEUE_TIMEOUT,
                            cancel_message(CANCEL_QUEUE_TIMEOUT, settings)) is not None:
            expired.append(job_id)
    return expired


def queue_view(queue: EngagementQueue, job_id: str, settings: QueueSettings) -> dict | None:
    """What a poller sees while its job waits: place in line and how full things are. None when the
    job isn't waiting. Best-effort: a queue read failure returns None rather than failing the poll."""
    try:
        position = queue.position(job_id)
        if position is None:
            return None
        snapshot = queue.snapshot()
    except Exception:  # noqa: BLE001
        logger.exception("could not read the queue position of job %s", job_id)
        return None
    return {"position": position, "waiting": len(snapshot.waiting), "running": len(snapshot.running),
            "max_running": settings.max_running}
