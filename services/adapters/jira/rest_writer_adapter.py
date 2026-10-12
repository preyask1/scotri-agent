"""Jira REST write adapter (jira-write SKILL.md) — comment, edit-own-comment, create-Bug and attach a
file ONLY (attach added 2026-10-05, OPEN_ITEMS.md #42).

Uses the SAME single Jira account as JiraRestTicketReader (JIRA_EMAIL / JIRA_API_TOKEN) — this
deployment has one account for both reading and writing. There is therefore no platform-side
permission backstop: the only thing keeping writes to comment / edit-own-comment / create-Bug is the
application-side confirm-before-submit gate enforced in app/cli.py (and, for attach, the source-path
policy in domain/jira_attachments.py) (plus the audit line from
run_governed_write), not anything in this adapter or on the Jira account.

Jira REST API v3 requires comment/description bodies in Atlassian Document Format (ADF), not plain
text — `markdown_to_adf` (adapters/jira/markdown_adf.py) converts the markdown a skill actually
drafts (headings, lists, code blocks, bold/italic, links, `@[Name]` mentions) into the matching ADF
node tree, so the formatting the user approved in chat is the formatting that lands on the ticket.

Also uses a scoped token routed through the Atlassian API gateway (cloudId-addressed) — see
adapters/jira/api_base.py.
"""
from __future__ import annotations

from typing import Any

import requests

from adapters.jira.api_base import resolve_api_base
from adapters.jira.markdown_adf import markdown_to_adf
from domain.errors import ErrorCategory, NotFoundError, ScotriError
from ports.secret_provider import SecretProvider


class JiraRestTicketWriter:
    """Implements ports.ticket_writer.TicketWriter."""

    def __init__(self, secrets: SecretProvider) -> None:
        self._email = secrets.get("JIRA_EMAIL")
        self._token = secrets.get("JIRA_API_TOKEN")
        self._base_url = resolve_api_base(secrets)

    def _auth(self) -> tuple[str, str]:
        return (self._email, self._token)

    def _body_to_adf(self, body: str) -> dict[str, Any]:
        return markdown_to_adf(body, mention_resolver=self.find_account_id_or_none)

    def find_account_id(self, query: str) -> str:
        """Look up a Jira user's accountId by email or display name (GET /user/search — a read,
        not a write; it doesn't touch any ticket). Needed because ADF mention nodes require an
        accountId, never a plain name — see markdown_adf.py's `@[Name]` mention syntax.

        Raises NotFoundError if nothing matches, and ScotriError(POLICY) if the query is
        ambiguous (matches more than one account and none of them is an exact email/display-name
        match) — never silently guesses one account out of several.
        """
        resp = requests.get(
            f"{self._base_url}/rest/api/3/user/search",
            params={"query": query, "maxResults": 5},
            auth=self._auth(),
            timeout=20,
        )
        resp.raise_for_status()
        users = resp.json()

        if not users:
            raise NotFoundError(f"no Jira user found matching {query!r}")
        if len(users) == 1:
            return users[0]["accountId"]

        exact = [
            u for u in users
            if query.lower() == (u.get("emailAddress") or "").lower()
            or query.lower() == (u.get("displayName") or "").lower()
        ]
        if len(exact) == 1:
            return exact[0]["accountId"]
        raise ScotriError(
            f"ambiguous match for {query!r}: {len(users)} Jira users found — narrow the query "
            "(e.g. the exact email address) and try again",
            category=ErrorCategory.POLICY,
        )

    def find_account_id_or_none(self, query: str) -> str | None:
        """Same as find_account_id, but degrades to None instead of raising — used as the
        mention_resolver passed into markdown_to_adf, so one unresolvable `@[Name]` in a long
        comment falls back to plain text instead of failing the whole post."""
        try:
            return self.find_account_id(query)
        except ScotriError:
            return None

    def add_comment(self, key: str, body: str) -> str:
        resp = requests.post(
            f"{self._base_url}/rest/api/3/issue/{key}/comment",
            json={"body": self._body_to_adf(body)},
            auth=self._auth(),
            timeout=20,
        )
        resp.raise_for_status()
        return resp.json()["id"]

    def add_comment_wiki(self, key: str, body: str) -> str:
        """Post a comment whose body is Jira wiki markup (headings, bold, lists, and real
        mentions via [~accountid:<id>]). Uses REST v2, which accepts wiki-markup string bodies;
        v3 requires ADF. Same credential, same permission (Add Comments) as add_comment."""
        resp = requests.post(
            f"{self._base_url}/rest/api/2/issue/{key}/comment",
            json={"body": body},
            auth=self._auth(),
            timeout=20,
        )
        resp.raise_for_status()
        return resp.json()["id"]

    def update_comment(self, key: str, comment_id: str, body: str) -> None:
        resp = requests.put(
            f"{self._base_url}/rest/api/3/issue/{key}/comment/{comment_id}",
            json={"body": self._body_to_adf(body)},
            auth=self._auth(),
            timeout=20,
        )
        resp.raise_for_status()

    def create_bug(self, project: str, summary: str, description: str) -> str:
        resp = requests.post(
            f"{self._base_url}/rest/api/3/issue",
            json={
                "fields": {
                    "project": {"key": project},
                    "summary": summary,
                    "description": self._body_to_adf(description),
                    "issuetype": {"name": "Bug"},
                }
            },
            auth=self._auth(),
            timeout=20,
        )
        resp.raise_for_status()
        return resp.json()["key"]

    def add_attachment(self, key: str, filename: str, data: bytes, content_type: str) -> list[dict[str, Any]]:
        """Attach one file to `key` (POST /issue/{key}/attachments, multipart). Jira refuses the call
        without `X-Atlassian-Token: no-check` (its XSRF guard). Needs the "Create Attachments"
        project permission on the account. Returns [{id, filename, size}] for what Jira stored."""
        resp = requests.post(
            f"{self._base_url}/rest/api/3/issue/{key}/attachments",
            files={"file": (filename, data, content_type)},
            headers={"X-Atlassian-Token": "no-check"},
            auth=self._auth(),
            timeout=120,
        )
        resp.raise_for_status()
        return [{"id": a.get("id"), "filename": a.get("filename"), "size": a.get("size")} for a in resp.json()]
