"""Port for presence-claim state (presence-claim SKILL.md)."""
from __future__ import annotations

from typing import Protocol

from domain.models import Claim


class PresenceStore(Protocol):
    def get_claim(self, entity: str) -> Claim | None:
        ...

    def set_claim(self, entity: str, owner: str) -> None:
        """Create or overwrite the claim on `entity`, stamping started_at/last_heartbeat = now."""
        ...

    def heartbeat(self, entity: str, owner: str) -> None:
        """Refresh last_heartbeat for an existing claim this `owner` holds on `entity`."""
        ...

    def release_claim(self, entity: str, owner: str) -> None:
        """Explicitly release a claim when the work concludes, rather than waiting out the TTL."""
        ...

    def track_run_claim(self, job_id: str, entity: str) -> None:
        """Remember that engagement `job_id` registered a claim on `entity`, so the engine can release
        it when the run ends even if the agent forgets to."""
        ...

    def run_claims(self, job_id: str) -> list[str]:
        """Entities engagement `job_id` registered claims on (see track_run_claim)."""
        ...

    def forget_run(self, job_id: str) -> None:
        ...
