"""domain/engagement_policy.py — cancelling an engagement, the startup sweep for jobs a restart left
"running", and the Agent SDK adapter's actual mid-run interruption.

The policy tests use in-memory fakes for the job store / runtime / log (same seam the real adapters
implement); the adapter test swaps out only the SDK's `query()` for a slow async generator."""
from __future__ import annotations

import asyncio
import threading
import time
from datetime import datetime, timezone

from adapters.agent_sdk import claude_agent_adapter
from adapters.agent_sdk.claude_agent_adapter import ClaudeAgentSdkRuntime
from domain.engagement import Engagement, EngagementStatus, is_terminal
from domain.engagement_policy import (
    cancel_engagement,
    fail_interrupted_engagements,
    run_engagement,
    start_engagement,
)
from domain.models import Principal
from ports.agent_runtime import AgentRunResult

PRINCIPAL = Principal(subject="dev", display_name="Dev", platform="test", roles=[])


class FakeJobStore:
    def __init__(self) -> None:
        self.jobs: dict[str, Engagement] = {}

    def create(self, engagement: Engagement) -> None:
        self.jobs[engagement.job_id] = engagement

    def get(self, job_id: str) -> Engagement | None:
        return self.jobs.get(job_id)

    def update(self, engagement: Engagement) -> None:
        self.jobs[engagement.job_id] = engagement

    def list_active(self) -> list[Engagement]:
        return [e for e in self.jobs.values() if not is_terminal(e.status)]


class FakeLog:
    def __init__(self) -> None:
        self.entries: list = []

    def record(self, entry) -> None:
        self.entries.append(entry)


class FakeRuntime:
    """Returns `result` from run(); if `on_run` is given it runs first (to simulate a cancel landing
    mid-run). cancel() just records the call and reports whether `interruptible`."""

    def __init__(self, result: AgentRunResult | None = None, on_run=None, interruptible: bool = True) -> None:
        self.result = result or AgentRunResult(text="done", is_error=False, session_id="s1")
        self.on_run = on_run
        self.interruptible = interruptible
        self.cancel_calls: list[str] = []
        self.run_calls: list[dict] = []

    def run(self, prompt, cwd, resume_session_id=None, job_id=None, env=None) -> AgentRunResult:
        self.run_calls.append({"prompt": prompt, "job_id": job_id, "env": env})
        if self.on_run:
            self.on_run()
        return self.result

    def cancel(self, job_id: str) -> bool:
        self.cancel_calls.append(job_id)
        return self.interruptible


def test_cancelling_a_queued_job_marks_it_cancelled_and_it_never_runs():
    store, log, runtime = FakeJobStore(), FakeLog(), FakeRuntime(interruptible=False)
    job_id = start_engagement(store, PRINCIPAL, "hello")

    result = cancel_engagement(store, runtime, job_id)
    run_engagement(store, runtime, log, job_id, "/ws")

    assert result is not None and result.status == EngagementStatus.CANCELLED
    assert store.jobs[job_id].status == EngagementStatus.CANCELLED
    assert runtime.run_calls == []
    assert [e.outcome for e in log.entries] == ["cancelled"]


def test_cancelling_mid_run_is_not_overwritten_by_a_late_result():
    """The stop lands while the agent is running and the agent still hands back a normal-looking
    result (it finished in the same instant) — the user's cancel must win."""
    store, log = FakeJobStore(), FakeLog()
    job_id = start_engagement(store, PRINCIPAL, "hello")
    runtime = FakeRuntime(on_run=lambda: cancel_engagement(store, runtime, job_id))

    run_engagement(store, runtime, log, job_id, "/ws")

    assert store.jobs[job_id].status == EngagementStatus.CANCELLED
    assert store.jobs[job_id].result is None
    assert runtime.cancel_calls == [job_id]
    assert [e.outcome for e in log.entries] == ["cancelled"]


def test_a_runtime_reported_cancellation_is_recorded_as_cancelled_not_failed():
    store, log = FakeJobStore(), FakeLog()
    runtime = FakeRuntime(result=AgentRunResult(text="Cancelled.", is_error=True, cancelled=True))
    job_id = start_engagement(store, PRINCIPAL, "hello")

    run_engagement(store, runtime, log, job_id, "/ws")

    assert store.jobs[job_id].status == EngagementStatus.CANCELLED
    assert store.jobs[job_id].error is None


def test_cancelling_a_finished_job_is_a_no_op_that_returns_its_real_status():
    store, log, runtime = FakeJobStore(), FakeLog(), FakeRuntime()
    job_id = start_engagement(store, PRINCIPAL, "hello")
    run_engagement(store, runtime, log, job_id, "/ws")

    result = cancel_engagement(store, runtime, job_id)

    assert result is not None and result.status == EngagementStatus.SUCCEEDED
    assert runtime.cancel_calls == []


def test_cancelling_an_unknown_job_returns_none():
    assert cancel_engagement(FakeJobStore(), FakeRuntime(), "nope") is None


