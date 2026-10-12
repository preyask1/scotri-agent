"""Thin CLI entry points. This is what a skill-driven tool call actually invokes — via the Bash
tool, e.g. `python -m app.cli databricks-select --user alice --catalog <prod_catalog> --statement "..."`.

Keep this file thin: argument parsing, JSON in/out, and wiring only. Any real logic belongs in
domain/, not here.

Output convention for every data-returning command: one JSON object on stdout.
- Success: `{"ok": true, ...command-specific fields...}`
- Failure: `{"ok": false, "category": "<domain.errors.ErrorCategory value>", "message": "..."}`,
  process exits 1. Never a raw traceback — that's what domain/errors.py's taxonomy is for.
"""
from __future__ import annotations

import argparse
import contextlib
import fnmatch
import io
import json
import logging
import os
import re
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path

import requests

from app.container import build_container
from app.logging_setup import configure_logging
from app.status_writer import write_status
from domain.adf_run_policy import (
    assert_pipeline_names_given,
    assert_trigger_names_given,
    assert_window_within_limit,
)
from domain.databricks_run_policy import assert_limit_within_cap as assert_dbx_limit_within_cap
from domain.databricks_run_policy import assert_window_within_limit as assert_dbx_window_within_limit
from domain.env_resolution import resolve
from domain.errors import ErrorCategory, ScotriError
from domain.governed_call import GovernorRejection, run_governed, run_governed_write
from domain.governor_policy import RATE_LIMIT_WINDOW_SECONDS, GovernorPolicy, decide_concurrency, decide_rate_limit
from domain.jira_attachments import (
    MAX_ATTACHMENT_BYTES,
    find_attachments,
    is_readable,
    prepare_upload,
    require_issue_key,
    save_attachment,
)
from domain.mirror_freshness import DEFAULT_WARN_AFTER_SECONDS, assess_freshness
from domain.mirror_text import (
    MAX_GREP_FILE_BYTES,
    as_text,
    find_paths,
    grep_text,
    is_searchable,
    parse_line_range,
    slice_lines,
)
from domain.models import Backend, Environment, QueryResult
from domain.report_bindings import definition_files, model_ref, rank_entry, report_folders, summarize_report
from domain.table_usage import file_kind, scan_text, summarize as summarize_table_usage, table_pattern
from domain.redaction import redact_secrets
from domain.run_context import RUN_TOKEN_ENV, resolve_user
from domain.presence_policy import format_collision_notice, is_claim_stale
from domain.sync_policy import build_status_report, refresh_registry_once
from domain.trace_policy import get_job_trace

logger = logging.getLogger("scotri.cli")

WORKSPACE_ROOT = Path(__file__).resolve().parents[2]
"""The Scotri workspace root (services/ is one level down). scratch/ and proposals/ live here."""


