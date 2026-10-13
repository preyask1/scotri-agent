# Copyright (c) 2026 Preyas Kulshrestha. Part of the Scotri engine; MIT License (see LICENSE).
# Keep this notice in copies and derived works, including AI-assisted ones.
"""Lexical (BM25) retriever adapter (OPEN_ITEMS.md item #38) — implements ports.retrieval.Retriever.

Assembles a corpus on demand from three sources and ranks it with domain.retrieval (pure BM25):
- "learnings": team-wide distilled learnings from every caller's past investigations, including
  service-triggered runs (OPEN_ITEMS.md #58) — how knowledge adds up across the team.
- "past-engagements": the durable engagement history (ports.engagement_history), scoped to ONE
  subject — a caller retrieves over its own past work by default. Team-wide retrieval is deliberately
  not done here: it needs the still-open "relevant users" access decision (OPEN_ITEMS.md #19 dec. 1),
  and reading everyone's full results by default would pre-empt that decision.
- "docs": the committed docs/ai/ corpus (*.md, *.yaml) — non-sensitive, so global, no subject.
  Markdown is indexed one heading section at a time (domain.retrieval.split_markdown); each hit
  carries `meta.lines` so the reader opens that range rather than the whole file.

No credential, no external call, no governor — this is a local rank over already-accessible content.
When Azure AI Foundry is available, a hybrid embeddings adapter replaces this behind the same port.

This adapter builds the corpus per query rather than maintaining an index. At this corpus size that is
fine; a persistent index (or the lineage graph's SQLite file) is the optimisation if it ever isn't.
"""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from domain.errors import PolicyError
from domain.retrieval import Document, RetrievalHit, rank, split_markdown
from domain.verdict_review import UNREVIEWED, apply_review_weights, is_retrievable
from ports.engagement_history import EngagementHistory

_DOC_GLOBS = ("*.md", "*.yaml", "*.yml")
_HISTORY_CORPUS_CAP = 500  # most-recent engagements to consider — a bound, not a result limit


class LexicalRetriever:
    def __init__(self, engagement_history: EngagementHistory, docs_root: Path) -> None:
        self._history = engagement_history
        self._docs_root = docs_root

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
        documents: list[Document] = []
        if "past-engagements" in scopes:
            if not subject:
                raise PolicyError(
                    "retrieving 'past-engagements' requires a subject (--user) — a caller retrieves "
                    "over its own history only; team-wide retrieval is a separate, still-open access "
                    "decision (OPEN_ITEMS.md #19)."
                )
            documents.extend(self._engagement_docs(subject, include_unsuccessful, include_overturned))
        if "learnings" in scopes:
            documents.extend(self._learning_docs(include_overturned))
        if "docs" in scopes:
            documents.extend(self._doc_docs())
        # Rank the whole eligible set, then re-weight by review and cut — cutting first would let a
        # review never surface a slightly-lower-scoring upheld result.
        return apply_review_weights(rank(query, documents, len(documents)), limit)

    def _engagement_docs(
        self, subject: str, include_unsuccessful: bool, include_overturned: bool
    ) -> list[Document]:
        engagements = self._history.list_for_subject(subject, _HISTORY_CORPUS_CAP)
        reviews = self._history.latest_reviews([e.job_id for e in engagements])
        now = datetime.now(timezone.utc)
        docs: list[Document] = []
        for e in engagements:
            review = reviews.get(e.job_id)
            outcome = review.outcome.value if review else UNREVIEWED
            if not is_retrievable(e.status.value, outcome,
                                  include_unsuccessful=include_unsuccessful,
                                  include_overturned=include_overturned):
                continue
            created = e.created_at if e.created_at.tzinfo else e.created_at.replace(tzinfo=timezone.utc)
            text = e.prompt + "\n\n" + (e.result or "")
            meta = {"job_id": e.job_id, "conversation_id": e.conversation_id,
                    "status": e.status.value, "created_at": e.created_at.isoformat(),
                    "age_days": (now - created).days, "review_outcome": outcome}
            if review:
                meta.update({"reviewed_at": review.reviewed_at.isoformat(),
                             "review_reason": review.reason,
                             "superseded_by_job_id": review.superseded_by_job_id})
            docs.append(Document(
                id=f"engagement:{e.job_id}",
                scope="past-engagements",
                title=f"{e.status.value} · {outcome} · {e.created_at.isoformat()} · {e.job_id}",
                text=text,
                meta=meta,
            ))
        return docs

    def _learning_docs(self, include_overturned: bool) -> list[Document]:
        """Team-wide: every caller's learnings, whoever (or whatever service) ran the investigation.
        Only learnings, never full results, cross users (domain/learning.py explains why)."""
        learnings = self._history.list_learnings(_HISTORY_CORPUS_CAP)
        reviews = self._history.latest_reviews([lr.job_id for lr in learnings])
        now = datetime.now(timezone.utc)
        docs: list[Document] = []
        for lr in learnings:
            review = reviews.get(lr.job_id)
            outcome = review.outcome.value if review else UNREVIEWED
            if not is_retrievable("succeeded", outcome, include_unsuccessful=False,
                                  include_overturned=include_overturned):
                continue
            created = lr.created_at if lr.created_at.tzinfo else lr.created_at.replace(tzinfo=timezone.utc)
            meta = {"job_id": lr.job_id, "ticket_key": lr.ticket_key, "verdict": lr.verdict,
                    "root_cause_category": lr.root_cause_category, "confidence": lr.confidence,
                    "takeaways": lr.takeaways, "entities": lr.entities,
                    "investigated_by": lr.source_display or lr.source_subject,
                    "created_at": lr.created_at.isoformat(), "age_days": (now - created).days,
                    "review_outcome": outcome}
            if review:
                meta.update({"review_reason": review.reason,
                             "superseded_by_job_id": review.superseded_by_job_id})
            docs.append(Document(
                id=f"learning:{lr.job_id}", scope="learnings",
                title=f"{lr.verdict} · {outcome} · {lr.title}", text=lr.search_text(), meta=meta,
            ))
        return docs

    def _doc_docs(self) -> list[Document]:
        docs: list[Document] = []
        if not self._docs_root.exists():
            return docs
        seen: set[Path] = set()
        for pattern in _DOC_GLOBS:
            for path in sorted(self._docs_root.rglob(pattern)):
                if path in seen or not path.is_file():
                    continue
                seen.add(path)
                try:
                    text = path.read_text(encoding="utf-8")
                except (OSError, UnicodeDecodeError):
                    continue
                rel = path.relative_to(self._docs_root).as_posix()
                if path.suffix != ".md":
                    docs.append(Document(
                        id=f"docs:{rel}", scope="docs", title=rel, text=text, meta={"path": str(path)},
                    ))
                    continue
                # One Document per heading section: a hit names the section and its line range, so
                # the reader opens those lines, not the whole file.
                for s in split_markdown(text):
                    docs.append(Document(
                        id=f"docs:{rel}#L{s.start_line}", scope="docs",
                        title=f"{rel} › {s.heading}" if s.heading else rel, text=s.text,
                        meta={"path": str(path), "section": s.heading,
                              "lines": f"{s.start_line}-{s.end_line}"},
                    ))
        return docs
