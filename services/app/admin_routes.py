# Copyright (c) 2026 Preyas Kulshrestha. Part of the Scotri engine; MIT License (see LICENSE).
# Keep this notice in copies and derived works, including AI-assisted ones.
"""The admin API (OPEN_ITEMS.md #43): debugging, caller apps, logs, and the admin audit trail.

Mounted by app/api.py under /v1/admin. Every route except /v1/admin/me:
1. authenticates the caller (same `_authenticate` as every other route),
2. requires a role in `ADMIN_ROLES` (empty by default, so nobody: fail closed),
3. appends one line to the admin audit log, whether it was allowed, refused or failed.

The audit line is written before the response leaves. If it cannot be written the request fails,
so there is no admin read without a record of it. The line never holds the payload (no log text,
no step contents, no prompt), only who, what, which target and the outcome.

Read-only apart from the caller-app register, which is Scotri's own metadata (domain/admin.py says
why that keeps the workspace's hard rules intact), and the manual mirror sync, which runs the same
`refresh_registry_once` the sync service's schedule and `sync-run-once` run. That is a human admin
asking the sync service's own code path to run now, not Scotri writing to the mirror: an engagement
has no admin token and its CLI still refuses `sync-run-once` in a batch. Nothing here mints, stores or shows a credential:
"API tokens" are Entra's, and this view only records which client apps present them.

The router takes the container and the authenticator as callables, looked up per request, so tests
that monkeypatch `app.api.container` / `app.api._authenticate` reach these routes too.
"""
from __future__ import annotations

import logging
import threading
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path

from fastapi import APIRouter, Header, HTTPException
from pydantic import BaseModel

from app.status_writer import write_status
from domain.access_policy import has_any_role, require_role
from domain.admin import (
    LOG_SOURCES,
    AdminAuditEntry,
    AuditOutcome,
    CallerApp,
    clamp_lines,
    merge_callers,
    resolve_log_source,
    tail_log,
    validate_caller_app,
)
from domain.engagement_queue import CANCEL_ADMIN, cancel_message, withdraw_waiting
from domain.env_resolution import is_ready_to_sync
from domain.errors import NotFoundError, PolicyError, ScotriError
from domain.governor_policy import RATE_LIMIT_WINDOW_SECONDS, GovernorPolicy
from domain.mirror_freshness import DEFAULT_WARN_AFTER_SECONDS, assess_freshness
from domain.models import Backend, Principal
from domain.sync_policy import SyncInProgressError, build_status_report, refresh_registry_once
from domain.trace_policy import get_job_trace

logger = logging.getLogger("scotri.admin")

ADMIN_ROLES_SETTING = "ADMIN_ROLES"
PROMPT_PREVIEW_CHARS = 200

CONFIG_FLAGS = (
    "SCOTRI_AUTH_MODE",
    "AZURE_AD_TENANT_ID",
    "AZURE_AD_API_AUDIENCE",
    "REDIS_HOST",
    "DATABRICKS_HOST",
    "DATABRICKS_SQL_WAREHOUSE_ID",
    "DATABRICKS_CLIENT_ID",
    "FABRIC_TENANT_ID",
    "FABRIC_SP_CLIENT_ID",
    "JIRA_CLOUD_ID",
    "JIRA_EMAIL",
    "JIRA_WRITE_EMAIL",
    "ADO_PAT_READONLY",
    "ARTIFACT_STORE",
    "MIRROR_ROOT",
    "ENGAGEMENT_HISTORY_DB_PATH",
)
"""Settings whose presence the overview reports as set/unset. Never a value: several are secrets,
and "is it configured" is the debugging question anyway (e.g. #57's empty AZURE_AD_* pair)."""

ROLE_SETTINGS = ("ADMIN_ROLES", "HISTORY_ADMIN_ROLES", "FILE_READER_ROLES", "VERDICT_REVIEWER_ROLES")
"""Role lists are not secrets; the overview shows them so an admin can see who is widened where."""


class MirrorSyncRequest(BaseModel):
    repo_key: str
    """A key from the repo registry, e.g. "databricks" or "adf"."""
    branch: str | None = None
    """One mirrored branch of that key; omitted means every mirrored branch of it."""


class CallerAppRequest(BaseModel):
    name: str
    kind: str
    """interactive | service"""
    owner: str = ""
    notes: str = ""


