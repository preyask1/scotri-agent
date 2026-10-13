# Copyright (c) 2026 Preyas Kulshrestha. Part of the Scotri engine; MIT License (see LICENSE).
# Keep this notice in copies and derived works, including AI-assisted ones.
"""Sharing an engagement's markdown deliverables and attaching uploads to a prompt (OPEN_ITEMS.md #6).

Covers the local store adapter, domain/shared_files.py, the engine wiring in run_engagement, the
Agent SDK adapter's file-write capture, and the HTTP routes. No Redis or Azure needed: the job store
and history are in-memory fakes and the store is a temp folder.
"""
from __future__ import annotations

from pathlib import Path

import pytest
from claude_agent_sdk import AssistantMessage, ResultMessage, ToolUseBlock

from adapters.agent_sdk import claude_agent_adapter
from adapters.agent_sdk.claude_agent_adapter import ClaudeAgentSdkRuntime
from adapters.artifact_store.local_fs_adapter import LocalFsArtifactStore
from domain.engagement import EngagementStatus
from domain.engagement_policy import run_engagement, start_engagement
from domain.errors import NotFoundError, PolicyError
from domain.models import Principal
from domain.shared_files import (
    UploadPolicy,
    list_engagement_files,
    publish_engagement_files,
    read_engagement_file,
    resolve_uploads,
    sanitize_filename,
    stage_attachments,
    store_upload,
)
from ports.agent_runtime import AgentRunResult
from tests.test_engagement_cancel import PRINCIPAL, FakeJobStore, FakeLog, FakeRuntime

OTHER = Principal(subject="someone-else", display_name="Other", platform="test", roles=[])
JOB = "a" * 32


@pytest.fixture
def store(tmp_path) -> LocalFsArtifactStore:
    return LocalFsArtifactStore(tmp_path / "store")


@pytest.fixture
def workspace(tmp_path) -> Path:
    ws = tmp_path / "ws"
    (ws / "scratch").mkdir(parents=True)
    (ws / "proposals" / "ACME-1").mkdir(parents=True)
    return ws


# --- local store ----------------------------------------------------------------------------------


def test_local_store_round_trips_bytes_and_metadata(store):
    store.put("engagements/x/scratch/a.md", b"hello", "text/markdown", {"owner": "dev"})
    data, record = store.get("engagements/x/scratch/a.md")
    assert data == b"hello"
    assert record.size == 5 and record.metadata == {"owner": "dev"}
    assert [r.key for r in store.list("engagements/x/")] == ["engagements/x/scratch/a.md"]
    assert store.get("engagements/x/missing.md") is None


@pytest.mark.parametrize("key", ["../escape.md", "/abs.md", "a/../../b.md", "a//b.md", ""])
def test_local_store_refuses_a_key_that_could_leave_its_root(store, key):
    with pytest.raises(PolicyError):
        store.put(key, b"x", "text/plain")


# --- publishing deliverables -----------------------------------------------------------------------


def test_only_markdown_and_patches_under_the_deliverable_folders_are_published(store, workspace, tmp_path):
    (workspace / "scratch" / "ACME-1-handoff.md").write_text("# Handoff")
    (workspace / "scratch" / "ACME-1-batch.json").write_text("[]")
    (workspace / "proposals" / "ACME-1" / "fix.patch").write_text("--- a\n+++ b\n")
    (workspace / "CLAUDE.md").write_text("not a deliverable")
    (workspace / "scratch" / "uploads" / JOB).mkdir(parents=True)
    (workspace / "scratch" / "uploads" / JOB / "brd.md").write_text("caller's own input")
    outside = tmp_path / "elsewhere.md"
    outside.write_text("outside the workspace")

    written = [
        str(workspace / "scratch" / "ACME-1-handoff.md"),
        "scratch/ACME-1-batch.json",
        "proposals/ACME-1/fix.patch",
        "CLAUDE.md",
        f"scratch/uploads/{JOB}/brd.md",
        str(outside),
        "scratch/deleted-during-run.md",
        "scratch/ACME-1-handoff.md",  # written twice: published once
    ]
    published = publish_engagement_files(store, JOB, workspace, written)

    assert sorted(f.path for f in published) == [
        "proposals/ACME-1/fix.patch", "scratch/ACME-1-batch.json", "scratch/ACME-1-handoff.md"]
    assert {f.content_type for f in published} == {"text/markdown", "text/x-diff", "application/json"}


