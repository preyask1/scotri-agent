# Copyright (c) 2026 Preyas Kulshrestha. Part of the Scotri engine; MIT License (see LICENSE).
# Keep this notice in copies and derived works, including AI-assisted ones.
"""OPEN_ITEMS.md #25: a turn's conversation and resumed session, the same over HTTP and MCP."""
from __future__ import annotations

import types
from datetime import datetime, timezone

import pytest

from domain.engagement import Engagement, EngagementStatus
from domain.engagement_policy import resolve_turn
from domain.errors import PolicyError
from domain.models import Principal

ALICE = Principal(subject="alice", display_name="Alice", platform="test", roles=[])
BOB = Principal(subject="bob", display_name="Bob", platform="test", roles=[])


def _eng(job_id, principal, conversation_id=None, session_id=None):
    return Engagement(job_id=job_id, prompt="p", principal=principal, status=EngagementStatus.SUCCEEDED,
                      created_at=datetime.now(timezone.utc), conversation_id=conversation_id,
                      session_id=session_id)


class Jobs:
    def __init__(self, *engs):
        self.jobs = {e.job_id: e for e in engs}

    def create(self, e):
        self.jobs[e.job_id] = e

    def get(self, job_id):
        return self.jobs.get(job_id)


class History:
    def __init__(self, *engs):
        self.engs = list(engs)

    def get(self, job_id):
        return next((e for e in self.engs if e.job_id == job_id), None)

    def get_conversation(self, cid):
        return [e for e in self.engs if e.conversation_id == cid]


def test_first_turn_gets_a_fresh_conversation():
    cid, session = resolve_turn(Jobs(), History(), ALICE)
    assert len(cid) == 32 and session is None


def test_resume_carries_the_conversation_and_session():
    jobs = Jobs(_eng("j1", ALICE, "c1", "s1"))
    assert resolve_turn(jobs, History(), ALICE, resume_job_id="j1") == ("c1", "s1")


def test_resume_falls_back_to_history_once_the_job_store_expired():
    history = History(_eng("j1", ALICE, "c1", "s1"))
    assert resolve_turn(Jobs(), history, ALICE, resume_job_id="j1") == ("c1", "s1")


def test_unknown_resume_job_runs_fresh():
    cid, session = resolve_turn(Jobs(), History(), ALICE, resume_job_id="nope")
    assert session is None and cid


def test_explicit_conversation_wins_and_unknown_ones_are_accepted():
    jobs = Jobs(_eng("j1", ALICE, "c1", "s1"))
    assert resolve_turn(jobs, History(), ALICE, conversation_id="c9", resume_job_id="j1") == ("c9", "s1")


def test_someone_elses_conversation_is_refused():
    with pytest.raises(PolicyError):
        resolve_turn(Jobs(), History(_eng("j1", BOB, "c1")), ALICE, conversation_id="c1")


def test_someone_elses_session_is_refused():
    with pytest.raises(PolicyError):
        resolve_turn(Jobs(_eng("j1", BOB, "c1", "s1")), History(), ALICE, resume_job_id="j1")


# --- MCP scotri_start ------------------------------------------------------------------------------

@pytest.fixture
def mcp(monkeypatch):
    import app.mcp_server as mcp_server

    jobs = Jobs(_eng("j1", ALICE, "c1", "s1"), _eng("j2", BOB, "c2", "s2"))
    fake = types.SimpleNamespace(job_store=jobs, engagement_history=History(), artifact_store=None,
                                 agent_runtime=None, interaction_log=None, run_context_store=None,
                                 run_context_ttl_seconds=60, progress_store=None,
                                 engagement_prefetch=lambda root: None)
    monkeypatch.setattr(mcp_server, "container", fake)
    monkeypatch.setattr(mcp_server, "_authenticate", lambda ctx: ALICE)
    # No real run: the job is admitted (no queue on this fake container, so it would start) but
    # nothing is spawned.
    monkeypatch.setattr(mcp_server.runner, "_spawn", lambda fn, name: None)
    return mcp_server, jobs


def test_mcp_start_returns_a_conversation_and_continues_one(mcp):
    mcp_server, jobs = mcp
    first = mcp_server.scotri_start("hello", None)
    assert first["conversation_id"] and first["resumed"] is False
    assert jobs.get(first["job_id"]).conversation_id == first["conversation_id"]

    follow = mcp_server.scotri_start("and then?", None, resume_job_id="j1")
    queued = jobs.get(follow["job_id"])
    assert follow == {"job_id": follow["job_id"], "resumed": True, "conversation_id": "c1", "queued": False}
    assert queued.resume_session_id == "s1" and queued.conversation_id == "c1"


def test_mcp_start_refuses_another_users_job(mcp):
    from mcp.server.mcpserver.exceptions import ToolError

    mcp_server, _ = mcp
    with pytest.raises(ToolError, match="another user"):
        mcp_server.scotri_start("x", None, resume_job_id="j2")
