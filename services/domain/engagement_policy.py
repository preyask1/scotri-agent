"""Orchestration for a single Scotri engagement — the sequence, not the mechanics (those live behind
ports.agent_runtime / ports.job_store / ports.interaction_log). Mirrors domain/sync_policy.py's
shape: one function every trigger path calls, so behavior can't quietly diverge between them.

No prompt template here, deliberately: the caller's raw prompt goes straight to the agent runtime.
Scotri decides what to do (which skill applies) by reading it, the same way an interactive Claude
Code session would if you typed that sentence at it. Scope/behavior boundaries come entirely from
Scotri's own agent-instructions.md/Scotri.md hard rules — there is no API-level "stop after X"
wrapper the way the ticket-shaped api-service branch has.
"""
from __future__ import annotations

import logging
import time
import uuid
from datetime import datetime, timezone
from typing import Callable

from domain.engagement import Engagement, EngagementStatus, is_terminal
from domain.engagement_progress import EngagementProgress
from domain.errors import PolicyError
from domain.grounding import check_grounding, correction_prompt, unsupported_note
from domain.learning import Learning, extract_learning
from domain.models import InteractionLogEntry, Principal
from domain.redaction import redact_secrets
from domain.run_context import RUN_TOKEN_ENV, RunContext
from domain.shared_files import attachment_prompt_block, publish_engagement_files, stage_attachments
from ports.agent_runtime import AgentEvent, AgentRuntime
from ports.artifact_store import ArtifactStore
from ports.engagement_history import EngagementHistory
from ports.engagement_queue import EngagementQueue
from ports.run_context import RunContextStore
from ports.interaction_log import InteractionLog
from ports.job_store import JobStore
from ports.progress_store import ProgressStore

logger = logging.getLogger(__name__)

_PROMPT_PREVIEW_CHARS = 200


def resolve_turn(
    job_store: JobStore,
    engagement_history: EngagementHistory | None,
    principal: Principal,
    conversation_id: str | None = None,
    resume_job_id: str | None = None,
) -> tuple[str, str | None]:
    """Which conversation a new turn joins, and which session it resumes (OPEN_ITEMS.md #25). Shared
    by both transports (POST /v1/chat and MCP scotri_start), so they group turns identically.

    The conversation is an explicit `conversation_id` the caller owns, else the one carried by the
    resumed job, else a fresh id. `resume_job_id` resolves to that job's session_id, never trusted as
    a session id directly; a job that isn't found or has no session yet just means the turn runs fresh.
    Both are refused (PolicyError) when they belong to someone else: an explicit conversation_id, so
    a caller can't graft a turn onto another person's thread, and a resume_job_id, since resuming
    another person's session would hand the new turn everything that session saw."""
    prior = None
    if resume_job_id:
        prior = job_store.get(resume_job_id)
        if prior is None and engagement_history is not None:
            prior = engagement_history.get(resume_job_id)
        if prior is not None and prior.principal.subject != principal.subject:
            raise PolicyError("resume_job_id belongs to another user")
    resume_session_id = prior.session_id if prior is not None else None

    if conversation_id:
        existing = engagement_history.get_conversation(conversation_id) if engagement_history else []
        if existing and existing[0].principal.subject != principal.subject:
            raise PolicyError("conversation_id belongs to another user")
        return conversation_id, resume_session_id
    if prior is not None and prior.conversation_id:
        return prior.conversation_id, resume_session_id
    return uuid.uuid4().hex, resume_session_id


ADOPT_MAX_JOBS = 200


