# Copyright (c) 2026 Preyas Kulshrestha. Part of the Scotri engine; MIT License (see LICENSE).
# Keep this notice in copies and derived works, including AI-assisted ones.
"""Atomic-swap orchestration policy for the mirror sync service (agent-instructions.md §"Why this
workspace looks different", fabric-read/adf-databricks-read SKILL.md).

This module decides the *sequence* of a refresh; the actual git/filesystem mechanics live in the
adapters/mirror_sync adapter. Kept separate so the safety property (never expose a half-swapped
tree) is expressed once, in one place, independent of how the fetch/swap is actually implemented.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Protocol

from domain.env_resolution import is_ready_to_sync
from collections.abc import Callable

from domain.errors import ErrorCategory, ScotriError, TransientError
from domain.models import RepoRef
from ports.maintenance_flag import MaintenanceFlag
from ports.mirror_sync import MirrorSyncTarget
from ports.repo_registry import RepoRegistry
from ports.sync_lock import SyncLock

logger = logging.getLogger(__name__)

SYNC_LOCK_TTL_SECONDS = 30 * 60
"""How long the sync lock lasts without renewal. It is renewed after every repo, so this only has to
cover one repo's fetch and swap; if a process dies mid-run, the lock frees itself after this long."""


class SyncInProgressError(TransientError):
    """Another sync run holds the lock. Nothing was fetched or swapped."""

    def __init__(self, holder: str | None) -> None:
        self.holder = holder
        super().__init__(f"A mirror sync is already running ({holder or 'holder unknown'}).")


FETCH_STAGES = {
    "receiving": (2, 70),   # git: Receiving objects
    "resolving": (70, 85),  # git: Resolving deltas
    "checkout": (85, 93),   # git: Updating files / Checking out files
    "indexing": (93, 96),   # the file manifest mirror-find/-grep read
}
"""How one repo's 0-100 progress is split across the fetch. Git reports a percentage per stage; each
stage maps onto its own slice of the bar. The rest (96-100) is the swap, which is ours and quick."""
SWAP_PERCENT = 96
CACHE_PERCENT = 98
"""After the swap, the tree's search cache is rebuilt (OPEN_ITEMS.md #46); shown as its own phase."""


def stage_percent(stage: str, percent: float) -> int:
    """One stage's own 0-100 (as git reports it) as the repo's overall 0-100."""
    lo, hi = FETCH_STAGES[stage]
    return int(lo + (hi - lo) * max(0.0, min(100.0, percent)) / 100)


class SyncProgress(Protocol):
    """Optional observer of a sync run, one call per change, for a UI that shows each repo's bar.
    Never told anything but phase and percent: no git output, so nothing that could carry a PAT."""

    def ref_started(self, ref: RepoRef) -> None: ...

    def ref_progress(self, ref: RepoRef, phase: str, percent: int) -> None: ...

    def ref_finished(self, ref: RepoRef, outcome: "SyncOutcome") -> None: ...


@dataclass(frozen=True)
class SyncOutcome:
    """One ref's result from a sync run — success or failure, always recorded, never just the
    failures. A health-status report needs to know a repo was checked and found fine, not only
    that something went wrong somewhere."""
    repo: str
    branch: str
    ok: bool
    category: str | None = None  # domain.errors.ErrorCategory value, when not ok
    message: str | None = None


def refresh_one(ref: RepoRef, sync_target: MirrorSyncTarget, flag: MaintenanceFlag,
                progress: SyncProgress | None = None,
                after_swap: Callable[[RepoRef], None] | None = None) -> None:
    """Refresh a single mirrored repo/branch: fetch into staging, swap, all under the flag.

    Only mirrored (Prod) refs should ever be passed here — Dev/UAT are never mirrored (see
    domain.env_resolution.is_live_read_required) and calling this on one is a programming error,
    not a runtime condition to handle gracefully.
    """
    if not ref.mirrored:
        raise ValueError(
            f"refresh_one called on a non-mirrored ref ({ref.repo}@{ref.branch}) — "
            f"Dev/UAT are read live, on demand, never mirrored."
        )

    logger.info("sync: fetching %s@%s into staging", ref.repo, ref.branch)
    if progress is None:
        sync_target.fetch_into_staging(ref.repo, ref.branch, ref.org, ref.project)
    else:
        best = 0  # git runs more than one pass (fetch, then reset); the bar never moves backwards

        def on_stage(stage: str, percent: float) -> None:
            nonlocal best
            if stage in FETCH_STAGES and (value := stage_percent(stage, percent)) > best:
                best = value
                progress.ref_progress(ref, "fetching", value)

        progress.ref_progress(ref, "fetching", 0)
        sync_target.fetch_into_staging(ref.repo, ref.branch, ref.org, ref.project, on_progress=on_stage)
        progress.ref_progress(ref, "swapping", SWAP_PERCENT)

    # The flag window is deliberately as short as possible: only around the swap itself, not the
    # (much longer) fetch — see the atomic-swap design in services/README.md.
    flag.set(ref.repo, eta_seconds=15)
    try:
        logger.info("sync: swapping %s@%s into active", ref.repo, ref.branch)
        sync_target.swap_to_active(ref.repo, ref.branch)
    finally:
        flag.clear(ref.repo)

    logger.info("sync: %s@%s refreshed", ref.repo, ref.branch)
    if after_swap is not None:
        # Rebuild what's derived from the tree (the mirror-grep cache), now rather than on the next
        # search. Best-effort: the tree is already live, and a stale cache is rebuilt on demand anyway.
        if progress is not None:
            progress.ref_progress(ref, "caching", CACHE_PERCENT)
        try:
            after_swap(ref)
        except Exception:  # noqa: BLE001
            logger.exception("sync: %s@%s is live, but its post-swap step failed", ref.repo, ref.branch)


