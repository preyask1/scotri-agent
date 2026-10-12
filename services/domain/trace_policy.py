"""Turning an engagement's stored record + its session transcript into a safe-to-show step list.

The transcript is the agent's raw working: tool inputs and outputs can contain anything a `cat` or a
query returned, including a credential that was never meant to leave the box (a hard rule for this
workspace: never store or echo a secret). So every free-form field is redacted and truncated here,
in the domain, before any entry point (app/api.py, app/cli.py) can hand it to a caller.
"""
from __future__ import annotations

from dataclasses import dataclass, replace

from domain.engagement import Engagement
from domain.errors import NotFoundError
from domain.models import TraceStep
from domain.redaction import redact_secrets  # shared with app/cli.py's output redaction (OPEN_ITEMS.md #34)
from ports.job_store import JobStore
from ports.progress_store import ProgressStore
from ports.trace_reader import TraceReader

MAX_FIELD_CHARS = 500  # per free-form field — a trace is for following the trail, not replaying output
MAX_MESSAGE_CHARS = 1500  # the agent's own narration is worth a bit more room than raw tool output


def _clean(text: str | None, limit: int) -> str | None:
    if text is None:
        return None
    text = redact_secrets(text)
    return text if len(text) <= limit else text[:limit] + f"… [+{len(text) - limit} chars]"


def _sanitize(step: TraceStep) -> TraceStep:
    return replace(
        step,
        input_summary=_clean(step.input_summary, MAX_FIELD_CHARS),
        result_preview=_clean(step.result_preview, MAX_FIELD_CHARS),
        text=_clean(step.text, MAX_MESSAGE_CHARS),
    )


@dataclass(frozen=True)
class JobTrace:
    engagement: Engagement
    steps: list[TraceStep]


def get_job_trace(job_store: JobStore, trace_reader: TraceReader, job_id: str,
                  progress_store: ProgressStore | None = None) -> JobTrace:
    """The steps the agent took for one job. Raises NotFoundError when the job is unknown, or when
    no trace is available for it (never got a session — cancelled/failed before the agent started, or
    its session transcript has since been cleaned up). A running job has a trace once its agent
    session has started, when `progress_store` is given."""
    engagement = job_store.get(job_id)
    if engagement is None:
        raise NotFoundError(f"No engagement with job_id {job_id}")

    # A finished run records its session_id on the job. A run still in flight has it only in its
    # live progress (OPEN_ITEMS.md #39), captured when the run started. A follow-up turn also carries
    # the session it resumed, which is the last fallback.
    progress = progress_store.get(job_id) if progress_store is not None and engagement.session_id is None else None
    session_id = engagement.session_id or (progress.session_id if progress else None) or engagement.resume_session_id
    if session_id is None:
        raise NotFoundError(
            f"No trace for job {job_id}: it has no agent session (status: {engagement.status.value})"
        )

    steps = trace_reader.read_turn(session_id, engagement.prompt, engagement.created_at)
    if steps is None:
        raise NotFoundError(f"No trace for job {job_id}: its session transcript was not found on this host")
    return JobTrace(engagement=engagement, steps=[_sanitize(s) for s in steps])