def test_any_format_is_published_binary_intact_except_credential_files(store, workspace):
    blob = bytes(range(256))
    (workspace / "scratch" / "ACME-1-report.xlsx").write_bytes(blob)
    (workspace / "scratch" / "ACME-1-data.zip").write_bytes(b"PK\x03\x04")
    (workspace / "scratch" / "server.pem").write_bytes(b"-----BEGIN")
    (workspace / "scratch" / ".env").write_text("A=1")
    published = publish_engagement_files(
        store, JOB, workspace,
        ["scratch/ACME-1-report.xlsx", "scratch/ACME-1-data.zip", "scratch/server.pem", "scratch/.env"])
    assert sorted(f.path for f in published) == ["scratch/ACME-1-data.zip", "scratch/ACME-1-report.xlsx"]
    assert read_engagement_file(store, JOB, "scratch/ACME-1-report.xlsx")[0] == blob


def test_any_format_can_be_uploaded_by_default(store):
    up = store_upload(store, PRINCIPAL, "model.xlsx", b"PK\x03\x04", UploadPolicy())
    assert not up.redacted and up.filename == "model.xlsx"


def test_published_files_are_secret_redacted(store, workspace):
    (workspace / "scratch" / "ACME-1-verdict.md").write_text("conn: AccountKey=abcd1234secretvalue;\n")
    publish_engagement_files(store, JOB, workspace, ["scratch/ACME-1-verdict.md"])
    data, _ = read_engagement_file(store, JOB, "scratch/ACME-1-verdict.md")
    assert b"abcd1234secretvalue" not in data and b"[REDACTED]" in data


def test_a_published_copy_is_frozen_when_the_ticket_is_rerun(store, workspace):
    handoff = workspace / "scratch" / "ACME-1-handoff.md"
    handoff.write_text("first run")
    publish_engagement_files(store, JOB, workspace, ["scratch/ACME-1-handoff.md"])
    handoff.write_text("second run")
    publish_engagement_files(store, "b" * 32, workspace, ["scratch/ACME-1-handoff.md"])

    assert read_engagement_file(store, JOB, "scratch/ACME-1-handoff.md")[0] == b"first run"
    assert read_engagement_file(store, "b" * 32, "scratch/ACME-1-handoff.md")[0] == b"second run"


@pytest.mark.parametrize("path", ["../x.md", "/etc/passwd", "scratch/../../x.md"])
def test_reading_a_file_by_a_path_outside_the_job_is_not_found(store, path):
    with pytest.raises(NotFoundError):
        read_engagement_file(store, JOB, path)


def test_a_malformed_job_id_is_not_found_rather_than_a_store_key(store):
    with pytest.raises(NotFoundError):
        list_engagement_files(store, "../uploads")


# --- uploads ----------------------------------------------------------------------------------------


def test_text_uploads_are_redacted_and_binary_ones_kept_as_sent(store):
    text = store_upload(store, PRINCIPAL, "notes.txt", b"password=hunter2hunter2", UploadPolicy())
    pdf = store_upload(store, PRINCIPAL, "brd.pdf", b"%PDF-1.7 password=hunter2hunter2", UploadPolicy())
    assert text.redacted and not pdf.redacted
    assert b"hunter2" not in store.get(f"uploads/{text.upload_id}/notes.txt")[0]
    assert store.get(f"uploads/{pdf.upload_id}/brd.pdf")[0].startswith(b"%PDF")


