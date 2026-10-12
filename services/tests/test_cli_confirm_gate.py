"""app/cli.py — the --confirmed gate in front of every Jira write (jira-write SKILL.md's single
most safety-critical rule: never write without the user having approved this exact content first).

These tests assert the refusal path never even reaches build_container()/the real Jira adapter when
--confirmed is missing or false, and that FLIPPING it to true is what's required to proceed — the
flag itself doesn't validate real approval happened (that's the calling skill's job), but the CLI
boundary must at least refuse cleanly without it, every time, for all three write commands."""
from __future__ import annotations

import argparse
import json

import pytest

import app.cli as cli
from domain.errors import ErrorCategory


def _fail_if_called(*_args, **_kwargs):
    raise AssertionError("build_container() must not be called when --confirmed is missing/false")


@pytest.fixture(autouse=True)
def block_container_build(monkeypatch):
    """Applied to every test in this module by default — the whole point of the gate is that an
    unconfirmed write never reaches the container (and therefore never reaches a real credential)."""
    monkeypatch.setattr(cli, "build_container", _fail_if_called)


def _run_and_capture_json(func, args, capsys) -> dict:
    exit_code = func(args)
    out = capsys.readouterr().out.strip()
    return exit_code, json.loads(out)


@pytest.mark.parametrize("cmd_func,extra_args", [
    (cli.cmd_jira_comment, {"key": "ACME-1234", "body": "draft text"}),
    (cli.cmd_jira_edit_comment, {"key": "ACME-1234", "comment_id": "10001", "body": "corrected text"}),
    (cli.cmd_jira_create_bug, {"project": "ACME", "summary": "s", "description": "d"}),
])
def test_refuses_without_confirmed_flag(cmd_func, extra_args, capsys):
    args = argparse.Namespace(confirmed=False, **extra_args)
    exit_code, payload = _run_and_capture_json(cmd_func, args, capsys)
    assert exit_code == 1
    assert payload["ok"] is False
    assert payload["category"] == ErrorCategory.POLICY.value


@pytest.mark.parametrize("cmd_func,extra_args", [
    (cli.cmd_jira_comment, {"key": "ACME-1234", "body": "draft text"}),
    (cli.cmd_jira_edit_comment, {"key": "ACME-1234", "comment_id": "10001", "body": "corrected text"}),
    (cli.cmd_jira_create_bug, {"project": "ACME", "summary": "s", "description": "d"}),
])
def test_refusal_message_tells_the_caller_what_to_do(cmd_func, extra_args, capsys):
    args = argparse.Namespace(confirmed=False, **extra_args)
    cmd_func(args)
    payload = json.loads(capsys.readouterr().out.strip())
    assert "--confirmed" in payload["message"]
    assert "explicit approval" in payload["message"]


def test_confirmed_true_proceeds_past_the_gate(monkeypatch, capsys):
    """Flip --confirmed to true: the gate must get out of the way and let the command reach the
    (fake) container, proving the gate is the only thing standing between the two states."""
    class _FakeJiraWriter:
        def add_comment(self, key, body):
            assert key == "ACME-1234"
            assert body == "draft text"
            return "10099"

    class _FakeContainer:
        jira_writer = _FakeJiraWriter()

    monkeypatch.setattr(cli, "build_container", lambda: _FakeContainer())
    args = argparse.Namespace(confirmed=True, key="ACME-1234", body="draft text", user="scotri")
    exit_code = cli.cmd_jira_comment(args)
    payload = json.loads(capsys.readouterr().out.strip())
    assert exit_code == 0
    assert payload == {"ok": True, "comment_id": "10099"}
