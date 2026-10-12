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