class ManualSync:
    """The admin panel's view of its own manual sync, and the progress observer it hands the sync
    (domain.sync_policy.SyncProgress). A sync can take minutes, so it runs on a thread and the page
    polls `state()`, which carries one row per repo being synced: phase (queued, fetching, swapping,
    caching, done, failed) and a 0-100 percent for its bar.

    This only stops a double click in one process from starting a second thread. The real guard is
    the Redis sync lock `refresh_registry_once` takes, shared with the sync service and
    `sync-run-once`; a run refused by it is recorded here as a failure naming who holds it."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._state: dict = {"running": False, "started_at": None, "finished_at": None,
                             "started_by": None, "scope": None, "checked": None, "failed": None,
                             "failures": [], "repos": []}
        self.thread: threading.Thread | None = None

    def state(self) -> dict:
        with self._lock:
            return {**self._state, "failures": list(self._state["failures"]),
                    "repos": [dict(r) for r in self._state["repos"]]}

    def _row(self, ref) -> dict | None:
        return next((r for r in self._state["repos"] if (r["repo"], r["branch"]) == (ref.repo, ref.branch)), None)

    # --- SyncProgress -----------------------------------------------------------------------------
    def ref_started(self, ref) -> None:
        with self._lock:
            if row := self._row(ref):
                row.update(phase="fetching", percent=0)

    def ref_progress(self, ref, phase: str, percent: int) -> None:
        with self._lock:
            if row := self._row(ref):
                row.update(phase=phase, percent=max(row["percent"], min(100, int(percent))))

    def ref_finished(self, ref, outcome) -> None:
        with self._lock:
            if row := self._row(ref):
                row.update(phase="done" if outcome.ok else "failed", percent=100, ok=outcome.ok,
                           message=None if outcome.ok else outcome.message)

    def start(self, run: Callable[[], list], started_by: str, scope: str, targets: list[tuple[str, object]],
              on_done: Callable[[list | None, Exception | None], None]) -> bool:
        """Start `run` on a thread unless one is already running. False means it was running.
        `targets` is the (repo_key, ref) list the run will sync, so every bar shows from the start."""
        with self._lock:
            if self._state["running"]:
                return False
            self._state = {
                "running": True, "started_at": datetime.now(timezone.utc).isoformat(), "finished_at": None,
                "started_by": started_by, "scope": scope, "checked": None, "failed": None, "failures": [],
                "repos": [{"repo_key": key, "repo": ref.repo, "branch": ref.branch, "phase": "queued",
                           "percent": 0, "ok": None, "message": None} for key, ref in targets],
            }

        def work() -> None:
            outcomes, error = None, None
            try:
                outcomes = run()
            except Exception as e:  # noqa: BLE001 — recorded on the state and the audit line
                logger.exception("admin: manual mirror sync failed")
                error = e
            failures = [{"repo": o.repo, "branch": o.branch, "category": o.category, "message": o.message}
                        for o in (outcomes or []) if not o.ok]
            if error is not None:
                known = isinstance(error, ScotriError)
                failures.append({"repo": None, "branch": None,
                                 "category": error.category.value if known else "unknown",
                                 "message": error.user_message if known else type(error).__name__})
            with self._lock:
                for row in self._state["repos"]:  # a run that never reached a repo leaves it unsynced
                    if row["phase"] in ("queued", "fetching", "swapping", "caching"):
                        row.update(phase="failed" if error is not None else "skipped", ok=False if error else None,
                                   message=failures[-1]["message"] if error is not None else None)
                self._state.update(running=False, finished_at=datetime.now(timezone.utc).isoformat(),
                                   checked=len(outcomes or []), failed=len(failures), failures=failures)
            try:
                on_done(outcomes, error)
            except Exception:  # noqa: BLE001 — a failed audit append must not kill the thread silently
                logger.exception("admin: could not audit the end of a manual mirror sync")

        self.thread = threading.Thread(target=work, name="admin-mirror-sync", daemon=True)
        self.thread.start()
        return True


def build_admin_router(get_container: Callable[[], object],
                       authenticate: Callable[[str | None], Principal]) -> APIRouter:
    router = APIRouter(prefix="/v1/admin", tags=["admin"])
    manual_sync = ManualSync()
    router.manual_sync = manual_sync  # tests wait on manual_sync.thread

    def admin_roles() -> set[str]:
        return get_container().role_set(ADMIN_ROLES_SETTING)

    def audit(principal: Principal, action: str, target: str | None, outcome: AuditOutcome,
              detail: str | None = None) -> None:
        get_container().admin_audit_log.append(AdminAuditEntry(
            at=datetime.now(timezone.utc), subject=principal.subject,
            display_name=principal.display_name, platform=principal.platform,
            action=action, target=target, outcome=outcome, detail=detail,
        ))

    @contextmanager
    def admin_action(authorization: str | None, action: str, target: str | None = None) -> Iterator[dict]:
        """Authenticate, gate on ADMIN_ROLES, run the body, audit the outcome. The body may set
        `ctx["detail"]` for a short non-payload note on the audit line."""
        principal = authenticate(authorization)
        try:
            require_role(principal, admin_roles(), action=f"admin action '{action}'")
        except PolicyError:
            audit(principal, action, target, AuditOutcome.DENIED)
            logger.warning("admin: denied %s to %s", action, principal.subject)
            raise
        ctx: dict = {"principal": principal, "detail": None}
        try:
            yield ctx
        except HTTPException as e:
            audit(principal, action, target, AuditOutcome.ERROR, f"http {e.status_code}")
            raise
        except ScotriError as e:
            audit(principal, action, target, AuditOutcome.ERROR, e.category.value)
            raise
        except Exception:
            audit(principal, action, target, AuditOutcome.ERROR, "unexpected")
            raise
        audit(principal, action, target, AuditOutcome.OK, ctx["detail"])

    @router.get("/me")
    def admin_me(authorization: str | None = Header(default=None)) -> dict:
        """Who the caller is and whether they hold an admin role, so a frontend can show or hide
        the admin entry point. Not gated and not audited: it returns only the caller's own claims.
        Hiding a link is UX; every admin route still checks the role itself."""
        principal = authenticate(authorization)
        return {
            "subject": principal.subject,
            "display_name": principal.display_name,
            "platform": principal.platform,
            "roles": list(principal.roles),
            "is_admin": has_any_role(principal, admin_roles()),
        }

    @router.get("/overview")
    def admin_overview(authorization: str | None = Header(default=None)) -> dict:
        """One debugging snapshot: Redis, the query governor's in-flight counts, running jobs, mirror
        freshness and maintenance flags, and which settings are configured (names only). Each section
        reports its own error instead of failing the whole page, since "Redis is down" is exactly
        when an admin opens this."""
        with admin_action(authorization, "overview") as ctx:
            c = get_container()
            return {
                "generated_at": datetime.now(timezone.utc).isoformat(),
                "redis": _section(lambda: _redis(c)),
                "governor": _section(lambda: _governor(c, ctx["principal"].subject)),
                "active_jobs": _section(lambda: _active_jobs(c)),
                "queue": _section(lambda: _queue(c)),
                "mirror": _section(lambda: _mirror(c)),
                "process": _section(_process),
                "config": _config(c),
            }

    @router.get("/queue")
    def admin_queue(authorization: str | None = Header(default=None)) -> dict:
        """The engagement admission queue (OPEN_ITEMS.md #32): jobs waiting for a slot, in the order
        they will start, and the jobs holding a slot now, with the limits. Prompt previews only."""
        with admin_action(authorization, "queue"):
            return _queue(get_container())

    @router.post("/queue/{job_id}/cancel")
    def admin_cancel_waiting(job_id: str, authorization: str | None = Header(default=None)) -> dict:
        """Take anyone's job out of the waiting line (decided 2026-10-07, OPEN_ITEMS.md #32/#43(f)).
        Waiting jobs only: it never ran, so nothing is interrupted. A running job is not touched here;
        409 says so. The owner sees cancel_reason `admin` and can send the prompt again."""
        with admin_action(authorization, "cancel_waiting", job_id) as ctx:
            c = get_container()
            cancelled = withdraw_waiting(c.job_store, c.engagement_queue, c.engagement_history, job_id,
                                         CANCEL_ADMIN, cancel_message(CANCEL_ADMIN, c.queue_settings))
            if cancelled is None:
                if c.job_store.get(job_id) is None:
                    raise HTTPException(status_code=404, detail=f"No engagement with job_id {job_id}")
                raise HTTPException(status_code=409, detail="That job is not waiting in the queue")
            ctx["detail"] = f"owner={cancelled.principal.subject}"
            return {"cancelled": _engagement_row(cancelled), "queue": _queue(c)}

    @router.get("/engagements")
    def admin_engagements(limit: int = 50, subject: str | None = None, status: str | None = None,
                          authorization: str | None = Header(default=None)) -> dict:
        """Every caller's recent engagements from durable history, newest first, filterable by
        subject and status, plus whatever is queued or running now. A prompt preview only: the full
        prompt and result stay behind the owner/HISTORY_ADMIN_ROLES conversation route."""
        target = ",".join(f"{k}={v}" for k, v in (("subject", subject), ("status", status)) if v) or None
        with admin_action(authorization, "list_engagements", target):
            c = get_container()
            limit = max(1, min(limit, 500))
            items = c.engagement_history.list_recent(limit, subject=subject or None, status=status or None)
            return {"count": len(items), "engagements": [_engagement_row(e) for e in items]}

    @router.get("/engagements/{job_id}/steps")
    def admin_steps(job_id: str, authorization: str | None = Header(default=None)) -> dict:
        """The steps any caller's job took, for debugging a stuck or failed run. The owner-only
        GET /v1/chat/{id}/steps exists because steps carry tool inputs and outputs; this widening is
        admin-only and every read of it is audited with the job id."""
        with admin_action(authorization, "read_steps", job_id):
            c = get_container()
            trace = get_job_trace(c.job_store, c.trace_reader, job_id, progress_store=c.progress_store)
            return {
                "job_id": job_id,
                "owner": trace.engagement.principal.subject,
                "status": trace.engagement.status.value,
                "steps": [asdict(step) for step in trace.steps],
            }

    @router.get("/logs")
    def admin_log_sources(authorization: str | None = Header(default=None)) -> dict:
        """The log files the log view can open, with size and last-modified time."""
        with admin_action(authorization, "list_logs"):
            logs_dir = get_container().logs_dir
            sources = []
            for name in sorted(LOG_SOURCES):
                path = resolve_log_source(logs_dir, name)
                stat = path.stat() if path.is_file() else None
                sources.append({
                    "source": name, "file": path.name, "exists": stat is not None,
                    "size": stat.st_size if stat else 0,
                    "modified_at": datetime.fromtimestamp(stat.st_mtime, timezone.utc).isoformat() if stat else None,
                })
            return {"sources": sources}

    @router.get("/logs/{source}")
    def admin_log_tail(source: str, lines: int | None = None, contains: str | None = None,
                       authorization: str | None = Header(default=None)) -> dict:
        """The last `lines` lines (default 200, max 2000) of one allowlisted log, optionally only
        lines containing `contains`. Every line is secret-redacted again on the way out. Only the
        current file is read, not its rotated backups."""
        with admin_action(authorization, "read_log", source) as ctx:
            try:
                path = resolve_log_source(get_container().logs_dir, source)
            except PolicyError as e:
                raise HTTPException(status_code=404, detail=e.user_message) from None
            n = clamp_lines(lines)
            out, exists = tail_log(path, n, contains)
            ctx["detail"] = f"lines={n}" + (" filtered" if contains else "")
            return {"source": source, "file": path.name, "exists": exists, "count": len(out), "lines": out}

    @router.get("/audit")
    def admin_audit_tail(limit: int = 100, authorization: str | None = Header(default=None)) -> dict:
        """The most recent admin audit entries, newest first. Reading the audit trail is itself
        audited."""
        with admin_action(authorization, "read_audit"):
            entries = get_container().admin_audit_log.tail(max(1, min(limit, 1000)))
            entries.reverse()
            return {"count": len(entries), "entries": entries}

    @router.get("/apps")
    def admin_list_apps(authorization: str | None = Header(default=None)) -> dict:
        """Registered caller apps merged with what history shows actually calling Scotri, keyed by
        the token's azp/appid. A caller seen but not registered comes back `registered: false`."""
        with admin_action(authorization, "list_apps"):
            c = get_container()
            rows = merge_callers(c.caller_registry.list(), c.engagement_history.platform_activity())
            return {"count": len(rows), "unregistered": sum(1 for r in rows if not r["registered"]),
                    "apps": rows}

    @router.put("/apps/{app_id}")
    def admin_put_app(app_id: str, body: CallerAppRequest,
                      authorization: str | None = Header(default=None)) -> dict:
        """Register or update one caller app. Metadata only; a field that looks like it carries a
        credential is refused."""
        with admin_action(authorization, "put_app", app_id) as ctx:
            c = get_container()
            try:
                kind = validate_caller_app(app_id, body.name, body.kind, body.owner, body.notes)
            except PolicyError as e:
                raise HTTPException(status_code=422, detail=e.user_message) from None
            now = datetime.now(timezone.utc)
            existing = c.caller_registry.get(app_id)
            app = CallerApp(
                app_id=app_id, name=body.name.strip(), kind=kind, owner=body.owner.strip(),
                notes=body.notes.strip(),
                registered_by=existing.registered_by if existing else ctx["principal"].subject,
                registered_at=existing.registered_at if existing else now,
                updated_at=now,
            )
            c.caller_registry.upsert(app)
            ctx["detail"] = "updated" if existing else "created"
            return {"app_id": app.app_id, "name": app.name, "kind": app.kind.value, "owner": app.owner,
                    "notes": app.notes, "registered_by": app.registered_by,
                    "registered_at": app.registered_at.isoformat(), "updated_at": app.updated_at.isoformat(),
                    "created": existing is None}

    @router.delete("/apps/{app_id}")
    def admin_delete_app(app_id: str, authorization: str | None = Header(default=None)) -> dict:
        """Remove one caller app's record. It removes Scotri's note about the app, nothing else:
        the app's tokens stay valid, because Entra issues them, not Scotri."""
        with admin_action(authorization, "delete_app", app_id):
            if not get_container().caller_registry.delete(app_id):
                raise NotFoundError(f"No registered app {app_id}")
            return {"app_id": app_id, "deleted": True}

    @router.get("/mirror/sync")
    def admin_mirror_sync_state(authorization: str | None = Header(default=None)) -> dict:
        """The state of the latest manual mirror sync started from this API process."""
        with admin_action(authorization, "read_mirror_sync"):
            return {**manual_sync.state(), "lock_holder": _lock_holder(get_container())}

    @router.post("/mirror/sync", status_code=202)
    def admin_mirror_sync(body: MirrorSyncRequest | None = None,
                          authorization: str | None = Header(default=None)) -> dict:
        """Refresh mirrored Prod repos now, exactly as the scheduled sync does, and write status.json.
        No body syncs every mirrored repo/branch; `{"repo_key", "branch"?}` syncs just that one (its
        status row is merged in, the others keep theirs). Returns at once with one row per repo to
        poll via GET /v1/admin/mirror/sync. 404 for a key/branch that isn't mirrored; 409 if any sync
        (this panel, the schedule, or `sync-run-once`) holds the shared lock.
        The end of the run is a second audit line, `mirror_sync_finished`."""
        scope = f"{body.repo_key}@{body.branch}" if body and body.branch else (body.repo_key if body else "all")
        with admin_action(authorization, "sync_mirror", None if scope == "all" else scope) as ctx:
            c = get_container()
            principal = ctx["principal"]
            targets = [(key, ref) for key in c.registry.all_repo_keys() for ref in c.registry.get(key)
                       if is_ready_to_sync(ref)
                       and (body is None or (key == body.repo_key and body.branch in (None, ref.branch)))]
            if not targets:
                raise HTTPException(status_code=404, detail=f"No mirrored repo/branch matches {scope}.")
            wanted = {(ref.repo, ref.branch) for _, ref in targets}
            holder = f"admin:{principal.subject}" + ("" if scope == "all" else f" ({scope})")

            def run() -> list:
                outcomes = refresh_registry_once(
                    c.registry, c.mirror_sync, c.maintenance_flag, c.sync_lock, holder=holder,
                    only=None if scope == "all" else (lambda ref: (ref.repo, ref.branch) in wanted),
                    progress=manual_sync, after_swap=getattr(c, "after_mirror_swap", None),
                    after_run=getattr(c, "after_mirror_sync", None))
                # A partial run merges into the current status so the other repos keep their rows.
                previous = None if scope == "all" else c.mirror_reader.read_status()
                write_status(c.mirror_root / "status.json", build_status_report(outcomes, previous))
                return outcomes

            def done(outcomes: list | None, error: Exception | None) -> None:
                failed = sum(1 for o in outcomes or [] if not o.ok)
                outcome = AuditOutcome.ERROR if error is not None or failed else AuditOutcome.OK
                if isinstance(error, SyncInProgressError):
                    detail = "lock held"
                elif error is not None:
                    detail = error.category.value if isinstance(error, ScotriError) else "unexpected"
                else:
                    detail = f"checked={len(outcomes or [])} failed={failed}"
                audit(principal, "mirror_sync_finished", None if scope == "all" else scope, outcome, detail)
                logger.info("admin: manual mirror sync (%s) by %s finished: %s", scope, principal.subject, detail)

            current = c.sync_lock.holder()
            if current is not None:
                raise HTTPException(status_code=409, detail=f"A mirror sync is already running ({current}).")
            if not manual_sync.start(run, principal.subject, scope, targets, done):
                raise HTTPException(status_code=409, detail="A manual mirror sync is already running.")
            ctx["detail"] = "started"
            logger.info("admin: manual mirror sync (%s) started by %s", scope, principal.subject)
            return {**manual_sync.state(), "lock_holder": None}

    return router


def _lock_holder(c) -> str | None:
    """Who holds the shared sync lock, or None. A Redis outage reads as unknown, not as free."""
    try:
        return c.sync_lock.holder()
    except ScotriError:
        return "unknown (Redis unavailable)"


def _section(build: Callable[[], dict]) -> dict:
    try:
        return {"ok": True, **build()}
    except ScotriError as e:
        return {"ok": False, "error": e.user_message}
    except Exception as e:  # noqa: BLE001 — one broken section must not hide the others
        logger.exception("admin overview section failed")
        return {"ok": False, "error": type(e).__name__}


def _redis(c) -> dict:
    c._redis_client.ping()  # noqa: SLF001 — same deliberate exception /health makes
    return {}


def _governor(c, subject: str) -> dict:
    policy = GovernorPolicy()
    backends = []
    for backend in Backend:
        state = c.rate_limiter.peek_state(subject, backend, RATE_LIMIT_WINDOW_SECONDS)
        backends.append({"backend": backend.value, "in_flight": state.concurrency_in_flight,
                         "max_concurrent": policy.max_concurrent_per_backend.get(backend)})
    return {"rate_limit_per_user_per_hour": policy.rate_limit_per_user_per_hour, "backends": backends}


def _active_jobs(c) -> dict:
    jobs = []
    for e in sorted(c.job_store.list_active(), key=lambda e: e.created_at):
        progress = None
        try:
            p = c.progress_store.get(e.job_id)
            progress = p.public_view(datetime.now(timezone.utc)) if p else None
        except Exception:  # noqa: BLE001 — progress is best-effort, the job row still matters
            progress = None
        jobs.append({**_engagement_row(e), "progress": progress})
    return {"count": len(jobs), "jobs": jobs}


def _queue(c) -> dict:
    settings = c.queue_settings
    snapshot = c.engagement_queue.snapshot()
    now = datetime.now(timezone.utc).timestamp()

    def row(job_id: str) -> dict:
        e = c.job_store.get(job_id)
        return _engagement_row(e) if e is not None else {"job_id": job_id, "status": "unknown"}

    waiting = [{"position": i + 1, **row(w.job_id), "waited_seconds": max(0, int(now - w.enqueued_at))}
               for i, w in enumerate(snapshot.waiting)]
    running = [{**row(r.job_id), "lease_seconds_left": max(0, int(r.lease_expires_at - now))}
               for r in snapshot.running]
    return {
        "limits": {"max_running": settings.max_running, "max_waiting_per_user": settings.max_waiting_per_user,
                   "max_wait_seconds": settings.max_wait_seconds, "max_length": settings.max_length},
        "waiting": waiting,
        "running": running,
    }


def git_head(root: Path) -> str | None:
    """The commit `root`'s git checkout is on, read from .git without running git (a worktree's
    `.git` file and packed refs included). SCOTRI_BUILD_COMMIT wins when set, for a container image
    built without .git. None when neither says."""
    import os

    if os.environ.get("SCOTRI_BUILD_COMMIT"):
        return os.environ["SCOTRI_BUILD_COMMIT"].strip()[:40]
    try:
        git = root / ".git"
        if git.is_file():  # a worktree: "gitdir: <path>"
            git = Path(git.read_text(encoding="utf-8").split(":", 1)[1].strip())
        head = (git / "HEAD").read_text(encoding="utf-8").strip()
        if not head.startswith("ref: "):
            return head[:40]
        ref = head[5:]
        common = git
        if (git / "commondir").is_file():
            common = (git / (git / "commondir").read_text(encoding="utf-8").strip()).resolve()
        for base in (git, common):
            if (base / ref).is_file():
                return (base / ref).read_text(encoding="utf-8").strip()[:40]
        for line in (common / "packed-refs").read_text(encoding="utf-8").splitlines():
            if line.endswith(" " + ref):
                return line.split(" ", 1)[0][:40]
    except (OSError, IndexError):
        return None
    return None


_WORKSPACE_ROOT = Path(__file__).resolve().parents[2]
PROCESS_STARTED_AT = datetime.now(timezone.utc).isoformat()
COMMIT_AT_START = git_head(_WORKSPACE_ROOT)
"""What this process is running: the checkout's commit when the module loaded. A process keeps the
code it started with, so a sync started from an old process runs the old post-swap step (seen
2026-10-13: lineage wasn't regenerated). The overview shows it next to the checkout's commit now."""


def _process() -> dict:
    from domain.model_lineage import GENERATOR_VERSION

    now = git_head(_WORKSPACE_ROOT)
    return {"started_at": PROCESS_STARTED_AT, "commit_at_start": COMMIT_AT_START, "commit_now": now,
            "restart_needed": bool(COMMIT_AT_START and now and COMMIT_AT_START != now),
            "lineage_generator_version": GENERATOR_VERSION}


def _lineage_state(c, ref) -> dict:
    """This tree's generated lineage against its current sync, the check ensure_lineage_current uses:
    current, stale (older than the tree's last sync), old_generator, or missing."""
    try:
        stored = c.lineage_generated_store.load(c.tree_label(ref))
        token = c.mirror_reader.manifest_token(ref.repo, ref.branch)
    except Exception as e:  # noqa: BLE001 — one tree must not sink the overview
        return {"state": "unknown", "error": str(e)[:200]}
    if stored is None:
        return {"state": "missing"}
    state = ("old_generator" if not stored.current
             else "stale" if token is not None and stored.manifest_token != token else "current")
    return {"state": state, "generated_at": stored.generated_at}


def _mirror(c) -> dict:
    warn_after = int(c.secrets.get_optional("MIRROR_STALE_WARN_SECONDS", str(DEFAULT_WARN_AFTER_SECONDS)))
    status = c.mirror_reader.read_status()
    now = datetime.now(timezone.utc)
    repos, maintenance_error = [], None
    for key in c.registry.all_repo_keys():
        for ref in c.registry.get(key):
            if not ref.mirrored:
                continue
            fresh = assess_freshness(status, ref.repo, ref.branch, now, warn_after)
            flag = None
            if maintenance_error is None:  # one Redis failure answers for every repo; don't wait on each
                try:
                    maintenance = c.maintenance_flag.status(ref.repo)
                    flag = {"active": maintenance.active, "eta": maintenance.eta_message}
                except ScotriError as e:
                    maintenance_error = e.user_message
            repos.append({"repo_key": key, "repo": ref.repo, "branch": ref.branch,
                          "freshness": fresh.to_dict(), "maintenance": flag,
                          "lineage": _lineage_state(c, ref)})
    return {"last_run_at": (status or {}).get("last_run_at"), "maintenance_error": maintenance_error,
            "repos": repos}


def _config(c) -> dict:
    client = getattr(c, "client_config", None)
    return {
        "client": ({"display_name": client.display_name, "client_name": client.client_name,
                    "components": sorted(client.components), "catalogs": list(client.catalogs)}
                   if client is not None else None),
        "auth_mode": c.secrets.get_optional("SCOTRI_AUTH_MODE", "prod") or "prod",
        "settings": {name: bool(c.secrets.get_optional(name)) for name in CONFIG_FLAGS},
        "roles": {name: sorted(c.role_set(name)) for name in ROLE_SETTINGS},
    }


def _engagement_row(e) -> dict:
    prompt = e.prompt or ""
    return {
        "job_id": e.job_id,
        "conversation_id": e.conversation_id,
        "subject": e.principal.subject,
        "display_name": e.principal.display_name,
        "platform": e.principal.platform,
        "origin": e.origin,
        "status": e.status.value,
        "created_at": e.created_at.isoformat(),
        "prompt_preview": prompt[:PROMPT_PREVIEW_CHARS] + ("…" if len(prompt) > PROMPT_PREVIEW_CHARS else ""),
        "error": e.error,
        "cancel_reason": e.cancel_reason,
    }