def adopt_into_conversation(
    engagement_history: EngagementHistory,
    principal: Principal,
    job_ids: list[str],
    conversation_id: str | None = None,
) -> dict:
    """Group the caller's own earlier jobs under one server conversation (OPEN_ITEMS.md #25). For
    threads started before conversations were tracked server-side, which exist as one thread only in
    the browser that ran them (scotri-ui's localStorage). The UI sends each such thread's job ids
    once; afterwards the thread opens the same way on any device.

    Refused outright (PolicyError) if any job belongs to someone else, or if `conversation_id` is
    another user's. Otherwise each job lands in one of: adopted (now in the conversation), already
    (was in it), unknown (not in durable history: too old, or never recorded) or in_other (already in
    a different conversation; never moved, so an existing thread can't be rewritten)."""
    ids = list(dict.fromkeys(j for j in job_ids if isinstance(j, str) and j))
    if not ids:
        raise PolicyError("job_ids is empty")
    if len(ids) > ADOPT_MAX_JOBS:
        raise PolicyError(f"at most {ADOPT_MAX_JOBS} job_ids per call")
    if conversation_id:
        existing = engagement_history.get_conversation(conversation_id)
        if existing and existing[0].principal.subject != principal.subject:
            raise PolicyError("conversation_id belongs to another user")
    target = conversation_id or uuid.uuid4().hex

    unknown, already, in_other, candidates = [], [], [], []
    for job_id in ids:
        engagement = engagement_history.get(job_id)
        if engagement is None:
            unknown.append(job_id)
        elif engagement.principal.subject != principal.subject:
            raise PolicyError(f"job {job_id} belongs to another user")
        elif engagement.conversation_id == target:
            already.append(job_id)
        elif engagement.conversation_id:
            in_other.append(job_id)
        else:
            candidates.append(job_id)
    adopted = engagement_history.assign_conversation(candidates, target)
    in_other.extend(j for j in candidates if j not in adopted)  # raced into another conversation
    grouped = bool(adopted or already)
    return {"conversation_id": target if grouped else None, "adopted": adopted, "already": already,
            "unknown": unknown, "in_other": in_other}


def start_engagement(
    job_store: JobStore,
    principal: Principal,
    prompt: str,
    resume_session_id: str | None = None,
    origin: str | None = None,
    conversation_id: str | None = None,
    attachment_ids: tuple[str, ...] = (),
) -> str:
    """Create a QUEUED engagement and return its job_id. Does not run anything — app/api.py is
    expected to schedule run_engagement separately (as a background task) right after this.

    `resume_session_id`, when given, makes this a follow-up turn in an existing conversation
    (app/api.py resolves a caller-supplied resume_job_id to the prior turn's session_id before
    calling this) — run_engagement passes it to the agent runtime so this turn shares context
    with the earlier one instead of starting fresh. `origin` is stored on the engagement as-is
    (see Engagement.origin). `attachment_ids` must already be checked as the caller's own uploads
    (domain/shared_files.resolve_uploads); run_engagement stages them for the agent."""
    job_id = uuid.uuid4().hex
    engagement = Engagement(
        job_id=job_id,
        prompt=prompt,
        principal=principal,
        status=EngagementStatus.QUEUED,
        created_at=datetime.now(timezone.utc),
        resume_session_id=resume_session_id,
        origin=origin,
        conversation_id=conversation_id,
        attachment_ids=tuple(attachment_ids),
    )
    job_store.create(engagement)
    return job_id


def cancel_engagement(job_store: JobStore, agent_runtime: AgentRuntime, job_id: str) -> Engagement | None:
    """Stop an engagement on the caller's request. Returns the engagement's record afterwards, or
    None if no such job. Idempotent: an already-terminal job is returned unchanged.

    The CANCELLED record is written FIRST, then the runtime is told to interrupt — so whatever
    happens on the worker side (interrupted mid-run, or it finishes in the same instant),
    run_engagement sees CANCELLED in the store and does not overwrite it with a result. The
    runtime can only interrupt a run in this process; when another process runs the job, its
    app/engagement_runner.py sees the CANCELLED record within seconds and interrupts it there
    (OPEN_ITEMS.md #33). That also
    covers a job that hasn't started yet (run_engagement skips a terminal job) and one orphaned by
    a restart (nothing to interrupt; the record is just corrected)."""
    engagement = job_store.get(job_id)
    if engagement is None:
        return None
    if is_terminal(engagement.status):
        return engagement

    cancelled = engagement.with_status(EngagementStatus.CANCELLED)
    job_store.update(cancelled)
    interrupted = agent_runtime.cancel(job_id)
    logger.info("cancel_engagement: job_id %s cancelled (in-flight run interrupted: %s)", job_id, interrupted)
    return cancelled


