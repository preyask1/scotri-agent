# Copyright (c) 2026 Preyas Kulshrestha. Part of the Scotri engine; MIT License (see LICENSE).
# Keep this notice in copies and derived works, including AI-assisted ones.
"""app/cli.py's `batch` — several read commands in one process, in parallel. These tests pin the
properties that make it safe rather than a bypass: write commands are refused, a bad item refuses
the whole batch before anything runs, each item runs its normal cmd_* function (so its normal
governed path), output order matches input order, and one container is shared across items."""
from __future__ import annotations

import json
import threading
import time

import pytest

import app.cli as cli


def _run_batch(tmp_path, capsys, items, workers=4):
    batch_file = tmp_path / "batch.json"
    batch_file.write_text(json.dumps(items), encoding="utf-8")
    code = cli.main(["batch", "--file", str(batch_file), "--workers", str(workers)])
    return code, json.loads(capsys.readouterr().out)


@pytest.fixture
def no_container(monkeypatch):
    calls = []

    def _fake_build_container():
        calls.append(1)
        return object()

    monkeypatch.setattr(cli, "build_container", _fake_build_container)
    return calls


@pytest.mark.parametrize("command", ["jira-comment", "jira-create-bug", "governor-reset", "sync-run-once", "batch"])
def test_refuses_non_batchable_command_and_runs_nothing(tmp_path, capsys, monkeypatch, no_container, command):
    ran = []
    monkeypatch.setattr(cli, "cmd_registry_resolve", lambda a: ran.append(a) or 0)
    code, out = _run_batch(tmp_path, capsys, [["registry-resolve", "--repo-key", "x"], [command]])
    assert code == 1
    assert out["ok"] is False and out["category"] == "policy"
    assert ran == [] and no_container == []


def test_malformed_item_refuses_whole_batch(tmp_path, capsys, monkeypatch, no_container):
    ran = []
    monkeypatch.setattr(cli, "cmd_registry_resolve", lambda a: ran.append(a) or 0)
    # databricks-select without its required --statement
    code, out = _run_batch(tmp_path, capsys, [
        ["registry-resolve", "--repo-key", "x"],
        ["databricks-select", "--user", "u", "--catalog", "acme_prod"],
    ])
    assert code == 1 and out["category"] == "configuration"
    assert "item 1" in out["message"]
    assert ran == [] and no_container == []


def test_runs_in_parallel_keeps_order_and_shares_one_container(tmp_path, capsys, monkeypatch, no_container):
    seen_containers = []
    lock = threading.Lock()

    def _slow_resolve(args):
        container = cli.build_container()
        with lock:
            seen_containers.append(container)
        time.sleep(0.4)
        print(json.dumps({"ok": True, "repo_key": args.repo_key}))
        return 0

    monkeypatch.setattr(cli, "cmd_registry_resolve", _slow_resolve)
    keys = ["a", "b", "c", "d"]
    start = time.monotonic()
    code, out = _run_batch(tmp_path, capsys, [["registry-resolve", "--repo-key", k] for k in keys], workers=4)
    elapsed = time.monotonic() - start

    assert code == 0 and out["ok"] is True and out["failed_count"] == 0
    assert [r["output"]["repo_key"] for r in out["results"]] == keys
    assert elapsed < 1.2, f"4 x 0.4s items took {elapsed:.2f}s — not parallel"
    assert len(no_container) == 1 and len({id(c) for c in seen_containers}) == 1
    # the swap is undone afterwards
    assert cli.build_container is not None and len(no_container) == 1
    cli.build_container()
    assert len(no_container) == 2


def test_one_failing_item_is_reported_and_the_others_still_run(tmp_path, capsys, monkeypatch, no_container):
    def _resolve(args):
        if args.repo_key == "bad":
            raise RuntimeError("boom")
        print(json.dumps({"ok": True, "repo_key": args.repo_key}))
        return 0

    monkeypatch.setattr(cli, "cmd_registry_resolve", _resolve)
    code, out = _run_batch(tmp_path, capsys, [
        ["registry-resolve", "--repo-key", "good"],
        ["registry-resolve", "--repo-key", "bad"],
    ])
    assert code == 1 and out["ok"] is False and out["failed_count"] == 1
    assert out["results"][0]["output"] == {"ok": True, "repo_key": "good"}
    assert out["results"][1]["exit_code"] == 1
    assert out["results"][1]["output"]["ok"] is False


def test_prose_output_is_kept_as_text_and_outputs_are_redacted(tmp_path, capsys, monkeypatch, no_container):
    def _presence(args):
        print("claim registered: me on ACME-1 password=hunter2")
        return 0

    monkeypatch.setattr(cli, "cmd_presence_check", _presence)
    code, out = _run_batch(tmp_path, capsys, [["presence-check", "--entity", "ACME-1", "--owner", "me"]])
    assert code == 0
    text = out["results"][0]["output"]
    assert isinstance(text, str) and text.startswith("claim registered")
    assert "hunter2" not in text


def test_rejects_oversized_and_non_list_input(tmp_path, capsys, no_container):
    code, out = _run_batch(tmp_path, capsys, [["registry-resolve", "--repo-key", "x"]] * (cli.BATCH_MAX_ITEMS + 1))
    assert code == 1 and "cap" in out["message"]
    code, out = _run_batch(tmp_path, capsys, {"not": "a list"})
    assert code == 1 and out["category"] == "configuration"
    assert no_container == []


def test_batch_names_its_items_on_stderr_before_running(tmp_path, capsys, monkeypatch, no_container):
    """One short line on stderr, first, so a truncated step preview still shows what a batch ran
    (the benchmark counts graph-tool use from it). Stdout stays pure JSON."""
    monkeypatch.setattr(cli, "cmd_registry_resolve", lambda a: print(json.dumps({"ok": True})) or 0)
    monkeypatch.setattr(cli, "cmd_lineage_trace", lambda a: print(json.dumps({"ok": True})) or 0)
    batch_file = tmp_path / "batch.json"
    batch_file.write_text(json.dumps([["registry-resolve", "--repo-key", "x"],
                                      ["lineage-trace", "--node", "gold_table:visit_summary"]]), encoding="utf-8")
    assert cli.main(["batch", "--file", str(batch_file)]) == 0
    captured = capsys.readouterr()
    assert captured.err.splitlines()[0] == "batch: 2 item(s): registry-resolve, lineage-trace"
    assert json.loads(captured.out)["ok"] is True
