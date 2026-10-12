"""domain/grounding.py and its engine hook (OPEN_ITEMS.md #68): checkable claims in an answer must
appear in what the run saw; unsupported ones go back to the agent once, in the same session."""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

from adapters.agent_sdk.transcript_reader import ClaudeTranscriptReader
from domain.engagement import EngagementStatus
from domain.engagement_policy import run_engagement, start_engagement
from domain.grounding import TAG, check_grounding, extract_claims
from ports.agent_runtime import AgentRunResult
from tests.test_engagement_cancel import PRINCIPAL, FakeJobStore, FakeLog

# --- the pure check ------------------------------------------------------------------------------


def test_extracts_large_numbers_tables_and_files_but_not_dates_years_or_code():
    answer = """The table acme_prod.gold.store_visit_report has 98,765 extra rows (4x, 58 keys) and 12.5%
are duplicates since 2026-09-01 at 12:02, in 2026. See Gold_Notebooks/OTHER/Store_Visit.ipynb.

```sql
SELECT 999999 FROM acme_prod.gold.not_a_claim
```
"""
    assert extract_claims(answer) == ["acme_prod.gold.store_visit_report", "Store_Visit.ipynb", "98,765", "12.5%"]


def test_numbers_match_with_or_without_digit_grouping():
    assert check_grounding("194,815 rows", '{"rows": [[194815]]}').ok
    assert check_grounding("194815 rows", "count: 194,815").ok
    assert not check_grounding("194,815 rows", "count: 1194815").ok  # whole tokens only


def test_unsupported_claims_are_listed():
    report = check_grounding("acme_prod.gold.a has 5,000 rows and acme_prod.gold.b has 7,000",
                             "DESCRIBE acme_prod.gold.a ... 5000")
    assert report.checked == 4
    assert report.unsupported == ["acme_prod.gold.b", "7,000"]


# --- the engine hook -----------------------------------------------------------------------------

LEARNING = ('\n\n```scotri-learning\n{"title": "t", "verdict": "question", "takeaways": ["k"]}\n```')


class ScriptedRuntime:
    """Returns one result per run() call, in order, and records each call."""

    def __init__(self, *results: AgentRunResult) -> None:
        self.results = list(results)
        self.calls: list[dict] = []

    def run(self, prompt, cwd, resume_session_id=None, job_id=None, env=None, on_event=None):
        self.calls.append({"prompt": prompt, "resume": resume_session_id})
        return self.results.pop(0)

    def cancel(self, job_id):
        return True


def _run(runtime, evidence):
    store, log = FakeJobStore(), FakeLog()
    job_id = start_engagement(store, PRINCIPAL, "how many rows?")
    run_engagement(store, runtime, log, job_id, "/ws", grounding_evidence=lambda s, p, t: evidence)
    return store.get(job_id)


def test_a_supported_answer_runs_once():
    runtime = ScriptedRuntime(AgentRunResult(text="It has 194,815 rows." + LEARNING, is_error=False, session_id="s1"))
    final = _run(runtime, "count(1) = 194815")
    assert len(runtime.calls) == 1
    assert final.status == EngagementStatus.SUCCEEDED and final.result == "It has 194,815 rows."


def test_an_unsupported_claim_goes_back_once_and_the_correction_replaces_the_answer():
    runtime = ScriptedRuntime(
        AgentRunResult(text="It has 250,000 rows." + LEARNING, is_error=False, session_id="s1"),
        AgentRunResult(text="It has 194,815 rows (checked)." + LEARNING, is_error=False, session_id="s1"),
    )
    final = _run(runtime, "count(1) = 194815")
    assert len(runtime.calls) == 2
    assert runtime.calls[1]["resume"] == "s1"
    assert runtime.calls[1]["prompt"].startswith(TAG) and "`250,000`" in runtime.calls[1]["prompt"]
    assert final.status == EngagementStatus.SUCCEEDED
    assert final.result == "It has 194,815 rows (checked)."  # learning block still stripped


def test_a_failed_correction_keeps_the_answer_with_a_note():
    runtime = ScriptedRuntime(
        AgentRunResult(text="It has 250,000 rows." + LEARNING, is_error=False, session_id="s1"),
        AgentRunResult(text="boom", is_error=True, session_id="s1"),
    )
    final = _run(runtime, "count(1) = 194815")
    assert final.status == EngagementStatus.SUCCEEDED
    assert final.result.startswith("It has 250,000 rows.")
    assert "Grounding check" in final.result and "`250,000`" in final.result