def fail_interrupted_engagements(job_store: JobStore, origin: str, progress_store: ProgressStore | None = None,
                                 queue: EngagementQueue | None = None) -> int:
    """Mark every QUEUED/RUNNING engagement started by `origin` as FAILED, returning how many.

    Meant to run once at process start: an engagement's worker lives in the process that started
    it (a FastAPI background task, in app/api.py's case), so anything still active from a previous
    process can never finish — without this it stays "running" forever and a polling frontend
    spins on it indefinitely. Assumes one process per `origin` (a second uvicorn worker would have
    its live jobs failed by a peer's startup); jobs from other origins are left alone.

    With the admission queue (`queue`, OPEN_ITEMS.md #32), two kinds are left alone: a job still
    waiting in line (it never started, so it survives the restart and starts when a slot frees),
    and a run that holds a live slot (any process may run any job now, so it may be running in
    another process; if it isn't, its slot lapses and the queue ticker fails it then).

    Failed, not resumed (OPEN_ITEMS.md #39, decided 2026-10-04): re-running a half-done turn
    unattended could repeat work it can't see it already did, a Jira write included. What is kept is
    the session: when `progress_store` captured one mid-run, it goes on the failed record, so a
    follow-up turn (resume_job_id = this job) continues with what the run had already found."""
    count = 0
    for engagement in job_store.list_active():
        if engagement.origin != origin:
            continue
        if queue is not None and (queue.position(engagement.job_id) is not None
                                  or queue.holds_slot(engagement.job_id)):
            continue
        job_store.update(_interrupted(engagement, progress_store))
        count += 1
    return count


def fail_lapsed_run(job_store: JobStore, job_id: str, progress_store: ProgressStore | None = None) -> bool:
    """Fail a job whose slot lapsed in the admission queue: the process running it died. True if a
    record was changed (an already-finished job is left as it is)."""
    engagement = job_store.get(job_id)
    if engagement is None or is_terminal(engagement.status):
        return False
    job_store.update(_interrupted(engagement, progress_store))
    return True


def _interrupted(engagement: Engagement, progress_store: ProgressStore | None) -> Engagement:
    session_id = _captured_session(progress_store, engagement.job_id)
    error = "Interrupted: the Scotri service restarted while this was running. Send it again to retry"
    error += (" — as a follow-up to this job, it keeps what the run had found so far." if session_id else ".")
    return engagement.with_status(EngagementStatus.FAILED, error=error, session_id=session_id)


