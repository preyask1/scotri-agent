# Copyright (c) 2026 Preyas Kulshrestha. Part of the Scotri engine; MIT License (see LICENSE).
# Keep this notice in copies and derived works, including AI-assisted ones.
"""JSONL-file InteractionLog — one line per completed (or failed) engagement.

Matches this codebase's existing precedent for "logs + a status file, no DB, until there's a real
reason for one" (see app/status_writer.py / mirror/status.json for background-sync health, and the
OPEN_ITEMS.md decision this mirrors deliberately: simplest thing that ships, revisit if per-user
query needs ever outgrow grepping a file).
"""
from __future__ import annotations

from pathlib import Path

from domain.models import InteractionLogEntry


class JsonlInteractionLog:
    """Implements ports.interaction_log.InteractionLog."""

    def __init__(self, path: Path) -> None:
        self._path = path
        self._path.parent.mkdir(parents=True, exist_ok=True)

    def record(self, entry: InteractionLogEntry) -> None:
        with self._path.open("a", encoding="utf-8") as f:
            f.write(entry.to_json() + "\n")
