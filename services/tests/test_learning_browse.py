# Copyright (c) 2026 Preyas Kulshrestha. Part of the Scotri engine; MIT License (see LICENSE).
# Keep this notice in copies and derived works, including AI-assisted ones.
"""OPEN_ITEMS.md #60(d): browsing learnings is own-only unless a LEARNINGS_READER_ROLES role says otherwise."""
from __future__ import annotations

import types
from datetime import datetime, timedelta, timezone

import pytest

from adapters.history.sqlite_engagement_history import SqliteEngagementHistory
from domain.errors import NotFoundError, PolicyError
from domain.learning import Learning
from domain.learning_browse import browse_learnings, get_learning
from domain.models import Principal
from domain.verdict_review import ReviewOutcome, VerdictReview

ALICE = Principal(subject="alice", display_name="Alice", platform="test", roles=[])
BOB = Principal(subject="bob", display_name="Bob", platform="test", roles=[])
LEAD = Principal(subject="lead", display_name="Lead", platform="test", roles=["scotri.learnings"])
READERS = {"scotri.learnings"}
T0 = datetime(2026, 10, 1, tzinfo=timezone.utc)


def _learning(job_id, owner, title, verdict="confirmed_bug", minutes=0):
    return Learning(job_id=job_id, title=title, verdict=verdict, takeaways=["check the export cap"],
                    entities=["Sales Report"], created_at=T0 + timedelta(minutes=minutes),
                    source_subject=owner, source_display=owner.title())


@pytest.fixture
def history(tmp_path):
    h = SqliteEngagementHistory(tmp_path / "h.db")
    h.record_learning(_learning("a1", "alice", "Brand download truncated at export cap", minutes=1))
    h.record_learning(_learning("a2", "alice", "Story: add region slicer", verdict="story", minutes=2))
    h.record_learning(_learning("b1", "bob", "Brand totals double counted", minutes=3))
    h.record_learning(_learning("w1", "service:webhook", "Brand refresh failed overnight", minutes=4))
    return h


def ids(items):
    return [i["job_id"] for i in items]


def test_mine_is_the_default_and_only_shows_your_own(history):
    assert ids(browse_learnings(history, ALICE, READERS)) == ["a2", "a1"]
    assert ids(browse_learnings(history, BOB, READERS)) == ["b1"]


def test_team_scope_is_refused_without_a_reader_role(history):
    with pytest.raises(PolicyError, match="LEARNINGS_READER_ROLES"):
        browse_learnings(history, ALICE, READERS, scope="team")


def test_team_scope_is_refused_for_everyone_when_no_role_is_configured(history):
    with pytest.raises(PolicyError):
        browse_learnings(history, LEAD, set(), scope="team")


def test_a_reader_sees_everyones_including_service_runs(history):
    assert ids(browse_learnings(history, LEAD, READERS, scope="team")) == ["w1", "b1", "a2", "a1"]


def test_search_ranks_and_stays_within_scope(history):
    assert ids(browse_learnings(history, ALICE, READERS, query="truncated")) == ["a1"]
    assert "b1" not in ids(browse_learnings(history, ALICE, READERS, query="brand"))
    assert set(ids(browse_learnings(history, LEAD, READERS, scope="team", query="brand"))) == {"a1", "b1", "w1"}


def test_verdict_filter_and_review_outcome(history):
    history.add_review(VerdictReview(job_id="a1", outcome=ReviewOutcome.UPHELD, reviewer_subject="alice",
                                     reviewed_at=T0))
    items = browse_learnings(history, ALICE, READERS, verdict="confirmed_bug")
    assert ids(items) == ["a1"] and items[0]["review_outcome"] == "upheld"
    assert browse_learnings(history, ALICE, READERS, verdict="story")[0]["review_outcome"] == "unreviewed"


@pytest.mark.parametrize("kw", [{"scope": "everyone"}, {"verdict": "maybe"}])
def test_bad_arguments_are_refused(history, kw):
    with pytest.raises(PolicyError):
        browse_learnings(history, ALICE, READERS, **kw)


def test_one_learning_is_owner_or_reader(history):
    assert get_learning(history, ALICE, READERS, "a1")["title"].startswith("Brand download")
    assert get_learning(history, LEAD, READERS, "b1")["investigated_by"] == "Bob"
    with pytest.raises(PolicyError):
        get_learning(history, ALICE, READERS, "b1")
    with pytest.raises(NotFoundError):
        get_learning(history, ALICE, READERS, "nope")


def test_routes_map_refusals_to_403(monkeypatch, history):
    from fastapi.testclient import TestClient

    import app.api as api

    monkeypatch.setattr(api, "container", types.SimpleNamespace(engagement_history=history))
    monkeypatch.setattr(api, "LEARNINGS_READER_ROLES", READERS)
    caller = {"p": ALICE}
    monkeypatch.setattr(api, "_authenticate", lambda authorization: caller["p"])
    client = TestClient(api.app, raise_server_exceptions=False)

    r = client.get("/v1/learnings")
    assert r.status_code == 200 and ids(r.json()["learnings"]) == ["a2", "a1"]
    assert client.get("/v1/learnings?scope=team").status_code == 403
    assert client.get("/v1/learnings/b1").status_code == 403
    assert client.get("/v1/learnings/zz").status_code == 404
    caller["p"] = LEAD
    assert client.get("/v1/learnings?scope=team&q=brand").json()["count"] == 3