def test_the_run_is_given_its_job_id_so_it_can_be_found_and_cancelled():
    store, log, runtime = FakeJobStore(), FakeLog(), FakeRuntime()
    job_id = start_engagement(store, PRINCIPAL, "hello")
    run_engagement(store, runtime, log, job_id, "/ws")
    assert runtime.run_calls == [{"prompt": "hello", "job_id": job_id, "env": None}]


def test_startup_sweep_fails_only_active_jobs_from_the_given_origin():
    store = FakeJobStore()
    api_running = start_engagement(store, PRINCIPAL, "a", origin="api")
    store.update(store.jobs[api_running].with_status(EngagementStatus.RUNNING))
    api_queued = start_engagement(store, PRINCIPAL, "b", origin="api")
    api_done = start_engagement(store, PRINCIPAL, "c", origin="api")
    store.update(store.jobs[api_done].with_status(EngagementStatus.SUCCEEDED, result="ok"))
    mcp_running = start_engagement(store, PRINCIPAL, "d")  # origin=None (MCP server)
    store.update(store.jobs[mcp_running].with_status(EngagementStatus.RUNNING))

    swept = fail_interrupted_engagements(store, origin="api")

    assert swept == 2
    assert store.jobs[api_running].status == EngagementStatus.FAILED
    assert "restarted" in (store.jobs[api_running].error or "")
    assert store.jobs[api_queued].status == EngagementStatus.FAILED
    assert store.jobs[api_done].status == EngagementStatus.SUCCEEDED
    assert store.jobs[mcp_running].status == EngagementStatus.RUNNING


def test_engagement_round_trips_origin_and_cancelled_status():
    e = Engagement(
        job_id="j", prompt="p", principal=PRINCIPAL, status=EngagementStatus.CANCELLED,
        created_at=datetime.now(timezone.utc), origin="api",
    )
    back = Engagement.from_json(e.to_json())
    assert back.origin == "api" and back.status == EngagementStatus.CANCELLED
    assert back.with_status(EngagementStatus.FAILED).origin == "api"


def test_adapter_cancel_interrupts_a_slow_in_flight_query(monkeypatch):
    """Real ClaudeAgentSdkRuntime, fake SDK query(): a stop from another thread must interrupt the
    run promptly, run the query's cleanup, and come back as cancelled=True."""
    started, cleaned_up = threading.Event(), threading.Event()

    async def slow_query(prompt, options):
        try:
            started.set()
            await asyncio.sleep(30)
            yield None  # never reached
        finally:
            cleaned_up.set()  # stands in for the SDK terminating its CLI subprocess

    monkeypatch.setattr(claude_agent_adapter, "query", slow_query)
    runtime = ClaudeAgentSdkRuntime()
    results: list[AgentRunResult] = []
    worker = threading.Thread(target=lambda: results.append(runtime.run("hi", "/ws", job_id="job1")))
    worker.start()
    assert started.wait(5)

    assert runtime.cancel("job1") is True
    t0 = time.monotonic()
    worker.join(5)

    assert not worker.is_alive() and time.monotonic() - t0 < 3
    assert results[0].cancelled is True
    assert cleaned_up.is_set()
    assert runtime.cancel("job1") is False  # deregistered once the run ended


def test_adapter_cancel_for_an_unknown_job_is_false():
    assert ClaudeAgentSdkRuntime().cancel("never-started") is False


# --- presence claims are released when the run ends (OPEN_ITEMS.md #68) --------------------------


def test_claims_are_released_when_the_run_ends_whatever_the_outcome():
    from app.container import Container
    from domain.models import Claim

    class Store:
        def __init__(self):
            now = datetime.now(timezone.utc)
            self.claims = {"ACME-1": Claim("ACME-1", "dev", now, now), "gold.x": Claim("gold.x", "bob", now, now)}
            self.runs = {"j": ["ACME-1", "gold.x"]}

        def run_claims(self, job_id):
            return list(self.runs.get(job_id, []))

        def get_claim(self, entity):
            return self.claims.get(entity)

        def release_claim(self, entity, owner):
            if self.claims.get(entity) and self.claims[entity].owner == owner:
                del self.claims[entity]

        def forget_run(self, job_id):
            self.runs.pop(job_id, None)

    store = Store()
    fake = type("C", (), {"presence_store": store})()
    released = Container.release_run_claims(fake, "j", "dev")
    assert released == ["ACME-1"]                   # bob took gold.x over: left alone
    assert "gold.x" in store.claims and store.runs == {}

    calls = []
    for result in (AgentRunResult(text="done", is_error=False, session_id="s"),
                   AgentRunResult(text="boom", is_error=True, session_id="s")):
        jobs, log = FakeJobStore(), FakeLog()
        job_id = start_engagement(jobs, PRINCIPAL, "ACME-1")
        run_engagement(jobs, FakeRuntime(result=result), log, job_id, "/ws",
                       release_claims=lambda j, owner: calls.append((j, owner)) or [])
        assert calls[-1] == (job_id, PRINCIPAL.subject)
    assert len(calls) == 2