def refresh_all(
    refs: list[RepoRef], sync_target: MirrorSyncTarget, flag: MaintenanceFlag,
    after_each: Callable[[], None] | None = None, progress: SyncProgress | None = None,
    after_swap: Callable[[RepoRef], None] | None = None,
) -> list[SyncOutcome]:
    """Refresh every mirrored ref, continuing past a single failure rather than aborting the batch.

    Returns one SyncOutcome per ref — success or failure — so the caller can build a complete
    health-status report, not just a list of what went wrong.
    """
    outcomes: list[SyncOutcome] = []
    for ref in refs:
        if not ref.mirrored:
            continue
        if progress is not None:
            progress.ref_started(ref)
        try:
            refresh_one(ref, sync_target, flag, progress, after_swap)
        except ScotriError as exc:
            logger.error("sync: failed to refresh %s@%s: %s", ref.repo, ref.branch, exc.user_message)
            outcomes.append(
                SyncOutcome(ref.repo, ref.branch, ok=False, category=exc.category.value, message=exc.user_message)
            )
        except Exception as exc:  # noqa: BLE001 — an un-categorized failure must still be recorded,
            logger.exception("sync: failed to refresh %s@%s", ref.repo, ref.branch)  # never abort the batch
            outcomes.append(
                SyncOutcome(ref.repo, ref.branch, ok=False, category=ErrorCategory.UNKNOWN.value, message=str(exc))
            )
        else:
            outcomes.append(SyncOutcome(ref.repo, ref.branch, ok=True))
        if progress is not None:
            progress.ref_finished(ref, outcomes[-1])
        if after_each is not None:
            after_each()
    return outcomes


def refresh_registry_once(
    registry: RepoRegistry, sync_target: MirrorSyncTarget, flag: MaintenanceFlag,
    lock: SyncLock, holder: str, lock_ttl_seconds: int = SYNC_LOCK_TTL_SECONDS,
    only: Callable[[RepoRef], bool] | None = None, progress: SyncProgress | None = None,
    after_swap: Callable[[RepoRef], None] | None = None,
    after_run: Callable[[list[SyncOutcome]], None] | None = None,
) -> list[SyncOutcome]:
    """The single entry point `app/cli.py`'s `sync-run-once`, `app/sync_service.py`'s scheduled loop
    and the admin panel's manual sync all call — so "manual" and "scheduled" are guaranteed to mean
    the same thing, never two copies of the ref-filtering logic that can silently drift apart.

    Holds the shared sync lock for the whole run, so two of those paths can never fetch into the same
    staging tree at once. `holder` names the run for whoever is refused (e.g. "sync-service",
    "cli:sync-run-once", "admin:<subject>"). Raises SyncInProgressError if the lock is held; the lock
    is renewed after each repo and always released, even when a repo fails.

    `only` narrows the run to the refs it accepts (the admin panel's per-repo Sync). It narrows, never
    widens: a ref that isn't ready to sync is still skipped. `progress` observes the run.
    `after_swap` runs after each successful swap (the mirror-grep cache rebuild, OPEN_ITEMS.md #46).
    `after_run` runs once at the end, still under the lock, when at least one ref swapped: for work
    built from every tree at once (the platform map), so a full sync does it once, not per tree.
    Best-effort like `after_swap`: its failure is logged and never fails the sync.
    """
    token = lock.acquire(holder, lock_ttl_seconds)
    if token is None:
        raise SyncInProgressError(lock.holder())

    def renew() -> None:
        if not lock.extend(token, lock_ttl_seconds):
            logger.warning("sync: lock for %s expired mid-run; another run may now start", holder)

    try:
        refs = [
            ref
            for key in registry.all_repo_keys()
            for ref in registry.get(key)
            if is_ready_to_sync(ref) and (only is None or only(ref))
        ]
        outcomes = refresh_all(refs, sync_target, flag, after_each=renew, progress=progress, after_swap=after_swap)
        if after_run is not None and any(o.ok for o in outcomes):
            try:
                after_run(outcomes)
            except Exception:  # noqa: BLE001 — a post-run step never fails the sync
                logger.exception("sync: post-run step failed")
        return outcomes
    finally:
        lock.release(token)


def build_status_report(outcomes: list[SyncOutcome], previous: dict | None = None) -> dict:
    """A JSON-serializable health-status snapshot — written to disk by app/status_writer.py so
    both an interactive Scotri session ("when was this last synced?") and an external monitor can
    check mirror freshness without parsing logs.

    For a partial run (one repo from the admin panel), pass the current status as `previous`: the
    synced rows replace theirs, every other row keeps its own `checked_at` and `ok`, and `last_run_at`
    stays the last full run's, so one fresh repo never makes the rest look fresh. The partial run's
    time goes in `last_partial_run_at`."""
    now = datetime.now(timezone.utc).isoformat()
    rows = [
        {
            "repo": o.repo,
            "branch": o.branch,
            "ok": o.ok,
            "category": o.category,
            "message": o.message,
            "checked_at": now,
        }
        for o in outcomes
    ]
    if not isinstance(previous, dict):
        return {"last_run_at": now, "overall_ok": all(r["ok"] for r in rows), "repos": rows}
    synced = {(r["repo"], r["branch"]) for r in rows}
    kept = [r for r in previous.get("repos", []) if (r.get("repo"), r.get("branch")) not in synced]
    merged = kept + rows
    return {
        "last_run_at": previous.get("last_run_at") or now,
        "last_partial_run_at": now,
        "overall_ok": all(r.get("ok") for r in merged),
        "repos": merged,
    }