def run_engagement(
    job_store: JobStore,
    agent_runtime: AgentRuntime,
    interaction_log: InteractionLog,
    job_id: str,
    workspace_root: str,
    engagement_history: EngagementHistory | None = None,
    prefetch: Callable[[Engagement], str] | None = None,
    run_context_store: RunContextStore | None = None,
    run_context_ttl_seconds: int = 30 * 60,
    artifact_store: ArtifactStore | None = None,
    progress_store: ProgressStore | None = None,
    grounding_evidence: Callable[[str, str, datetime], str | None] | None = None,
    grounding_catalogs: tuple[str, ...] = (),
    release_claims: Callable[[str, str], list[str]] | None = None,
) -> None:
    """Run a previously-QUEUED engagement to completion, updating the job store at each transition
    and writing exactly one interaction-log entry at the end, success or failure. Never raises —
    a failure here must show up as a FAILED engagement + a logged entry, not a crashed background
    task app/api.py has no way to observe.

    When `engagement_history` is given (OPEN_ITEMS.md #19/#25), the final full record (prompt +
    result + status + conversation_id) is also persisted to the durable history store — the interaction
    log stays a truncated audit trail, this is the searchable/resumable record. Optional so existing
    callers/tests that don't pass it keep working unchanged.

    When `prefetch` is given (domain/engagement_prefetch.py), its block is put above the prompt the
    agent sees: the fixed opening steps (Jira fetch, presence claim, mirror state) already done in
    parallel. The stored prompt stays exactly what the caller sent. A pre-fetch failure is logged
    and the run continues without the block; the agent then does those steps itself as before.

    When `run_context_store` is given (OPEN_ITEMS.md #59), a per-run token identifying
    the caller is issued, passed into the agent's environment, and revoked when the run ends, so
    every command the agent runs is attributed to the real caller rather than a guessed --user.

    On a SUCCEEDED run, a ```scotri-learning``` block in the result is extracted by the engine, not
    the agent (OPEN_ITEMS.md #58): stripped from the stored result and, when
    `engagement_history` is given and the block is valid, recorded as a team-wide learning.

    When `artifact_store` is given (OPEN_ITEMS.md #6, domain/shared_files.py), the engagement's
    attachments are staged into the workspace before the run, and the markdown deliverables the
    agent wrote are published to the store afterwards, whatever the outcome, so the API and MCP can
    share them. Attachments that can't be staged fail the run up front: answering without the files
    the caller attached would look like an answer about them.

    When `progress_store` is given (OPEN_ITEMS.md #39, domain/engagement_progress.py), live progress
    (lifecycle phase, tool-call count, last activity) is kept while the run is in flight, and the
    session id is captured as soon as the runtime reports it. That session id also goes on the final
    record when the run ends without one of its own (timeout, cancel, crash), so the turn can still
    be continued as a follow-up.

    When `grounding_evidence` is given (OPEN_ITEMS.md #68, domain/grounding.py), a SUCCEEDED answer's
    checkable claims (large numbers, table names, file names) are looked up in everything the run saw;
    unsupported ones go back to the agent once, in the same session, before the learning is extracted.

    When `release_claims` is given, every presence claim the run registered (the pre-fetch's ticket
    claim and any the agent made through the CLI) is released when the run ends, however it ends: a
    finished run that keeps its claim blocks the next person for the TTL."""
    engagement = job_store.get(job_id)
    if engagement is None:
        logger.error("run_engagement: job_id %s not found — nothing to run", job_id)
        return

    if is_terminal(engagement.status):
        # Cancelled (see cancel_engagement) before it ever got to run — nothing to do but log it.
        _record(interaction_log, engagement, 0, engagement.status.value)
        if engagement_history is not None:
            try:
                engagement_history.record(engagement)
            except Exception:  # noqa: BLE001
                logger.exception("run_engagement: job_id %s history persistence failed (pre-run terminal)", job_id)
        return

    job_store.update(engagement.with_status(EngagementStatus.RUNNING))
    start = time.monotonic()
    progress = _ProgressTracker(progress_store, job_id)

    agent_prompt = engagement.prompt
    if engagement.attachment_ids:
        try:
            if artifact_store is None:
                raise RuntimeError("no file store is configured for this service")
            staged = stage_attachments(artifact_store, engagement.attachment_ids, engagement.principal.subject,
                                       workspace_root, job_id)
            agent_prompt = f"{attachment_prompt_block(staged)}\n\n{agent_prompt}"
        except Exception as exc:  # noqa: BLE001 — must land as a FAILED job, never a crashed task
            logger.exception("run_engagement: job_id %s could not stage its attachments", job_id)
            failed = engagement.with_status(EngagementStatus.FAILED,
                                            error=f"Could not attach the uploaded files: {exc}")
            job_store.update(failed)
            _record(interaction_log, engagement, int((time.monotonic() - start) * 1000), "failed")
            if engagement_history is not None:
                try:
                    engagement_history.record(failed)
                except Exception:  # noqa: BLE001
                    logger.exception("run_engagement: job_id %s history persistence failed", job_id)
            return

    if prefetch is not None:
        prefetch_start = time.monotonic()
        try:
            block = prefetch(engagement)
        except Exception:  # noqa: BLE001 — an optimisation must never fail the engagement
            logger.exception("run_engagement: job_id %s pre-fetch failed; continuing without it", job_id)
            block = ""
        if block:
            agent_prompt = f"{block}\n\n{engagement.prompt}"
        logger.info("run_engagement: job_id %s pre-fetch %s in %.1fs", job_id,
                    "added context" if block else "added nothing", time.monotonic() - prefetch_start)

    run_token: str | None = None
    env: dict[str, str] | None = None
    if run_context_store is not None:
        try:
            run_token = run_context_store.issue(
                RunContext.for_principal(engagement.principal, job_id), run_context_ttl_seconds
            )
            env = {RUN_TOKEN_ENV: run_token}
            agent_prompt = (
                f"[Run context] You are working for {engagement.principal.display_name or engagement.principal.subject} "
                f"(via {engagement.principal.platform or 'unknown'}). Your commands are attributed to them "
                f"automatically through {RUN_TOKEN_ENV}; any --user you pass is ignored for attribution.\n\n"
                + agent_prompt
            )
        except Exception:  # noqa: BLE001 — identity is best-effort; commands then fall back to --user
            logger.exception("run_engagement: job_id %s could not issue a run token", job_id)

    learning: Learning | None = None
    try:
        run_kwargs = {"on_event": progress.on_event} if progress_store is not None else {}
        result = agent_runtime.run(
            agent_prompt,
            cwd=workspace_root,
            resume_session_id=engagement.resume_session_id,
            job_id=job_id,
            env=env,
            **run_kwargs,
        )
        session_id = result.session_id or progress.session_id
        if result.num_turns is not None or result.cost_usd is not None:
            # One line per run, so turn and cost ceilings (#33) can be set from real numbers.
            logger.info("run_engagement: job_id %s usage: turns=%s cost_usd=%s", job_id, result.num_turns,
                        f"{result.cost_usd:.4f}" if result.cost_usd is not None else None)
        # Published before the status turns terminal, so a caller that fetches the files the moment
        # polling reports a finished job finds them there.
        _publish(artifact_store, job_id, workspace_root, result.files_written)
        latest = job_store.get(job_id)
        if result.cancelled or (latest is not None and latest.status == EngagementStatus.CANCELLED):
            # Never let a finished-just-as-it-was-stopped result overwrite the user's cancel.
            job_store.update(engagement.with_status(EngagementStatus.CANCELLED, session_id=session_id))
            outcome = "cancelled"
        elif result.is_error:
            job_store.update(
                engagement.with_status(
                    EngagementStatus.FAILED,
                    result=_safe(result.text, job_id),
                    error=_safe(result.text, job_id),
                    session_id=session_id,
                )
            )
            outcome = "failed"
        elif result.incomplete:
            # A run that stopped early can still return non-empty text and is_error=False, so
            # is_error alone would record it SUCCEEDED and hand a fragment to the caller as though
            # it were the answer. The partial text is kept in `result` — it's often useful — but the
            # status must not claim completion.
            job_store.update(
                engagement.with_status(
                    EngagementStatus.INCOMPLETE,
                    result=_safe(result.text, job_id),
                    error="Stopped before finishing: the run ended early (turn limit, or it died "
                    "mid-execution). Any text on this job is partial work, not a completed answer. "
                    "Send it again to retry.",
                    session_id=session_id,
                )
            )
            outcome = "incomplete"
        else:
            final_text, session_id, cancelled = _grounded(
                result.text, engagement, job_id, session_id, grounding_evidence, grounding_catalogs, agent_runtime,
                workspace_root, env, run_kwargs, artifact_store, job_store)
            if cancelled:
                job_store.update(engagement.with_status(EngagementStatus.CANCELLED, session_id=session_id))
                outcome = "cancelled"
            else:
                extraction = extract_learning(final_text)
                if extraction.fields is not None:
                    learning = Learning(
                        job_id=job_id,
                        created_at=engagement.created_at,
                        source_subject=engagement.principal.subject,
                        source_display=engagement.principal.display_name,
                        **extraction.fields,
                    )
                else:
                    logger.info("run_engagement: job_id %s recorded no learning (%s)", job_id, extraction.problem)
                job_store.update(
                    engagement.with_status(
                        EngagementStatus.SUCCEEDED,
                        result=_safe(extraction.cleaned_text or final_text, job_id),
                        session_id=session_id,
                    )
                )
                outcome = "succeeded"
    except Exception as exc:  # noqa: BLE001 — a setup failure must still land as a FAILED job,
        logger.exception("run_engagement: job_id %s crashed", job_id)  # never a silently-stuck one
        job_store.update(engagement.with_status(EngagementStatus.FAILED, error=_safe(str(exc), job_id),
                                                session_id=progress.session_id))
        outcome = "failed"
    finally:
        if run_token is not None and run_context_store is not None:
            try:
                run_context_store.revoke(run_token)
            except Exception:  # noqa: BLE001 — it expires on its own TTL anyway
                logger.exception("run_engagement: job_id %s could not revoke its run token", job_id)

    _record(interaction_log, engagement, int((time.monotonic() - start) * 1000), outcome)

    if release_claims is not None:
        try:
            released = release_claims(job_id, engagement.principal.subject)
            if released:
                logger.info("run_engagement: job_id %s released claim(s) %s", job_id, released)
        except Exception:  # noqa: BLE001 — an unreleased claim still expires on its TTL
            logger.exception("run_engagement: job_id %s could not release its presence claims", job_id)

    if engagement_history is not None:
        # The durable, searchable, resumable record — the full final engagement, not a preview.
        # A history-store failure must never turn a completed engagement into a crashed task.
        try:
            final = job_store.get(job_id)
            if final is not None:
                engagement_history.record(final)
            if learning is not None:
                engagement_history.record_learning(learning)
        except Exception:  # noqa: BLE001
            logger.exception("run_engagement: job_id %s completed but history persistence failed", job_id)


