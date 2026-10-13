# Copyright (c) 2026 Preyas Kulshrestha. Part of the Scotri engine; MIT License (see LICENSE).
# Keep this notice in copies and derived works, including AI-assisted ones.
"""Port for reading Jira tickets (jira-intake SKILL.md) — read-only, always."""
from __future__ import annotations

from typing import Any, Protocol


class TicketReader(Protocol):
    def get_issue(self, key: str) -> dict[str, Any]:
        """Return the raw issue payload (summary, description, comments, attachments, status, ...)."""
        ...

    def download_attachment(self, attachment_id: str, max_bytes: int) -> bytes:
        """Return one attachment's bytes. Raises PolicyError past `max_bytes`."""
        ...
