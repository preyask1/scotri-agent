# Copyright (c) 2026 Preyas Kulshrestha. Part of the Scotri engine; MIT License (see LICENSE).
# Keep this notice in copies and derived works, including AI-assisted ones.
"""app/cli.py — what a confirmed Jira write may carry. Every text field is secret-redacted before it
leaves this host, and `jira-comment --body-file` reads only Scotri's own output or the caller's uploads
(the jira-attach source rule), so a comment can never post services/.env or a raw mirror file."""
from __future__ import annotations

import argparse
import json

import pytest

import app.cli as cli


class _Writer:
    def __init__(self):
        self.calls = []

    def add_comment(self, key, body):
        self.calls.append(("comment", key, body))
        return "c1"

    def update_comment(self, key, comment_id, body):
        self.calls.append(("edit", key, body))

    def create_bug(self, project, summary, description):
        self.calls.append(("bug", summary, description))
        return "ACME-9"


@pytest.fixture
def writer(monkeypatch, tmp_path):
    w = _Writer()
    monkeypatch.setattr(cli, "build_container", lambda *a, **k: argparse.Namespace(jira_writer=w))
    monkeypatch.setattr(cli, "WORKSPACE_ROOT", tmp_path)
    (tmp_path / "scratch").mkdir()
    (tmp_path / "services").mkdir()
    return w


def _ns(**kw):
    base = {"confirmed": True, "user": "u", "wiki": False, "body": None, "body_file": None}
    return argparse.Namespace(**{**base, **kw})


def test_comment_body_is_redacted(writer, capsys):
    assert cli.cmd_jira_comment(_ns(key="ACME-1", body="found it: password=hunter2xyz")) == 0
    assert "hunter2xyz" not in writer.calls[0][2]


def test_comment_body_file_from_scratch_is_posted_redacted(writer, tmp_path, capsys):
    (tmp_path / "scratch" / "c.md").write_text("verdict; AccountKey=Zm9vYmFyYmF6", encoding="utf-8")
    assert cli.cmd_jira_comment(_ns(key="ACME-1", body_file=str(tmp_path / "scratch" / "c.md"))) == 0
    assert writer.calls[0][2].startswith("verdict") and "Zm9vYmFyYmF6" not in writer.calls[0][2]


def test_comment_body_file_outside_the_output_folders_is_refused(writer, tmp_path, capsys):
    (tmp_path / "services" / "notes.md").write_text("JIRA_API_TOKEN=abc", encoding="utf-8")
    assert cli.cmd_jira_comment(_ns(key="ACME-1", body_file=str(tmp_path / "services" / "notes.md"))) == 1
    assert writer.calls == []
    assert json.loads(capsys.readouterr().out)["category"] == "policy"


def test_edit_and_bug_text_are_redacted(writer, capsys):
    cli.cmd_jira_edit_comment(_ns(key="ACME-1", comment_id="1", body="token=abcd1234efgh"))
    cli.cmd_jira_create_bug(_ns(project="DP", summary="s", description="client_secret: s3cr3tvalue"))
    assert "abcd1234efgh" not in writer.calls[0][2]
    assert "s3cr3tvalue" not in writer.calls[1][2]
