"""Who may browse stored learnings, and how (OPEN_ITEMS.md #60(d)).

Learnings are team-wide *for the agent*: every engagement's `retrieve` searches all of them, so the
next investigation benefits whoever ran the last one (#58). Browsing them as a person is a different
question, decided 2026-10-06: not everyone should be able to page through everything the team has
investigated. So, fail closed, like every other gate in domain/access_policy.py:

- scope "mine" (the default): learnings from runs you started. Always allowed.
- scope "team": every learning, including ones from service/webhook runs that have no human owner.
  Only for a role in LEARNINGS_READER_ROLES, which is empty by default, so nobody until an admin
  names a role.
- One learning by job_id: its owner, or a LEARNINGS_READER_ROLES holder.

What the agent retrieves is unchanged by this. Learnings carry no data values by design
(domain/learning.py), so this gates who sees what the team investigated, not query results.
"""
from __future__ import annotations

from collections.abc import Iterable
from datetime import timezone

from domain.access_policy import has_any_role, require_owner_or_role
from domain.errors import NotFoundError, PolicyError
from domain.learning import VERDICTS, Learning
from domain.models import Principal
from domain.retrieval import Document, rank
from domain.verdict_review import UNREVIEWED

SCOPES = ("mine", "team")
CANDIDATE_CAP = 500  # most-recent learnings considered for a search; a bound, not a result limit
MAX_LIMIT = 200


def learning_dict(lr: Learning, review_outcome: str) -> dict:
    created = lr.created_at if lr.created_at.tzinfo else lr.created_at.replace(tzinfo=timezone.utc)
    return {
        "job_id": lr.job_id, "ticket_key": lr.ticket_key, "title": lr.title, "verdict": lr.verdict,
        "symptom": lr.symptom, "checked": lr.checked, "root_cause_category": lr.root_cause_category,
        "root_cause": lr.root_cause, "confidence": lr.confidence, "takeaways": lr.takeaways,
        "entities": lr.entities, "investigated_by": lr.source_display or lr.source_subject,
        "created_at": created.isoformat(), "review_outcome": review_outcome,
    }


def browse_learnings(
    history, principal: Principal, reader_roles: Iterable[str], *,
    scope: str = "mine", query: str = "", verdict: str | None = None, limit: int = 50,
) -> list[dict]:
    """Newest first, or best match first when `query` is given (BM25, as `retrieve` ranks them)."""
    if scope not in SCOPES:
        raise PolicyError(f"scope must be one of: {', '.join(SCOPES)}")
    if verdict is not None and verdict not in VERDICTS:
        raise PolicyError(f"verdict must be one of: {', '.join(sorted(VERDICTS))}")
    if scope == "team" and not has_any_role(principal, reader_roles):
        raise PolicyError("browsing the team's learnings needs a role in LEARNINGS_READER_ROLES; "
                          "use scope=mine for your own")
    limit = max(1, min(limit, MAX_LIMIT))

    owner = principal.subject if scope == "mine" else None
    filtered = bool(query.strip() or verdict)
    learnings = history.list_learnings(CANDIDATE_CAP if filtered else limit, source_subject=owner)
    if verdict:
        learnings = [lr for lr in learnings if lr.verdict == verdict]
    if query.strip():
        by_id = {lr.job_id: lr for lr in learnings}
        docs = [Document(id=lr.job_id, scope="learnings", title=lr.title, text=lr.search_text())
                for lr in learnings]
        learnings = [by_id[hit.id] for hit in rank(query, docs, limit)]
    learnings = learnings[:limit]

    reviews = history.latest_reviews([lr.job_id for lr in learnings])
    return [learning_dict(lr, reviews[lr.job_id].outcome.value if lr.job_id in reviews else UNREVIEWED)
            for lr in learnings]


def get_learning(history, principal: Principal, reader_roles: Iterable[str], job_id: str) -> dict:
    """One learning: its owner, or a LEARNINGS_READER_ROLES holder. A missing id is 404 and someone
    else's is 403, matching the other owner-gated routes (job ids are unguessable uuid4 hex)."""
    lr = history.get_learning(job_id)
    if lr is None:
        raise NotFoundError(f"No learning for job_id {job_id}")
    require_owner_or_role(principal, lr.source_subject, reader_roles, resource=f"learning {job_id}")
    reviews = history.latest_reviews([job_id])
    return learning_dict(lr, reviews[job_id].outcome.value if job_id in reviews else UNREVIEWED)
