# Copyright (c) 2026 Preyas Kulshrestha. Part of the Scotri engine; MIT License (see LICENSE).
# Keep this notice in copies and derived works, including AI-assisted ones.
"""Port for the repo registry (docs/ai/client/repo-registry.md) — the single source of truth for which
repos/branches exist and how each is read. Kept as a port (not a plain domain function) because it's
external, changing state: a config file that's actively edited as the platform migrates, not a fixed
business rule."""
from __future__ import annotations

from typing import Protocol

from domain.models import RepoRef


class RepoRegistry(Protocol):
    def get(self, repo_key: str) -> list[RepoRef]:
        """All known environment variants (Prod/Dev/UAT) registered for `repo_key`."""
        ...

    def all_repo_keys(self) -> list[str]:
        ...
