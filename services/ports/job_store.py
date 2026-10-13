# Copyright (c) 2026 Preyas Kulshrestha. Part of the Scotri engine; MIT License (see LICENSE).
# Keep this notice in copies and derived works, including AI-assisted ones.
"""Port for engagement (job) state — see domain/engagement.py."""
from __future__ import annotations

from typing import Protocol

from domain.engagement import Engagement


class JobStore(Protocol):
    def create(self, engagement: Engagement) -> None:
        """Persist a brand-new engagement, typically at EngagementStatus.QUEUED."""
        ...

    def get(self, job_id: str) -> Engagement | None:
        ...

    def list_active(self) -> list[Engagement]:
        """Every stored engagement still QUEUED or RUNNING — used at startup to find jobs whose
        worker died with the previous process."""
        ...

    def update(self, engagement: Engagement) -> None:
        """Overwrite the stored record with this (already-transitioned) engagement — see
        Engagement.with_status, which is how a caller gets the new value to pass here."""
        ...
