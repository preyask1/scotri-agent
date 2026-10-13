# Copyright (c) 2026 Preyas Kulshrestha. Part of the Scotri engine; MIT License (see LICENSE).
# Keep this notice in copies and derived works, including AI-assisted ones.
"""Port for the mirror's maintenance flag (agent-instructions.md, fabric-read/adf-databricks-read
SKILL.md) — gates reads during the atomic-swap window."""
from __future__ import annotations

from typing import Protocol

from domain.models import MaintenanceStatus


class MaintenanceFlag(Protocol):
    def status(self, repo: str) -> MaintenanceStatus:
        ...

    def set(self, repo: str, eta_seconds: int) -> None:
        ...

    def clear(self, repo: str) -> None:
        ...
