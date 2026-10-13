# Copyright (c) 2026 Preyas Kulshrestha. Part of the Scotri engine; MIT License (see LICENSE).
# Keep this notice in copies and derived works, including AI-assisted ones.
"""Port for durable, per-user engagement/conversation history (OPEN_ITEMS.md items #19 and #25).

Neither existing store fits: RedisJobStore holds the full Engagement but expires after 24h and has no
"list by user" query; JsonlInteractionLog is durable and per-Principal but deliberately stores only a
truncated prompt_preview, never the full prompt or result. This is the third store the design agreed
on — durable, keyed by principal.subject, holding full engagement records, and grouped by the
server-issued conversation_id (#25) so a conversation exists server-side, not only in the browser.

Storage choice (recorded in the adapter): a SQLite adapter for local/dev now; on the Azure AKS target
this swaps to a managed networked DB (Azure SQL Database / PostgreSQL Flexible Server), because an
AKS pod's local filesystem is ephemeral and can't be shared across replicas. Same one-adapter-swap
pattern this workspace already uses for Redis and secrets. The port is what keeps that a swap.

Access control is NOT in this port — it belongs at the route (app/api.py filters by the caller's own
validated subject/roles; the frontend is a thin consumer, never trusted to enforce who sees whose
history). This port only stores and returns what it is asked for.

Secret redaction (OPEN_ITEMS.md #19 decision 3 / #34): these records hold the full prompt and result,
which is exactly what the still-open deliverable-redaction item covers. Until that lands, records are
stored as produced; the write boundary here is the single place a redaction hook would attach later.
"""
from __future__ import annotations

from typing import Protocol

from domain.admin import PlatformActivity
from domain.engagement import Engagement
from domain.learning import Learning
from domain.verdict_review import VerdictReview


class EngagementHistory(Protocol):
    def record(self, engagement: Engagement) -> None:
        """Upsert one engagement's full record (called on terminal completion). Idempotent on job_id."""
        ...

    def list_for_subject(self, subject: str, limit: int) -> list[Engagement]:
        """This subject's engagements, most recent first. The caller (route) must already have
        constrained `subject` to the authenticated principal — this method trusts its argument."""
        ...

    def get_conversation(self, conversation_id: str) -> list[Engagement]:
        """Every turn of one conversation, oldest first. Access control (does this caller own the
        conversation, or hold a role that may see it) is enforced by the route, not here."""
        ...

    def get(self, job_id: str) -> Engagement | None:
        ...

    def assign_conversation(self, job_ids: list[str], conversation_id: str) -> list[str]:
        """Put these jobs into `conversation_id`, but only those that have no conversation yet; one
        already in a conversation is never moved. Returns the job ids actually changed. Ownership is
        checked by the caller (domain/engagement_policy.adopt_into_conversation), not here."""
        ...

    def list_recent(self, limit: int, *, subject: str | None = None, status: str | None = None) -> list[Engagement]:
        """Every caller's engagements, most recent first, optionally narrowed to one subject and/or
        status. Cross-user on purpose: only the admin surface (OPEN_ITEMS.md #43) calls this, behind
        its ADMIN_ROLES gate. Never call it from a route a non-admin can reach."""
        ...

    def platform_activity(self) -> list[PlatformActivity]:
        """Per caller platform (the token's azp/appid): engagement count, distinct subjects and the
        latest created_at. Feeds the admin caller-app view (#43)."""
        ...

    def add_review(self, review: VerdictReview) -> None:
        """Append one human verdict review (OPEN_ITEMS.md #47). Append-only: never overwrites, so
        the full who-changed-their-mind trail survives; the latest row is the current outcome.
        Who may call this is decided at the route — this port trusts its argument."""
        ...

    def list_reviews(self, job_id: str) -> list[VerdictReview]:
        """Every review of one engagement, oldest first."""
        ...

    def latest_reviews(self, job_ids: list[str]) -> dict[str, VerdictReview]:
        """Current (latest) review per job_id; a job with no review is simply absent from the map."""
        ...

    def record_learning(self, learning: Learning) -> None:
        """Upsert the team-wide learning extracted from one engagement (OPEN_ITEMS.md #58). Not
        subject-scoped: learnings are readable by every caller, by design."""
        ...

    def list_learnings(self, limit: int, source_subject: str | None = None) -> list[Learning]:
        """Most recent learnings first: across all callers, or only those from runs by
        `source_subject` when given (the "mine" view of GET /v1/learnings, OPEN_ITEMS.md #60(d))."""
        ...

    def get_learning(self, job_id: str) -> Learning | None:
        """The learning one engagement recorded, or None. Access is checked by the caller
        (domain/learning_browse.py), not here."""
        ...

    def learning_verdicts(self, job_ids: list[str]) -> dict[str, str]:
        """job_id -> learning verdict, for the jobs that recorded a learning (#58)."""
        ...
