# Copyright (c) 2026 Preyas Kulshrestha. Part of the Scotri engine; MIT License (see LICENSE).
# Keep this notice in copies and derived works, including AI-assisted ones.
"""Starts engagements on threads, through the shared admission queue (OPEN_ITEMS.md #32). Used by
both transports (app/api.py, app/mcp_server.py), so the queue behaves the same whichever one a job
came in on.

- `submit` admits a newly created job: it starts now if a slot is free and nobody waits, otherwise
  it waits in line (or is refused at the door, ServerBusyError).
- When a run ends, its slot is released and the next waiting job is started by whichever process
  freed the slot. Any process can run any job: everything a run needs is in Redis and the container.
- A ticker thread renews the slots of the runs this process is executing, fails runs whose process
  died (their slot lapsed), expires jobs that waited too long, and starts waiting jobs if slots are
  free (e.g. after a crash freed one).
- The same thread, every few seconds, interrupts any run executing here whose job record says
  CANCELLED (OPEN_ITEMS.md #33). Cancelling writes that record first, from whichever process the
  request reached; only the process running the job can interrupt it, so this is how a cancel
  crosses processes.

The container is looked up through a callable on each use, so tests that swap `app.api.container`
or `app.mcp_server.container` reach the runner too. A container without `engagement_queue` (older
test fakes) runs each job at once, as before the queue existed.
"""
from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable

from domain.engagement import Engagement, EngagementStatus
from domain.engagement_policy import fail_lapsed_run, run_engagement
from domain.engagement_queue import (
    CANCEL_ADMIN,
    CANCEL_USER,
    Admission,
    AdmissionOutcome,
    admit,
    cancel_message,
    expire_waiting,
    withdraw_waiting,
)

logger = logging.getLogger("scotri.runner")


