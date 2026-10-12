"""Port for retrieval over past engagements and docs/ai/ (OPEN_ITEMS.md item #38).

The query surface for "has this been investigated before, and what was concluded?" and for surfacing
relevant docs — a ranker, never a candidate generator. The design note's hard rule holds at every
call site: retrieval only *orders* an already-complete set; it is never what decides *which* things
impact-analysis considers (docs/ai/retrieval-and-graph-design.md §2).

One adapter today (lexical BM25, no credential/outbound call). A hybrid embeddings adapter
(Foundry-hosted, in-tenant, once Azure AI Foundry is stood up) is a later swap behind this same port.
"""
from __future__ import annotations

from typing import Protocol

from domain.retrieval import RetrievalHit


class Retriever(Protocol):
    def search(
        self,
        query: str,
        scopes: list[str],
        subject: str | None,
        limit: int,
        *,
        include_unsuccessful: bool = False,
        include_overturned: bool = False,
    ) -> list[RetrievalHit]:
        """Rank content in the requested `scopes` against `query`.

        Past engagements are filtered and weighted by human verdict review (OPEN_ITEMS.md #47): by
        default only SUCCEEDED runs whose verdict was not overturned are eligible, and every
        past-engagement hit carries `meta.review_outcome` ("unreviewed" when no human has judged it)
        so a caller treats an unreviewed hit as a lead to re-verify, not as an established fact.

        Scopes: "learnings" (team-wide, every caller's distilled learnings, OPEN_ITEMS.md #58),
        "past-engagements" (requires `subject`: the caller's own FULL results only,
        since full results can hold data rows other users aren't entitled to) and "docs" (docs/ai/,
        non-sensitive, no subject needed)."""
        ...
