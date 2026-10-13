# Copyright (c) 2026 Preyas Kulshrestha. Part of the Scotri engine; MIT License (see LICENSE).
# Keep this notice in copies and derived works, including AI-assisted ones.
"""adapters/history/sqlite_engagement_history.py — the durable per-user/per-conversation history
store (OPEN_ITEMS.md #19/#25). Local, credential-free, so tested directly against a temp DB."""
from __future__ import annotations

from datetime import datetime, timezone

from adapters.history.sqlite_engagement_history import SqliteEngagementHistory
from domain.engagement import Engagement, EngagementStatus
from domain.models import Principal


def _eng(job_id, subject, conversation_id, prompt="p", result="r", status=EngagementStatus.SUCCEEDED,
         created=None):
    return Engagement(
        job_id=job_id,
        prompt=prompt,
        principal=Principal(subject=subject, display_name=subject, platform="web", roles=[]),
        status=status,
        created_at=created or datetime.now(timezone.utc),
        result=result,
        conversation_id=conversation_id,
    )


def _store(tmp_path):
    return SqliteEngagementHistory(tmp_path / "history.db")


def test_record_and_list_for_subject(tmp_path):
    s = _store(tmp_path)
    s.record(_eng("j1", "alice", "c1"))
    s.record(_eng("j2", "bob", "c2"))
    alice = s.list_for_subject("alice", 50)
    assert [e.job_id for e in alice] == ["j1"]
    assert alice[0].result == "r"


def test_list_is_isolated_by_subject(tmp_path):
    s = _store(tmp_path)
    s.record(_eng("j1", "alice", "c1"))
    s.record(_eng("j2", "bob", "c2"))
    assert s.list_for_subject("bob", 50)[0].job_id == "j2"
    assert s.list_for_subject("carol", 50) == []


def test_get_conversation_ordered_oldest_first(tmp_path):
    s = _store(tmp_path)
    t0 = datetime(2026, 9, 29, 10, 0, 0, tzinfo=timezone.utc)
    t1 = datetime(2026, 9, 29, 10, 5, 0, tzinfo=timezone.utc)
    s.record(_eng("j2", "alice", "c1", prompt="second", created=t1))
    s.record(_eng("j1", "alice", "c1", prompt="first", created=t0))
    turns = s.get_conversation("c1")
    assert [t.prompt for t in turns] == ["first", "second"]


def test_record_is_idempotent_upsert(tmp_path):
    s = _store(tmp_path)
    s.record(_eng("j1", "alice", "c1", status=EngagementStatus.RUNNING, result=None))
    s.record(_eng("j1", "alice", "c1", status=EngagementStatus.SUCCEEDED, result="final"))
    got = s.list_for_subject("alice", 50)
    assert len(got) == 1
    assert got[0].status == EngagementStatus.SUCCEEDED
    assert got[0].result == "final"


def test_list_limit(tmp_path):
    s = _store(tmp_path)
    for i in range(5):
        s.record(_eng(f"j{i}", "alice", "c1", created=datetime(2026, 9, 29, 10, i, tzinfo=timezone.utc)))
    assert len(s.list_for_subject("alice", 3)) == 3