class EngagementRunner:
    def __init__(self, get_container: Callable[[], object], workspace_root: str, name: str,
                 spawn: Callable[[Callable[[], None], str], None] | None = None) -> None:
        """`spawn(fn, name)` starts a run; a daemon thread by default. Tests pass one that runs
        inline or not at all."""
        self._get_container = get_container
        self._spawn = spawn or _spawn_thread
        self._workspace_root = workspace_root
        self._name = name
        self._mine: set[str] = set()
        self._signalled: set[str] = set()
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._ticker: threading.Thread | None = None

    # --- admission ----------------------------------------------------------------------------

    def submit(self, engagement: Engagement) -> Admission:
        """Admit a job created with start_engagement. Raises ServerBusyError when refused."""
        c = self._get_container()
        queue = getattr(c, "engagement_queue", None)
        if queue is None:
            self._launch(engagement.job_id)
            return Admission(AdmissionOutcome.START)
        admission = admit(c.job_store, queue, engagement, c.queue_settings)
        if admission.outcome == AdmissionOutcome.START:
            self._launch(engagement.job_id)
        else:
            logger.info("%s: job %s waits in line at position %s", self._name, engagement.job_id, admission.position)
        return admission

    def withdraw(self, job_id: str, by_admin: bool = False) -> Engagement | None:
        """Cancel a job that is still waiting. None if it wasn't waiting (the caller then cancels it
        the ordinary way, which also covers a running job)."""
        c = self._get_container()
        queue = getattr(c, "engagement_queue", None)
        if queue is None:
            return None
        reason = CANCEL_ADMIN if by_admin else CANCEL_USER
        return withdraw_waiting(c.job_store, queue, c.engagement_history, job_id, reason,
                                cancel_message(reason, c.queue_settings))

    # --- running ------------------------------------------------------------------------------

    def _launch(self, job_id: str) -> None:
        with self._lock:
            self._mine.add(job_id)
        self._spawn(lambda: self._run(job_id), f"engagement-{job_id[:8]}")

    def _run(self, job_id: str) -> None:
        c = self._get_container()
        try:
            run_engagement(
                c.job_store, c.agent_runtime, c.interaction_log, job_id, self._workspace_root,
                c.engagement_history, c.engagement_prefetch(self._workspace_root),
                run_context_store=c.run_context_store,
                run_context_ttl_seconds=c.run_context_ttl_seconds,
                artifact_store=c.artifact_store,
                progress_store=c.progress_store,
                grounding_evidence=getattr(c, "grounding_evidence", None),
                grounding_catalogs=getattr(c, "grounding_catalogs", ()),
                release_claims=getattr(c, "release_run_claims", None),
            )
        finally:
            with self._lock:
                self._mine.discard(job_id)
                self._signalled.discard(job_id)
            queue = getattr(c, "engagement_queue", None)
            if queue is not None:
                try:
                    queue.release(job_id)
                except Exception:  # noqa: BLE001 — the lease lapses on its own; the ticker reaps it
                    logger.exception("%s: could not release the slot of job %s", self._name, job_id)
                self.dispatch()

    def dispatch(self) -> int:
        """Start waiting jobs while slots are free. Returns how many started."""
        c = self._get_container()
        queue = getattr(c, "engagement_queue", None)
        if queue is None:
            return 0
        started = 0
        while True:
            try:
                job_id = queue.claim_next(c.queue_settings)
            except Exception:  # noqa: BLE001 — retried on the next tick
                logger.exception("%s: could not claim the next waiting job", self._name)
                break
            if job_id is None:
                break
            logger.info("%s: starting waiting job %s", self._name, job_id)
            self._launch(job_id)
            started += 1
        return started

    def running_here(self) -> list[str]:
        with self._lock:
            return sorted(self._mine)

    def check_cancelled(self) -> list[str]:
        """Interrupt every run executing here whose job was cancelled, from this process or another.
        Each run is signalled once. Returns the job ids signalled on this pass."""
        c = self._get_container()
        with self._lock:
            candidates = [j for j in self._mine if j not in self._signalled]
        signalled = []
        for job_id in candidates:
            try:
                engagement = c.job_store.get(job_id)
            except Exception:  # noqa: BLE001 — retried on the next pass
                logger.exception("%s: could not read job %s to check for a cancel", self._name, job_id)
                continue
            if engagement is None or engagement.status != EngagementStatus.CANCELLED:
                continue
            with self._lock:
                if job_id not in self._mine:
                    continue  # finished meanwhile
                self._signalled.add(job_id)
            interrupted = c.agent_runtime.cancel(job_id)
            logger.info("%s: job %s was cancelled; run interrupted here: %s", self._name, job_id, interrupted)
            signalled.append(job_id)
        return signalled

    # --- housekeeping -------------------------------------------------------------------------

    def tick(self) -> None:
        """One housekeeping pass. Each step is best-effort: a failure is logged and the rest run."""
        c = self._get_container()
        queue = getattr(c, "engagement_queue", None)
        if queue is None:
            return
        settings = c.queue_settings
        mine = self.running_here()
        try:
            queue.renew(mine, settings)
        except Exception:  # noqa: BLE001
            logger.exception("%s: could not renew slots", self._name)
        try:
            for job_id in queue.reap_expired():
                if job_id in mine:
                    continue  # still running here; only its renewal was late
                if fail_lapsed_run(c.job_store, job_id, c.progress_store):
                    logger.warning("%s: job %s failed: the process running it stopped", self._name, job_id)
        except Exception:  # noqa: BLE001
            logger.exception("%s: could not reap lapsed slots", self._name)
        try:
            for job_id in expire_waiting(c.job_store, queue, c.engagement_history, settings):
                logger.info("%s: job %s expired after waiting too long", self._name, job_id)
        except Exception:  # noqa: BLE001
            logger.exception("%s: could not expire waiting jobs", self._name)
        self.dispatch()

    def start_ticking(self) -> None:
        """Start the housekeeping thread (idempotent)."""
        with self._lock:
            if self._ticker is not None:
                return
            self._ticker = threading.Thread(target=self._tick_loop, daemon=True, name=f"{self._name}-queue-ticker")
        self._ticker.start()

    def stop_ticking(self) -> None:
        self._stop.set()

    def _tick_loop(self) -> None:
        """Cancel checks every `cancel_check_seconds`, the full tick every `tick_seconds`."""
        next_tick = 0.0
        while not self._stop.is_set():
            cancel_every, tick_every = 3, 15
            try:
                settings = self._get_container().queue_settings
                cancel_every, tick_every = settings.cancel_check_seconds, settings.tick_seconds
                self.check_cancelled()
                if time.monotonic() >= next_tick:
                    self.tick()
                    next_tick = time.monotonic() + tick_every
            except Exception:  # noqa: BLE001 — the loop must outlive any one bad pass
                logger.exception("%s: queue tick failed", self._name)
                next_tick = time.monotonic() + tick_every
            self._stop.wait(cancel_every)


def _spawn_thread(fn: Callable[[], None], name: str) -> None:
    threading.Thread(target=fn, daemon=True, name=name).start()
