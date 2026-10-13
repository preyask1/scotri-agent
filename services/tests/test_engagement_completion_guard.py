# Copyright (c) 2026 Preyas Kulshrestha. Part of the Scotri engine; MIT License (see LICENSE).
# Keep this notice in copies and derived works, including AI-assisted ones.
"""The two guards against a false-green engagement (ACME-2085, 2026-09-28).

Background: the SDK tears down the CLI subprocess the moment its message iterator ends, and the
iterator ends at ResultMessage — the moment the top-level turn returns text. So a turn that spawns a
background subagent and says "I'll report back" kills that subagent instantly, while the turn itself
looks entirely successful. The engagement then lands as SUCCEEDED with a promise as its result.

Guard 1 (adapter): the `Agent` tool is denied, so the lifecycle has to run inline in the turn.
Guard 2 (policy): a run that stopped early is recorded INCOMPLETE, never SUCCEEDED.
"""
from __future__ import annotations

from claude_agent_sdk import AssistantMessage, ResultMessage, TextBlock, ToolUseBlock

from adapters.agent_sdk import claude_agent_adapter
from adapters.agent_sdk.claude_agent_adapter import ClaudeAgentSdkRuntime
from domain.engagement import EngagementStatus, is_terminal
from domain.engagement_policy import cancel_engagement, run_engagement, start_engagement
from ports.agent_runtime import AgentRunResult
from tests.test_engagement_cancel import PRINCIPAL, FakeJobStore, FakeLog, FakeRuntime


def _result_message(subtype: str, text: str = "partial work") -> ResultMessage:
    return ResultMessage(
        subtype=subtype,
        duration_ms=1,
        duration_api_ms=1,
        is_error=False,
        num_turns=1,
        session_id="s1",
        result=text,
    )


def _run_capturing_options(monkeypatch, message: ResultMessage) -> dict:
    """Run the real adapter against a fake SDK query(), returning the captured call."""
    captured: dict = {}

    async def fake_query(prompt, options):
        captured["options"] = options
        yield message

    monkeypatch.setattr(claude_agent_adapter, "query", fake_query)
    captured["result"] = ClaudeAgentSdkRuntime().run("hi", "/ws")
    return captured


# --- Guard 1: no background delegation -------------------------------------------------------


def test_the_agent_tool_is_denied_so_the_lifecycle_cannot_be_handed_to_a_background_subagent(
    monkeypatch,
):
    captured = _run_capturing_options(monkeypatch, _result_message("success"))
    assert "Agent" in captured["options"].disallowed_tools


def test_send_message_is_denied_so_work_cannot_be_handed_to_a_peer_session(monkeypatch):
    """OPEN_ITEMS.md #73: an engagement used ListAgents + SendMessage to push work elsewhere and end
    its turn. Denied both as a disallowed tool and by the PreToolUse scope check."""
    from domain.tool_scope import check_tool_call

    captured = _run_capturing_options(monkeypatch, _result_message("success"))
    assert {"SendMessage", "ListAgents"} <= set(captured["options"].disallowed_tools)
    assert check_tool_call("SendMessage", {"to": "x", "message": "do it"}, ".") is not None


# --- Guard 2: early stops are not success ----------------------------------------------------


def test_adapter_flags_a_turn_limit_stop_as_incomplete_even_though_it_did_not_error(monkeypatch):
    """error_max_turns arrives with is_error=False and real partial text — the case that would
    otherwise sail through as a successful run."""
    captured = _run_capturing_options(monkeypatch, _result_message("error_max_turns"))
    result = captured["result"]
    assert result.incomplete is True
    assert result.is_error is False
    assert result.text == "partial work"


def test_adapter_flags_a_mid_execution_death_as_incomplete(monkeypatch):
    captured = _run_capturing_options(monkeypatch, _result_message("error_during_execution"))
    assert captured["result"].incomplete is True


def test_adapter_does_not_flag_a_normal_completion(monkeypatch):
    captured = _run_capturing_options(monkeypatch, _result_message("success", "the real answer"))
    assert captured["result"].incomplete is False
    assert captured["result"].text == "the real answer"


def _run_stream(monkeypatch, messages: list):
    async def fake_query(prompt, options):
        for message in messages:
            yield message

    monkeypatch.setattr(claude_agent_adapter, "query", fake_query)
    return ClaudeAgentSdkRuntime().run("hi", "/ws")


def _say(text: str, parent: str | None = None) -> AssistantMessage:
    return AssistantMessage(content=[TextBlock(text=text)], model="m", parent_tool_use_id=parent)


def test_an_answer_given_before_later_tool_calls_is_kept_in_the_result(monkeypatch):
    """Engagement 2577baf2 (ACME-2052): the answer to the user's question came mid-run, and only the
    closing summary reached the result."""
    result = _run_stream(monkeypatch, [
        _say("Here is why: the intake stage never queried the schema."),
        AssistantMessage(content=[ToolUseBlock(id="t1", name="Bash", input={})], model="m"),
        _say("Batch done: all three tables check out."),
        _result_message("success", "Batch done: all three tables check out."),
    ])
    assert result.text == ("Here is why: the intake stage never queried the schema.\n\n"
                           "Batch done: all three tables check out.")


def test_a_single_message_run_reads_exactly_as_before(monkeypatch):
    result = _run_stream(monkeypatch, [_say("the real answer"), _result_message("success", "the real answer")])
    assert result.text == "the real answer"


def test_subagent_text_is_not_part_of_the_result(monkeypatch):
    result = _run_stream(monkeypatch, [_say("inner chatter", parent="tool-1"), _say("answer"),
                                       _result_message("success", "answer")])
    assert result.text == "answer"


