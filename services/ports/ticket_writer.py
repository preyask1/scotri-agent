"""Port for the narrow, explicit Jira write exception (jira-write SKILL.md) — comment, edit a
comment Scotri itself posted, create a Bug ticket, and attach a file Scotri produced or the caller
uploaded (added 2026-10-05). Deliberately separate from
ports.ticket_reader.TicketReader: reading is unconditional, writing is a distinct, narrower
capability with its own credential and its own confirm-before-submit gate."""
from __future__ import annotations

from typing import Protocol


class TicketWriter(Protocol):
    def add_comment(self, key: str, body: str) -> str:
        """Post a new comment on `key`. Returns the new comment's id (needed to edit it later)."""
        ...

    def add_comment_wiki(self, key: str, body: str) -> str:
        """Post a new comment on `key` whose body is Jira wiki markup. Returns the comment id."""
        ...

    def update_comment(self, key: str, comment_id: str, body: str) -> None:
        """Replace the body of a comment Scotri itself previously posted. Never call this on a
        comment_id you didn't get back from add_comment in this workspace's own history."""
        ...

    def create_bug(self, project: str, summary: str, description: str) -> str:
        """Create a new Bug-type issue. Returns the new issue's key (e.g. 'PROJ-1234')."""
        ...

    def add_attachment(self, key: str, filename: str, data: bytes, content_type: str) -> list[dict]:
        """Attach one file to `key`. Returns [{id, filename, size}]. The caller has already checked
        the source path and redacted text content (domain/jira_attachments.prepare_upload)."""
        ...

    def find_account_id(self, query: str) -> str:
        """Look up a Jira user's accountId by email or display name. A read (GET /user/search,
        no ticket touched), kept on this port because it exists to support mentions in the bodies
        this port writes. Raises if the query matches zero or more-than-one account."""
        ...
