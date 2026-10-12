"""domain/adf_run_policy.py — the guardrails that make an earlier investigation runbook's documented gotcha (an
unfiltered ADF run-history query only returning the newest ~100 runs across the whole factory)
structurally impossible to hit from this codebase, and cap the query window to something ARM can
answer without silently truncating."""
from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from domain.adf_run_policy import (
    MAX_QUERY_WINDOW,
    assert_pipeline_names_given,
    assert_trigger_names_given,
    assert_window_within_limit,
)
from domain.errors import PolicyError

START = datetime(2026, 9, 26, 0, 0, 0)


def test_pipeline_names_required():
    with pytest.raises(PolicyError):
        assert_pipeline_names_given([])


def test_pipeline_names_present_does_not_raise():
    assert_pipeline_names_given(["PL_Forecast_Sales"])  # must not raise


def test_trigger_names_required():
    with pytest.raises(PolicyError):
        assert_trigger_names_given([])


def test_trigger_names_present_does_not_raise():
    assert_trigger_names_given(["Trg_Daily_Forecast"])  # must not raise


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