def test_an_incomplete_run_is_recorded_incomplete_and_keeps_its_partial_work():
    store, log = FakeJobStore(), FakeLog()
    runtime = FakeRuntime(
        result=AgentRunResult(text="half an analysis", is_error=False, session_id="s1", incomplete=True)
    )
    job_id = start_engagement(store, PRINCIPAL, "analyse ACME-2085")

    run_engagement(store, runtime, log, job_id, "/ws")

    job = store.jobs[job_id]
    assert job.status == EngagementStatus.INCOMPLETE
    assert job.result == "half an analysis"  # partial work is kept, not thrown away
    assert "Stopped before finishing" in (job.error or "")
    assert [e.outcome for e in log.entries] == ["incomplete"]


def test_incomplete_is_terminal_so_a_polling_caller_stops_waiting():
    assert is_terminal(EngagementStatus.INCOMPLETE)


def test_a_normal_run_is_still_recorded_succeeded_with_no_error():
    store, log = FakeJobStore(), FakeLog()
    runtime = FakeRuntime(result=AgentRunResult(text="the real answer", is_error=False, session_id="s1"))
    job_id = start_engagement(store, PRINCIPAL, "analyse ACME-2085")

    run_engagement(store, runtime, log, job_id, "/ws")

    job = store.jobs[job_id]
    assert job.status == EngagementStatus.SUCCEEDED
    assert job.result == "the real answer"
    assert job.error is None
    assert [e.outcome for e in log.entries] == ["succeeded"]


def test_a_user_cancel_still_wins_over_an_incomplete_result():
    """Cancelling must not be downgraded to "incomplete" just because the interrupted run also
    stopped early — the caller asked for this one."""
    store, log = FakeJobStore(), FakeLog()
    job_id = start_engagement(store, PRINCIPAL, "analyse ACME-2085")
    runtime = FakeRuntime(
        result=AgentRunResult(text="half", is_error=False, incomplete=True),
        on_run=lambda: cancel_engagement(store, runtime, job_id),
    )

    run_engagement(store, runtime, log, job_id, "/ws")

    assert store.jobs[job_id].status == EngagementStatus.CANCELLED
    assert [e.outcome for e in log.entries] == ["cancelled"]


def test_incomplete_status_round_trips_through_the_job_store():
    store, log = FakeJobStore(), FakeLog()
    runtime = FakeRuntime(result=AgentRunResult(text="half", is_error=False, incomplete=True))
    job_id = start_engagement(store, PRINCIPAL, "analyse ACME-2085")
    run_engagement(store, runtime, log, job_id, "/ws")

    from domain.engagement import Engagement

    back = Engagement.from_json(store.jobs[job_id].to_json())
    assert back.status == EngagementStatus.INCOMPLETE


# --- Turn and cost ceilings (OPEN_ITEMS.md #33) -----------------------------------------------


def test_ceilings_reach_the_sdk_and_turns_and_cost_come_back(monkeypatch):
    message = ResultMessage(subtype="success", duration_ms=1, duration_api_ms=1, is_error=False,
                            num_turns=12, session_id="s1", result="done", total_cost_usd=0.4321)
    captured = {}

    async def fake_query(prompt, options):
        captured["options"] = options
        yield message

    monkeypatch.setattr(claude_agent_adapter, "query", fake_query)
    result = ClaudeAgentSdkRuntime(max_turns=150, max_budget_usd=2.5).run("hi", "/ws")
    assert captured["options"].max_turns == 150 and captured["options"].max_budget_usd == 2.5
    assert result.num_turns == 12 and result.cost_usd == 0.4321 and not result.incomplete


def test_the_default_runtime_has_no_budget_ceiling(monkeypatch):
    captured = _run_capturing_options(monkeypatch, _result_message("success"))
    assert captured["options"].max_budget_usd is None


def test_container_reads_the_ceilings_from_the_environment(monkeypatch):
    from app.container import build_container
    monkeypatch.setenv("ENGAGEMENT_MAX_TURNS", "80")
    monkeypatch.setenv("ENGAGEMENT_MAX_BUDGET_USD", "3")
    runtime = build_container().__class__.agent_runtime.func(build_container())
    assert runtime._max_turns == 80 and runtime._max_budget_usd == 3.0
    monkeypatch.setenv("ENGAGEMENT_MAX_TURNS", "0")
    monkeypatch.setenv("ENGAGEMENT_MAX_BUDGET_USD", "")
    runtime = build_container().__class__.agent_runtime.func(build_container())
    assert runtime._max_turns is None and runtime._max_budget_usd is None


# --- secrets never reach the job store or history (OPEN_ITEMS.md #19) ---------------------------


def test_results_and_errors_are_redacted_before_they_are_stored():
    secret = "Server=db;User=sa;Password=hunter2hunter2;"
    for result in (AgentRunResult(text=f"Found it: {secret}", is_error=False, session_id="s"),
                   AgentRunResult(text=f"Failed near {secret}", is_error=True, session_id="s"),
                   AgentRunResult(text=f"Partial {secret}", is_error=False, session_id="s", incomplete=True)):
        store, log = FakeJobStore(), FakeLog()
        recorded = []
        history = type("H", (), {"record": lambda self, e: recorded.append(e),
                                 "record_learning": lambda self, l: None})()
        job_id = start_engagement(store, PRINCIPAL, "ACME-1")
        run_engagement(store, FakeRuntime(result=result), log, job_id, "/ws", engagement_history=history)
        final = store.get(job_id)
        for stored in (final, recorded[-1]):
            assert "hunter2hunter2" not in (stored.result or "") and "hunter2hunter2" not in (stored.error or "")
