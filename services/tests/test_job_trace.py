# Copyright (c) 2026 Preyas Kulshrestha. Part of the Scotri engine; MIT License (see LICENSE).
# Keep this notice in copies and derived works, including AI-assisted ones.
"""Reading back the steps an engagement job took — adapters/agent_sdk/transcript_reader.py (finding
the right turn in a shared session file), domain/trace_policy.py (redaction, truncation, not-found
cases), and the owner-only GET .../steps endpoint. Transcripts here are small synthetic files in the
Agent SDK's format, not the real ones."""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from adapters.agent_sdk.transcript_reader import ClaudeTranscriptReader
from domain.engagement import Engagement, EngagementStatus
from domain.errors import NotFoundError
from domain.models import Principal, TraceStep
from domain.trace_policy import MAX_FIELD_CHARS, get_job_trace, redact_secrets

SESSION = "00000000-0000-0000-0000-000000000013"
T0 = datetime(2026, 9, 27, 2, 16, 7, tzinfo=timezone.utc)
PRINCIPAL = Principal(subject="alice", display_name="Alice", platform="test", roles=[])


def _ts(offset_s: float) -> str:
    return (T0 + timedelta(seconds=offset_s)).isoformat().replace("+00:00", "Z")


def _prompt(text: str, at: float) -> dict:
    return {"type": "user", "isSidechain": False, "timestamp": _ts(at), "message": {"role": "user", "content": text}}


def _tool_call(call_id: str, name: str, tool_input: dict, at: float) -> dict:
    return {
        "type": "assistant", "isSidechain": False, "timestamp": _ts(at),
        "message": {"role": "assistant", "content": [{"type": "tool_use", "id": call_id, "name": name, "input": tool_input}]},
    }


def _tool_result(call_id: str, output: str, at: float) -> dict:
    return {
        "type": "user", "isSidechain": False, "timestamp": _ts(at),
        "message": {"role": "user", "content": [{"type": "tool_result", "tool_use_id": call_id, "content": output}]},
    }


def _text(text: str, at: float) -> dict:
    return {
        "type": "assistant", "isSidechain": False, "timestamp": _ts(at),
        "message": {"role": "assistant", "content": [{"type": "text", "text": text}]},
    }


def _write_session(config_dir: Path, records: list[dict], session_id: str = SESSION) -> None:
    folder = config_dir / "projects" / "-some-encoded-cwd"
    folder.mkdir(parents=True)
    lines = [json.dumps(r) for r in records] + ["{not json — a half-written trailing line"]
    (folder / f"{session_id}.jsonl").write_text("\n".join(lines), encoding="utf-8")


def _two_turn_session() -> list[dict]:
    return [
        {"type": "queue-operation", "operation": "enqueue", "timestamp": _ts(0)},
        _prompt("investigate it", 1),
        {"type": "assistant", "isSidechain": False, "timestamp": _ts(2),
         "message": {"role": "assistant", "content": [{"type": "thinking", "thinking": "hmm"}]}},
        _tool_call("c1", "Read", {"file_path": "/w/registry.md"}, 3),
        _tool_result("c1", "registry contents", 4),
        _tool_call("c2", "Bash", {"command": "ls mirror", "description": "list"}, 5),
        _tool_result("c2", "", 6),
        _text("found nothing", 7),
        _prompt("investigate it", 3600),  # same prompt again, an hour later — a different job
        _tool_call("c3", "Bash", {"command": "cat status.json"}, 3601),
        _tool_result("c3", "ok", 3602),
    ]


def test_reader_slices_the_right_turn_and_pairs_results(tmp_path):
    _write_session(tmp_path, _two_turn_session())
    reader = ClaudeTranscriptReader(tmp_path)

    first = reader.read_turn(SESSION, "investigate it", not_before=T0)
    assert [(s.kind, s.tool) for s in first] == [("tool_call", "Read"), ("tool_call", "Bash"), ("message", None)]
    assert first[0].input_summary == "/w/registry.md"
    assert first[0].result_preview == "registry contents"
    assert first[1].input_summary == "ls mirror"
    assert first[2].text == "found nothing"

    # Same prompt text, later job: the not_before time must select the second occurrence.
    second = reader.read_turn(SESSION, "investigate it", not_before=T0 + timedelta(seconds=3599))
    assert [s.input_summary for s in second] == ["cat status.json"]


def test_reader_returns_none_when_session_or_turn_missing(tmp_path):
    _write_session(tmp_path, _two_turn_session())
    reader = ClaudeTranscriptReader(tmp_path)
    assert reader.read_turn("11111111-2222-3333-4444-555555555555", "investigate it", T0) is None
    assert reader.read_turn(SESSION, "a prompt that was never sent", T0) is None
    assert reader.read_turn(SESSION, "investigate it", not_before=T0 + timedelta(days=1)) is None


def test_reader_finds_a_turn_whose_prompt_the_engine_prefixed(tmp_path):
    """OPEN_ITEMS.md #61: the agent is sent run-context / pre-fetch / attachment blocks above the
    caller's prompt, but the job stores only the caller's prompt."""
    sent = ("[Run context] You are working for Alice.\n\n<scotri-prefetch>\nJira: fetched\n</scotri-prefetch>"
            "\n\n[Attached files] 1 file\n- scratch/uploads/x/brd.md\n\ninvestigate it")
    _write_session(tmp_path, [_prompt(sent, 1), _tool_call("c1", "Read", {"file_path": "/w/a.md"}, 2),
                              _tool_result("c1", "a", 3)])
    reader = ClaudeTranscriptReader(tmp_path)
    assert [s.input_summary for s in reader.read_turn(SESSION, "investigate it", T0)] == ["/w/a.md"]