class _ProgressTracker:
    """Keeps one run's EngagementProgress current as runtime events arrive and writes it through to
    the store. Best-effort throughout: a store failure is logged, never raised into the run."""

    def __init__(self, store: ProgressStore | None, job_id: str) -> None:
        self._store = store
        self._progress = EngagementProgress.start(job_id, datetime.now(timezone.utc))
        self._put()

    @property
    def session_id(self) -> str | None:
        return self._progress.session_id

    def on_event(self, event: AgentEvent) -> None:
        now = datetime.now(timezone.utc)
        if event.kind == "session" and event.session_id:
            self._progress = self._progress.with_session(event.session_id, now)
        elif event.kind == "tool_call":
            self._progress = self._progress.with_tool_call(event.skill if event.tool == "Skill" else None, now)
        else:
            return
        self._put()

    def _put(self) -> None:
        if self._store is None:
            return
        try:
            self._store.put(self._progress)
        except Exception:  # noqa: BLE001
            logger.exception("run_engagement: job_id %s could not record progress", self._progress.job_id)


def _captured_session(progress_store: ProgressStore | None, job_id: str) -> str | None:
    if progress_store is None:
        return None
    try:
        progress = progress_store.get(job_id)
    except Exception:  # noqa: BLE001 — a lost session id only costs the follow-up its context
        logger.exception("could not read progress for job_id %s", job_id)
        return None
    return progress.session_id if progress else None


