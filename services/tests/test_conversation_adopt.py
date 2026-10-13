# Copyright (c) 2026 Preyas Kulshrestha. Part of the Scotri engine; MIT License (see LICENSE).
# Keep this notice in copies and derived works, including AI-assisted ones.
"""OPEN_ITEMS.md #25: moving browser-only threads into server conversations, owner-only, never re-homing."""
from __future__ import annotations

import types
from datetime import datetime, timedelta, timezone

import pytest

from adapters.history.sqlite_engagement_history import SqliteEngagementHistory
from domain.engagement import Engagement, EngagementStatus
from domain.engagement_policy import adopt_into_conversation
from domain.errors import PolicyError
from domain.models import Principal

ALICE = Principal(subject="alice", display_name="Alice", platform="test", roles=[])
BOB = Principal(subject="bob", display_name="Bob", platform="test", roles=[])
T0 = datetime(2026, 9, 20, tzinfo=timezone.utc)


def _record(h, job_id, who, conversation_id=None, minutes=0):
    h.record(Engagement(job_id=job_id, prompt=f"p {job_id}", principal=who, status=EngagementStatus.SUCCEEDED,
                        created_at=T0 + timedelta(minutes=minutes), result=f"r {job_id}",
                        conversation_id=conversation_id))


@pytest.fixture
def history(tmp_path):
    h = SqliteEngagementHistory(tmp_path / "h.db")
    _record(h, "a1", ALICE, minutes=1)
    _record(h, "a2", ALICE, minutes=2)
    _record(h, "a3", ALICE, conversation_id="cX", minutes=3)
    _record(h, "b1", BOB, minutes=4)
    return h


def test_old_jobs_become_one_conversation(history):
    out = adopt_into_conversation(history, ALICE, ["a1", "a2"])
    cid = out["conversation_id"]
    assert cid and out["adopted"] == ["a1", "a2"]
    assert [t.job_id for t in history.get_conversation(cid)] == ["a1", "a2"]


def test_jobs_join_an_existing_conversation_and_repeat_calls_are_harmless(history):
    out = adopt_into_conversation(history, ALICE, ["a1", "a3"], conversation_id="cX")
    assert out["adopted"] == ["a1"] and out["already"] == ["a3"]
    again = adopt_into_conversation(history, ALICE, ["a1", "a3"], conversation_id="cX")
    assert again["adopted"] == [] and again["already"] == ["a1", "a3"] and again["conversation_id"] == "cX"


def test_a_job_in_another_conversation_is_never_moved(history):
    out = adopt_into_conversation(history, ALICE, ["a2", "a3"])
    assert out["adopted"] == ["a2"] and out["in_other"] == ["a3"]
    assert history.get("a3").conversation_id == "cX"


def test_unknown_jobs_are_reported_and_nothing_grouped_means_no_id(history):
    out = adopt_into_conversation(history, ALICE, ["gone1", "gone2"])
    assert out == {"conversation_id": None, "adopted": [], "already": [], "unknown": ["gone1", "gone2"],
                   "in_other": []}


def test_someone_elses_job_refuses_the_whole_call(history):
    with pytest.raises(PolicyError):
        adopt_into_conversation(history, ALICE, ["a1", "b1"])
    assert history.get("a1").conversation_id is None


def test_someone_elses_conversation_is_refused(history):
    _record(history, "b2", BOB, conversation_id="cB", minutes=5)
    with pytest.raises(PolicyError):
        adopt_into_conversation(history, ALICE, ["a1"], conversation_id="cB")


@pytest.mark.parametrize("ids", [[], [""], [f"j{i}" for i in range(201)]])
def test_empty_or_oversized_requests_are_refused(history, ids):
    with pytest.raises(PolicyError):
        adopt_into_conversation(history, ALICE, ids)


def test_route(monkeypatch, history):
    from fastapi.testclient import TestClient

    import app.api as api

    monkeypatch.setattr(api, "container", types.SimpleNamespace(engagement_history=history))
    monkeypatch.setattr(api, "_authenticate", lambda authorization: ALICE)
    client = TestClient(api.app, raise_server_exceptions=False)
    r = client.post("/v1/conversations/adopt", json={"job_ids": ["a1", "a2", "nope"]})
    assert r.status_code == 200 and r.json()["adopted"] == ["a1", "a2"] and r.json()["unknown"] == ["nope"]
    assert client.post("/v1/conversations/adopt", json={"job_ids": ["b1"]}).status_code == 403