@pytest.mark.parametrize("name, data, message", [
    ("server.pem", b"x", "hold credentials"),
    (".env", b"x", "hold credentials"),
    (".env.prod", b"x", "hold credentials"),
    ("empty.md", b"", "empty"),
    ("big.md", b"x" * 11, "limit"),
])
def test_uploads_outside_the_policy_are_refused(store, name, data, message):
    with pytest.raises(PolicyError, match=message):
        store_upload(store, PRINCIPAL, name, data, UploadPolicy(max_bytes=10))


@pytest.mark.parametrize("raw, clean", [
    ("../../etc/passwd.md", "passwd.md"),
    ("C:\\Users\\me\\BRD v2.pdf", "BRD v2.pdf"),
    (".hidden.md", "hidden.md"),
    ("na\u00efve;rm -rf.md", "na_ve_rm -rf.md"),
])
def test_upload_filenames_lose_any_directory_part_or_unsafe_character(raw, clean):
    assert sanitize_filename(raw) == clean


def test_only_the_uploader_can_attach_an_upload(store):
    upload = store_upload(store, PRINCIPAL, "brd.md", b"# BRD", UploadPolicy())
    assert len(resolve_uploads(store, [upload.upload_id], PRINCIPAL.subject)) == 1
    with pytest.raises(PolicyError):
        resolve_uploads(store, [upload.upload_id], OTHER.subject)
    with pytest.raises(NotFoundError):
        resolve_uploads(store, ["f" * 32], PRINCIPAL.subject)
    with pytest.raises(NotFoundError):
        resolve_uploads(store, ["../escape"], PRINCIPAL.subject)


def test_staging_copies_attachments_into_the_jobs_own_folder(store, workspace):
    a = store_upload(store, PRINCIPAL, "brd.md", b"# one", UploadPolicy())
    b = store_upload(store, PRINCIPAL, "brd.md", b"# two", UploadPolicy())
    staged = stage_attachments(store, [a.upload_id, b.upload_id], PRINCIPAL.subject, workspace, JOB)

    assert staged[0].path == f"scratch/uploads/{JOB}/brd.md"
    assert staged[1].path == f"scratch/uploads/{JOB}/{b.upload_id[:8]}-brd.md"  # same name, both kept
    assert (workspace / staged[1].path).read_text() == "# two"


# --- engine wiring ---------------------------------------------------------------------------------


def test_a_run_stages_attachments_into_the_prompt_and_publishes_what_it_wrote(store, workspace):
    upload = store_upload(store, PRINCIPAL, "brd.md", b"# BRD", UploadPolicy())
    jobs, log = FakeJobStore(), FakeLog()
    job_id = start_engagement(jobs, PRINCIPAL, "ACME-1", attachment_ids=(upload.upload_id,))

    def agent_writes_a_handoff():
        (workspace / "scratch" / "ACME-1-handoff.md").write_text("# Handoff")

    runtime = FakeRuntime(
        AgentRunResult(text="done", is_error=False, session_id="s1", files_written=("scratch/ACME-1-handoff.md",)),
        on_run=agent_writes_a_handoff,
    )
    run_engagement(jobs, runtime, log, job_id, str(workspace), artifact_store=store)

    prompt = runtime.run_calls[0]["prompt"]
    assert "[Attached files]" in prompt and f"scratch/uploads/{job_id}/brd.md" in prompt
    assert prompt.rstrip().endswith("ACME-1")  # the caller's prompt still comes last
    assert jobs.get(job_id).status == EngagementStatus.SUCCEEDED
    assert [f.path for f in list_engagement_files(store, job_id)] == ["scratch/ACME-1-handoff.md"]


def test_files_are_published_before_the_job_turns_terminal(store, workspace):
    """A UI fetches the file list the moment polling sees a finished job; it must not find it empty."""
    (workspace / "scratch" / "ACME-1-handoff.md").write_text("# Handoff")
    seen_at_terminal: list[int] = []

    class WatchingJobStore(FakeJobStore):
        def update(self, engagement):
            if engagement.status == EngagementStatus.SUCCEEDED:
                seen_at_terminal.append(len(list_engagement_files(store, engagement.job_id)))
            super().update(engagement)

    jobs = WatchingJobStore()
    job_id = start_engagement(jobs, PRINCIPAL, "ACME-1")
    runtime = FakeRuntime(AgentRunResult(text="done", is_error=False, files_written=("scratch/ACME-1-handoff.md",)))
    run_engagement(jobs, runtime, FakeLog(), job_id, str(workspace), artifact_store=store)
    assert seen_at_terminal == [1]


