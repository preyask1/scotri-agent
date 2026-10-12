"""app/cli.py's presence-heartbeat — must only ever refresh a claim the caller's own `--owner`
holds (presence-claim SKILL.md). The port's heartbeat() silently no-ops on a claim it doesn't own,
so the CLI checks ownership itself first and reports an explicit message rather than a silent
success. These tests assert the no-claim, wrong-owner, and matching-owner paths each produce the
right exit code and message, and that `heartbeat` is only ever invoked when the owner matches."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone

import app.cli as cli
from domain.models import Claim


class _FakePresenceStore:
    def __init__(self, claim: Claim | None):
        self._claim = claim
        self.heartbeat_called_with = None

    def get_claim(self, entity):
        return self._claim

    def heartbeat(self, entity, owner):
        self.heartbeat_called_with = (entity, owner)


class _FakeContainer:
    def __init__(self, presence_store):
        self.presence_store = presence_store


def _now():
    return datetime.now(timezone.utc)


def test_heartbeat_when_no_claim_fails(monkeypatch, capsys):
    fake_store = _FakePresenceStore(claim=None)
    monkeypatch.setattr(cli, "build_container", lambda: _FakeContainer(fake_store))

    args = argparse.Namespace(entity="notebook:NB_FOO", owner="alice")
    exit_code = cli.cmd_presence_heartbeat(args)
    out = capsys.readouterr().out.strip()

    assert exit_code == 1
    assert "no claim" in out
    assert fake_store.heartbeat_called_with is None


def test_heartbeat_refuses_to_refresh_someone_elses_claim(monkeypatch, capsys):
    claim = Claim(entity="notebook:NB_FOO", owner="bob", started_at=_now(), last_heartbeat=_now())
    fake_store = _FakePresenceStore(claim=claim)
    monkeypatch.setattr(cli, "build_container", lambda: _FakeContainer(fake_store))

    args = argparse.Namespace(entity="notebook:NB_FOO", owner="alice")
    exit_code = cli.cmd_presence_heartbeat(args)
    out = capsys.readouterr().out.strip()

    assert exit_code == 1
    assert "bob" in out
    assert "not refreshed" in out
    assert fake_store.heartbeat_called_with is None


def test_heartbeat_succeeds_for_the_owning_claim(monkeypatch, capsys):
    claim = Claim(entity="notebook:NB_FOO", owner="alice", started_at=_now(), last_heartbeat=_now())
    fake_store = _FakePresenceStore(claim=claim)
    monkeypatch.setattr(cli, "build_container", lambda: _FakeContainer(fake_store))

    args = argparse.Namespace(entity="notebook:NB_FOO", owner="alice")
    exit_code = cli.cmd_presence_heartbeat(args)
    out = capsys.readouterr().out.strip()

    assert exit_code == 0
    assert "refreshed" in out
    assert fake_store.heartbeat_called_with == ("notebook:NB_FOO", "alice")


# --- presence-check: the owner's own live claim is not a collision -----------------------------


class _CheckStore(_FakePresenceStore):
    def __init__(self, claim):
        super().__init__(claim)
        self.set_calls = []

    def set_claim(self, entity, owner):
        self.set_calls.append((entity, owner))


def _check(monkeypatch, capsys, holder, asker):
    claim = Claim(entity="gold.x", owner=holder, started_at=_now(), last_heartbeat=_now())
    store = _CheckStore(claim)
    monkeypatch.setattr(cli, "build_container", lambda: _FakeContainer(store))
    code = cli.cmd_presence_check(argparse.Namespace(entity="gold.x", owner=asker, force=False))
    return code, capsys.readouterr().out, store.set_calls


def test_check_refreshes_the_callers_own_live_claim(monkeypatch, capsys):
    code, out, sets = _check(monkeypatch, capsys, holder="dev-user", asker="dev-user")
    assert code == 0 and "claim refreshed" in out and sets == [("gold.x", "dev-user")]


def test_check_still_stops_on_someone_elses_live_claim(monkeypatch, capsys):
    code, out, sets = _check(monkeypatch, capsys, holder="bob", asker="dev-user")
    assert code == 1 and "bob" in out and sets == []


# --- inside an engagement: the owner is the caller, and the claim is recorded under the job -------


class _RunStore(_CheckStore):
    def __init__(self, claim=None):
        super().__init__(claim)
        self.tracked = []

    def track_run_claim(self, job_id, entity):
        self.tracked.append((job_id, entity))


class _RunContexts:
    def resolve(self, token):
        from domain.run_context import RunContext
        return RunContext(subject="dev-user", display_name="Dev", platform="web", job_id="job-1") if token == "t" else None


def test_inside_a_run_the_owner_is_the_caller_and_the_claim_is_tracked(monkeypatch, capsys):
    store = _RunStore()
    container = _FakeContainer(store)
    container.run_context_store = _RunContexts()
    monkeypatch.setattr(cli, "build_container", lambda: container)
    monkeypatch.setenv(cli.RUN_TOKEN_ENV, "t")
    args = argparse.Namespace(entity="gold.x", owner="Scotri:er-diagram", force=False, func=cli.cmd_presence_check)
    cli._apply_run_identity(args)
    assert cli.cmd_presence_check(args) == 0
    assert store.set_calls == [("gold.x", "dev-user")]
    assert store.tracked == [("job-1", "gold.x")]


def test_outside_a_run_the_owner_is_used_as_given(monkeypatch, capsys):
    store = _RunStore()
    monkeypatch.setattr(cli, "build_container", lambda: _FakeContainer(store))
    monkeypatch.delenv(cli.RUN_TOKEN_ENV, raising=False)
    args = argparse.Namespace(entity="gold.x", owner="alice", force=False, func=cli.cmd_presence_check)
    cli._apply_run_identity(args)
    assert cli.cmd_presence_check(args) == 0
    assert store.set_calls == [("gold.x", "alice")] and store.tracked == []
