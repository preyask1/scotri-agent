# Copyright (c) 2026 Preyas Kulshrestha. Part of the Scotri engine; MIT License (see LICENSE).
# Keep this notice in copies and derived works, including AI-assisted ones.
"""Port for Databricks job/notebook run-history reads — the "missing third leg" between the ADF
run-history reader (ports/pipeline_run_reader.py) and the Fabric refresh-history reader
(ports/semantic_model.py's refresh methods). OPEN_ITEMS.md, item #30.

Read-only by construction: every method maps to a Jobs API 2.1 *read* endpoint
(`jobs/runs/list`, `jobs/runs/get`, `jobs/runs/get-output`). No trigger/cancel/rerun/repair
operation exists here, matching the SELECT-only / query-only posture everywhere else Scotri touches
Databricks (agent-instructions.md).

Prod workspace only for now — the single configured connection (DATABRICKS_HOST), matching the ADF
reader's Prod-factory-only decision. The separate second workspace (<secondary workspace host>) is a
still-pending access request tracked in OPEN_ITEMS.md decision 4 and is deliberately NOT reachable
from here.
"""
from __future__ import annotations

from datetime import datetime
from typing import Protocol


class DatabricksRunReader(Protocol):
    def list_runs(
        self, job_id: int | None, start: datetime, end: datetime, limit: int
    ) -> list[dict]:
        """Recent job runs in [start, end), most recent first. `job_id` optionally narrows to one
        job; the window and limit are mandatory and bounded (domain.databricks_run_policy) so an
        unfiltered "list everything" pull can't silently truncate. Each element is the SDK's own
        `BaseRun.as_dict()` — run_id, state (life_cycle_state/result_state), timings, task list."""
        ...

    def get_run(self, run_id: int) -> dict:
        """Full detail for one run (a run_id from list_runs), including per-task state and the
        cluster/notebook each task ran on. The SDK's `Run.as_dict()`."""
        ...

    def get_run_output(self, run_id: int) -> dict:
        """Output/error detail for a single *task* run (a task-level run_id, not the parent job
        run_id) — `error`, `error_trace`, `logs`, `notebook_output`. The SDK's `RunOutput.as_dict()`.
        This is where a notebook's actual failure text/stack trace lives, the depth ADF's relayed
        `error.message` (activity-runs) doesn't reach — see OPEN_ITEMS.md decision 4."""
        ...