def test_partial_work_from_an_incomplete_run_is_still_shared(store, workspace):
    (workspace / "scratch" / "ACME-1-intake.md").write_text("# Intake")
    jobs, log = FakeJobStore(), FakeLog()
    job_id = start_engagement(jobs, PRINCIPAL, "ACME-1")
    runtime = FakeRuntime(AgentRunResult(text="partial", is_error=False, incomplete=True,
                                         files_written=("scratch/ACME-1-intake.md",)))
    run_engagement(jobs, runtime, log, job_id, str(workspace), artifact_store=store)

    assert jobs.get(job_id).status == EngagementStatus.INCOMPLETE
    assert [f.path for f in list_engagement_files(store, job_id)] == ["scratch/ACME-1-intake.md"]


def test_attachments_that_cannot_be_staged_fail_the_run_before_the_agent_starts(workspace):
    jobs, log, runtime = FakeJobStore(), FakeLog(), FakeRuntime()
    job_id = start_engagement(jobs, PRINCIPAL, "ACME-1", attachment_ids=("c" * 32,))
    run_engagement(jobs, runtime, log, job_id, str(workspace), artifact_store=None)

    assert jobs.get(job_id).status == EngagementStatus.FAILED
    assert "attach" in jobs.get(job_id).error
    assert runtime.run_calls == []
    assert log.entries[-1].outcome == "failed"


def test_attachment_ids_survive_the_job_store_round_trip():
    jobs = FakeJobStore()
    job_id = start_engagement(jobs, PRINCIPAL, "hi", attachment_ids=("d" * 32,))
    engagement = jobs.get(job_id)
    restored = type(engagement).from_json(engagement.to_json())
    assert restored.attachment_ids == ("d" * 32,)
    assert restored.with_status(EngagementStatus.RUNNING).attachment_ids == ("d" * 32,)


# --- Agent SDK adapter --------------------------------------------------------------------------------


def test_the_adapter_reports_files_the_agent_wrote(monkeypatch):
    async def fake_query(prompt, options):
        yield AssistantMessage(content=[
            ToolUseBlock(id="1", name="Write", input={"file_path": "/ws/scratch/ACME-1-handoff.md", "content": "x"}),
            ToolUseBlock(id="2", name="Read", input={"file_path": "/ws/CLAUDE.md"}),
            ToolUseBlock(id="3", name="Edit", input={"file_path": "scratch/ACME-1-plan.md"}),
        ], model="m")
        yield ResultMessage(subtype="success", duration_ms=1, duration_api_ms=1, is_error=False,
                            num_turns=1, session_id="s1", result="done")

    monkeypatch.setattr(claude_agent_adapter, "query", fake_query)
    result = ClaudeAgentSdkRuntime().run("hi", "/ws")
    assert result.files_written == ("/ws/scratch/ACME-1-handoff.md", "scratch/ACME-1-plan.md")


def test_files_written_before_a_timeout_are_still_reported(monkeypatch):
    import asyncio

    async def slow_query(prompt, options):
        yield AssistantMessage(content=[ToolUseBlock(id="1", name="Write", input={"file_path": "scratch/a.md"})],
                               model="m")
        await asyncio.sleep(5)

    monkeypatch.setattr(claude_agent_adapter, "query", slow_query)
    result = ClaudeAgentSdkRuntime(timeout_seconds=0.2).run("hi", "/ws")
    assert result.incomplete and result.files_written == ("scratch/a.md",)


# --- HTTP routes ------------------------------------------------------------------------------------


