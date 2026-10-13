# Copyright (c) 2026 Preyas Kulshrestha. Part of the Scotri engine; MIT License (see LICENSE).
# Keep this notice in copies and derived works, including AI-assisted ones.
"""app/cli.py's presence-release — must only ever release a claim the caller's own `--owner` holds
(presence-claim SKILL.md's port contract: releasing is explicit, early-exit state, never a way to
clear someone else's in-progress claim). These tests assert the no-claim, wrong-owner, and
matching-owner paths each produce the right exit code and message, and that `release_claim` is only
ever invoked when the owner actually matches."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone

import app.cli as cli
from domain.models import Claim


class _FakePresenceStore:
    def __init__(self, claim: Claim | None):
        self._claim = claim
        self.released_with = None

    def get_claim(self, entity):
        return self._claim

    def release_claim(self, entity, owner):
        self.released_with = (entity, owner)
        self._claim = None


class _FakeContainer:
    def __init__(self, presence_store):
        self.presence_store = presence_store


def _now():
    return datetime.now(timezone.utc)


def test_release_when_no_claim_is_a_noop(monkeypatch, capsys):
    fake_store = _FakePresenceStore(claim=None)
    monkeypatch.setattr(cli, "build_container", lambda: _FakeContainer(fake_store))

    args = argparse.Namespace(entity="notebook:NB_FOO", owner="alice")
    exit_code = cli.cmd_presence_release(args)
    out = capsys.readouterr().out.strip()

    assert exit_code == 0
    assert "no claim" in out
    assert fake_store.released_with is None


def test_release_refuses_to_clear_someone_elses_claim(monkeypatch, capsys):
    claim = Claim(entity="notebook:NB_FOO", owner="bob", started_at=_now(), last_heartbeat=_now())
    fake_store = _FakePresenceStore(claim=claim)
    monkeypatch.setattr(cli, "build_container", lambda: _FakeContainer(fake_store))

    args = argparse.Namespace(entity="notebook:NB_FOO", owner="alice")
    exit_code = cli.cmd_presence_release(args)
    out = capsys.readouterr().out.strip()

    assert exit_code == 1
    assert "bob" in out
    assert "not released" in out
    assert fake_store.released_with is None


def test_release_succeeds_for_the_owning_claim(monkeypatch, capsys):
    claim = Claim(entity="notebook:NB_FOO", owner="alice", started_at=_now(), last_heartbeat=_now())
    fake_store = _FakePresenceStore(claim=claim)
    monkeypatch.setattr(cli, "build_container", lambda: _FakeContainer(fake_store))

    args = argparse.Namespace(entity="notebook:NB_FOO", owner="alice")
    exit_code = cli.cmd_presence_release(args)
    out = capsys.readouterr().out.strip()

    assert exit_code == 0
    assert "released" in out
    assert fake_store.released_with == ("notebook:NB_FOO", "alice")
