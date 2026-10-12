"""Live progress for multi-minute runs (OPEN_ITEMS.md #39): phases derived from Skill calls, a poll
view that names nothing the run is looking at, the session id captured mid-run (so /steps works
while running and an interrupted job can be continued), and the SDK adapter's event extraction."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from claude_agent_sdk import AssistantMessage, SystemMessage, TextBlock, ToolUseBlock

from adapters.agent_sdk.claude_agent_adapter import _events
from domain.engagement import Engagement, EngagementStatus
from domain.engagement_policy import fail_interrupted_engagements, run_engagement, start_engagement
from domain.engagement_progress import STARTING, WORKING, EngagementProgress, phase_for_skill
from domain.trace_policy import get_job_trace
from ports.agent_runtime import AgentEvent, AgentRunResult
from tests.test_engagement_cancel import PRINCIPAL, FakeJobStore, FakeLog

T0 = datetime(2026, 10, 4, 9, 0, tzinfo=timezone.utc)


class FakeProgressStore:
    def __init__(self) -> None:
        self.items: dict[str, EngagementProgress] = {}
        self.puts = 0

    def put(self, progress: EngagementProgress) -> None:
        # Round-trip through JSON so the stored shape is what Redis would hold.
        self.items[progress.job_id] = EngagementProgress.from_json(progress.to_json())
        self.puts += 1

    def get(self, job_id: str) -> EngagementProgress | None:
        return self.items.get(job_id)


class EventRuntime:
    """Emits `events` through on_event, then returns `result`. `seen` records the progress store's
    state mid-run, after the events and before the run returns."""

    def __init__(self, events, result: AgentRunResult, progress_store=None) -> None:
        self.events, self.result, self.progress_store = events, result, progress_store
        self.seen: EngagementProgress | None = None

    def run(self, prompt, cwd, resume_session_id=None, job_id=None, env=None, on_event=None) -> AgentRunResult:
        for event in self.events:
            on_event(event)
        if self.progress_store is not None:
            self.seen = self.progress_store.get(job_id)
        return self.result

    def cancel(self, job_id):
        return False


# --- domain ------------------------------------------------------------------------------

def test_only_lifecycle_stage_skills_move_the_phase():
    assert phase_for_skill("jira-intake") == "intake"
    assert phase_for_skill("bug-investigation") == "verdict"
    assert phase_for_skill("scotri:impact-analysis") == "impact"  # plugin-style name
    for support in ("presence-claim", "databricks-query", "lineage-trace", "fabric-read", None, ""):
        assert phase_for_skill(support) is None

    p = EngagementProgress.start("j", T0)
    assert p.phase == STARTING
    p = p.with_tool_call(None, T0)  # first plain tool call: no stage yet
    assert p.phase == WORKING
    p = p.with_tool_call("jira-intake", T0)
    p = p.with_tool_call("databricks-query", T0)  # support skill inside intake leaves it there
    assert p.phase == "intake"
    p = p.with_tool_call("bug-investigation", T0 + timedelta(seconds=30))
    assert p.phase == "verdict"
    assert p.phases_seen == (STARTING, WORKING, "intake", "verdict")
    assert p.tool_calls == 4
    assert p.last_activity_at == T0 + timedelta(seconds=30)


def test_public_view_is_phase_counts_and_times_only():
    p = EngagementProgress.start("j", T0).with_session("sess-123", T0).with_tool_call("impact-analysis", T0)
    view = p.public_view(T0 + timedelta(seconds=95))
    assert set(view) == {"phase", "phase_label", "phases_seen", "tool_calls", "started_at",
                         "last_activity_at", "elapsed_seconds"}
    assert "sess-123" not in str(view)
    assert view["phase_label"] == "Analysing impact"
    assert view["elapsed_seconds"] == 95


def test_progress_round_trips_through_json():
    p = EngagementProgress.start("j", T0).with_session("s", T0).with_tool_call("handoff-doc", T0)
    assert EngagementProgress.from_json(p.to_json()) == p


# --- the engine ----------------------------------------------------------------------------

def test_run_engagement_records_progress_and_the_session_while_running():
    store, log, progress = FakeJobStore(), FakeLog(), FakeProgressStore()
    job_id = start_engagement(store, PRINCIPAL, "ACME-1 please")
    runtime = EventRuntime(
        [AgentEvent(kind="session", session_id="sess-1"),
         AgentEvent(kind="tool_call", tool="Skill", skill="jira-intake"),
         AgentEvent(kind="tool_call", tool="Bash"),
         AgentEvent(kind="tool_call", tool="Skill", skill="bug-investigation")],
        AgentRunResult(text="verdict", is_error=False, session_id="sess-1"),
        progress_store=progress,
    )

    run_engagement(store, runtime, log, job_id, "/ws", progress_store=progress)

    assert runtime.seen.session_id == "sess-1"  # known mid-run, not only at the end
    assert runtime.seen.phase == "verdict"
    assert runtime.seen.tool_calls == 3
    assert store.get(job_id).status == EngagementStatus.SUCCEEDED


def test_a_run_ending_without_a_session_keeps_the_one_captured_mid_run():
    """A timeout returns no session_id; the job must still be continuable as a follow-up."""
    store, log, progress = FakeJobStore(), FakeLog(), FakeProgressStore()
    job_id = start_engagement(store, PRINCIPAL, "long one")
    runtime = EventRuntime([AgentEvent(kind="session", session_id="sess-9")],
                           AgentRunResult(text="timed out", is_error=True, incomplete=True))

    run_engagement(store, runtime, log, job_id, "/ws", progress_store=progress)

    assert store.get(job_id).status == EngagementStatus.FAILED
    assert store.get(job_id).session_id == "sess-9"


def test_a_failing_progress_store_never_fails_the_run():
    class Broken:
        def put(self, progress):
            raise RuntimeError("redis down")

        def get(self, job_id):
            raise RuntimeError("redis down")

    store, log = FakeJobStore(), FakeLog()
    job_id = start_engagement(store, PRINCIPAL, "x")
    runtime = EventRuntime([AgentEvent(kind="tool_call", tool="Bash")],
                           AgentRunResult(text="ok", is_error=False, session_id="s"))
    run_engagement(store, runtime, log, job_id, "/ws", progress_store=Broken())
    assert store.get(job_id).status == EngagementStatus.SUCCEEDED


def test_restart_sweep_fails_the_job_but_keeps_its_session():
    store, progress = FakeJobStore(), FakeProgressStore()
    with_session = start_engagement(store, PRINCIPAL, "a", origin="api")
    without = start_engagement(store, PRINCIPAL, "b", origin="api")
    for job_id in (with_session, without):
        store.update(store.get(job_id).with_status(EngagementStatus.RUNNING))
    progress.put(EngagementProgress.start(with_session, T0).with_session("sess-r", T0))

    assert fail_interrupted_engagements(store, origin="api", progress_store=progress) == 2

    kept = store.get(with_session)
    assert kept.status == EngagementStatus.FAILED and kept.session_id == "sess-r"
    assert "follow-up" in kept.error
    assert store.get(without).session_id is None


def test_steps_are_readable_mid_run_from_the_captured_session():
    class Reader:
        def __init__(self):
            self.calls = []

        def read_turn(self, session_id, prompt, not_before):
            self.calls.append(session_id)
            return []

    store, progress, reader = FakeJobStore(), FakeProgressStore(), Reader()
    job_id = start_engagement(store, PRINCIPAL, "x")
    store.update(store.get(job_id).with_status(EngagementStatus.RUNNING))
    progress.put(EngagementProgress.start(job_id, T0).with_session("sess-live", T0))

    get_job_trace(store, reader, job_id, progress_store=progress)
    assert reader.calls == ["sess-live"]


# --- the SDK adapter -------------------------------------------------------------------------

def test_adapter_reports_the_init_session_and_each_tool_call_without_inputs():
    init = SystemMessage(subtype="init", data={"session_id": "sess-a", "cwd": "/ws"})
    assistant = AssistantMessage(
        content=[
            TextBlock(text="Reading the ticket"),
            ToolUseBlock(id="t1", name="Skill", input={"skill": "jira-intake"}),
            ToolUseBlock(id="t2", name="Bash", input={"command": "databricks-select ... gold.secret_table"}),
        ],
        model="m",
    )
    assert _events(init) == [AgentEvent(kind="session", session_id="sess-a")]
    events = _events(assistant)
    assert events == [AgentEvent(kind="tool_call", tool="Skill", skill="jira-intake"),
                      AgentEvent(kind="tool_call", tool="Bash")]
    assert "secret_table" not in str(events)
    assert _events(SystemMessage(subtype="other", data={"session_id": "x"})) == []


# --- the poll endpoint ---------------------------------------------------------------------

def test_poll_response_carries_progress_and_nothing_identifying(monkeypatch):
    from fastapi.testclient import TestClient

    import app.api as api

    job = Engagement(job_id="job1", prompt="Why is ACME-1234 wrong in gold.vw_sales?", principal=PRINCIPAL,
                     status=EngagementStatus.RUNNING, created_at=T0)
    progress = FakeProgressStore()
    progress.put(EngagementProgress.start("job1", T0).with_session("sess-x", T0)
                 .with_tool_call("bug-investigation", T0))

    class _Store:
        def get(self, job_id):
            return job if job_id == "job1" else None

    class _Container:
        pass

    fake = _Container()
    fake.job_store, fake.progress_store = _Store(), progress
    monkeypatch.setattr(api, "container", fake)
    monkeypatch.setattr(api, "_authenticate", lambda authorization: PRINCIPAL)
    client = TestClient(api.app, raise_server_exceptions=False)

    for path in ("/v1/chat/job1", "/v1/engagements/job1"):
        body = client.get(path, headers={"Authorization": "Bearer t"}).json()
        assert body["progress"]["phase"] == "verdict"
        assert body["progress"]["tool_calls"] == 1
        assert "sess-x" not in str(body["progress"])
        assert "ACME-1234" not in str(body["progress"]) and "vw_sales" not in str(body["progress"])


def test_elapsed_stops_at_the_last_activity_once_the_job_is_finished():
    from datetime import timedelta
    start = datetime(2026, 10, 8, 4, 0, tzinfo=timezone.utc)
    p = EngagementProgress.start("j", start).with_tool_call(None, start + timedelta(seconds=90))
    later = start + timedelta(hours=2)
    assert p.public_view(later)["elapsed_seconds"] == 7200
    assert p.public_view(later, finished=True)["elapsed_seconds"] == 90
