"""Port for actually running the Scotri lifecycle through an LLM.

Deliberately vendor-agnostic in shape even though, today, exactly one adapter exists
(adapters/agent_sdk/claude_agent_adapter.py, wrapping the Claude Agent SDK) — this is what lets the
already-stubbed multi-vendor idea (ports/model_registry.py, config/model_registry.yaml,
OPEN_ITEMS.md) slot in later as a second adapter behind this same port, without redesigning
engagement_policy.py or app/api.py.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Protocol


@dataclass(frozen=True)
class AgentEvent:
    """Something the run did while still in flight, reported to `run()`'s `on_event` so the engine
    can keep live progress (domain/engagement_progress.py, OPEN_ITEMS.md #39).

    `kind` is "session" (the run's session is now known: `session_id`) or "tool_call" (`tool` is the
    tool's name; `skill` is the skill name when that tool is Skill). Deliberately no tool input: the
    engine only needs to know that a call happened and which lifecycle skill it started."""
    kind: str
    session_id: str | None = None
    tool: str | None = None
    skill: str | None = None


@dataclass(frozen=True)
class AgentRunResult:
    text: str
    is_error: bool
    session_id: str | None = None
    """The Claude Agent SDK session this run ended up in — None if the run failed before a session
    was established. A caller wanting a follow-up turn to share context passes this back in as
    `resume_session_id` on the next `run()` call (see domain/engagement.py's own session_id /
    resume_session_id fields, which thread this end-to-end from app/api.py's request body)."""
    cancelled: bool = False
    """True when the run was interrupted by `AgentRuntime.cancel()` before it finished — `text` is
    then a placeholder, not an answer, and `session_id` is None (no completed session to resume)."""
    incomplete: bool = False
    """True when the turn ended without running to a natural finish — it hit the turn limit, died
    mid-execution, or otherwise stopped early. `text` may hold partial work, which is exactly why
    this can't be folded into `is_error`: the run looks superficially fine (non-empty text, no
    exception) but the task was not carried to the end, and recording it as SUCCEEDED produces a
    green job whose `result` is a fragment. Callers must treat it as a distinct, non-success
    terminal outcome."""
    num_turns: int | None = None
    """Agent turns the run took, as the SDK reports it at the end (None if it never got there)."""
    cost_usd: float | None = None
    """The run's model cost as the SDK computes it (None if it never got there)."""
    files_written: tuple[str, ...] = ()
    """Every path the agent wrote with a file-writing tool during the run, in order, as the tool was
    given it (absolute or cwd-relative). Reported even for a run that timed out, was cancelled or
    stopped early, since partial deliverables are still worth sharing. domain/shared_files.py
    decides which of these are deliverables to publish."""


class AgentRuntime(Protocol):
    def run(
        self,
        prompt: str,
        cwd: str,
        resume_session_id: str | None = None,
        job_id: str | None = None,
        env: dict[str, str] | None = None,
        on_event: Callable[[AgentEvent], None] | None = None,
    ) -> AgentRunResult:
        """`on_event`, when given, is called with an AgentEvent as the run progresses, on the run's
        own thread. An exception it raises is logged and ignored: progress must never fail a run.

        `env`, when given, is added to the agent process's environment (the per-run identity
        token, domain/run_context.py) — never secrets, which the process already loads itself.

        Run one Scotri engagement to completion and return its final text. Must never raise for
        an LLM-side failure — translate it into AgentRunResult(is_error=True, text=<what happened>)
        so callers get one uniform shape regardless of what went wrong; still raise a
        domain.errors.ScotriError for a genuine setup failure (e.g. missing credential).

        `resume_session_id`, when given, continues an earlier session (same conversation context)
        instead of starting a fresh one — the mechanism a multi-turn frontend chat relies on.

        `job_id`, when given, is the handle `cancel()` uses to find this run while it is in flight."""
        ...

    def cancel(self, job_id: str) -> bool:
        """Interrupt the in-flight run registered under `job_id` in THIS process. Returns True if a
        run was found and told to stop (its `run()` then returns AgentRunResult(cancelled=True)),
        False if none is running here — already finished, not started yet, or owned by another
        process. Safe to call from any thread."""
        ...