def _deep_redact(obj):
    """Redact every string value in a nested structure. Applied to the payload BEFORE serialization
    (not to the serialized blob), so json.dumps re-escapes cleanly and a secret abutting an escaped
    quote can never corrupt the JSON."""
    if isinstance(obj, str):
        return redact_secrets(obj)
    if isinstance(obj, dict):
        return {k: _deep_redact(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_deep_redact(v) for v in obj]
    return obj


def _redact_dumps(obj) -> str:
    """Every value the CLI prints to stdout reaches the agent's context (Bash tool result) and, from
    there, any file the agent writes (handoff docs, proposals). This is the choke point where a
    secret embedded in a mirror notebook, a Databricks row, or a Jira field is stripped before it can
    propagate — the code backing the "never store or echo a secret" hard rule (OPEN_ITEMS.md #34)."""
    return json.dumps(_deep_redact(obj))


def _print_error(category: str, message: str, extra: dict | None = None) -> None:
    logger.error("command failed [%s]: %s", category, message)
    payload = {"ok": False, "category": category, "message": message}
    if extra:
        payload.update(extra)
    print(_redact_dumps(payload))


def _governor_extra(e: ScotriError) -> dict:
    """Structured governor fields for a queued/rate-limited rejection (OPEN_ITEMS.md #22), so the
    agent (and, via the result/steps it produces, the frontend) gets a real queue position and an
    honest wait — not just a prose message. Empty for any other error."""
    if isinstance(e, GovernorRejection):
        return {"queue_position": e.queue_position, "wait_seconds": e.wait_seconds}
    return {}


def _categorize_generic_exception(exc: Exception) -> ErrorCategory:
    """Best-effort categorization for exceptions raised by adapters that don't yet raise
    ScotriError themselves (the ADO/Jira REST adapters use `requests` directly)."""
    if isinstance(exc, requests.HTTPError) and exc.response is not None:
        status = exc.response.status_code
        if status in (401, 403):
            return ErrorCategory.CREDENTIAL
        if status == 404:
            return ErrorCategory.NOT_FOUND
        if status == 429 or status >= 500:
            return ErrorCategory.TRANSIENT
    if isinstance(exc, (requests.ConnectionError, requests.Timeout)):
        return ErrorCategory.TRANSIENT
    return ErrorCategory.UNKNOWN


def cmd_sync_run_once(args: argparse.Namespace) -> int:
    container = build_container()
    # A run already holding the sync lock (the schedule, or the admin panel's manual sync) raises
    # SyncInProgressError, which main() prints as a categorized JSON error.
    outcomes = refresh_registry_once(container.registry, container.mirror_sync, container.maintenance_flag,
                                     container.sync_lock, holder="cli:sync-run-once",
                                     after_swap=getattr(container, "rebuild_mirror_text_cache", None))
    write_status(container.mirror_root / "status.json", build_status_report(outcomes))

    failures = [o for o in outcomes if not o.ok]
    if failures:
        for f in failures:
            logger.error("sync: %s@%s failed [%s]: %s", f.repo, f.branch, f.category, f.message)
        return 1
    logger.info("sync run completed with no failures (%d repo/branch checked).", len(outcomes))
    return 0


def cmd_governor_check(args: argparse.Namespace) -> int:
    """Read-only status report — never increments the rate counter or acquires a concurrency slot.

    This intentionally does NOT make the real accept/reject decision `run_governed` makes inside
    `databricks-select`/`dax-execute` — it only reports the state those commands would see. There is
    no side-effecting way to "check" the governor from outside an actual query anymore: a prior
    version of this command incremented the same Redis counters `run_governed` uses, so calling it
    stand-alone silently consumed a real user's quota/concurrency slot for no actual query. Use this
    only to inspect current state; run an actual governed command to test the real accept/reject path.
    """
    container = build_container()
    policy = GovernorPolicy()
    backend = Backend(args.backend)

    state = container.rate_limiter.peek_state(args.user, backend, window_seconds=RATE_LIMIT_WINDOW_SECONDS)
    rate_decision = decide_rate_limit(policy, current_count_this_window=state.rate_count_this_window)
    concurrency_decision = decide_concurrency(policy, backend, current_in_flight=state.concurrency_in_flight)

    print(_redact_dumps({
        "ok": True,
        "rate_count_this_window": state.rate_count_this_window,
        "rate_limit_per_hour": policy.rate_limit_per_user_per_hour,
        "would_be_rate_limited": not rate_decision.allowed,
        "concurrency_in_flight": state.concurrency_in_flight,
        "concurrency_cap": policy.max_concurrent_per_backend[backend],
        "would_be_queued": not concurrency_decision.allowed,
    }))
    return 0


def cmd_artifacts_purge(args: argparse.Namespace) -> int:
    """Ops only (OPEN_ITEMS.md #6(a), domain/retention.py): delete uploads, their staged copies and
    published deliverables older than UPLOAD_RETENTION_DAYS / SHARED_FILE_RETENTION_DAYS. Without
    --confirmed it is a dry run that lists what would go. Both settings empty means nothing expires,
    and it says so rather than deleting anything. Refused inside engagements (domain/tool_scope.py)
    and by batch."""
    from domain.retention import purge_expired

    container = build_container()
    policy = container.retention_policy
    if not policy.enabled:
        print(_redact_dumps({"ok": True, "dry_run": True, "message":
                             "Retention is off: UPLOAD_RETENTION_DAYS and SHARED_FILE_RETENTION_DAYS are both "
                             "empty, so nothing expires."}))
        return 0
    report = purge_expired(container.artifact_store, WORKSPACE_ROOT, policy, dry_run=not args.confirmed)
    print(_redact_dumps({"ok": True, **report.to_dict()}))
    return 0


def cmd_governor_reset(args: argparse.Namespace) -> int:
    """Manual escape hatch for a concurrency slot leaked by a killed/crashed governed call —
    `enter_concurrency_slot`'s TTL (adapters/redis/rate_limiter_adapter.py) is a self-healing
    backstop, not something an operator should have to wait out once `governor-check` has already
    shown a count that can't be real. Gated behind --confirmed like every other command that mutates
    shared state this agent doesn't exclusively own: the operator must already know, from
    governor-check plus their own knowledge of what's actually running, that this is safe."""
    if not _require_confirmed(args):
        return 1
    container = build_container()
    backend = Backend(args.backend)
    previous = container.rate_limiter.reset_concurrency_slot(backend)
    print(_redact_dumps({"ok": True, "backend": backend.value, "was": previous, "now": 0}))
    return 0


def cmd_presence_check(args: argparse.Namespace) -> int:
    """Advisory only (presence-claim SKILL.md) — a collision is a notice, never a hard lock. Without
    --force, a live collision blocks registration (exit 1) so the caller sees the notice and makes
    the call explicitly; --force re-registers anyway, printing the notice first so the override is
    never silent. The owner's own live claim is no collision: it is refreshed, so a person repeating
    a question within the TTL isn't asked whether to proceed against themselves."""
    container = build_container()
    claim = container.presence_store.get_claim(args.entity)
    now = datetime.now(timezone.utc)
    if claim is not None and not is_claim_stale(claim, now) and claim.owner == args.owner:
        container.presence_store.set_claim(args.entity, owner=args.owner)
        _track_run_claim(container, args)
        print(f"claim refreshed: {args.owner} already held {args.entity}")
        return 0
    if claim is not None and not is_claim_stale(claim, now):
        print(format_collision_notice(claim, now))
        if not args.force:
            return 1
        print(f"--force: proceeding anyway, re-registering {args.owner} on {args.entity}")
    container.presence_store.set_claim(args.entity, owner=args.owner)
    _track_run_claim(container, args)
    print(f"claim registered: {args.owner} on {args.entity}")
    return 0


def _track_run_claim(container, args: argparse.Namespace) -> None:
    job_id = getattr(args, "run_job_id", None)
    if job_id:
        try:
            container.presence_store.track_run_claim(job_id, args.entity)
        except Exception:  # noqa: BLE001 — losing the record only means the TTL releases it instead
            _identity_logger.exception("could not record claim %s for job %s", args.entity, job_id)


def cmd_presence_release(args: argparse.Namespace) -> int:
    """Explicitly release a claim when work concludes, rather than waiting out the TTL
    (presence-claim SKILL.md) — a no-op if `--owner` doesn't hold the current claim, so this can't
    be used to clear someone else's."""
    container = build_container()
    claim = container.presence_store.get_claim(args.entity)
    if claim is None:
        print(f"no claim on {args.entity}")
        return 0
    if claim.owner != args.owner:
        print(f"claim on {args.entity} is held by {claim.owner}, not {args.owner} — not released")
        return 1
    container.presence_store.release_claim(args.entity, owner=args.owner)
    print(f"claim released: {args.owner} on {args.entity}")
    return 0


def cmd_presence_heartbeat(args: argparse.Namespace) -> int:
    """Refresh last_heartbeat (and the TTL) on a claim `--owner` still holds, for long-running work
    that shouldn't go stale at 30 minutes (presence-claim SKILL.md). The port's own heartbeat()
    silently no-ops on a claim it doesn't own (adapters/redis/presence_store_adapter.py) — checked
    here first so the caller gets an explicit message instead of a silent no-op."""
    container = build_container()
    claim = container.presence_store.get_claim(args.entity)
    if claim is None:
        print(f"no claim on {args.entity} to heartbeat")
        return 1
    if claim.owner != args.owner:
        print(f"claim on {args.entity} is held by {claim.owner}, not {args.owner} — not refreshed")
        return 1
    container.presence_store.heartbeat(args.entity, owner=args.owner)
    print(f"heartbeat refreshed: {args.owner} on {args.entity}")
    return 0


def cmd_job_trace(args: argparse.Namespace) -> int:
    """Read-only: the steps the agent took for one engagement job (a job_id from /v1/chat, an
    interaction-log line, or the API log), read back from its session transcript on this host.
    Tool inputs/outputs come back truncated and secret-redacted (domain/trace_policy.py)."""
    container = build_container()
    try:
        trace = get_job_trace(container.job_store, container.trace_reader, args.job_id,
                              progress_store=container.progress_store)
    except ScotriError as e:
        _print_error(e.category.value, e.user_message, extra=_governor_extra(e))
        return 1
    eng = trace.engagement
    print(
        json.dumps(
            {
                "ok": True,
                "job_id": eng.job_id,
                "status": eng.status.value,
                "prompt": eng.prompt,
                "created_at": eng.created_at.isoformat(),
                "steps": [asdict(step) for step in trace.steps],
            },
            ensure_ascii=False,
        )
    )
    return 0


def cmd_registry_resolve(args: argparse.Namespace) -> int:
    container = build_container()
    environment = Environment(args.environment) if args.environment else None
    ref = resolve(container.registry, args.repo_key, environment)
    print(
        f"{ref.repo} @ {ref.branch}  (org={ref.org}, project={ref.project}, "
        f"env={ref.environment.value}, mirrored={ref.mirrored}"
        + (f", catalog={ref.catalog}" if ref.catalog else "")
        + (f", status={ref.status}" if ref.status else "")
        + ")"
    )
    return 0


def cmd_databricks_select(args: argparse.Namespace) -> int:
    """databricks-query SKILL.md's actual invocation point. SELECT-only is enforced inside
    adapters/databricks/sdk_adapter.py's execute() (domain.query_guard) — this command does not
    duplicate that check, so it must always go through container.databricks, never around it."""
    container = build_container()
    try:
        result, meta = run_governed(
            user=args.user,
            backend=Backend.DATABRICKS,
            cache_key_input=f"{args.catalog}:{args.statement}",
            rate_limiter=container.rate_limiter,
            cache=container.cache,
            policy=GovernorPolicy(),
            execute=lambda: container.databricks.execute(args.catalog, args.statement),
            serialize=lambda r: r.to_json(),
            deserialize=QueryResult.from_json,
        )
    except ScotriError as e:
        _print_error(e.category.value, e.user_message, extra=_governor_extra(e))
        return 1
    except Exception as e:  # noqa: BLE001 — categorized generically, never a raw traceback to stdout.
        # Safety net matching cmd_mirror_list/cmd_mirror_read/etc.: the databricks-sdk isn't
        # guaranteed to only ever raise ScotriError (confirmed the analogous gap was real for the
        # Fabric adapter, fixed 2026-09-25 — this closes the same class of bug here defensively).
        _print_error(_categorize_generic_exception(e).value, str(e))
        return 1
    print(_redact_dumps({
        "ok": True, "from_cache": meta.from_cache,
        "columns": result.columns, "rows": result.rows,
        "row_count": result.row_count, "source": result.source,
    }))
    return 0


def cmd_dax_execute(args: argparse.Namespace) -> int:
    """dax-debug SKILL.md's actual invocation point — read-only DAX Execute, governed."""
    container = build_container()
    try:
        result, meta = run_governed(
            user=args.user,
            backend=Backend.FABRIC,
            cache_key_input=f"{args.workspace}:{args.model}:{args.dax}:{args.role or ''}",
            rate_limiter=container.rate_limiter,
            cache=container.cache,
            policy=GovernorPolicy(),
            execute=lambda: container.fabric.execute_dax(args.workspace, args.model, args.dax, args.role),
            serialize=lambda r: r.to_json(),
            deserialize=QueryResult.from_json,
        )
    except ScotriError as e:
        _print_error(e.category.value, e.user_message, extra=_governor_extra(e))
        return 1
    except Exception as e:  # noqa: BLE001 — categorized generically, never a raw traceback to stdout.
        _print_error(_categorize_generic_exception(e).value, str(e))
        return 1
    print(_redact_dumps({
        "ok": True, "from_cache": meta.from_cache,
        "columns": result.columns, "rows": result.rows,
        "row_count": result.row_count, "source": result.source,
    }))
    return 0


def _parse_iso8601(value: str) -> datetime:
    """Accepts a trailing 'Z' (Python's fromisoformat handles it natively from 3.11+, which this
    deployment's .venv already is — see services/README.md)."""
    return datetime.fromisoformat(value)


def cmd_pipeline_runs(args: argparse.Namespace) -> int:
    """adf-databricks-read SKILL.md's "ADF pipeline failures" step 1 — find failed runs for named
    pipeline(s) in a window. OPEN_ITEMS.md: decided 2026-09-27. The mandatory --pipeline filter and
    the 14-day window cap are enforced by domain.adf_run_policy before this ever reaches ARM —
    see that module for why (the runbook's "unfiltered query only returns ~100 runs" gotcha)."""
    container = build_container()
    try:
        environment = Environment(args.environment)
        factory = container.adf_factories.get(environment, getattr(args, "factory", None))
        start, end = _parse_iso8601(args.start), _parse_iso8601(args.end)
        assert_pipeline_names_given(args.pipeline)
        assert_window_within_limit(start, end)
        runs, meta = run_governed(
            user=args.user,
            backend=Backend.ADF,
            cache_key_input=f"pipeline-runs:{environment.value}:{factory.factory_name}:{sorted(args.pipeline)}:{args.start}:{args.end}",
            rate_limiter=container.rate_limiter,
            cache=container.cache,
            policy=GovernorPolicy(),
            execute=lambda: container.adf_runs.query_pipeline_runs(factory, args.pipeline, start, end),
            serialize=json.dumps,
            deserialize=json.loads,
        )
    except ScotriError as e:
        _print_error(e.category.value, e.user_message, extra=_governor_extra(e))
        return 1
    except Exception as e:  # noqa: BLE001 — categorized generically, never a raw traceback to stdout.
        _print_error(_categorize_generic_exception(e).value, str(e))
        return 1
    out = {"ok": True, "from_cache": meta.from_cache, "runs": runs, "run_count": len(runs)}
    if not runs:
        # Zero runs is NOT the same as "the pipeline failed" — a disabled/paused/failed-to-fire
        # trigger produces no pipeline runs at all. Surface the distinction from the tool rather than
        # leaving the agent to remember it (OPEN_ITEMS.md #35).
        out["hint"] = (
            "no pipeline runs in this window — this does not mean the pipeline failed. A disabled, "
            "paused, or failed-to-fire trigger produces zero pipeline runs. Check `trigger-runs` for "
            "the trigger(s) that should have started it, and read the trigger's `enabled` state from "
            "the mirror (adf-databricks-read), before concluding the pipeline itself is at fault."
        )
    print(_redact_dumps(out))
    return 0


def cmd_activity_runs(args: argparse.Namespace) -> int:
    """adf-databricks-read SKILL.md's "ADF pipeline failures" step 2 — per-activity error/retry
    detail for one pipeline run (a runId from pipeline-runs). OPEN_ITEMS.md: decided 2026-09-27."""
    container = build_container()
    try:
        environment = Environment(args.environment)
        factory = container.adf_factories.get(environment, getattr(args, "factory", None))
        start, end = _parse_iso8601(args.start), _parse_iso8601(args.end)
        assert_window_within_limit(start, end)
        runs, meta = run_governed(
            user=args.user,
            backend=Backend.ADF,
            cache_key_input=f"activity-runs:{environment.value}:{factory.factory_name}:{args.run_id}:{args.start}:{args.end}",
            rate_limiter=container.rate_limiter,
            cache=container.cache,
            policy=GovernorPolicy(),
            execute=lambda: container.adf_runs.query_activity_runs(factory, args.run_id, start, end),
            serialize=json.dumps,
            deserialize=json.loads,
        )
    except ScotriError as e:
        _print_error(e.category.value, e.user_message, extra=_governor_extra(e))
        return 1
    except Exception as e:  # noqa: BLE001
        _print_error(_categorize_generic_exception(e).value, str(e))
        return 1
    print(_redact_dumps({"ok": True, "from_cache": meta.from_cache, "activity_runs": runs, "run_count": len(runs)}))
    return 0


def cmd_trigger_runs(args: argparse.Namespace) -> int:
    """adf-databricks-read SKILL.md's "ADF pipeline failures" — check whether a trigger actually
    fired (and with what status), the case pipeline-runs can't distinguish from "the pipeline
    didn't fail" (OPEN_ITEMS.md #35). Mandatory --trigger filter + 14-day window cap enforced by
    domain.adf_run_policy. Governed on Backend.ADF, Prod factory only."""
    container = build_container()
    try:
        environment = Environment(args.environment)
        factory = container.adf_factories.get(environment, getattr(args, "factory", None))
        start, end = _parse_iso8601(args.start), _parse_iso8601(args.end)
        assert_trigger_names_given(args.trigger)
        assert_window_within_limit(start, end)
        runs, meta = run_governed(
            user=args.user,
            backend=Backend.ADF,
            cache_key_input=f"trigger-runs:{environment.value}:{factory.factory_name}:{sorted(args.trigger)}:{args.start}:{args.end}",
            rate_limiter=container.rate_limiter,
            cache=container.cache,
            policy=GovernorPolicy(),
            execute=lambda: container.adf_runs.query_trigger_runs(factory, args.trigger, start, end),
            serialize=json.dumps,
            deserialize=json.loads,
        )
    except ScotriError as e:
        _print_error(e.category.value, e.user_message, extra=_governor_extra(e))
        return 1
    except Exception as e:  # noqa: BLE001 — categorized generically, never a raw traceback to stdout.
        _print_error(_categorize_generic_exception(e).value, str(e))
        return 1
    out = {"ok": True, "from_cache": meta.from_cache, "trigger_runs": runs, "run_count": len(runs)}
    if not runs:
        # Empty is two-valued: a disabled trigger and one that simply didn't fire both look like this.
        out["hint"] = (
            "no trigger runs in this window — a trigger that is disabled/paused and one that was "
            "enabled but didn't fire both produce zero runs. Read the trigger's `enabled` state from "
            "the mirror (adf-databricks-read) to tell them apart."
        )
    print(_redact_dumps(out))
    return 0


def cmd_job_runs(args: argparse.Namespace) -> int:
    """adf-databricks-read SKILL.md's "Databricks job/notebook failures" step 1 — recent job runs
    for a window (optionally one --job-id), most recent first. OPEN_ITEMS.md item #30. The mandatory
    capped window and the limit cap are enforced by domain.databricks_run_policy before this reaches
    the Jobs API. Governed on Backend.DATABRICKS, same cap the SELECT path uses."""
    container = build_container()
    try:
        start, end = _parse_iso8601(args.start), _parse_iso8601(args.end)
        assert_dbx_window_within_limit(start, end)
        assert_dbx_limit_within_cap(args.limit)
        runs, meta = run_governed(
            user=args.user,
            backend=Backend.DATABRICKS,
            cache_key_input=f"job-runs:{args.job_id}:{args.start}:{args.end}:{args.limit}",
            rate_limiter=container.rate_limiter,
            cache=container.cache,
            policy=GovernorPolicy(),
            execute=lambda: container.databricks_runs.list_runs(args.job_id, start, end, args.limit),
            serialize=json.dumps,
            deserialize=json.loads,
        )
    except ScotriError as e:
        _print_error(e.category.value, e.user_message, extra=_governor_extra(e))
        return 1
    except Exception as e:  # noqa: BLE001 — categorized generically, never a raw traceback to stdout.
        _print_error(_categorize_generic_exception(e).value, str(e))
        return 1
    print(_redact_dumps({"ok": True, "from_cache": meta.from_cache, "runs": runs, "run_count": len(runs)}))
    return 0


def cmd_job_run_detail(args: argparse.Namespace) -> int:
    """adf-databricks-read SKILL.md's "Databricks job/notebook failures" step 2 — full detail for
    one run (a --run-id from job-runs), and with --with-output the task-level error/stack-trace/
    notebook output (a *task* run_id, per the Jobs API). OPEN_ITEMS.md item #30. Governed on
    Backend.DATABRICKS."""
    container = build_container()
    try:
        if args.with_output:
            payload, meta = run_governed(
                user=args.user,
                backend=Backend.DATABRICKS,
                cache_key_input=f"job-run-output:{args.run_id}",
                rate_limiter=container.rate_limiter,
                cache=container.cache,
                policy=GovernorPolicy(),
                execute=lambda: container.databricks_runs.get_run_output(args.run_id),
                serialize=json.dumps,
                deserialize=json.loads,
            )
            print(_redact_dumps({"ok": True, "from_cache": meta.from_cache, "run_output": payload}))
            return 0
        run, meta = run_governed(
            user=args.user,
            backend=Backend.DATABRICKS,
            cache_key_input=f"job-run:{args.run_id}",
            rate_limiter=container.rate_limiter,
            cache=container.cache,
            policy=GovernorPolicy(),
            execute=lambda: container.databricks_runs.get_run(args.run_id),
            serialize=json.dumps,
            deserialize=json.loads,
        )
    except ScotriError as e:
        _print_error(e.category.value, e.user_message, extra=_governor_extra(e))
        return 1
    except Exception as e:  # noqa: BLE001
        _print_error(_categorize_generic_exception(e).value, str(e))
        return 1
    print(_redact_dumps({"ok": True, "from_cache": meta.from_cache, "run": run}))
    return 0


def cmd_lineage_rebuild(args: argparse.Namespace) -> int:
    """Rebuild the SQLite lineage index from docs/ai/client/lineage-graph.yaml (OPEN_ITEMS.md item #37).
    Local/offline — no governor, no credential (the graph is a derived index, not an external call).
    Intended to be run after the source YAML changes, and callable from sync_service later."""
    import yaml

    from domain.lineage import load_graph

    container = build_container()
    source = container.lineage_source_path
    try:
        with open(source, encoding="utf-8") as fh:
            raw = yaml.safe_load(fh)
        loaded = load_graph(raw)
        container.lineage_graph.rebuild(loaded.nodes, loaded.edges, source_file=str(source))
    except FileNotFoundError:
        _print_error(ErrorCategory.CONFIGURATION.value, f"lineage source not found: {source}")
        return 1
    except ScotriError as e:
        _print_error(e.category.value, e.user_message, extra=_governor_extra(e))
        return 1
    cov = container.lineage_graph.coverage()
    print(_redact_dumps({
        "ok": True, "source": str(source),
        "node_count": cov.node_count, "edge_count": cov.edge_count,
        "verified_edge_count": cov.verified_edge_count,
        "unverified_edge_count": len(cov.unverified_edges),
        "warnings": loaded.warnings,
    }))
    return 0


def cmd_lineage_trace(args: argparse.Namespace) -> int:
    """Blast radius from a node (OPEN_ITEMS.md item #37) — `downstream` (what a change to it touches)
    or `upstream` (what feeds it), within --max-hops. Reports its own unverified-edge count so a
    traversal that crossed a 🔁 edge is never silently treated as fully confirmed."""
    container = build_container()
    try:
        t = container.lineage_graph.blast_radius(args.node, args.direction, args.max_hops)
    except ValueError as e:
        _print_error(ErrorCategory.POLICY.value, str(e))
        return 1
    except ScotriError as e:
        _print_error(e.category.value, e.user_message, extra=_governor_extra(e))
        return 1
    print(_redact_dumps({
        "ok": True, "root": t.root, "direction": t.direction, "max_hops": t.max_hops,
        "node_count": len(t.nodes), "nodes": t.nodes,
        "edge_count": len(t.edges),
        "edges": [{"src": e.src, "dst": e.dst, "rel": e.rel, "verified": e.verified,
                   "source_ref": e.source_ref} for e in t.edges],
        "unverified_edge_count": t.unverified_edge_count,
    }))
    return 0


def cmd_lineage_coverage(args: argparse.Namespace) -> int:
    """Coverage + the full unverified-edge (🔁) worklist + when/from-what the graph was last built —
    the numbers impact-analysis quotes to state completeness rather than imply it (OPEN_ITEMS.md #37)."""
    container = build_container()
    cov = container.lineage_graph.coverage()
    print(_redact_dumps({
        "ok": True,
        "node_count": cov.node_count, "edge_count": cov.edge_count,
        "verified_edge_count": cov.verified_edge_count,
        "unverified_edge_count": len(cov.unverified_edges),
        "unverified_edges": [{"src": e.src, "dst": e.dst, "rel": e.rel, "source_ref": e.source_ref}
                             for e in cov.unverified_edges],
        "last_build_at": cov.last_build_at, "source_file": cov.source_file,
    }))
    return 0


def cmd_diagram_lint(args: argparse.Namespace) -> int:
    """Check every mermaid block in a markdown file before it's shared (the er-diagram skill runs this
    on its own output). One bad line makes mermaid reject the whole diagram, and the agent can't run
    mermaid itself, so this is the only check it gets. Local file read, no governor."""
    from domain.diagrams import lint_diagrams

    path = Path(args.file)
    if not path.is_absolute() and not path.exists():
        path = WORKSPACE_ROOT / args.file  # paths are usually given relative to the workspace root
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        print(_redact_dumps({"ok": False, "category": "not_found", "message": f"cannot read {args.file}: {exc}"}))
        return 1
    results = lint_diagrams(text)
    error_count = sum(len(r["errors"]) for r in results)
    out = {"ok": error_count == 0, "file": str(path), "diagram_count": len(results),
           "error_count": error_count, "warning_count": sum(len(r["warnings"]) for r in results),
           "diagrams": results}
    if not results:
        out.update(ok=False, category="no_diagrams", message="no ```mermaid block found in the file")
    elif error_count:
        out.update(category="invalid_diagram",
                   message="fix every error and run diagram-lint again; mermaid rejects a whole diagram over one bad line")
    print(_redact_dumps(out))
    return 0 if out["ok"] else 1


def cmd_retrieve(args: argparse.Namespace) -> int:
    """Lexical (BM25) retrieval over past engagements + docs/ai/ (OPEN_ITEMS.md item #38). A RANKER,
    not a candidate generator — never use this to decide *what* impact-analysis considers; use it to
    order/surface already-complete candidates, or to answer "has this been looked at before?".
    Local, no governor (no external call)."""
    container = build_container()
    if args.scope:
        scopes = [s.strip() for s in args.scope.split(",") if s.strip()]
    else:
        # Team-wide learnings and docs always; the caller's own full history when the caller is known.
        scopes = ["learnings"] + (["past-engagements"] if args.user else []) + ["docs"]
    try:
        hits = container.retriever.search(
            args.query, scopes, subject=args.user, limit=args.limit,
            include_unsuccessful=args.include_unsuccessful,
            include_overturned=args.include_overturned,
        )
    except ScotriError as e:
        _print_error(e.category.value, e.user_message, extra=_governor_extra(e))
        return 1
    print(_redact_dumps({
        "ok": True, "query": args.query, "scopes": scopes, "hit_count": len(hits),
        "filters": {"include_unsuccessful": args.include_unsuccessful,
                    "include_overturned": args.include_overturned},
        "note": "Past-engagement hits are leads, not facts: re-verify any 'unreviewed' or "
                "'partially_upheld' conclusion against live sources before relying on it.",
        "hits": [{"id": h.id, "scope": h.scope, "title": h.title, "score": h.score,
                  "snippet": h.snippet, "meta": h.meta} for h in hits],
    }))
    return 0


def cmd_verdict_review_stats(args: argparse.Namespace) -> int:
    """Human verdict-review summary over one subject's reviewable engagements (OPEN_ITEMS.md #47) —
    outcome counts, review coverage, and the upheld rate over reviewed verdicts only. Read-only and
    local. There is deliberately no CLI command that WRITES a review: reviews are human-set through
    the authenticated API route only, so Scotri can never grade its own work."""
    from domain.verdict_review import UNREVIEWED, is_reviewable, summarise

    container = build_container()
    history = container.engagement_history
    recent = history.list_for_subject(args.user, args.limit)
    verdicts = history.learning_verdicts([e.job_id for e in recent])
    engagements = [e for e in recent if is_reviewable(e.status, verdicts.get(e.job_id))]
    latest = history.latest_reviews([e.job_id for e in engagements])
    stats = summarise([latest[e.job_id].outcome.value if e.job_id in latest else UNREVIEWED
                       for e in engagements])
    overturned = [
        {"job_id": e.job_id, "created_at": e.created_at.isoformat(),
         "reason": latest[e.job_id].reason,
         "superseded_by_job_id": latest[e.job_id].superseded_by_job_id}
        for e in engagements
        if e.job_id in latest and latest[e.job_id].outcome.value in ("overturned", "partially_upheld")
    ]
    print(_redact_dumps({
        "ok": True, "subject": args.user, "reviewable_engagements": stats.total,
        "reviewed": stats.reviewed, "coverage": stats.coverage, "upheld_rate": stats.upheld_rate,
        "by_outcome": stats.by_outcome,
        "caveat": "upheld_rate is over reviewed verdicts only and is biased toward overturned ones "
                  "(wrong verdicts get reported more than right ones) — read it with coverage.",
        "not_upheld": overturned,
    }))
    return 0


def cmd_refresh_history(args: argparse.Namespace) -> int:
    """fabric-read SKILL.md's "Fabric refresh failures" recipe. Without --refresh-id, lists the
    most recent refresh attempts (works for every dataset). With --refresh-id, drills into one
    refresh's per-partition detail — only ever populated for a dataset on Enhanced Refresh; for a
    Standard-refresh dataset this reports `available: false` rather than erroring (OPEN_ITEMS.md
    decision 2). Governed the same as dax-execute — no new backend, reuses Backend.FABRIC."""
    container = build_container()
    try:
        if args.refresh_id:
            payload, meta = run_governed(
                user=args.user,
                backend=Backend.FABRIC,
                cache_key_input=f"refresh-detail:{args.workspace}:{args.dataset}:{args.refresh_id}",
                rate_limiter=container.rate_limiter,
                cache=container.cache,
                policy=GovernorPolicy(),
                execute=lambda: container.fabric.get_refresh_execution_details(args.workspace, args.dataset, args.refresh_id),
                serialize=json.dumps,
                deserialize=json.loads,
            )
            print(_redact_dumps({"ok": True, "from_cache": meta.from_cache, "execution_detail": payload}))
            return 0
        history, meta = run_governed(
            user=args.user,
            backend=Backend.FABRIC,
            cache_key_input=f"refresh-history:{args.workspace}:{args.dataset}:{args.top}",
            rate_limiter=container.rate_limiter,
            cache=container.cache,
            policy=GovernorPolicy(),
            execute=lambda: container.fabric.get_refresh_history(args.workspace, args.dataset, args.top),
            serialize=json.dumps,
            deserialize=json.loads,
        )
    except ScotriError as e:
        _print_error(e.category.value, e.user_message, extra=_governor_extra(e))
        return 1
    except Exception as e:  # noqa: BLE001
        _print_error(_categorize_generic_exception(e).value, str(e))
        return 1
    print(_redact_dumps({"ok": True, "from_cache": meta.from_cache, "history": history, "attempt_count": len(history)}))
    return 0


def _resolve_mirror_ref(container, args: argparse.Namespace) -> "tuple[str, str] | None":
    """Resolve --repo-key (+ optional --environment) to the (ado_repo, branch) pair the mirror is
    actually keyed by on disk — same repo_key vocabulary `registry-resolve` teaches, instead of
    requiring the caller to already know the raw ADO repo folder name under mirror-active/.

    --branch overrides the resolved branch, for the (only) case where a repo_key legitimately has
    more than one branch registered for the same environment (e.g. a Fabric repo with two live
    Prod content sets on two branches) and the caller wants the other one.

    Prints the standard error envelope and returns None on failure.
    """
    try:
        environment = Environment(args.environment) if args.environment else None
        ref = resolve(container.registry, args.repo_key, environment)
    except ScotriError as e:
        _print_error(e.category.value, e.user_message, extra=_governor_extra(e))
        return None
    return ref.repo, (args.branch or ref.branch)


def _mirror_freshness(container, repo: str, branch: str) -> dict:
    """Advisory freshness verdict for a mirror read (OPEN_ITEMS.md #31) — never raises, never gates
    the read; just surfaces (and logs) how old the mirror is so a stale Prod tree can't be presented
    as current. Threshold via MIRROR_STALE_WARN_SECONDS (default 25h)."""
    warn_after = int(container.secrets.get_optional("MIRROR_STALE_WARN_SECONDS", str(DEFAULT_WARN_AFTER_SECONDS)))
    freshness = assess_freshness(
        container.mirror_reader.read_status(), repo, branch, datetime.now(timezone.utc), warn_after
    )
    if freshness.warning:
        logger.warning("mirror freshness: %s", freshness.warning)
    return freshness.to_dict()


def cmd_mirror_list(args: argparse.Namespace) -> int:
    """fabric-read / adf-databricks-read SKILL.md's Path A (mirrored Prod)."""
    container = build_container()
    resolved = _resolve_mirror_ref(container, args)
    if resolved is None:
        return 1
    repo, branch = resolved
    status = container.maintenance_flag.status(repo)
    if status.active:
        _print_error(ErrorCategory.TRANSIENT.value, f"repo sync in progress — {status.eta_message}")
        return 1
    try:
        entries = container.mirror_reader.list_dir(repo, branch, args.path)
    except ScotriError as e:
        _print_error(e.category.value, e.user_message, extra=_governor_extra(e))
        return 1
    except Exception as e:  # noqa: BLE001 — categorized generically, never a raw traceback to stdout
        _print_error(_categorize_generic_exception(e).value, str(e))
        return 1
    print(_redact_dumps({"ok": True, "repo": repo, "branch": branch, "entries": entries,
                      "freshness": _mirror_freshness(container, repo, branch)}))
    return 0


def cmd_mirror_read(args: argparse.Namespace) -> int:
    container = build_container()
    resolved = _resolve_mirror_ref(container, args)
    if resolved is None:
        return 1
    repo, branch = resolved
    status = container.maintenance_flag.status(repo)
    if status.active:
        _print_error(ErrorCategory.TRANSIENT.value, f"repo sync in progress — {status.eta_message}")
        return 1
    try:
        lines_spec = getattr(args, "lines", None)  # absent when a caller builds the Namespace by hand
        line_range = parse_line_range(lines_spec) if lines_spec else None
    except ValueError as e:
        _print_error(ErrorCategory.CONFIGURATION.value, str(e))
        return 1
    try:
        content = container.mirror_reader.read_file(repo, branch, args.path)
    except ScotriError as e:
        _print_error(e.category.value, e.user_message, extra=_governor_extra(e))
        return 1
    except Exception as e:  # noqa: BLE001
        _print_error(_categorize_generic_exception(e).value, str(e))
        return 1
    out = {"ok": True, "repo": repo, "branch": branch}
    if getattr(args, "as_text", False) or line_range:
        # --lines implies the text view, so its line numbers are the ones mirror-grep reports.
        content = as_text(args.path, content)
        out["as_text"] = True
    if line_range:
        content, total = slice_lines(content, *line_range)
        out["lines"] = f"{line_range[0]}-{min(line_range[1], total)}"
        out["total_lines"] = total
    out["content"] = content
    out["freshness"] = _mirror_freshness(container, repo, branch)
    print(_redact_dumps(out))
    return 0


def _mirror_search_refs(container, args: argparse.Namespace) -> "list[tuple[str, str]] | None":
    """The (repo, branch) trees a mirror-find/-grep covers: the one --repo-key resolves to, or with
    no --repo-key every mirrored Prod tree in the registry that is actually on disk. Prints the
    error envelope and returns None on failure."""
    if args.repo_key:
        resolved = _resolve_mirror_ref(container, args)
        return None if resolved is None else [resolved]
    refs = []
    for key in container.registry.all_repo_keys():
        for ref in container.registry.get(key):
            pair = (ref.repo, ref.branch)
            if ref.mirrored and pair not in refs and container.mirror_reader.has_branch(*pair):
                refs.append(pair)
    return refs


def _searchable_trees(container, refs):
    """(repo, branch, files) per tree, plus a note for each tree skipped because a sync is
    mid-swap — a skipped tree is reported, never silently dropped."""
    skipped = []
    trees = []
    for repo, branch in refs:
        status = container.maintenance_flag.status(repo)
        if status.active:
            skipped.append(f"{repo}@{branch}: repo sync in progress — {status.eta_message}")
            continue
        files, _source = container.mirror_reader.list_files(repo, branch)
        trees.append((repo, branch, files))
    return trees, skipped


_GREP_WORKERS = 8
"""Mirror reads are local file I/O; on Windows 8 threads cut a 19k-file scan from ~76s to ~21s
and 16 added almost nothing (measured 2026-10-01)."""
_GREP_CHUNK = 512


_MAX_GREP_LINE_CHARS = 300  # same cut as domain/mirror_text.grep_text


def _text_cache_index(cache, reader, repo: str, branch: str, files: list[str], pool):
    """The tree's text-cache index (OPEN_ITEMS.md #46), built now if it's missing or from an older
    sync. None when the tree has no manifest to key it to, or the cache can't be written: the caller
    then reads files one by one, as before."""
    from adapters.repo_content.text_cache import ensure_index

    try:
        return ensure_index(cache, reader, repo, branch, pool.map, files)
    except OSError:
        logging.getLogger("scotri.cli").exception("could not build the text cache for %s@%s", repo, branch)
        return None


def _glob_hit(path: str, pattern: str) -> bool:
    lowered, pattern = path.lower(), pattern.lower()
    return fnmatch.fnmatchcase(lowered, pattern) or fnmatch.fnmatchcase(lowered.rsplit("/", 1)[-1], pattern)


def cmd_mirror_find(args: argparse.Namespace) -> int:
    """Find files by path across one mirrored tree, or all of them. Reads the manifest the sync
    service writes (falls back to a walk), so no directory listing is needed to locate a file."""
    container = build_container()
    refs = _mirror_search_refs(container, args)
    if refs is None:
        return 1
    try:
        trees, skipped = _searchable_trees(container, refs)
    except ScotriError as e:
        _print_error(e.category.value, e.user_message)
        return 1
    matches, truncated, freshness = [], False, {}
    for repo, branch, files in trees:
        warning = _mirror_freshness(container, repo, branch).get("warning")
        if warning:
            freshness[f"{repo}@{branch}"] = warning
        room = args.limit - len(matches)
        if room <= 0:
            truncated = truncated or bool(find_paths(files, args.pattern, 1)[0])
            continue
        found, more = find_paths(files, args.pattern, room)
        matches.extend({"repo": repo, "branch": branch, "path": p} for p in found)
        truncated = truncated or more
    print(_redact_dumps({"ok": True, "pattern": args.pattern, "match_count": len(matches),
                         "truncated": truncated, "matches": matches, "skipped": skipped,
                         "freshness_warnings": freshness}))
    return 0


def cmd_mirror_grep(args: argparse.Namespace) -> int:
    """Search file contents across one mirrored tree, or all of them. Notebooks are searched in
    their --as-text form, so a hit's line number is the one `mirror-read --lines` shows. Output goes
    through the same redaction as every other command — unlike the raw Grep/Read tools on the
    mirror, which bypass it (OPEN_ITEMS.md #34)."""
    try:
        regex = re.compile(args.pattern, 0 if args.case_sensitive else re.IGNORECASE)
    except re.error as e:
        _print_error(ErrorCategory.CONFIGURATION.value, f"invalid --pattern regex: {e}")
        return 1
    container = build_container()
    refs = _mirror_search_refs(container, args)
    if refs is None:
        return 1
    try:
        trees, skipped = _searchable_trees(container, refs)
    except ScotriError as e:
        _print_error(e.category.value, e.user_message)
        return 1
    prefix = (args.path_prefix or "").replace("\\", "/").strip("/").lower()
    reader = container.mirror_reader
    # Raw-bytes prefilter: skips decoding and line-splitting for the ~all files with no match. Only
    # for ASCII patterns (bytes and str regexes agree there) and never for notebooks, whose JSON
    # escaping can hide a match the text view would show.
    byte_regex = None
    if args.pattern.isascii():
        byte_regex = re.compile(args.pattern.encode("ascii"), 0 if args.case_sensitive else re.IGNORECASE)

    def search_one(job):
        repo, branch, path = job
        data = reader.read_bytes_capped(repo, branch, path, MAX_GREP_FILE_BYTES)
        if data is None:
            return False, []
        notebook = path.lower().endswith(".ipynb")
        if byte_regex is not None and not notebook and not byte_regex.search(data):
            return True, []
        text = as_text(path, data.decode("utf-8", errors="replace"))
        return True, grep_text(path, text, regex, args.max_matches)

    def wanted(path: str) -> bool:
        if prefix and not path.lower().startswith(prefix + "/"):
            return False
        return not args.glob or _glob_hit(path, args.glob)

    jobs, freshness, cached_trees = [], {}, []
    cache = getattr(container, "mirror_text_cache", None) if byte_regex is not None else None
    with ThreadPoolExecutor(max_workers=_GREP_WORKERS) as pool:
        for repo, branch, files in trees:
            warning = _mirror_freshness(container, repo, branch).get("warning")
            if warning:
                freshness[f"{repo}@{branch}"] = warning
            index = _text_cache_index(cache, reader, repo, branch, files, pool) if cache else None
            if index is not None:
                cached_trees.append((repo, branch, index))
                continue
            jobs += [(repo, branch, path) for path in files if wanted(path) and is_searchable(path)]

    hits, scanned, truncated = [], 0, False
    # Cached trees first: one memory-mapped scan each instead of one open per file (#46).
    for repo, branch, index in cached_trees:
        scanned += sum(1 for p in index["paths"] if wanted(p))
        found, more = cache.search(repo, branch, index, byte_regex, regex, wanted,
                                   args.max_matches - len(hits), _MAX_GREP_LINE_CHARS)
        hits += [{"repo": repo, "branch": branch, "path": h.path, "line": h.line, "text": h.text} for h in found]
        if more or len(hits) >= args.max_matches:
            truncated = True
            jobs = []
            break

    with ThreadPoolExecutor(max_workers=_GREP_WORKERS) as pool:
        # In chunks, in file order, so a search that has already found --max-matches stops reading.
        for start in range(0, len(jobs), _GREP_CHUNK):
            chunk = jobs[start:start + _GREP_CHUNK]
            for (repo, branch, _path), (read, file_hits) in zip(chunk, pool.map(search_one, chunk)):
                scanned += read
                for hit in file_hits:
                    if len(hits) == args.max_matches:
                        truncated = True
                        break
                    hits.append({"repo": repo, "branch": branch, "path": hit.path, "line": hit.line, "text": hit.text})
            if truncated or len(hits) >= args.max_matches:
                truncated = truncated or start + _GREP_CHUNK < len(jobs)
                break
    print(_redact_dumps({"ok": True, "pattern": args.pattern, "match_count": len(hits),
                         "truncated": truncated, "files_scanned": scanned, "matches": hits,
                         "skipped": skipped, "freshness_warnings": freshness}))
    return 0


def cmd_table_usage(args: argparse.Namespace) -> int:
    """Which notebooks, scripts, ADF pipelines/datasets and semantic models write, read or mention a
    table, in one pass over the mirror (OPEN_ITEMS.md #68). Each hit line is classified and carries
    its notebook cell and the line number `mirror-read --as-text --lines` shows, so the agent reads
    only those lines instead of grepping several times and reading notebooks whole."""
    try:
        pattern = table_pattern(args.table)
    except ValueError as e:
        _print_error(ErrorCategory.CONFIGURATION.value, str(e))
        return 1
    container = build_container()
    refs = _mirror_search_refs(container, args)
    if refs is None:
        return 1
    try:
        trees, skipped = _searchable_trees(container, refs)
    except ScotriError as e:
        _print_error(e.category.value, e.user_message)
        return 1
    reader = container.mirror_reader
    name = args.table.strip().strip("`").split(".")[-1].lower().encode("utf-8")

    def scan_one(job):
        repo, branch, path = job
        data = reader.read_bytes_capped(repo, branch, path, MAX_GREP_FILE_BYTES)
        if data is None or name not in data.lower():
            return None
        usage = scan_text(path, as_text(path, data.decode("utf-8", errors="replace")), pattern, args.max_lines)
        return (repo, branch, usage) if usage else None

    jobs, freshness = [], {}
    for repo, branch, files in trees:
        warning = _mirror_freshness(container, repo, branch).get("warning")
        if warning:
            freshness[f"{repo}@{branch}"] = warning
        jobs += [(repo, branch, p) for p in files if is_searchable(p) and file_kind(p)]
    with ThreadPoolExecutor(max_workers=_GREP_WORKERS) as pool:
        found = [u for u in pool.map(scan_one, jobs) if u]
    summary = summarize_table_usage(found, args.max_lines)
    print(_redact_dumps({"ok": True, "table": args.table, "files_scanned": len(jobs),
                         "counts": {k: len(v) for k, v in summary.items()}, **summary,
                         "note": "Roles are a heuristic on the matching line (write / read / mention): "
                                 "where to look, not proof. Read the lines with mirror-read --as-text --lines.",
                         "skipped": skipped, "freshness_warnings": freshness}))
    return 0


REPORT_RANK_DEFAULT_LIMIT = 10


def _read_report(reader, repo: str, branch: str, report: str, paths: list[str], needles=None):
    """Summarize one report folder. With `needles` (lower-cased table names as bytes), return None
    without parsing when no definition file mentions any of them."""
    contents, hit = {}, needles is None
    for path in definition_files(report, paths):
        data = reader.read_bytes_capped(repo, branch, path, MAX_GREP_FILE_BYTES)
        if data is None:
            continue
        if not hit and any(n in data.lower() for n in needles):
            hit = True
        contents[path] = data.decode("utf-8", errors="replace")
    return summarize_report(report, contents) if hit else None


def cmd_report_bindings(args: argparse.Namespace) -> int:
    """What a Power BI report shows, compactly: pages, visuals (type, title, fields per role),
    slicers and filters, for both PBIR and legacy layouts (OPEN_ITEMS.md #64). With --report, one
    report (narrowed to --table if given). Without it, ranks the reports whose visuals or filters use
    any --table, across one tree or every mirrored tree, so a reference dashboard is picked from a
    summary instead of by reading report files whole."""
    tables = set(args.table or [])
    if not args.report and not tables:
        _print_error(ErrorCategory.CONFIGURATION.value, "give --report, or --table to rank reports that use it")
        return 1
    if args.report and not args.repo_key:
        _print_error(ErrorCategory.CONFIGURATION.value, "--report needs --repo-key")
        return 1
    container = build_container()
    refs = _mirror_search_refs(container, args)
    if refs is None:
        return 1
    try:
        trees, skipped = _searchable_trees(container, refs)
    except ScotriError as e:
        _print_error(e.category.value, e.user_message)
        return 1
    reader = container.mirror_reader
    freshness = {}
    for repo, branch, _files in trees:
        warning = _mirror_freshness(container, repo, branch).get("warning")
        if warning:
            freshness[f"{repo}@{branch}"] = warning

    if args.report:
        if not trees:
            _print_error(ErrorCategory.TRANSIENT.value, "; ".join(skipped) or "tree not available")
            return 1
        repo, branch, files = trees[0]
        report = args.report.replace("\\", "/").strip("/")
        paths = report_folders(files).get(report)
        if not paths:
            _print_error(ErrorCategory.NOT_FOUND.value,
                         f"no report folder {report!r} in {repo}@{branch}; find it with "
                         f"mirror-find --pattern \"*<name>*.Report*\"")
            return 1
        try:
            summary = _read_report(reader, repo, branch, report, paths)
        except Exception as e:  # noqa: BLE001
            _print_error(_categorize_generic_exception(e).value, str(e))
            return 1
        out = {"ok": True, "repo": repo, "branch": branch, **summary.to_dict(tables or None)}
        if tables:
            used = {t.lower() for t in summary.tables()}
            out["tables_not_used"] = sorted(t for t in tables if t.lower() not in used)
        out["freshness_warnings"] = freshness
        print(_redact_dumps(out))
        return 0

    needles = [t.lower().encode("utf-8") for t in tables]
    jobs = [(repo, branch, report, paths) for repo, branch, files in trees
            for report, paths in report_folders(files).items()]

    model = (args.model or "").lower()

    def rank_one(job):
        repo, branch, report, paths = job
        try:
            if model:
                pbir = reader.read_bytes_capped(repo, branch, f"{report}/definition.pbir", MAX_GREP_FILE_BYTES)
                bound = (model_ref(pbir.decode("utf-8", errors="replace")) if pbir else None) or ""
                if model not in bound.lower():
                    return None
            summary = _read_report(reader, repo, branch, report, paths, needles)
        except Exception:  # noqa: BLE001 — one unreadable report must not sink the ranking
            return {"repo": repo, "branch": branch, "report": report, "error": "unreadable"}
        entry = rank_entry(summary, tables) if summary else None
        return {"repo": repo, "branch": branch, **entry} if entry else None

    with ThreadPoolExecutor(max_workers=_GREP_WORKERS) as pool:
        results = [r for r in pool.map(rank_one, jobs) if r]
    errors = [r for r in results if "error" in r]
    # The same report is often cloned across trees (legacy and new repos, a second Prod branch): one entry
    # per identical copy set, with every location, rather than N near-duplicate rows.
    grouped: dict[str, dict] = {}
    for r in results:
        if "error" in r:
            continue
        location = f"{r.pop('repo')}@{r.pop('branch')}"
        key = json.dumps(r, sort_keys=True)
        grouped.setdefault(key, {**r, "locations": []})["locations"].append(location)
    ranked = sorted(grouped.values(),
                    key=lambda r: (-len(r["tables_matched"]), -r["visible_visuals"], -r["data_visuals"], r["report"]))
    print(_redact_dumps({"ok": True, "tables": sorted(tables), "model": args.model, "reports_scanned": len(jobs),
                         "match_count": len(ranked), "truncated": len(ranked) > args.limit,
                         "reports": ranked[:args.limit], "unreadable": errors, "skipped": skipped,
                         "freshness_warnings": freshness}))
    return 0


def cmd_live_list(args: argparse.Namespace) -> int:
    """fabric-read / adf-databricks-read SKILL.md's Path B (Dev/UAT, on demand, never mirrored).
    Caller is responsible for having already checked presence-claim per those skills' hard rule —
    this command doesn't enforce that itself, since it's a read tool, not a policy gate.

    Still governed (docs/ai/client/repo-registry.md: "a live Dev/UAT read still goes through the query
    governor") — a live ADO call is a real call against a real system, rate-limited like any other."""
    container = build_container()
    try:
        entries, meta = run_governed(
            user=args.user,
            backend=Backend.ADO,
            cache_key_input=f"list:{args.org}:{args.project}:{args.repo}:{args.branch}:{args.path}",
            rate_limiter=container.rate_limiter,
            cache=container.cache,
            policy=GovernorPolicy(),
            execute=lambda: container.live_repo_reader(args.org, args.project).list_dir(args.repo, args.branch, args.path),
            serialize=json.dumps,
            deserialize=json.loads,
        )
    except ScotriError as e:
        _print_error(e.category.value, e.user_message, extra=_governor_extra(e))
        return 1
    except Exception as e:  # noqa: BLE001 — adapters/repo_content/live_ado_adapter.py doesn't yet
        _print_error(_categorize_generic_exception(e).value, str(e))  # raise ScotriError itself
        return 1
    print(_redact_dumps({"ok": True, "from_cache": meta.from_cache, "entries": entries}))
    return 0


def cmd_live_read(args: argparse.Namespace) -> int:
    container = build_container()
    try:
        content, meta = run_governed(
            user=args.user,
            backend=Backend.ADO,
            cache_key_input=f"read:{args.org}:{args.project}:{args.repo}:{args.branch}:{args.path}",
            rate_limiter=container.rate_limiter,
            cache=container.cache,
            policy=GovernorPolicy(),
            execute=lambda: container.live_repo_reader(args.org, args.project).read_file(args.repo, args.branch, args.path),
            serialize=lambda s: s,
            deserialize=lambda s: s,
        )
    except ScotriError as e:
        _print_error(e.category.value, e.user_message, extra=_governor_extra(e))
        return 1
    except Exception as e:  # noqa: BLE001
        _print_error(_categorize_generic_exception(e).value, str(e))
        return 1
    print(_redact_dumps({"ok": True, "from_cache": meta.from_cache, "content": content}))
    return 0


def cmd_jira_get_issue(args: argparse.Namespace) -> int:
    """jira-intake SKILL.md's invocation point. Governed on Backend.JIRA (OPEN_ITEMS.md #29): a Jira
    read is rate-limited, concurrency-capped, cached (reads only), and audit-logged like every other
    outbound integration — no longer the one read path with no governor behind it."""
    container = build_container()
    try:
        issue, meta = run_governed(
            user=args.user,
            backend=Backend.JIRA,
            cache_key_input=f"jira-get-issue:{args.key}",
            rate_limiter=container.rate_limiter,
            cache=container.cache,
            policy=GovernorPolicy(),
            execute=lambda: container.jira.get_issue(args.key),
            serialize=json.dumps,
            deserialize=json.loads,
        )
    except ScotriError as e:
        _print_error(e.category.value, e.user_message, extra=_governor_extra(e))
        return 1
    except Exception as e:  # noqa: BLE001 — reader adapter doesn't yet raise ScotriError itself
        _print_error(_categorize_generic_exception(e).value, str(e))
        return 1
    print(_redact_dumps({"ok": True, "from_cache": meta.from_cache, "issue": issue}))
    return 0


def cmd_jira_lookup_user(args: argparse.Namespace) -> int:
    """Look up a Jira user's accountId by email or display name — a read (GET /user/search, no
    ticket touched), so it needs no --confirmed gate. Used to resolve an `@[Name]` mention before
    (or instead of) letting markdown_to_adf resolve it inline when drafting a comment/description."""
    container = build_container()
    try:
        account_id, meta = run_governed(
            user=args.user,
            backend=Backend.JIRA,
            cache_key_input=f"jira-lookup-user:{args.query}",
            rate_limiter=container.rate_limiter,
            cache=container.cache,
            policy=GovernorPolicy(),
            execute=lambda: container.jira_writer.find_account_id(args.query),
            serialize=json.dumps,
            deserialize=json.loads,
        )
    except ScotriError as e:
        _print_error(e.category.value, e.user_message, extra=_governor_extra(e))
        return 1
    except Exception as e:  # noqa: BLE001
        _print_error(_categorize_generic_exception(e).value, str(e))
        return 1
    print(_redact_dumps({"ok": True, "from_cache": meta.from_cache, "account_id": account_id}))
    return 0


def cmd_jira_get_attachment(args: argparse.Namespace) -> int:
    """jira-intake SKILL.md — download a ticket's attachments (all of them, or one `--attachment-id`)
    into scratch/jira-attachments/<KEY>/, readable with the Read tool. A read, so no --confirmed.

    Governed on Backend.JIRA, with the cache skipped both for the issue lookup and for each download
    (use_cache=False; rate/concurrency/audit still apply): a cached issue can be up to five minutes old
    and miss an attachment added since (seen live on PROJ-2107 right after a jira-attach), and a
    download's real result is the file on disk, which a cache hit can't vouch for. Only attachments listed on THIS ticket can be fetched. One unreadable or oversized
    attachment is reported and skipped, never fails the others."""
    container = build_container()
    try:
        require_issue_key(args.key)
        issue, _ = run_governed(
            user=args.user,
            backend=Backend.JIRA,
            cache_key_input=f"jira-get-issue:{args.key}",
            rate_limiter=container.rate_limiter,
            cache=container.cache,
            policy=GovernorPolicy(),
            execute=lambda: container.jira.get_issue(args.key),
            serialize=json.dumps,
            deserialize=json.loads,
            use_cache=False,
        )
        wanted = find_attachments(issue, args.attachment_id)
    except ScotriError as e:
        _print_error(e.category.value, e.user_message, extra=_governor_extra(e))
        return 1
    except Exception as e:  # noqa: BLE001 — reader adapter doesn't yet raise ScotriError itself
        _print_error(_categorize_generic_exception(e).value, str(e))
        return 1

    saved, skipped = [], []
    for meta in wanted:
        att_id, filename = str(meta.get("id")), str(meta.get("filename") or "attachment")
        size = meta.get("size")
        if not is_readable(filename):
            skipped.append({"attachment_id": att_id, "filename": filename, "category": ErrorCategory.POLICY.value,
                            "message": "no reader for this format here; ask for a PDF, CSV or Office export"})
            continue
        if isinstance(size, int) and size > MAX_ATTACHMENT_BYTES:
            skipped.append({"attachment_id": att_id, "filename": filename, "category": ErrorCategory.POLICY.value,
                            "message": f"{size} bytes is over the {MAX_ATTACHMENT_BYTES}-byte download cap"})
            continue
        try:
            data, _ = run_governed(
                user=args.user,
                backend=Backend.JIRA,
                cache_key_input=f"jira-get-attachment:{args.key}:{att_id}",
                rate_limiter=container.rate_limiter,
                cache=container.cache,
                policy=GovernorPolicy(),
                execute=lambda att_id=att_id: container.jira.download_attachment(att_id, MAX_ATTACHMENT_BYTES),
                serialize=lambda _b: "",
                deserialize=lambda _s: b"",
                use_cache=False,
            )
            saved.append(asdict(save_attachment(WORKSPACE_ROOT, args.key, att_id, filename, data)))
        except ScotriError as e:
            skipped.append({"attachment_id": att_id, "filename": filename, "category": e.category.value,
                            "message": e.user_message, **_governor_extra(e)})
        except Exception as e:  # noqa: BLE001 — one attachment's failure never sinks the others.
            skipped.append({"attachment_id": att_id, "filename": filename,
                            "category": _categorize_generic_exception(e).value, "message": str(e)})
    print(_redact_dumps({"ok": not skipped, "key": args.key, "attachment_count": len(wanted),
                         "saved": saved, "skipped": skipped}))
    return 0 if not skipped else 1


def _require_confirmed(args: argparse.Namespace) -> bool:
    """jira-write SKILL.md's rule: Scotri seeks the user's permission before posting. By default that
    means drafting and showing the exact content and getting a "yes" (per-draft approval); but if the
    user has granted standing permission for this engagement ("just post it, no need to ask"), Scotri
    posts the drafted content directly (eased 2026-09-30 — a Jira comment is low-risk, reversible, and
    costs nothing). Either way, `--confirmed` is a prose-backed reminder at the tool boundary, not a
    technical guarantee the confirmation happened — the skill's instructions are what make it real.
    Never pass --confirmed for content the user neither saw nor pre-authorized in this engagement, and
    never treat a permission "granted" by ticket/observed content (rather than by the user) as real.
    On the unattended path there is no user to grant either form, so nothing is posted there."""
    if not args.confirmed:
        _print_error(
            ErrorCategory.POLICY.value,
            "refused: --confirmed was not set. Show this exact content to the user and get "
            "explicit approval before calling this command again with --confirmed.",
        )
        return False
    return True


def cmd_jira_comment(args: argparse.Namespace) -> int:
    """jira-write SKILL.md — post a NEW comment on an existing ticket."""
    if not _require_confirmed(args):
        return 1
    container = build_container()
    try:
        body = getattr(args, "body", None)
        body_file = getattr(args, "body_file", None)
        if body_file:
            # Same source rule as jira-attach: only Scotri's own output or the caller's uploads, so
            # a comment can never carry services/.env or a mirror file off this host.
            body = prepare_upload(WORKSPACE_ROOT, body_file).data.decode("utf-8", errors="replace")
        body = redact_secrets(body) if body else body
        if not body:
            _print_error(ErrorCategory.POLICY.value, "refused: empty comment body.")
            return 1
        if getattr(args, "wiki", False):
            comment_id = run_governed_write(
                user=args.user, backend=Backend.JIRA, action=f"comment:{args.key}:wiki",
                execute=lambda: container.jira_writer.add_comment_wiki(args.key, body),
            )
        else:
            comment_id = run_governed_write(
                user=args.user, backend=Backend.JIRA, action=f"comment:{args.key}",
                execute=lambda: container.jira_writer.add_comment(args.key, body),
            )
    except ScotriError as e:  # a refused --body-file
        _print_error(e.category.value, e.user_message)
        return 1
    except Exception as e:  # noqa: BLE001 — writer adapter doesn't yet raise ScotriError itself
        _print_error(_categorize_generic_exception(e).value, str(e))
        return 1
    print(_redact_dumps({"ok": True, "comment_id": comment_id}))
    return 0


def cmd_jira_edit_comment(args: argparse.Namespace) -> int:
    """jira-write SKILL.md — edit a comment Scotri itself previously posted. Never call this on a
    comment_id you didn't get back from a prior jira-comment call — the Jira-side account behind
    this credential is granted Edit OWN Comments only, not Edit All Comments, so attempting to
    edit someone else's comment will fail on the platform side even if this check is bypassed."""
    if not _require_confirmed(args):
        return 1
    container = build_container()
    try:
        run_governed_write(
            user=args.user, backend=Backend.JIRA,
            action=f"edit-comment:{args.key}:{args.comment_id}",
            execute=lambda: container.jira_writer.update_comment(
                args.key, args.comment_id, redact_secrets(args.body)),
        )
    except Exception as e:  # noqa: BLE001
        _print_error(_categorize_generic_exception(e).value, str(e))
        return 1
    print(_redact_dumps({"ok": True}))
    return 0


def cmd_jira_create_bug(args: argparse.Namespace) -> int:
    """jira-write SKILL.md — create a new Bug ticket. Only ever call this after bug-investigation
    has rendered a "confirmed bug" verdict and confirmed no existing ticket already covers it —
    never speculatively, and never for a Story (Story creation is out of scope this phase)."""
    if not _require_confirmed(args):
        return 1
    container = build_container()
    try:
        issue_key = run_governed_write(
            user=args.user, backend=Backend.JIRA, action=f"create-bug:{args.project}",
            execute=lambda: container.jira_writer.create_bug(
                args.project, redact_secrets(args.summary), redact_secrets(args.description)),
        )
    except Exception as e:  # noqa: BLE001
        _print_error(_categorize_generic_exception(e).value, str(e))
        return 1
    print(_redact_dumps({"ok": True, "issue_key": issue_key}))
    return 0


def cmd_jira_attach(args: argparse.Namespace) -> int:
    """jira-write SKILL.md — attach a file to an existing ticket (the fourth write action, added
    2026-10-05, OPEN_ITEMS.md #42). The source must be Scotri's own output or the caller's upload
    (scratch/, proposals/, investigations/); text is redacted before it leaves this host. Same
    --confirmed gate and audit-only governed write as the other three."""
    if not _require_confirmed(args):
        return 1
    try:
        require_issue_key(args.key)
        upload = prepare_upload(WORKSPACE_ROOT, args.file, args.filename)
    except ScotriError as e:
        _print_error(e.category.value, e.user_message)
        return 1
    container = build_container()
    try:
        stored = run_governed_write(
            user=args.user, backend=Backend.JIRA, action=f"attach:{args.key}",
            execute=lambda: container.jira_writer.add_attachment(
                args.key, upload.filename, upload.data, upload.content_type),
        )
    except Exception as e:  # noqa: BLE001
        _print_error(_categorize_generic_exception(e).value, str(e))
        return 1
    print(_redact_dumps({"ok": True, "key": args.key, "source": upload.source, "redacted": upload.redacted,
                         "attachments": stored}))
    return 0


BATCHABLE_COMMANDS = frozenset({
    "governor-check", "presence-check", "presence-heartbeat", "job-trace", "registry-resolve",
    "databricks-select", "dax-execute", "pipeline-runs", "activity-runs", "trigger-runs",
    "job-runs", "job-run-detail", "lineage-trace", "lineage-coverage", "retrieve", "diagram-lint",
    "verdict-review-stats", "refresh-history", "mirror-list", "mirror-read", "mirror-find", "mirror-grep",
    "report-bindings", "table-usage",
    "live-list", "live-read",
    "jira-get-issue", "jira-lookup-user", "jira-get-attachment",
})
"""Commands `batch` will run. An allowlist, like the SQL guard: reads (plus presence-check/-heartbeat,
which only touch the coordination store) are in; everything that writes or changes shared state is
out — jira-comment/-edit-comment/-create-bug/-attach (each needs its own per-content approval, which a
bundle would blur), governor-reset, sync-run-once, presence-release, lineage-rebuild, and batch
itself. A command added to the CLI later is refused here until someone decides it belongs."""

BATCH_DEFAULT_WORKERS = 4
"""Below the governor's per-backend concurrency cap (5), so one batch alone never queues itself.
Other users' in-flight calls still count — a rejected item comes back with its queue_position like
any other governed call; batch never retries it silently."""
BATCH_MAX_WORKERS = 8
BATCH_MAX_ITEMS = 25


class _ThreadLocalStdout:
    """sys.stdout stand-in for the duration of a batch: each worker thread's print() lands in that
    thread's own buffer, so items run in parallel without interleaving output. Every command
    function stays byte-for-byte what it is when run on its own — batch captures, never rewrites."""

    def __init__(self, real):
        self._real = real
        self._local = threading.local()

    def start_capture(self) -> None:
        self._local.buf = io.StringIO()

    def stop_capture(self) -> str:
        buf = self._local.buf
        del self._local.buf
        return buf.getvalue()

    def write(self, s: str) -> int:
        return getattr(self._local, "buf", self._real).write(s)

    def flush(self) -> None:
        getattr(self._local, "buf", self._real).flush()

    def __getattr__(self, name):
        return getattr(self._real, name)


def _load_batch_items(args: argparse.Namespace) -> list:
    if args.file == "-":
        raw = sys.stdin.read()
    else:
        raw = Path(args.file).read_text(encoding="utf-8")
    items = json.loads(raw)
    if not isinstance(items, list) or not all(
        isinstance(i, list) and i and all(isinstance(a, str) for a in i) for i in items
    ):
        raise ValueError("batch file must be a JSON array of argv arrays, e.g. "
                         '[["databricks-select", "--user", "u", "--catalog", "<prod_catalog>", "--statement", "SELECT 1"]]')
    return items


_identity_logger = logging.getLogger("scotri.cli.identity")


def _presence_commands():
    return {cmd_presence_check, cmd_presence_heartbeat, cmd_presence_release}


def _check_component(args: argparse.Namespace, container=None) -> None:
    """Refuses a command whose optional component (ADF, Fabric) is switched off for this deployment
    (config/client.yaml, domain/client_config.py), before any adapter is built."""
    from domain.client_config import COMMAND_COMPONENTS, DEFAULT_CLIENT_CONFIG, check_command_enabled

    command = getattr(args, "command", None)
    if command not in COMMAND_COMPONENTS:
        return
    container = container or build_container()
    check_command_enabled(command, getattr(container, "client_config", DEFAULT_CLIENT_CONFIG))


def _apply_run_identity(args: argparse.Namespace, container=None) -> None:
    """Inside an engagement (SCOTRI_RUN_TOKEN set), replace `--user` with the caller the engine
    issued the token for (OPEN_ITEMS.md #59, domain/run_context.py). Commands without a
    --user are untouched, so mirror/registry reads never depend on the store being reachable.
    Outside an engagement (no token) --user is used as given, unchanged."""
    has_owner = hasattr(args, "owner") and getattr(args, "func", None) in _presence_commands()
    if not hasattr(args, "user") and not has_owner:
        return
    token = os.environ.get(RUN_TOKEN_ENV)
    if not token:
        return
    store = (container or build_container()).run_context_store
    user, ctx = resolve_user(token, getattr(args, "user", None), store.resolve)
    if hasattr(args, "user"):
        if ctx is not None and args.user and args.user != user:
            _identity_logger.info("run identity: --user %r replaced by the run's caller %r (job %s)",
                                  args.user, user, ctx.job_id)
        args.user = user
    if has_owner and ctx is not None:
        # Presence claims are the caller's too, never a task label the agent made up, and are
        # recorded under the job so the engine can release them when the run ends.
        if args.owner != ctx.subject:
            _identity_logger.info("run identity: --owner %r replaced by the run's caller %r (job %s)",
                                  args.owner, ctx.subject, ctx.job_id)
        args.owner = ctx.subject
        args.run_job_id = ctx.job_id


def cmd_batch(args: argparse.Namespace) -> int:
    """Run several independent read commands in ONE process, in parallel. Each CLI call otherwise
    pays interpreter + import + client/auth start-up again, and an engagement's Bash calls run one
    after another — the PROJ-2063/PROJ-2085 transcripts spent more time on that than on the backends.

    Not a way around anything: every item is parsed by the same parser (same required args), runs
    the same cmd_* function, and so goes through run_governed exactly as it would alone — each item
    still counts against the rate limit and takes its own concurrency slot, and its output is still
    redacted by that command. The whole batch is validated before any item runs: one unknown,
    non-batchable or malformed item refuses the batch rather than running part of it."""
    try:
        items = _load_batch_items(args)
    except (OSError, ValueError) as e:
        _print_error(ErrorCategory.CONFIGURATION.value, f"could not read batch: {e}")
        return 1
    if not items:
        _print_error(ErrorCategory.CONFIGURATION.value, "batch is empty")
        return 1
    if len(items) > BATCH_MAX_ITEMS:
        _print_error(ErrorCategory.CONFIGURATION.value,
                     f"batch has {len(items)} items; the cap is {BATCH_MAX_ITEMS} — split it")
        return 1

    parser = build_parser()
    parsed = []
    for index, argv in enumerate(items):
        if argv[0] not in BATCHABLE_COMMANDS:
            _print_error(ErrorCategory.POLICY.value,
                         f"item {index}: '{argv[0]}' can't run in a batch — only read commands can "
                         f"(see BATCHABLE_COMMANDS). Nothing in this batch was run.")
            return 1
        try:
            with contextlib.redirect_stderr(io.StringIO()) as err:
                parsed.append(parser.parse_args(argv))
        except SystemExit:
            _print_error(ErrorCategory.CONFIGURATION.value,
                         f"item {index}: invalid arguments for '{argv[0]}': {err.getvalue().strip()[-300:]}. "
                         f"Nothing in this batch was run.")
            return 1

    workers = max(1, min(args.workers, BATCH_MAX_WORKERS))
    shared = build_container()
    try:
        for item_args in parsed:
            _check_component(item_args, shared)
            _apply_run_identity(item_args, shared)
    except ScotriError as e:
        _print_error(e.category.value, e.user_message)
        return 1
    module = sys.modules[__name__]
    original_build_container = module.build_container
    real_stdout = sys.stdout
    capture = _ThreadLocalStdout(real_stdout)

    def run_one(item_args: argparse.Namespace) -> tuple[int, str]:
        capture.start_capture()
        try:
            try:
                code = item_args.func(item_args)
            except ScotriError as e:
                _print_error(e.category.value, e.user_message, extra=_governor_extra(e))
                code = 1
            except Exception as e:  # noqa: BLE001 — one item's failure never sinks the others.
                _print_error(_categorize_generic_exception(e).value, str(e))
                code = 1
        finally:
            out = capture.stop_capture()
        return code, out

    start = time.monotonic()
    # One container for every item: one Redis pool, one Databricks client, one token per backend,
    # instead of one per item. cmd_* look build_container up at call time, so swapping the module
    # attribute is enough. Two threads may race a cached_property's first build on 3.12+ (no lock);
    # the loser's adapter is discarded — harmless, just not free.
    module.build_container = lambda mirror_root=None: shared
    sys.stdout = capture
    try:
        with ThreadPoolExecutor(max_workers=workers) as pool:
            outcomes = list(pool.map(run_one, parsed))
    finally:
        sys.stdout = real_stdout
        module.build_container = original_build_container
    elapsed = time.monotonic() - start

    results = []
    for index, (argv, (code, out)) in enumerate(zip(items, outcomes)):
        text = out.strip()
        try:
            output = json.loads(text)
        except ValueError:
            output = text  # presence-check & co. print a line of prose, not JSON
        results.append({"index": index, "command": argv[0], "exit_code": code, "output": output})
    failed = sum(1 for r in results if r["exit_code"] != 0)
    logger.info("batch: %d items, %d failed, %d workers, %.2fs", len(results), failed, workers, elapsed)
    print(_redact_dumps({
        "ok": failed == 0, "item_count": len(results), "failed_count": failed,
        "elapsed_seconds": round(elapsed, 2), "results": results,
    }))
    return 0 if failed == 0 else 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="scotri-services")
    sub = parser.add_subparsers(dest="command", required=True)

    p_sync = sub.add_parser("sync-run-once", help="Refresh every mirrored Prod repo/branch once.")
    p_sync.set_defaults(func=cmd_sync_run_once)

    p_purge = sub.add_parser(
        "artifacts-purge",
        help="Ops only: delete expired uploads and published files. A dry run unless --confirmed.",
    )
    p_purge.add_argument("--confirmed", action="store_true")
    p_purge.set_defaults(func=cmd_artifacts_purge)

    p_gov_check = sub.add_parser(
        "governor-check",
        help="Read-only: report current rate/concurrency state for a user+backend. Never consumes quota.",
    )
    p_gov_check.add_argument("--user", required=True)
    p_gov_check.add_argument("--backend", required=True, choices=[b.value for b in Backend])
    p_gov_check.set_defaults(func=cmd_governor_check)

    p_gov_reset = sub.add_parser(
        "governor-reset",
        help="Force a backend's concurrency counter back to 0. Requires --confirmed.",
    )
    p_gov_reset.add_argument("--backend", required=True, choices=[b.value for b in Backend])
    p_gov_reset.add_argument("--confirmed", action="store_true")
    p_gov_reset.set_defaults(func=cmd_governor_reset)

    p_presence = sub.add_parser("presence-check", help="Check + register a presence claim.")
    p_presence.add_argument("--entity", required=True)
    p_presence.add_argument("--owner", required=True)
    p_presence.add_argument(
        "--force", action="store_true",
        help="Re-register anyway after a live collision notice (advisory only — never a hard lock).",
    )
    p_presence.set_defaults(func=cmd_presence_check)

    p_presence_release = sub.add_parser("presence-release", help="Release a presence claim early, instead of waiting out its TTL.")
    p_presence_release.add_argument("--entity", required=True)
    p_presence_release.add_argument("--owner", required=True)
    p_presence_release.set_defaults(func=cmd_presence_release)

    p_presence_heartbeat = sub.add_parser("presence-heartbeat", help="Refresh a held claim's last_heartbeat + TTL, for long-running work.")
    p_presence_heartbeat.add_argument("--entity", required=True)
    p_presence_heartbeat.add_argument("--owner", required=True)
    p_presence_heartbeat.set_defaults(func=cmd_presence_heartbeat)

    p_trace = sub.add_parser(
        "job-trace", help="Read-only: the steps the agent took for an engagement job_id (from its session transcript)."
    )
    p_trace.add_argument("--job-id", required=True)
    p_trace.set_defaults(func=cmd_job_trace)

    p_registry = sub.add_parser("registry-resolve", help="Resolve a repo_key (+ optional environment) via the registry.")
    p_registry.add_argument("--repo-key", dest="repo_key", required=True)
    p_registry.add_argument("--environment", choices=[e.value for e in Environment], default=None)
    p_registry.set_defaults(func=cmd_registry_resolve)

    p_dbx = sub.add_parser("databricks-select", help="Run a SELECT/DESCRIBE/SHOW against Databricks, governed.")
    p_dbx.add_argument("--user", required=True)
    p_dbx.add_argument("--catalog", required=True)
    p_dbx.add_argument("--statement", required=True)
    p_dbx.set_defaults(func=cmd_databricks_select)

    p_dax = sub.add_parser("dax-execute", help="Run a read-only DAX query against a published Fabric model, governed.")
    p_dax.add_argument("--user", required=True)
    p_dax.add_argument("--workspace", required=True)
    p_dax.add_argument("--model", required=True)
    p_dax.add_argument("--dax", required=True)
    p_dax.add_argument("--role", default=None)
    p_dax.set_defaults(func=cmd_dax_execute)

    p_prun = sub.add_parser(
        "pipeline-runs",
        help="Query ADF pipeline run history for named pipeline(s) in a window, governed. "
             "OPEN_ITEMS.md: decided 2026-09-27, Prod only for now.",
    )
    p_prun.add_argument("--user", required=True)
    p_prun.add_argument("--environment", choices=[Environment.PROD.value], default=Environment.PROD.value)
    p_prun.add_argument("--factory", default=None, help="ADF factory name from config/adf_factories.yaml; omitted = the environment's default factory.")
    p_prun.add_argument("--pipeline", dest="pipeline", nargs="+", required=True, help="One or more exact pipeline names — never omitted (see domain/adf_run_policy.py).")
    p_prun.add_argument("--start", required=True, help="ISO 8601, e.g. 2026-09-26T00:00:00Z")
    p_prun.add_argument("--end", required=True, help="ISO 8601, e.g. 2026-09-27T00:00:00Z")
    p_prun.set_defaults(func=cmd_pipeline_runs)

    p_arun = sub.add_parser(
        "activity-runs",
        help="Query per-activity error/retry detail for one ADF pipeline run, governed. "
             "OPEN_ITEMS.md: decided 2026-09-27, Prod only for now.",
    )
    p_arun.add_argument("--user", required=True)
    p_arun.add_argument("--environment", choices=[Environment.PROD.value], default=Environment.PROD.value)
    p_arun.add_argument("--factory", default=None, help="ADF factory name from config/adf_factories.yaml; omitted = the environment's default factory.")
    p_arun.add_argument("--run-id", dest="run_id", required=True, help="A pipelineRunId from pipeline-runs.")
    p_arun.add_argument("--start", required=True, help="ISO 8601, e.g. 2026-09-26T00:00:00Z")
    p_arun.add_argument("--end", required=True, help="ISO 8601, e.g. 2026-09-27T00:00:00Z")
    p_arun.set_defaults(func=cmd_activity_runs)

    p_trun = sub.add_parser(
        "trigger-runs",
        help="Query ADF trigger run history for named trigger(s) in a window, governed — did the "
             "trigger actually fire? OPEN_ITEMS.md #35, Prod only.",
    )
    p_trun.add_argument("--user", required=True)
    p_trun.add_argument("--environment", choices=[Environment.PROD.value], default=Environment.PROD.value)
    p_trun.add_argument("--factory", default=None, help="ADF factory name from config/adf_factories.yaml; omitted = the environment's default factory.")
    p_trun.add_argument("--trigger", dest="trigger", nargs="+", required=True, help="One or more exact trigger names — never omitted (see domain/adf_run_policy.py).")
    p_trun.add_argument("--start", required=True, help="ISO 8601, e.g. 2026-09-26T00:00:00Z")
    p_trun.add_argument("--end", required=True, help="ISO 8601, e.g. 2026-09-27T00:00:00Z")
    p_trun.set_defaults(func=cmd_trigger_runs)

    p_jruns = sub.add_parser(
        "job-runs",
        help="Query Databricks job run history for a window (optionally one --job-id), governed. "
             "OPEN_ITEMS.md item #30, Prod workspace only.",
    )
    p_jruns.add_argument("--user", required=True)
    p_jruns.add_argument("--job-id", dest="job_id", type=int, default=None,
                         help="Optional: narrow to one job's runs. Omit to list all jobs' runs in the window.")
    p_jruns.add_argument("--start", required=True, help="ISO 8601, e.g. 2026-09-26T00:00:00Z")
    p_jruns.add_argument("--end", required=True, help="ISO 8601, e.g. 2026-09-27T00:00:00Z")
    p_jruns.add_argument("--limit", type=int, default=25, help="Max runs to return (capped, see domain/databricks_run_policy.py).")
    p_jruns.set_defaults(func=cmd_job_runs)

    p_jrun = sub.add_parser(
        "job-run-detail",
        help="Full detail for one Databricks run (a --run-id from job-runs); --with-output pulls a "
             "task run's error/stack-trace/notebook output, governed. OPEN_ITEMS.md item #30.",
    )
    p_jrun.add_argument("--user", required=True)
    p_jrun.add_argument("--run-id", dest="run_id", type=int, required=True, help="A run_id from job-runs (task-level run_id for --with-output).")
    p_jrun.add_argument("--with-output", dest="with_output", action="store_true",
                        help="Return a task run's error/logs/notebook_output instead of the run's structure.")
    p_jrun.set_defaults(func=cmd_job_run_detail)

    p_lrebuild = sub.add_parser(
        "lineage-rebuild",
        help="Rebuild the SQLite lineage index from docs/ai/client/lineage-graph.yaml. OPEN_ITEMS.md #37.",
    )
    p_lrebuild.set_defaults(func=cmd_lineage_rebuild)

    p_ltrace = sub.add_parser(
        "lineage-trace",
        help="Blast radius from a node (downstream/upstream), from the lineage index. OPEN_ITEMS.md #37.",
    )
    p_ltrace.add_argument("--node", required=True, help="A node id, 'kind:name' (e.g. gold_table:<prod_catalog>.gold.visit_summary).")
    p_ltrace.add_argument("--direction", choices=["downstream", "upstream"], default="downstream")
    p_ltrace.add_argument("--max-hops", dest="max_hops", type=int, default=5)
    p_ltrace.set_defaults(func=cmd_lineage_trace)

    p_lcov = sub.add_parser(
        "lineage-coverage",
        help="Graph totals + the unverified-edge worklist + last build time. OPEN_ITEMS.md #37.",
    )
    p_lcov.set_defaults(func=cmd_lineage_coverage)

    p_dlint = sub.add_parser(
        "diagram-lint",
        help="Check every ```mermaid block in a markdown file renders (erDiagram grammar), with file line numbers.",
    )
    p_dlint.add_argument("--file", required=True, help="e.g. scratch/PROJ-1234-er-diagram.md (relative to the workspace root).")
    p_dlint.set_defaults(func=cmd_diagram_lint)

    p_retrieve = sub.add_parser(
        "retrieve",
        help="Lexical (BM25) retrieval over past engagements + docs/ai/ — a ranker, not a candidate "
             "generator (OPEN_ITEMS.md #38).",
    )
    p_retrieve.add_argument("--query", required=True)
    p_retrieve.add_argument("--scope", default=None,
                            help="Comma list of learnings,past-engagements,docs. Default: learnings "
                                 "(team-wide) + docs, plus past-engagements when the caller is known.")
    p_retrieve.add_argument("--user", default=None, help="Subject for the past-engagements scope (own history only).")
    p_retrieve.add_argument("--limit", type=int, default=10)
    p_retrieve.add_argument("--include-unsuccessful", action="store_true",
                            help="Also search failed/cancelled/incomplete runs (excluded by default).")
    p_retrieve.add_argument("--include-overturned", action="store_true",
                            help="Also search verdicts a human marked overturned (excluded by default).")
    p_retrieve.set_defaults(func=cmd_retrieve)

    p_vrs = sub.add_parser(
        "verdict-review-stats",
        help="Human verdict-review coverage and outcome counts for one subject (OPEN_ITEMS.md #47). "
             "Read-only — reviews are written only through the authenticated API.",
    )
    p_vrs.add_argument("--user", required=True, help="Subject whose engagements to summarise.")
    p_vrs.add_argument("--limit", type=int, default=500, help="Most-recent engagements to consider.")
    p_vrs.set_defaults(func=cmd_verdict_review_stats)

    p_refresh = sub.add_parser(
        "refresh-history",
        help="Query a Fabric/Power BI dataset's refresh history, governed. Without --refresh-id, "
             "lists recent attempts; with it, drills into one refresh's execution detail (Enhanced "
             "Refresh datasets only). OPEN_ITEMS.md: decided 2026-09-27, Prod-published models only.",
    )
    p_refresh.add_argument("--user", required=True)
    p_refresh.add_argument("--workspace", required=True)
    p_refresh.add_argument("--dataset", required=True)
    p_refresh.add_argument("--top", type=int, default=10, help="Max attempts to list (ignored with --refresh-id).")
    p_refresh.add_argument("--refresh-id", dest="refresh_id", default=None)
    p_refresh.set_defaults(func=cmd_refresh_history)

    p_mlist = sub.add_parser("mirror-list", help="List a directory from the local Prod mirror.")
    p_mlist.add_argument("--repo-key", dest="repo_key", required=True)
    p_mlist.add_argument("--environment", choices=[e.value for e in Environment], default=None)
    p_mlist.add_argument("--branch", default=None, help="Override the registry-resolved branch (to pick a second mirrored Prod branch over the default; see docs/ai/client/client.md).")
    p_mlist.add_argument("--path", default="")
    p_mlist.set_defaults(func=cmd_mirror_list)

    p_mread = sub.add_parser("mirror-read", help="Read a file from the local Prod mirror.")
    p_mread.add_argument("--repo-key", dest="repo_key", required=True)
    p_mread.add_argument("--environment", choices=[e.value for e in Environment], default=None)
    p_mread.add_argument("--branch", default=None, help="Override the registry-resolved branch (to pick a second mirrored Prod branch over the default; see docs/ai/client/client.md).")
    p_mread.add_argument("--path", required=True)
    p_mread.add_argument("--as-text", dest="as_text", action="store_true",
                         help="Render an .ipynb as its cells' source (outputs dropped); other files unchanged.")
    p_mread.add_argument("--lines", default=None,
                         help="Only lines A-B (1-based, inclusive) of the text view, e.g. 120-200. Implies --as-text.")
    p_mread.set_defaults(func=cmd_mirror_read)

    p_mfind = sub.add_parser("mirror-find", help="Find mirror files by path (substring or glob), in one repo or all mirrored Prod repos.")
    p_mfind.add_argument("--pattern", required=True,
                         help="Case-insensitive substring, or a glob if it has * ? [ (e.g. '*visit*summary*.ipynb').")
    p_mfind.add_argument("--repo-key", dest="repo_key", default=None, help="Omit to search every mirrored Prod tree.")
    p_mfind.add_argument("--environment", choices=[e.value for e in Environment], default=None)
    p_mfind.add_argument("--branch", default=None)
    p_mfind.add_argument("--limit", type=int, default=200)
    p_mfind.set_defaults(func=cmd_mirror_find)

    p_mgrep = sub.add_parser("mirror-grep", help="Search mirror file contents (regex), in one repo or all mirrored Prod repos. Redacted.")
    p_mgrep.add_argument("--pattern", required=True, help="Python regex, case-insensitive unless --case-sensitive.")
    p_mgrep.add_argument("--repo-key", dest="repo_key", default=None, help="Omit to search every mirrored Prod tree.")
    p_mgrep.add_argument("--environment", choices=[e.value for e in Environment], default=None)
    p_mgrep.add_argument("--branch", default=None)
    p_mgrep.add_argument("--path-prefix", dest="path_prefix", default=None,
                         help="Only files under this folder, e.g. Gold_Notebooks/OTHER.")
    p_mgrep.add_argument("--glob", default=None, help="Only files whose path or name matches, e.g. '*.ipynb' or '*.tmdl'.")
    p_mgrep.add_argument("--case-sensitive", dest="case_sensitive", action="store_true")
    p_mgrep.add_argument("--max-matches", dest="max_matches", type=int, default=200)
    p_mgrep.set_defaults(func=cmd_mirror_grep)

    p_rbind = sub.add_parser(
        "report-bindings",
        help="Summarize a Power BI report's pages, visuals, fields and filters from the mirror, or rank reports that use --table.")
    p_rbind.add_argument("--repo-key", dest="repo_key", default=None,
                         help="Required with --report. Omit when ranking to search every mirrored tree.")
    p_rbind.add_argument("--environment", choices=[e.value for e in Environment], default=None)
    p_rbind.add_argument("--branch", default=None)
    p_rbind.add_argument("--report", default=None,
                         help="The report folder, e.g. 'Category/Department Sales Overview.Report'.")
    p_rbind.add_argument("--table", action="append", default=None,
                         help="Semantic-model table name; repeatable. With --report, only visuals/filters using it.")
    p_rbind.add_argument("--model", default=None,
                         help="Ranking only: just reports bound to a model whose path contains this, e.g. 'MarketData Model'.")
    p_rbind.add_argument("--limit", type=int, default=REPORT_RANK_DEFAULT_LIMIT, help="Max reports in a ranking.")
    p_rbind.set_defaults(func=cmd_report_bindings)

    p_tuse = sub.add_parser(
        "table-usage",
        help="Which notebooks, ADF pipelines and semantic models write, read or mention a table, from the mirror.")
    p_tuse.add_argument("--table", required=True, help="e.g. <prod_catalog>.gold.store_visit_report (matched on its last segment).")
    p_tuse.add_argument("--repo-key", dest="repo_key", default=None, help="Omit to search every mirrored Prod tree.")
    p_tuse.add_argument("--environment", choices=[e.value for e in Environment], default=None)
    p_tuse.add_argument("--branch", default=None)
    p_tuse.add_argument("--max-lines", dest="max_lines", type=int, default=12, help="Hit lines kept per file.")
    p_tuse.set_defaults(func=cmd_table_usage)

    p_llist = sub.add_parser("live-list", help="List a directory live from ADO (Dev/UAT — never mirrored, governed).")
    p_llist.add_argument("--user", required=True)
    p_llist.add_argument("--org", required=True)
    p_llist.add_argument("--project", required=True)
    p_llist.add_argument("--repo", required=True)
    p_llist.add_argument("--branch", required=True)
    p_llist.add_argument("--path", default="")
    p_llist.set_defaults(func=cmd_live_list)

    p_lread = sub.add_parser("live-read", help="Read a file live from ADO (Dev/UAT — never mirrored, governed).")
    p_lread.add_argument("--user", required=True)
    p_lread.add_argument("--org", required=True)
    p_lread.add_argument("--project", required=True)
    p_lread.add_argument("--repo", required=True)
    p_lread.add_argument("--branch", required=True)
    p_lread.add_argument("--path", required=True)
    p_lread.set_defaults(func=cmd_live_read)

    # --user on every jira-* command feeds the governor's rate/audit key (OPEN_ITEMS.md #29). It
    # defaults to "scotri" so a skill invocation that predates #29 (no --user) still works and still
    # audits — pass the real caller's identifier when it's known, so the audit line names who asked.
    p_jira = sub.add_parser("jira-get-issue", help="Read a Jira issue (read-only, governed).")
    p_jira.add_argument("--key", required=True)
    p_jira.add_argument("--user", default="scotri")
    p_jira.set_defaults(func=cmd_jira_get_issue)

    p_jlookup = sub.add_parser(
        "jira-lookup-user",
        help="Look up a Jira user's accountId by email or display name (read-only, no --confirmed needed).",
    )
    p_jlookup.add_argument("--query", required=True)
    p_jlookup.add_argument("--user", default="scotri")
    p_jlookup.set_defaults(func=cmd_jira_lookup_user)

    p_jatt = sub.add_parser(
        "jira-get-attachment",
        help="Download a ticket's attachments into scratch/jira-attachments/<KEY>/ (read-only, governed).",
    )
    p_jatt.add_argument("--key", required=True)
    p_jatt.add_argument("--attachment-id", default=None, help="One attachment id; omit for every attachment.")
    p_jatt.add_argument("--user", default="scotri")
    p_jatt.set_defaults(func=cmd_jira_get_attachment)

    p_jcomment = sub.add_parser("jira-comment", help="Post a NEW comment on a ticket. Requires --confirmed.")
    p_jcomment.add_argument("--key", required=True)
    p_jcomment.add_argument("--user", default="scotri")
    p_jcomment_body = p_jcomment.add_mutually_exclusive_group(required=True)
    p_jcomment_body.add_argument("--body")
    p_jcomment_body.add_argument("--body-file", dest="body_file",
                                 help="Read the exact, already-approved body from a UTF-8 file under scratch/, proposals/ or investigations/.")
    p_jcomment.add_argument("--wiki", action="store_true",
                            help="Body is Jira wiki markup (formatting + [~accountid:...] mentions).")
    p_jcomment.add_argument("--confirmed", action="store_true")
    p_jcomment.set_defaults(func=cmd_jira_comment)

    p_jedit = sub.add_parser("jira-edit-comment", help="Edit a comment Scotri itself posted. Requires --confirmed.")
    p_jedit.add_argument("--key", required=True)
    p_jedit.add_argument("--comment-id", dest="comment_id", required=True)
    p_jedit.add_argument("--body", required=True)
    p_jedit.add_argument("--user", default="scotri")
    p_jedit.add_argument("--confirmed", action="store_true")
    p_jedit.set_defaults(func=cmd_jira_edit_comment)

    p_jbug = sub.add_parser("jira-create-bug", help="Create a new Bug ticket. Requires --confirmed.")
    p_jbug.add_argument("--project", required=True)
    p_jbug.add_argument("--summary", required=True)
    p_jbug.add_argument("--description", required=True)
    p_jbug.add_argument("--user", default="scotri")
    p_jbug.add_argument("--confirmed", action="store_true")
    p_jbug.set_defaults(func=cmd_jira_create_bug)

    p_jattach = sub.add_parser("jira-attach", help="Attach a file Scotri wrote (or the caller uploaded) to a ticket. Requires --confirmed.")
    p_jattach.add_argument("--key", required=True)
    p_jattach.add_argument("--file", required=True, help="Workspace path under scratch/, proposals/ or investigations/.")
    p_jattach.add_argument("--filename", default=None, help="Name to give it on the ticket (default: the file's own).")
    p_jattach.add_argument("--user", default="scotri")
    p_jattach.add_argument("--confirmed", action="store_true")
    p_jattach.set_defaults(func=cmd_jira_attach)

    p_batch = sub.add_parser(
        "batch",
        help="Run several independent READ commands in one process, in parallel, each still governed. "
             "Input: a JSON array of argv arrays (a file, or - for stdin).",
    )
    p_batch.add_argument("--file", required=True,
                         help='JSON array of argv arrays, e.g. [["mirror-read","--repo-key","databricks","--path","x.py"]]; - reads stdin.')
    p_batch.add_argument("--workers", type=int, default=BATCH_DEFAULT_WORKERS,
                         help=f"Parallel items (default {BATCH_DEFAULT_WORKERS}, max {BATCH_MAX_WORKERS}).")
    p_batch.set_defaults(func=cmd_batch)

    return parser


def main(argv: list[str] | None = None) -> int:
    # Force UTF-8 stdout/stderr regardless of the host console's codepage — several messages
    # (presence-collision notices, "ALLOWED — ...") use an em-dash, and a default Windows console
    # codepage (cp1252/cp437) mangles it into "�" otherwise. Content stays as written; only the
    # encoding changes.
    if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")

    configure_logging(log_file=Path(__file__).resolve().parents[1] / "logs" / "scotri-cli.log")

    parser = build_parser()
    args = parser.parse_args(argv)

    try:
        if args.func is not cmd_batch:  # batch applies both per item itself
            _check_component(args)
            _apply_run_identity(args)
        return args.func(args)
    except ScotriError as e:
        # A safety net for any command that doesn't already catch ScotriError itself — never let
        # one reach the top as a raw traceback.
        _print_error(e.category.value, e.user_message, extra=_governor_extra(e))
        return 1


if __name__ == "__main__":
    sys.exit(main())