def test_no_transcript_means_no_check():
    runtime = ScriptedRuntime(AgentRunResult(text="It has 250,000 rows." + LEARNING, is_error=False, session_id="s1"))
    final = _run(runtime, None)
    assert len(runtime.calls) == 1 and final.result == "It has 250,000 rows."


# --- the evidence reader -------------------------------------------------------------------------


def _write_session(tmp_path, records):
    folder = tmp_path / "projects" / "C--ws"
    folder.mkdir(parents=True)
    path = folder / "0a1b2c3d-1234-abcd.jsonl"
    path.write_text("\n".join(json.dumps(r) for r in records), encoding="utf-8")
    return folder


def test_evidence_has_results_commands_batch_files_and_saved_outputs_but_not_prose(tmp_path):
    ts = datetime.now(timezone.utc).isoformat()
    folder = _write_session(tmp_path, [])
    saved = folder / "0a1b2c3d-1234-abcd" / "tool-results" / "big.txt"
    saved.parent.mkdir(parents=True)
    saved.write_text("row_count 136610", encoding="utf-8")
    outside = tmp_path / "secret.txt"
    outside.write_text("must not be read", encoding="utf-8")
    records = [
        {"type": "user", "timestamp": ts, "message": {"content": "how many rows?"}},
        {"type": "assistant", "timestamp": ts, "message": {"content": [
            {"type": "text", "text": "I think 999,999 rows"},
            {"type": "tool_use", "id": "a", "name": "Bash", "input": {"command": "app.cli databricks-select q1"}},
            {"type": "tool_use", "id": "b", "name": "Write", "input": {"file_path": "scratch/x-batch.json", "content": '[["q2"]]'}},
            {"type": "tool_use", "id": "c", "name": "Write", "input": {"file_path": "scratch/x.md", "content": "claim 424242"}},
        ]}},
        {"type": "user", "timestamp": ts, "message": {"content": [
            {"type": "tool_result", "tool_use_id": "a", "content": f"Full output saved to: {saved}"},
            {"type": "tool_result", "tool_use_id": "b", "content": f"Full output saved to: {outside}"},
        ]}},
        {"type": "user", "timestamp": ts, "message": {"content": f"{TAG} These value(s): `777,777`"}},
    ]
    (folder / "0a1b2c3d-1234-abcd.jsonl").write_text("\n".join(json.dumps(r) for r in records), encoding="utf-8")

    evidence = ClaudeTranscriptReader(tmp_path).read_turn_evidence(
        "0a1b2c3d-1234-abcd", "how many rows?", datetime.now(timezone.utc) - timedelta(minutes=1))
    assert "q1" in evidence and '[["q2"]]' in evidence and "136610" in evidence
    for absent in ("999,999", "424242", "must not be read", "777,777"):
        assert absent not in evidence


def test_steps_continue_through_the_correction_turn(tmp_path):
    ts = datetime.now(timezone.utc).isoformat()
    folder = _write_session(tmp_path, [])
    records = [
        {"type": "user", "timestamp": ts, "message": {"content": "how many rows?"}},
        {"type": "assistant", "timestamp": ts, "message": {"content": [
            {"type": "tool_use", "id": "a", "name": "Bash", "input": {"command": "first"}}]}},
        {"type": "user", "timestamp": ts, "message": {"content": f"{TAG} check `250,000`"}},
        {"type": "assistant", "timestamp": ts, "message": {"content": [
            {"type": "tool_use", "id": "b", "name": "Bash", "input": {"command": "second"}}]}},
        {"type": "user", "timestamp": ts, "message": {"content": "next question"}},
        {"type": "assistant", "timestamp": ts, "message": {"content": [
            {"type": "tool_use", "id": "c", "name": "Bash", "input": {"command": "third"}}]}},
    ]
    (folder / "0a1b2c3d-1234-abcd.jsonl").write_text("\n".join(json.dumps(r) for r in records), encoding="utf-8")
    steps = ClaudeTranscriptReader(tmp_path).read_turn(
        "0a1b2c3d-1234-abcd", "how many rows?", datetime.now(timezone.utc) - timedelta(minutes=1))
    assert [s.input_summary for s in steps] == ["first", "second"]
