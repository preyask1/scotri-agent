"""Human verdict review — "did this investigation's conclusion hold up?" (OPEN_ITEMS.md #47).

The only ground-truth signal in the workspace: every log records how a run *ended*, never whether its
verdict was *right*, so a confident wrong verdict is indistinguishable from a correct one without this.
It feeds two things: retrieval (overturned results stop being served as knowledge) and the log-review
loop (an accuracy figure per outcome, always reported with its review coverage).

Invariants, enforced at the boundaries that call this module:
- **Human-set only.** Written solely through an authenticated API route; no CLI command and no MCP
  tool writes one, so Scotri can never grade its own work, and ticket text can never flip a value.
- **Unreviewed is not upheld.** Absence of a review is its own state and is never weighted as a pass.
- **Append-only.** A re-review adds a row; the latest row is the current outcome, earlier ones stay
  as the audit trail of who changed their mind and why.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import Enum

from domain.engagement import EngagementStatus
from domain.errors import PolicyError
from domain.retrieval import RetrievalHit


class ReviewOutcome(str, Enum):
    UPHELD = "upheld"
    PARTIALLY_UPHELD = "partially_upheld"
    OVERTURNED = "overturned"


UNREVIEWED = "unreviewed"
"""Reported value when no review row exists. Deliberately not a ReviewOutcome member: a human can
never *submit* "unreviewed", it is only ever the absence of a submission."""

REASON_MAX_CHARS = 2000

_REASON_REQUIRED = {ReviewOutcome.PARTIALLY_UPHELD, ReviewOutcome.OVERTURNED}

REVIEWABLE_STATUSES = {EngagementStatus.SUCCEEDED}
"""Only a finished run produced a verdict to judge. Failed/cancelled/incomplete runs are already
excluded from retrieval by status, so reviewing them would grade nothing."""

REVIEWABLE_VERDICTS = {"confirmed_bug", "not_a_bug", "inconclusive", "story"}
"""A run is reviewable only if it reached a conclusion that can later prove right or wrong: a bug
verdict, or a story's technical design. The engine knows this from the run's learning (#58), so it
is decided server-side, never guessed from the text. Plain chat and "question" learnings have
nothing to uphold or overturn."""


def is_reviewable(status: EngagementStatus, learning_verdict: str | None) -> bool:
    return status in REVIEWABLE_STATUSES and learning_verdict in REVIEWABLE_VERDICTS


@dataclass(frozen=True)
class VerdictReview:
    job_id: str
    outcome: ReviewOutcome
    reviewer_subject: str
    reviewed_at: datetime
    reason: str = ""
    superseded_by_job_id: str | None = None
    """The later engagement whose conclusion replaces this one, if any — lets a reader of an
    overturned result jump straight to the correction."""


def validate_review(
    job_id: str, outcome: ReviewOutcome, reason: str, superseded_by_job_id: str | None
) -> str:
    """Return the normalised reason, or raise PolicyError. A bare "overturned" can't tell the log
    review what went wrong, so anything short of a full uphold needs a reason."""
    reason = (reason or "").strip()
    if outcome in _REASON_REQUIRED and not reason:
        raise PolicyError(f"a reason is required when the outcome is '{outcome.value}'")
    if len(reason) > REASON_MAX_CHARS:
        raise PolicyError(f"reason is limited to {REASON_MAX_CHARS} characters")
    if superseded_by_job_id is not None and superseded_by_job_id == job_id:
        raise PolicyError("an engagement cannot supersede itself")
    return reason


# Retrieval weighting. Multiplies the BM25 score, so a review reorders hits of similar relevance but
# never lifts an irrelevant upheld document above a strongly matching unreviewed one.
_WEIGHT = {
    ReviewOutcome.UPHELD.value: 1.25,
    UNREVIEWED: 1.0,
    ReviewOutcome.PARTIALLY_UPHELD.value: 0.75,
    ReviewOutcome.OVERTURNED.value: 0.25,
}


def is_retrievable(
    status: str, review_outcome: str, *, include_unsuccessful: bool, include_overturned: bool
) -> bool:
    """Corpus eligibility for a past engagement. Defaults keep crashed/cancelled/incomplete runs and
    human-overturned verdicts out of the knowledge pool; both are opt-in for a caller who wants them
    (e.g. "what went wrong last time we looked at this")."""
    if status != EngagementStatus.SUCCEEDED.value and not include_unsuccessful:
        return False
    if review_outcome == ReviewOutcome.OVERTURNED.value and not include_overturned:
        return False
    return True


def apply_review_weights(hits: list[RetrievalHit], limit: int) -> list[RetrievalHit]:
    """Re-score past-engagement hits by their `meta["review_outcome"]`, re-sort, cut to `limit`.
    Hits without that key (docs/ai/ content) are left at weight 1.0."""
    weighted: list[RetrievalHit] = []
    for h in hits:
        outcome = h.meta.get("review_outcome")
        w = _WEIGHT.get(outcome, 1.0) if outcome is not None else 1.0
        weighted.append(RetrievalHit(
            id=h.id, scope=h.scope, title=h.title, score=round(h.score * w, 4),
            snippet=h.snippet, meta=h.meta,
        ))
    weighted.sort(key=lambda h: h.score, reverse=True)
    return weighted[:limit]


@dataclass(frozen=True)
class ReviewStats:
    total: int
    reviewed: int
    by_outcome: dict[str, int]

    @property
    def coverage(self) -> float:
        return round(self.reviewed / self.total, 4) if self.total else 0.0

    @property
    def upheld_rate(self) -> float | None:
        """Share of *reviewed* verdicts fully upheld. None when nothing is reviewed — an accuracy
        figure over zero reviews is not 0% or 100%, it doesn't exist."""
        if not self.reviewed:
            return None
        return round(self.by_outcome.get(ReviewOutcome.UPHELD.value, 0) / self.reviewed, 4)


def summarise(outcomes: list[str]) -> ReviewStats:
    """`outcomes` is one current-outcome string per reviewable engagement (UNREVIEWED included)."""
    by: dict[str, int] = {o.value: 0 for o in ReviewOutcome}
    by[UNREVIEWED] = 0
    for o in outcomes:
        by[o] = by.get(o, 0) + 1
    total = len(outcomes)
    return ReviewStats(total=total, reviewed=total - by[UNREVIEWED], by_outcome=by)
