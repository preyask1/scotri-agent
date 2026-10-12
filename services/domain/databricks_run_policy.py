"""Guardrails for the Databricks job/notebook run-history reader (OPEN_ITEMS.md: the "missing
third leg" between the ADF reader and the Fabric reader, both built 2026-09-27).

Pure decision logic, no SDK call — mirrors domain/adf_run_policy.py exactly, for the same reason:
the adapter and CLI command call these before the real Jobs API request, so the same "an unfiltered
run-history query silently truncates to the newest N runs" gotcha the ADF runbook hit is structurally
impossible to reach here too, not just discouraged in prose.

Unlike the ADF reader, a *job id* filter is NOT mandatory here — the Databricks Jobs API filters by
time window server-side (`start_time_from`/`start_time_to`), so a bounded window is itself a real
filter, which matches the common investigation shape ("what failed last night") where the job id
isn't known up front. The window, however, is always mandatory and always capped, and the page limit
is capped too, so an unbounded "list everything" pull is refused the same way.
"""
from __future__ import annotations

from datetime import datetime, timedelta

from domain.errors import PolicyError

MAX_QUERY_WINDOW = timedelta(days=14)
MAX_LIMIT = 100


def assert_window_within_limit(start: datetime, end: datetime) -> None:
    """Refuse a window wider than MAX_QUERY_WINDOW, and an inverted/empty one outright — identical
    semantics to domain/adf_run_policy.py, deliberately, so the two readers behave the same way."""
    if end <= start:
        raise PolicyError(f"query window is empty or inverted: start={start.isoformat()} end={end.isoformat()}.")
    if end - start > MAX_QUERY_WINDOW:
        raise PolicyError(
            f"query window ({(end - start).days} days) exceeds the {MAX_QUERY_WINDOW.days}-day cap — "
            f"narrow --start/--end. Widening the cap itself is a decision for OPEN_ITEMS.md, not a "
            f"per-call override."
        )


def assert_limit_within_cap(limit: int) -> None:
    """A run-history list with no ceiling is the same silent-truncation gotcha as an unbounded window —
    refuse a non-positive or over-cap limit rather than letting a caller pull an unbounded page set."""
    if limit <= 0:
        raise PolicyError(f"--limit must be positive, got {limit}.")
    if limit > MAX_LIMIT:
        raise PolicyError(
            f"--limit ({limit}) exceeds the {MAX_LIMIT}-run cap. Narrow the window instead, or list "
            f"the specific job with --job-id — a huge unfiltered pull silently truncates anyway."
        )
