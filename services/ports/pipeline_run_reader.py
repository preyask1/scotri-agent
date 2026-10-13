# Copyright (c) 2026 Preyas Kulshrestha. Part of the Scotri engine; MIT License (see LICENSE).
# Keep this notice in copies and derived works, including AI-assisted ones.
"""Port for ADF pipeline/activity run-history reads (adf-databricks-read SKILL.md's "ADF pipeline
failures" section; OPEN_ITEMS.md: decided 2026-09-27). Read-only by construction: both methods map
to ARM's query-style GET/POST endpoints, never a create/cancel/rerun operation.
"""
from __future__ import annotations

from datetime import datetime
from typing import Protocol

from domain.models import AdfFactoryRef


class PipelineRunReader(Protocol):
    def query_pipeline_runs(
        self, factory: AdfFactoryRef, pipeline_names: list[str], start: datetime, end: datetime
    ) -> list[dict]:
        """Runs for the named pipeline(s) in [start, end). `pipeline_names` must be non-empty —
        domain.adf_run_policy.assert_pipeline_names_given enforces this before the call is made."""
        ...

    def query_activity_runs(
        self, factory: AdfFactoryRef, run_id: str, start: datetime, end: datetime
    ) -> list[dict]:
        """Per-activity status/error/retry detail for one pipeline run (a runId from
        query_pipeline_runs). `start`/`end` bound the activity-run query the same way they bound
        the pipeline-run query — ARM requires a window on this endpoint too."""
        ...

    def query_trigger_runs(
        self, factory: AdfFactoryRef, trigger_names: list[str], start: datetime, end: datetime
    ) -> list[dict]:
        """Runs of the named trigger(s) in [start, end) — did the trigger actually fire, and with
        what status. Answers the case query_pipeline_runs can't distinguish: zero pipeline runs
        because a trigger was disabled/paused/failed to fire, vs. because the pipeline itself didn't
        run. `trigger_names` must be non-empty — domain.adf_run_policy.assert_trigger_names_given
        enforces this. Note an *empty* result is still two-valued: a disabled trigger and one that
        simply didn't fire both produce no runs — pair this with the trigger's `enabled` state read
        from the mirror (adf-databricks-read)."""
        ...
