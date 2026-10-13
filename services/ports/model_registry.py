# Copyright (c) 2026 Preyas Kulshrestha. Part of the Scotri engine; MIT License (see LICENSE).
# Keep this notice in copies and derived works, including AI-assisted ones.
"""Port for the model registry (docs/ai/model-registry.md) — which LLM vendor/model a given
lifecycle stage should use. Kept as a port (not a plain domain function) for the same reason as
RepoRegistry: it's external, changing state — a config file meant to be edited as multi-vendor/
multi-model routing is actually introduced, not a fixed business rule.

app/container.py resolves the engagement model through it (`get("default")`). Since 2026-10-07
(OPEN_ITEMS.md #4) every stage uses one model, Claude Sonnet 5.5; per-stage entries remain possible
but none are configured."""
from __future__ import annotations

from typing import Protocol

from domain.models import ModelRef


class ModelRegistry(Protocol):
    def get(self, stage_key: str) -> ModelRef:
        """The vendor/model assignment for `stage_key` (e.g. 'jira-intake', 'bug-investigation')."""
        ...

    def all_stage_keys(self) -> list[str]:
        ...