def test_reader_does_not_match_a_prompt_that_only_shares_its_ending(tmp_path):
    _write_session(tmp_path, [_prompt("please say yes", 1), _text("no", 2)])
    reader = ClaudeTranscriptReader(tmp_path)
    assert reader.read_turn(SESSION, "yes", T0) is None


def test_reader_rejects_a_session_id_that_could_steer_the_glob(tmp_path):
    _write_session(tmp_path, _two_turn_session())
    reader = ClaudeTranscriptReader(tmp_path)
    assert reader.read_turn("../*", "investigate it", T0) is None
    assert reader.read_turn("*", "investigate it", T0) is None


@pytest.mark.parametrize(
    "raw",
    [
        "DATABRICKS_CLIENT_SECRET=abc123supersecret",
        'JIRA_API_TOKEN: "ATATT3xFfGF0abcdef"',
        '{"password": "hunter2hunter2"}',
        "curl -H 'Authorization: Bearer eyAbCdEf123456789xyz' https://x",
    ],
)
def test_redact_secrets_removes_the_value(raw):
    cleaned = redact_secrets(raw)
    assert "[REDACTED]" in cleaned
    for leaked in ("abc123supersecret", "ATATT3xFfGF0abcdef", "hunter2hunter2", "eyAbCdEf123456789xyz"):
        assert leaked not in cleaned


def test_redact_secrets_leaves_ordinary_text_alone():
    text = "SELECT COUNT(*) FROM gold.vw_dim_store_region -- 991 rows"
    assert redact_secrets(text) == text


class _FakeJobStore:
    def __init__(self, *engagements: Engagement):
        self.jobs = {e.job_id: e for e in engagements}

    def get(self, job_id):
        return self.jobs.get(job_id)


class _FakeReader:
    def __init__(self, steps):
        self.steps, self.calls = steps, []

    def read_turn(self, session_id, prompt, not_before):
        self.calls.append((session_id, prompt, not_before))
        return self.steps


def _engagement(**overrides) -> Engagement:
    fields = dict(
        job_id="job1", prompt="investigate it", principal=PRINCIPAL, status=EngagementStatus.SUCCEEDED,
        created_at=T0, session_id=SESSION,
    )
    fields.update(overrides)
    return Engagement(**fields)


def test_get_job_trace_sanitizes_free_text():
    long_output = "x" * (MAX_FIELD_CHARS + 200)
    steps = [TraceStep(kind="tool_call", tool="Bash", input_summary="cat .env # TOKEN=abcd1234efgh", result_preview=long_output)]
    trace = get_job_trace(_FakeJobStore(_engagement()), _FakeReader(steps), "job1")

    step = trace.steps[0]
    assert "abcd1234efgh" not in step.input_summary
    assert len(step.result_preview) < len(long_output)
    assert step.result_preview.endswith("[+200 chars]")


def test_get_job_trace_uses_the_resumed_session_when_the_job_never_recorded_one():
    reader = _FakeReader([])
    job = _engagement(session_id=None, resume_session_id=SESSION, status=EngagementStatus.FAILED)
    trace = get_job_trace(_FakeJobStore(job), reader, "job1")
    assert trace.steps == []  # an empty list is "turn found, no steps" — not an error
    assert reader.calls == [(SESSION, "investigate it", T0)]


def test_get_job_trace_not_found_cases():
    with pytest.raises(NotFoundError, match="No engagement"):
        get_job_trace(_FakeJobStore(), _FakeReader([]), "nope")
    no_session = _engagement(session_id=None, status=EngagementStatus.CANCELLED)
    with pytest.raises(NotFoundError, match="no agent session"):
        get_job_trace(_FakeJobStore(no_session), _FakeReader([]), "job1")
    with pytest.raises(NotFoundError, match="transcript was not found"):
        get_job_trace(_FakeJobStore(_engagement()), _FakeReader(None), "job1")


# --- the endpoint -------------------------------------------------------------------------

def _client(monkeypatch, job_store, reader, caller: Principal):
    from fastapi.testclient import TestClient

    import app.api as api

    class _Container:
        pass

    class _NoProgress:
        def get(self, job_id):
            return None

    fake = _Container()
    fake.job_store, fake.trace_reader, fake.progress_store = job_store, reader, _NoProgress()
    monkeypatch.setattr(api, "container", fake)
    monkeypatch.setattr(api, "_authenticate", lambda authorization: caller)
    return TestClient(api.app, raise_server_exceptions=False)


def test_steps_endpoint_returns_steps_to_the_owner(monkeypatch):
    steps = [TraceStep(kind="tool_call", tool="Read", input_summary="/w/a.md", result_preview="hi")]
    client = _client(monkeypatch, _FakeJobStore(_engagement()), _FakeReader(steps), PRINCIPAL)
    for path in ("/v1/chat/job1/steps", "/v1/engagements/job1/steps"):
        body = client.get(path).json()
        assert body["status"] == "succeeded"
        assert body["steps"][0]["tool"] == "Read"


def test_steps_endpoint_refuses_a_non_owner_and_reads_nothing(monkeypatch):
    reader = _FakeReader([])
    other = Principal(subject="bob", display_name="Bob", platform="test", roles=[])
    client = _client(monkeypatch, _FakeJobStore(_engagement()), reader, other)
    assert client.get("/v1/chat/job1/steps").status_code == 403
    assert reader.calls == []


def test_steps_endpoint_404s_for_unknown_job_and_missing_trace(monkeypatch):
    client = _client(monkeypatch, _FakeJobStore(_engagement()), _FakeReader(None), PRINCIPAL)
    assert client.get("/v1/chat/unknown/steps").status_code == 404
    assert client.get("/v1/chat/job1/steps").status_code == 404
