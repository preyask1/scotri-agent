# Copyright (c) 2026 Preyas Kulshrestha. Part of the Scotri engine; MIT License (see LICENSE).
# Keep this notice in copies and derived works, including AI-assisted ones.
"""Port for reading repo file content — implemented twice: a mirror (filesystem) adapter for Prod,
and a live ADO-REST adapter for Dev/UAT (fabric-read / adf-databricks-read SKILL.md). Both adapters
present the same shape so callers don't need to know which one they're talking to."""
from __future__ import annotations

from typing import Protocol


class RepoContentReader(Protocol):
    def list_dir(self, repo: str, branch: str, path: str) -> list[str]:
        """List entries under `path` — used to narrow before reading, per every read-skill's
        'list/narrow before reading' rule."""
        ...

    def read_file(self, repo: str, branch: str, path: str) -> str:
        ...
