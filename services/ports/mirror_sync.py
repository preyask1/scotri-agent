"""Port for the mirror's actual fetch/swap mechanics — used only by the sync service, never by a
skill-driven read (agent-instructions.md: 'this agent never writes into mirror-active/ or
mirror-staging/'; only the sync service touches this port)."""
from __future__ import annotations

from collections.abc import Callable
from typing import Protocol


class MirrorSyncTarget(Protocol):
    def fetch_into_staging(self, repo: str, branch: str, org: str, project: str,
                           on_progress: Callable[[str, float], None] | None = None) -> None:
        """git fetch + reset `repo`@`branch` into mirror-staging/, incremental, not a full re-clone.

        `on_progress(stage, percent)`, when given, is called as the fetch moves: stage is one of
        domain.sync_policy.FETCH_STAGES ("receiving", "resolving", "checkout", "indexing") and percent
        is that stage's own 0-100. Only those two values, never git's text."""
        ...

    def swap_to_active(self, repo: str, branch: str) -> None:
        """Atomically move the freshly-synced staging tree into mirror-active/."""
        ...