class FakeHistory:
    def __init__(self, jobs: FakeJobStore) -> None:
        self.jobs = jobs

    def get(self, job_id):
        return self.jobs.get(job_id)

    def get_conversation(self, conversation_id):
        return [e for e in self.jobs.jobs.values() if e.conversation_id == conversation_id]


@pytest.fixture
def api(monkeypatch, store):
    monkeypatch.setenv("SCOTRI_AUTH_MODE", "dev")
    monkeypatch.setenv("SCOTRI_DEV_AUTH_TOKEN", "owner-token")
    monkeypatch.setenv("SCOTRI_DEV_PRINCIPAL_SUBJECT", "dev")
    # Pin the caller to no roles and the widening role sets to empty, so a local services/.env that
    # makes the dev user a history admin can't turn the owner-only checks below into a pass-through.
    monkeypatch.setenv("SCOTRI_DEV_PRINCIPAL_ROLES", "")
    from fastapi.testclient import TestClient

    from app import api as api_module

    monkeypatch.setattr(api_module, "HISTORY_ADMIN_ROLES", set())
    monkeypatch.setattr(api_module, "FILE_READER_ROLES", set())
    jobs = FakeJobStore()
    c = api_module.container
    for name, value in {"artifact_store": store, "job_store": jobs, "engagement_history": FakeHistory(jobs)}.items():
        monkeypatch.setitem(c.__dict__, name, value)
    monkeypatch.delitem(c.__dict__, "identity_provider", raising=False)
    return TestClient(api_module.app), jobs, api_module


AUTH = {"Authorization": "Bearer owner-token"}


def test_upload_then_download_a_published_file_over_http(api, store, workspace):
    client, jobs, _ = api

    res = client.post("/v1/uploads", headers=AUTH, files={"file": ("brd.md", b"# BRD", "text/markdown")})
    assert res.status_code == 201 and res.json()["filename"] == "brd.md"

    job_id = start_engagement(jobs, PRINCIPAL, "ACME-1", conversation_id="conv1")
    (workspace / "scratch" / "ACME-1-handoff.md").write_text("# Handoff")
    publish_engagement_files(store, job_id, workspace, ["scratch/ACME-1-handoff.md"])

    listing = client.get(f"/v1/engagements/{job_id}/files", headers=AUTH).json()
    assert [f["path"] for f in listing["files"]] == ["scratch/ACME-1-handoff.md"]
    body = client.get(listing["files"][0]["url"], headers=AUTH)
    assert body.status_code == 200 and body.text == "# Handoff"
    assert body.headers["content-type"].startswith("text/markdown")
    assert client.get(f"/v1/chat/{job_id}/files/scratch/ACME-1-handoff.md?download=true",
                      headers=AUTH).headers["content-disposition"].startswith("attachment")
    assert client.get("/v1/conversations/conv1/files", headers=AUTH).json()["count"] == 1


def test_another_users_files_are_forbidden_over_http(api, store):
    client, jobs, _ = api
    job_id = start_engagement(jobs, OTHER, "ACME-1")
    assert client.get(f"/v1/engagements/{job_id}/files", headers=AUTH).status_code == 403
    assert client.get(f"/v1/engagements/{'e' * 32}/files", headers=AUTH).status_code == 404


def test_uploads_outside_the_policy_get_a_clear_http_error(api, monkeypatch):
    client, _, api_module = api
    assert client.post("/v1/uploads", headers=AUTH,
                       files={"file": ("x.pem", b"key", "application/octet-stream")}).status_code == 422
    monkeypatch.setitem(api_module.container.__dict__, "upload_policy", UploadPolicy(max_bytes=4))
    assert client.post("/v1/uploads", headers=AUTH,
                       files={"file": ("x.md", b"12345", "text/markdown")}).status_code == 413


def test_attaching_an_unknown_upload_fails_the_request_not_the_run(api):
    client, jobs, _ = api
    res = client.post("/v1/chat", headers=AUTH, json={"prompt": "hi", "attachment_ids": ["f" * 32]})
    assert res.status_code == 404
    assert jobs.jobs == {}
