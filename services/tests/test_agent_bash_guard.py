# Copyright (c) 2026 Preyas Kulshrestha. Part of the Scotri engine; MIT License (see LICENSE).
# Keep this notice in copies and derived works, including AI-assisted ones.
"""adapters/agent_sdk/claude_agent_adapter.py — engagements must have a Bash tool.

Every governed read is an `app.cli` call through Bash. On 2026-10-07 the API was started from a shell
where the Claude CLI couldn't find Git Bash, and every engagement silently ran without the tool: the
agent searched for it, gave up, and marked every column `unchecked`. These tests stub the SDK's
`query` (no CLI subprocess) and lock down both halves of the fix: the CLI is pointed at Git Bash on
Windows, and an engagement that starts without Bash anyway is stopped with a clear error."""
from __future__ import annotations

import os
from pathlib import Path

import pytest
from claude_agent_sdk import SystemMessage

import adapters.agent_sdk.claude_agent_adapter as adapter
from adapters.agent_sdk.claude_agent_adapter import (
    GIT_BASH_ENV_VAR, MISSING_BASH_MESSAGE, ClaudeAgentSdkRuntime, find_git_bash,
)


def _fake_query(tools: list[str], seen: dict):
    """A stand-in for claude_agent_sdk.query: records the options, yields the init message, and
    notes whether the stream was closed (the real one terminates the CLI in its own cleanup)."""

    def query(prompt, options):
        seen["options"] = options

        async def stream():
            try:
                yield SystemMessage(subtype="init", data={"tools": tools, "session_id": "s1"})
                seen["continued_past_init"] = True
            finally:
                seen["closed"] = True

        return stream()

    return query


def test_engagement_without_bash_is_stopped_with_a_clear_error(monkeypatch):
    seen: dict = {}
    monkeypatch.setattr(adapter, "query", _fake_query(["Read", "Write", "Skill"], seen))
    result = ClaudeAgentSdkRuntime(timeout_seconds=5).run("prompt", cwd=".")
    assert result.is_error is True
    assert result.text == MISSING_BASH_MESSAGE
    assert "continued_past_init" not in seen   # stopped at init, before the agent did anything
    assert seen["closed"] is True              # stream closed, so the SDK tears the CLI down


def test_engagement_with_bash_carries_on(monkeypatch):
    seen: dict = {}
    monkeypatch.setattr(adapter, "query", _fake_query(["Bash", "Read", "Write", "Skill"], seen))
    result = ClaudeAgentSdkRuntime(timeout_seconds=5).run("prompt", cwd=".")
    assert seen.get("continued_past_init") is True
    assert result.text != MISSING_BASH_MESSAGE  # (no ResultMessage here, so it's the "no output" error)


def test_cli_is_pointed_at_git_bash_on_windows(monkeypatch):
    seen: dict = {}
    monkeypatch.setattr(adapter, "query", _fake_query(["Bash"], seen))
    monkeypatch.setattr(adapter.os, "name", "nt")
    monkeypatch.delenv(GIT_BASH_ENV_VAR, raising=False)
    monkeypatch.setattr(adapter, "find_git_bash", lambda: r"C:\Git\bin\bash.exe")
    ClaudeAgentSdkRuntime(timeout_seconds=5).run("prompt", cwd=".", env={"SCOTRI_RUN_TOKEN": "t"})
    assert seen["options"].env == {GIT_BASH_ENV_VAR: r"C:\Git\bin\bash.exe", "SCOTRI_RUN_TOKEN": "t"}


def test_stream_buffer_fits_a_read_attachment(monkeypatch):
    # The SDK's 1 MB default killed ACME-2129 when the agent Read a 587 KB PDF (base64 in one line).
    seen: dict = {}
    monkeypatch.setattr(adapter, "query", _fake_query(["Bash"], seen))
    ClaudeAgentSdkRuntime(timeout_seconds=5).run("prompt", cwd=".")
    assert seen["options"].max_buffer_size >= 32 * 1024 * 1024


def test_an_explicit_setting_or_another_os_is_left_alone(monkeypatch):
    monkeypatch.setattr(adapter, "find_git_bash", lambda: r"C:\Git\bin\bash.exe")
    monkeypatch.setattr(adapter.os, "name", "nt")
    monkeypatch.setenv(GIT_BASH_ENV_VAR, r"D:\own\bash.exe")
    assert adapter._git_bash_env() == {}          # the deployment's own value wins (inherited)
    monkeypatch.delenv(GIT_BASH_ENV_VAR)
    monkeypatch.setattr(adapter.os, "name", "posix")
    assert adapter._git_bash_env() == {}          # Linux/macOS have bash on PATH


def test_find_git_bash_derives_bash_from_git_on_path(monkeypatch, tmp_path):
    root = tmp_path / "Git"
    (root / "cmd").mkdir(parents=True)
    (root / "bin").mkdir()
    (root / "cmd" / "git.exe").write_bytes(b"")
    (root / "bin" / "bash.exe").write_bytes(b"")
    monkeypatch.setattr(adapter.shutil, "which", lambda name: str(root / "cmd" / "git.exe"))
    assert Path(find_git_bash()) == root / "bin" / "bash.exe"


@pytest.mark.skipif(os.name != "nt", reason="Git Bash is a Windows concern")
def test_find_git_bash_finds_this_machines_bash():
    found = find_git_bash()
    assert found is None or Path(found).is_file()
