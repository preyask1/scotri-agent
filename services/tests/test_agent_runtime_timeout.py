# Copyright (c) 2026 Preyas Kulshrestha. Part of the Scotri engine; MIT License (see LICENSE).
# Keep this notice in copies and derived works, including AI-assisted ones.
"""adapters/agent_sdk/claude_agent_adapter.py — the wall-clock timeout (OPEN_ITEMS.md #33).

Exercises the timeout wrapper by stubbing the instance's own `_query` (so no real Claude Agent SDK
subprocess is spawned). Locks down that a timeout is returned as a non-success AgentRunResult — never
raised (the AgentRuntime contract) — and that a fast run still returns its result unchanged."""
from __future__ import annotations

import asyncio

from adapters.agent_sdk.claude_agent_adapter import ClaudeAgentSdkRuntime
from ports.agent_runtime import AgentRunResult


def test_run_times_out_as_non_success_without_raising(monkeypatch):
    rt = ClaudeAgentSdkRuntime(timeout_seconds=0.05)

    async def _slow(prompt, cwd, resume, env=None, written=None, on_event=None):  # matches self._query(prompt, cwd, resume_session_id, env, written, on_event)
        await asyncio.sleep(5)
        return AgentRunResult(text="should never arrive", is_error=False)

    monkeypatch.setattr(rt, "_query", _slow)

    result = rt.run("prompt", cwd=".", job_id="job-1")  # must not raise
    assert result.is_error is True
    assert result.incomplete is True          # distinct non-success terminal outcome
    assert result.cancelled is False          # a timeout is NOT an external cancel
    assert "time limit" in result.text
    assert result.session_id is None


def test_run_returns_fast_result_untouched(monkeypatch):
    rt = ClaudeAgentSdkRuntime(timeout_seconds=5)

    async def _fast(prompt, cwd, resume, env=None, written=None, on_event=None):
        return AgentRunResult(text="answer", is_error=False, session_id="s1")

    monkeypatch.setattr(rt, "_query", _fast)

    result = rt.run("prompt", cwd=".")
    assert result.text == "answer"
    assert result.is_error is False
    assert result.incomplete is False
    assert result.session_id == "s1"


def test_active_registry_is_cleared_after_a_timeout(monkeypatch):
    """The job_id -> (loop, task) entry must be removed on every exit path, timeout included, so a
    later cancel()/lookup doesn't see a stale run."""
    rt = ClaudeAgentSdkRuntime(timeout_seconds=0.05)

    async def _slow(prompt, cwd, resume, env=None, written=None, on_event=None):
        await asyncio.sleep(5)

    monkeypatch.setattr(rt, "_query", _slow)
    rt.run("prompt", cwd=".", job_id="job-2")
    assert rt.cancel("job-2") is False        # nothing left registered under that id
