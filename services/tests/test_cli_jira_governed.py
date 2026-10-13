# Copyright (c) 2026 Preyas Kulshrestha. Part of the Scotri engine; MIT License (see LICENSE).
# Keep this notice in copies and derived works, including AI-assisted ones.
"""app/cli.py — Jira reads go through the query governor and writes are audit-logged (OPEN_ITEMS.md
#29). Before this, the four jira-* commands called the adapter directly: no rate limit, no cache,
and — the real hole — no audit entry, on the workspace's only mutating surface.

These are CLI-boundary tests with fakes (no Redis, no real Jira). The write-path governor semantics
(no cache / no rate count / audit-only) are locked down in test_governed_call.py; here we prove the
CLI wiring: a read is served from cache without touching the reader, and a read counts against the
governor."""
from __future__ import annotations

import argparse
import json

import app.cli as cli
from domain.models import Backend


class _FakeCache:
    def __init__(self, hit: str | None = None) -> None:
        self._hit = hit
        self.store: dict[str, str] = {}

    def get(self, key):  # noqa: ANN001
        return self._hit if self._hit is not None else self.store.get(key)

    def set(self, key, value, ttl_seconds):  # noqa: ANN001
        self.store[key] = value


class _FakeRateLimiter:
    def __init__(self) -> None:
        self.enter_calls = 0
        self.rate_calls = 0

    def increment_and_get_count(self, user, window_seconds):  # noqa: ANN001
        self.rate_calls += 1
        return 1

    def enter_concurrency_slot(self, backend):  # noqa: ANN001
        self.enter_calls += 1
        return 1

    def exit_concurrency_slot(self, backend):  # noqa: ANN001
        pass


def _container(cache, limiter, jira):
    class _C:
        pass

    c = _C()
    c.cache = cache
    c.rate_limiter = limiter
    c.jira = jira
    return c


def test_jira_get_issue_served_from_cache_never_hits_reader(monkeypatch, capsys):
    """A cache hit must short-circuit the real Jira reader — proof the read is genuinely governed,
    not just calling the adapter and ignoring the cache."""
    class _Reader:
        def get_issue(self, key):  # noqa: ANN001
            raise AssertionError("reader must not be called on a cache hit")

    cache = _FakeCache(hit=json.dumps({"key": "ACME-1", "fields": {}}))
    limiter = _FakeRateLimiter()
    monkeypatch.setattr(cli, "build_container", lambda: _container(cache, limiter, _Reader()))

    args = argparse.Namespace(key="ACME-1", user="alice")
    exit_code = cli.cmd_jira_get_issue(args)
    payload = json.loads(capsys.readouterr().out.strip())

    assert exit_code == 0
    assert payload["ok"] is True and payload["from_cache"] is True
    assert limiter.rate_calls == 0  # a cache hit consumes no quota


def test_jira_get_issue_miss_counts_against_governor(monkeypatch, capsys):
    """A cache miss runs the reader once, counts one rate slot on Backend.JIRA, and caches."""
    calls = []

    class _Reader:
        def get_issue(self, key):  # noqa: ANN001
            calls.append(key)
            return {"key": key, "fields": {"summary": "s"}}

    cache = _FakeCache()
    limiter = _FakeRateLimiter()
    monkeypatch.setattr(cli, "build_container", lambda: _container(cache, limiter, _Reader()))

    args = argparse.Namespace(key="ACME-2", user="alice")
    exit_code = cli.cmd_jira_get_issue(args)
    payload = json.loads(capsys.readouterr().out.strip())

    assert exit_code == 0
    assert calls == ["ACME-2"]
    assert payload["from_cache"] is False and payload["issue"]["key"] == "ACME-2"
    assert limiter.enter_calls == 1 and limiter.rate_calls == 1
    assert cache.store  # the read was cached


def test_jira_is_a_governor_backend():
    """The enum member the whole wiring hangs on — a guard against it being renamed/removed."""
    assert Backend.JIRA.value == "jira"
