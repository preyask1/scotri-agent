"""Guardrails for the ADF pipeline/activity run-history reader (OPEN_ITEMS.md: decided 2026-09-27).

Pure decision logic, no HTTP — mirrors the shape of domain/query_guard.py and domain/governor_policy.py:
the adapter and CLI command call these before making the real ARM request, so the runbook's own
documented gotcha ("the unfiltered default query only returns the newest ~100 runs across the whole
factory" — see an earlier investigation runbook) is structurally impossible to hit here, not
just discouraged in prose.
"""
from __future__ import annotations

from datetime import datetime, timedelta

from domain.errors import PolicyError

MAX_QUERY_WINDOW = timedelta(days=14)


def assert_pipeline_names_given(pipeline_names: list[str]) -> None:
    """A run-history query with no pipeline-name filter is exactly the gotcha an earlier investigation runbook hit —
    refuse it here rather than letting a caller "just this once" query the whole factory."""
    if not pipeline_names:
        raise PolicyError(
            "pipeline-runs requires at least one --pipeline name — an unfiltered query only "
            "returns the newest ~100 runs across the whole factory, which silently drops older "
            "history in a busy factory. List the candidate pipeline(s) from the mirror first "
            "(adf-databricks-read) and pass their names explicitly."
        )


def assert_trigger_names_given(trigger_names: list[str]) -> None:
    """A trigger-run query with no trigger-name filter hits the same ~100-run truncation gotcha as an
    unfiltered pipeline-run query — refuse it. Read the candidate trigger name(s) from the mirror
    first (adf-databricks-read reads trigger JSON) and pass them explicitly."""
    if not trigger_names:
        raise PolicyError(
            "trigger-runs requires at least one --trigger name — an unfiltered query only returns "
            "the newest ~100 runs across the whole factory, silently dropping older history. Read "
            "the trigger name(s) from the mirror first (adf-databricks-read) and pass them explicitly."
        )


def assert_window_within_limit(start: datetime, end: datetime) -> None:
    """Refuse a query window wider than MAX_QUERY_WINDOW, and refuse an inverted one outright —
    both are almost always a caller mistake, not a real want, and ARM will otherwise happily return
    an enormous or empty result set without complaint either way."""
    if end <= start:
        raise PolicyError(f"query window is empty or inverted: start={start.isoformat()} end={end.isoformat()}.")
    if end - start > MAX_QUERY_WINDOW:
        raise PolicyError(
            f"query window ({(end - start).days} days) exceeds the {MAX_QUERY_WINDOW.days}-day cap — "
            f"narrow --start/--end. Widening the cap itself is a decision for OPEN_ITEMS.md, not a "
            f"per-call override."
        )
