"""Jira REST adapter (jira-intake SKILL.md) — plain REST + Basic auth, read-only, no OAuth MCP.

Uses a scoped API token, routed through the Atlassian API gateway (cloudId-addressed) rather than
the site's own domain — see adapters/jira/api_base.py for why the two token kinds need different
URLs.
"""
from __future__ import annotations

from typing import Any

import requests

from adapters.jira.api_base import resolve_api_base
from domain.errors import PolicyError
from ports.secret_provider import SecretProvider

_FIELDS = "summary,description,comment,attachment,issuetype,status,priority,labels"


class JiraRestTicketReader:
    """Implements ports.ticket_reader.TicketReader."""

    def __init__(self, secrets: SecretProvider) -> None:
        self._email = secrets.get("JIRA_EMAIL")
        self._token = secrets.get("JIRA_API_TOKEN")
        self._base_url = resolve_api_base(secrets)

    def get_issue(self, key: str) -> dict[str, Any]:
        resp = requests.get(
            f"{self._base_url}/rest/api/3/issue/{key}",
            params={"fields": _FIELDS},
            auth=(self._email, self._token),
            timeout=20,
        )
        resp.raise_for_status()
        return resp.json()

    def download_attachment(self, attachment_id: str, max_bytes: int) -> bytes:
        """Bytes of one attachment (GET /attachment/content/{id}, a read). Jira answers with a redirect
        to its media store; `requests` follows it and drops the Basic-auth header on the cross-host
        hop, which is right: the redirect URL is pre-signed. Streamed, and refused past `max_bytes`
        so a huge file can't fill the disk or memory before the size check."""
        with requests.get(
            f"{self._base_url}/rest/api/3/attachment/content/{attachment_id}",
            auth=(self._email, self._token),
            timeout=60,
            stream=True,
        ) as resp:
            resp.raise_for_status()
            chunks, total = [], 0
            for chunk in resp.iter_content(chunk_size=64 * 1024):
                total += len(chunk)
                if total > max_bytes:
                    raise PolicyError(f"attachment {attachment_id} is larger than {max_bytes} bytes")
                chunks.append(chunk)
        return b"".join(chunks)
