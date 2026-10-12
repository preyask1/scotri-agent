"""domain/databricks_run_policy.py — the guardrails that keep the Databricks Jobs-API run-history
reader (OPEN_ITEMS.md item #30) from making the same silent-truncation mistake the ADF runbook hit:
the window is always bounded and capped, and the page limit is capped, so an unbounded "list every
run" pull is refused rather than quietly returning only the newest N."""
from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from domain.databricks_run_policy import (
    MAX_LIMIT,
    MAX_QUERY_WINDOW,
    assert_limit_within_cap,
    assert_window_within_limit,
)
from domain.errors import PolicyError

START = datetime(2026, 9, 26, 0, 0, 0)


def test_window_within_limit_does_not_raise():
    assert_window_within_limit(START, START + timedelta(days=1))  # must not raise


def test_window_exactly_at_limit_does_not_raise():
    assert_window_within_limit(START, START + MAX_QUERY_WINDOW)  # must not raise


def test_window_over_limit_raises():
    with pytest.raises(PolicyError):
        assert_window_within_limit(START, START + MAX_QUERY_WINDOW + timedelta(seconds=1))


def test_inverted_window_raises():
    with pytest.raises(PolicyError):
        assert_window_within_limit(START, START - timedelta(days=1))


def test_empty_window_raises():
    with pytest.raises(PolicyError):
        assert_window_within_limit(START, START)


def test_limit_within_cap_does_not_raise():
    assert_limit_within_cap(1)
    assert_limit_within_cap(MAX_LIMIT)


def test_limit_zero_raises():
    with pytest.raises(PolicyError):
        assert_limit_within_cap(0)


def test_limit_negative_raises():
    with pytest.raises(PolicyError):
        assert_limit_within_cap(-5)


def test_limit_over_cap_raises():
    with pytest.raises(PolicyError):
        assert_limit_within_cap(MAX_LIMIT + 1)