def _safe(text: str | None, job_id: str) -> str | None:
    """Secret-redact what gets stored as a result or error (OPEN_ITEMS.md #19). The same text is
    served by the poll API from the job store and kept in history, so this is the one write boundary
    for both. CLI output is already redacted, so this mostly catches what the agent wrote itself, or
    a secret quoted from a ticket or file it read with a raw tool."""
    if not text:
        return text
    cleaned = redact_secrets(text)
    if cleaned != text:
        logger.warning("run_engagement: job_id %s result had secret-like text; redacted before storing", job_id)
    return cleaned


def _grounded(text: str, engagement: Engagement, job_id: str, session_id: str | None,
              grounding_evidence: Callable[[str, str, datetime], str | None] | None,
              grounding_catalogs: tuple[str, ...],
              agent_runtime: AgentRuntime, workspace_root: str, env: dict[str, str] | None,
              run_kwargs: dict, artifact_store: ArtifactStore | None,
              job_store: JobStore) -> tuple[str, str | None, bool]:
    """The grounding check (domain/grounding.py) on a finished answer. Returns the answer to store,
    the session id, and whether the job was cancelled during the correction turn.

    Claims found in none of the run's evidence go back to the agent once, resumed into the same
    session, to verify, label as derived, or remove; its reply replaces the answer. If that turn
    can't run, the original answer is kept with a note listing the unsupported values, so nothing
    unverified passes silently. Best-effort: any failure of the check itself keeps the answer as-is."""
    if grounding_evidence is None or not session_id:
        return text, session_id, False
    try:
        evidence = grounding_evidence(session_id, engagement.prompt, engagement.created_at)
        if evidence is None:
            logger.info("run_engagement: job_id %s grounding skipped (no transcript)", job_id)
            return text, session_id, False
        report = check_grounding(text, evidence, grounding_catalogs)
    except Exception:  # noqa: BLE001 — a check that breaks must never fail a finished run
        logger.exception("run_engagement: job_id %s grounding check failed; answer kept as-is", job_id)
        return text, session_id, False
    logger.info("run_engagement: job_id %s grounding: %d claim(s) checked, %d unsupported%s", job_id,
                report.checked, len(report.unsupported),
                f" {report.unsupported}" if report.unsupported else "")
    if report.ok:
        return text, session_id, False

    try:
        fix = agent_runtime.run(correction_prompt(report), cwd=workspace_root, resume_session_id=session_id,
                                job_id=job_id, env=env, **run_kwargs)
    except Exception:  # noqa: BLE001
        logger.exception("run_engagement: job_id %s grounding correction crashed", job_id)
        return text + unsupported_note(report), session_id, False
    _publish(artifact_store, job_id, workspace_root, fix.files_written)
    logger.info("run_engagement: job_id %s grounding correction usage: turns=%s cost_usd=%s", job_id,
                fix.num_turns, f"{fix.cost_usd:.4f}" if fix.cost_usd is not None else None)
    latest = job_store.get(job_id)
    if fix.cancelled or (latest is not None and latest.status == EngagementStatus.CANCELLED):
        return text, fix.session_id or session_id, True
    if fix.is_error or fix.incomplete or not (fix.text or "").strip():
        logger.info("run_engagement: job_id %s grounding correction did not finish; noting the claims", job_id)
        return text + unsupported_note(report), fix.session_id or session_id, False
    logger.info("run_engagement: job_id %s grounding correction replaced the answer", job_id)
    return fix.text, fix.session_id or session_id, False


def _publish(artifact_store: ArtifactStore | None, job_id: str, workspace_root: str, files_written) -> None:
    if artifact_store is None or not files_written:
        return
    try:
        published = publish_engagement_files(artifact_store, job_id, workspace_root, files_written)
        logger.info("run_engagement: job_id %s published %d file(s)", job_id, len(published))
    except Exception:  # noqa: BLE001 — sharing is an extra; it must never fail a finished run
        logger.exception("run_engagement: job_id %s could not publish its files", job_id)


def _record(interaction_log: InteractionLog, engagement: Engagement, duration_ms: int, outcome: str) -> None:
    interaction_log.record(
        InteractionLogEntry(
            timestamp=datetime.now(timezone.utc),
            principal=engagement.principal,
            prompt_preview=engagement.prompt[:_PROMPT_PREVIEW_CHARS],
            job_id=engagement.job_id,
            duration_ms=duration_ms,
            outcome=outcome,
        )
    )
