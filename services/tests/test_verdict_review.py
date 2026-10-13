# Copyright (c) 2026 Preyas Kulshrestha. Part of the Scotri engine; MIT License (see LICENSE).
# Keep this notice in copies and derived works, including AI-assisted ones.
"""Human verdict review (OPEN_ITEMS.md #47): the domain rules, the append-only store, and the
retrieval filter/weighting that keeps overturned or unsuccessful runs out of the knowledge pool."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from adapters.history.sqlite_engagement_history import SqliteEngagementHistory
from adapters.retrieval.lexical_retrieval import LexicalRetriever
from domain.engagement import Engagement, EngagementStatus
from domain.errors import PolicyError
from domain.models import Principal
from domain.retrieval import RetrievalHit
from domain.verdict_review import (
    UNREVIEWED,
    ReviewOutcome,
    VerdictReview,
    apply_review_weights,
    is_retrievable,
    summarise,
    validate_review,
)

T0 = datetime(2026, 9, 29, 10, 0, 0, tzinfo=timezone.utc)


def _eng(job_id, result, status=EngagementStatus.SUCCEEDED, subject="alice", created=T0):
    return Engagement(
        job_id=job_id, prompt="investigate acme_prod.gold.visit_summary",
        principal=Principal(subject=subject, display_name=subject, platform="web", roles=[]),
        status=status, created_at=created, result=result, conversation_id=f"c-{job_id}",
    )


def _review(job_id, outcome, at=T0, reason="r", superseded=None):
    return VerdictReview(job_id=job_id, outcome=outcome, reviewer_subject="alice",
                         reviewed_at=at, reason=reason, superseded_by_job_id=superseded)


# --- domain ----------------------------------------------------------------------------------------

@pytest.mark.parametrize("outcome", [ReviewOutcome.OVERTURNED, ReviewOutcome.PARTIALLY_UPHELD])
def test_reason_required_unless_fully_upheld(outcome):
    with pytest.raises(PolicyError):
        validate_review("j1", outcome, "   ", None)


def test_upheld_needs_no_reason_and_reason_is_trimmed():
    assert validate_review("j1", ReviewOutcome.UPHELD, "", None) == ""
    assert validate_review("j1", ReviewOutcome.OVERTURNED, "  wrong table  ", None) == "wrong table"


def test_cannot_supersede_itself_and_reason_capped():
    with pytest.raises(PolicyError):
        validate_review("j1", ReviewOutcome.OVERTURNED, "x", "j1")
    with pytest.raises(PolicyError):
        validate_review("j1", ReviewOutcome.OVERTURNED, "x" * 2001, None)


def test_unreviewed_is_not_a_submittable_outcome():
    with pytest.raises(ValueError):
        ReviewOutcome(UNREVIEWED)


def test_eligibility_defaults_exclude_unsuccessful_and_overturned():
    assert is_retrievable("succeeded", UNREVIEWED, include_unsuccessful=False, include_overturned=False)
    assert not is_retrievable("failed", UNREVIEWED, include_unsuccessful=False, include_overturned=False)
    assert not is_retrievable("incomplete", UNREVIEWED, include_unsuccessful=False, include_overturned=False)
    assert not is_retrievable("succeeded", "overturned", include_unsuccessful=False, include_overturned=False)
    assert is_retrievable("failed", UNREVIEWED, include_unsuccessful=True, include_overturned=False)
    assert is_retrievable("succeeded", "overturned", include_unsuccessful=False, include_overturned=True)


def test_weights_reorder_close_hits_and_unreviewed_is_neutral():
    def hit(i, score, outcome):
        return RetrievalHit(id=i, scope="past-engagements", title=i, score=score, snippet="",
                            meta={"review_outcome": outcome})
    out = apply_review_weights(
        [hit("unrev", 1.0, UNREVIEWED), hit("up", 0.9, "upheld"), hit("part", 1.1, "partially_upheld")],
        limit=10,
    )
    assert [h.id for h in out] == ["up", "unrev", "part"]
    assert next(h for h in out if h.id == "unrev").score == 1.0


def test_docs_hits_are_untouched_by_weighting():
    h = RetrievalHit(id="docs:x", scope="docs", title="x", score=2.0, snippet="", meta={})
    assert apply_review_weights([h], limit=5)[0].score == 2.0


def test_summary_reports_coverage_and_no_rate_without_reviews():
    s = summarise([UNREVIEWED, UNREVIEWED])
    assert (s.total, s.reviewed, s.coverage, s.upheld_rate) == (2, 0, 0.0, None)
    s = summarise(["upheld", "overturned", UNREVIEWED, "upheld"])
    assert s.reviewed == 3 and s.coverage == 0.75 and s.upheld_rate == round(2 / 3, 4)


# --- store -----------------------------------------------------------------------------------------

def test_reviews_are_append_only_and_latest_wins(tmp_path):
    store = SqliteEngagementHistory(tmp_path / "h.db")
    store.record(_eng("j1", "verdict: confirmed bug"))
    store.add_review(_review("j1", ReviewOutcome.UPHELD, at=T0))
    store.add_review(_review("j1", ReviewOutcome.OVERTURNED, at=T0 + timedelta(days=2), reason="fix disproved it"))
    trail = store.list_reviews("j1")
    assert [r.outcome for r in trail] == [ReviewOutcome.UPHELD, ReviewOutcome.OVERTURNED]
    assert store.latest_reviews(["j1", "missing"])["j1"].outcome == ReviewOutcome.OVERTURNED
    assert "missing" not in store.latest_reviews(["j1", "missing"])
    assert store.latest_reviews([]) == {}


def test_get_returns_none_for_unknown_job(tmp_path):
    store = SqliteEngagementHistory(tmp_path / "h.db")
    assert store.get("nope") is None
    store.record(_eng("j1", "x"))
    assert store.get("j1").job_id == "j1"


# --- retrieval end to end --------------------------------------------------------------------------

def _retriever(tmp_path):
    store = SqliteEngagementHistory(tmp_path / "h.db")
    docs = tmp_path / "docs"
    docs.mkdir()
    return store, LexicalRetriever(store, docs)


def test_retrieval_excludes_overturned_and_failed_by_default(tmp_path):
    store, r = _retriever(tmp_path)
    store.record(_eng("good", "visit_summary verdict confirmed"))
    store.record(_eng("wrong", "visit_summary verdict confirmed"))
    store.record(_eng("crashed", "visit_summary partial", status=EngagementStatus.FAILED))
    store.add_review(_review("wrong", ReviewOutcome.OVERTURNED, reason="wrong root cause", superseded="good"))

    ids = {h.meta["job_id"] for h in r.search("visit_summary", ["past-engagements"], "alice", 10)}
    assert ids == {"good"}

    ids = {h.meta["job_id"] for h in r.search("visit_summary", ["past-engagements"], "alice", 10,
                                              include_unsuccessful=True, include_overturned=True)}
    assert ids == {"good", "wrong", "crashed"}


def test_retrieval_hit_carries_review_metadata(tmp_path):
    store, r = _retriever(tmp_path)
    store.record(_eng("a", "visit_summary"))
    store.record(_eng("b", "visit_summary"))
    store.add_review(_review("b", ReviewOutcome.UPHELD, reason=""))
    hits = {h.meta["job_id"]: h for h in r.search("visit_summary", ["past-engagements"], "alice", 10)}
    assert hits["a"].meta["review_outcome"] == UNREVIEWED
    assert "reviewed_at" not in hits["a"].meta
    assert hits["b"].meta["review_outcome"] == "upheld"
    assert isinstance(hits["a"].meta["age_days"], int)
    # identical text, so the upheld one must rank first
    assert r.search("visit_summary", ["past-engagements"], "alice", 10)[0].meta["job_id"] == "b"


# --- only conclusions are reviewable -------------------------------------------------------------

def test_only_bug_verdicts_and_story_designs_are_reviewable():
    from domain.verdict_review import is_reviewable
    for verdict in ("confirmed_bug", "not_a_bug", "inconclusive", "story"):
        assert is_reviewable(EngagementStatus.SUCCEEDED, verdict)
    assert not is_reviewable(EngagementStatus.SUCCEEDED, "question")
    assert not is_reviewable(EngagementStatus.SUCCEEDED, None)  # plain chat: no learning at all
    assert not is_reviewable(EngagementStatus.FAILED, "confirmed_bug")


def test_learning_verdicts_lookup(tmp_path):
    from domain.learning import Learning
    store = SqliteEngagementHistory(tmp_path / "h.db")
    store.record_learning(Learning(job_id="j1", title="t", verdict="confirmed_bug", takeaways=["x"],
                                   entities=[], created_at=T0, source_subject="s", source_display=""))
    assert store.learning_verdicts(["j1", "chat"]) == {"j1": "confirmed_bug"}
    assert store.learning_verdicts([]) == {}
