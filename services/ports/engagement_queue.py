"""Port for the engagement admission queue (domain/engagement_queue.py, OPEN_ITEMS.md #32): one FIFO
line of waiting jobs plus the set of running jobs, each running job holding a slot on a lease.
Shared by every process, so it is external state behind a port, like the job store."""
from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from domain.engagement_queue import Admission, QueueSettings


@dataclass(frozen=True)
class WaitingEntry:
    job_id: str
    subject: str
    enqueued_at: float
    """Unix seconds."""


@dataclass(frozen=True)
class RunningEntry:
    job_id: str
    lease_expires_at: float
    """Unix seconds."""


@dataclass(frozen=True)
class QueueSnapshot:
    waiting: list[WaitingEntry]
    """In line order, first to start first."""
    running: list[RunningEntry]
    """Live leases only."""


class EngagementQueue(Protocol):
    def admit(self, job_id: str, subject: str, settings: "QueueSettings") -> "Admission":
        """Atomically: take a slot if one is free and nobody waits (START), else join the line
        (QUEUED with a position), unless the line is full or `subject` already has the most
        waiting jobs allowed."""
        ...

    def claim_next(self, settings: "QueueSettings") -> str | None:
        """Atomically move the first waiting job into a free slot and return it, or None when no
        slot is free or nobody waits."""
        ...

    def release(self, job_id: str) -> None:
        """Free the slot a finished run held. No-op if it held none."""
        ...

    def renew(self, job_ids: list[str], settings: "QueueSettings") -> None:
        """Extend the leases of runs this process is still executing."""
        ...

    def reap_expired(self) -> list[str]:
        """Remove and return running jobs whose lease lapsed: the process running them died."""
        ...

    def remove_waiting(self, job_id: str) -> bool:
        """Take a job out of the line. True if it was waiting."""
        ...

    def waiting_older_than(self, seconds: int) -> list[str]:
        ...

    def position(self, job_id: str) -> int | None:
        """1-based place in line, or None if not waiting."""
        ...

    def holds_slot(self, job_id: str) -> bool:
        """True while the job holds a live lease."""
        ...

    def snapshot(self) -> QueueSnapshot:
        ...
