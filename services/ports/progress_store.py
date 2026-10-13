# Copyright (c) 2026 Preyas Kulshrestha. Part of the Scotri engine; MIT License (see LICENSE).
# Keep this notice in copies and derived works, including AI-assisted ones.
"""Port for live engagement progress (domain/engagement_progress.py, OPEN_ITEMS.md #39).

Kept apart from ports/job_store.py on purpose: progress is written many times a minute from the
run's own thread, while the job record's status is written by cancel_engagement and by
run_engagement's final transition. Separate keys mean a progress write can never overwrite a
CANCELLED or a final status with a stale copy of the record.
"""
from __future__ import annotations

from typing import Protocol

from domain.engagement_progress import EngagementProgress


class ProgressStore(Protocol):
    def put(self, progress: EngagementProgress) -> None:
        ...

    def get(self, job_id: str) -> EngagementProgress | None:
        ...
