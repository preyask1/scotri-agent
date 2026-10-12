"""app/cli.py's governor-check — must be READ-ONLY reporting (see the fix for the double
-enforcement risk: a prior version incremented the same Redis counters `run_governed` uses for a
real query, so calling it stand-alone silently consumed real quota). This test asserts the command
only ever calls `peek_state` and never touches any of the mutating RateLimiter methods."""
from __future__ import annotations

import argparse
import json

import app.cli as cli
from domain.models import Backend
from ports.rate_limiter import GovernorState


class _MutationAttempted(AssertionError):
    pass


class _FakeRateLimiter:
    def __init__(self, rate_count: int, in_flight: int):
        self._rate_count = rate_count
        self._in_flight = in_flight
        self.peek_state_called_with = None

    def peek_state(self, user, backend, window_seconds):
        self.peek_state_called_with = (user, backend, window_seconds)
        return GovernorState(rate_count_this_window=self._rate_count, concurrency_in_flight=self._in_flight)

    def increment_and_get_count(self, *args, **kwargs):
        raise _MutationAttempted("governor-check must never increment the rate counter")

    def enter_concurrency_slot(self, *args, **kwargs):
        raise _MutationAttempted("governor-check must never acquire a concurrency slot")

    def exit_concurrency_slot(self, *args, **kwargs):
        raise _MutationAttempted("governor-check must never release a slot it never acquired")


class _FakeContainer:
    def __init__(self, rate_limiter):
        self.rate_limiter = rate_limiter


def test_governor_check_never_mutates_state(monkeypatch, capsys):
    fake_limiter = _FakeRateLimiter(rate_count=3, in_flight=1)
    monkeypatch.setattr(cli, "build_container", lambda: _FakeContainer(fake_limiter))

    args = argparse.Namespace(user="alice", backend="databricks")
    exit_code = cli.cmd_governor_check(args)
    payload = json.loads(capsys.readouterr().out.strip())

    assert exit_code == 0
    assert payload["ok"] is True
    assert payload["rate_count_this_window"] == 3
    assert payload["concurrency_in_flight"] == 1
    assert fake_limiter.peek_state_called_with[1] == Backend.DATABRICKS


def test_governor_check_reports_would_be_limited_at_the_cap(monkeypatch, capsys):
    fake_limiter = _FakeRateLimiter(rate_count=30, in_flight=0)
    monkeypatch.setattr(cli, "build_container", lambda: _FakeContainer(fake_limiter))

    args = argparse.Namespace(user="alice", backend="databricks")
    cli.cmd_governor_check(args)
    payload = json.loads(capsys.readouterr().out.strip())

    assert payload["would_be_rate_limited"] is True


def test_governor_check_reports_would_be_queued_at_the_concurrency_cap(monkeypatch, capsys):
    fake_limiter = _FakeRateLimiter(rate_count=0, in_flight=5)
    monkeypatch.setattr(cli, "build_container", lambda: _FakeContainer(fake_limiter))

    args = argparse.Namespace(user="alice", backend="databricks")
    cli.cmd_governor_check(args)
    payload = json.loads(capsys.readouterr().out.strip())

    assert payload["would_be_queued"] is True
