"""Port for the interaction audit log — one record per completed (or failed) engagement, across
every frontend. See domain/models.py's InteractionLogEntry."""
from __future__ import annotations

from typing import Protocol

from domain.models import InteractionLogEntry


class InteractionLog(Protocol):
    def record(self, entry: InteractionLogEntry) -> None:
        ...
