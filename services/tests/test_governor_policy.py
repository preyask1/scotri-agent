"""domain/governor_policy.py — pure decision logic, no Redis needed.

Covers the rate-limit and concurrency-cap boundary conditions the query governor relies on (see
query-governor SKILL.md) and the read-only peek_state reporting path added to close the
governor-check double-enforcement risk (see services/app/cli.py's cmd_governor_check)."""
from __future__ import annotations

from domain.governor_policy import GovernorPolicy, decide_concurrency, decide_rate_limit
from domain.models import Backend


def test_rate_limit_allows_under_the_cap():
    policy = GovernorPolicy(rate_limit_per_user_per_hour=30)
    decision = decide_rate_limit(policy, current_count_this_window=29)
    assert decision.allowed is True


def test_rate_limit_blocks_at_the_cap():
    """current_count_this_window is the count AFTER incrementing, so `count == limit` is already
    one over the allowed number of prior requests — must block, not allow one more."""
    policy = GovernorPolicy(rate_limit_per_user_per_hour=30)
    decision = decide_rate_limit(policy, current_count_this_window=30)
    assert decision.allowed is False
    assert "30" in decision.reason


def test_rate_limit_blocks_well_over_the_cap():
    policy = GovernorPolicy(rate_limit_per_user_per_hour=30)
    decision = decide_rate_limit(policy, current_count_this_window=45)
    assert decision.allowed is False


def test_concurrency_allows_under_the_cap():
    policy = GovernorPolicy(max_concurrent_per_backend={Backend.DATABRICKS: 5})
    decision = decide_concurrency(policy, Backend.DATABRICKS, current_in_flight=4)
    assert decision.allowed is True


def test_concurrency_blocks_at_the_cap_and_reports_queue_position():
    policy = GovernorPolicy(max_concurrent_per_backend={Backend.DATABRICKS: 5})
    decision = decide_concurrency(policy, Backend.DATABRICKS, current_in_flight=5)
    assert decision.allowed is False
    assert decision.queue_position == 1


def test_concurrency_caps_are_independent_per_backend():
    """A Fabric burst must never starve a Databricks user, and vice versa (agent-instructions.md,
    query-governor SKILL.md) — decide_concurrency must key off the specific backend passed in."""
    policy = GovernorPolicy(max_concurrent_per_backend={Backend.DATABRICKS: 1, Backend.FABRIC: 5})
    databricks_decision = decide_concurrency(policy, Backend.DATABRICKS, current_in_flight=1)
    fabric_decision = decide_concurrency(policy, Backend.FABRIC, current_in_flight=1)
    assert databricks_decision.allowed is False
    assert fabric_decision.allowed is True


def test_default_policy_has_a_cap_for_every_backend():
    """Guards against a future new Backend enum member shipping without a corresponding default cap
    (which would raise a KeyError deep inside decide_concurrency at call time instead of at import)."""
    policy = GovernorPolicy()
    for backend in Backend:
        assert backend in policy.max_concurrent_per_backend


def test_limits_come_from_the_environment_with_the_old_numbers_as_defaults(monkeypatch):
    import pytest
    from domain.governor_policy import GovernorPolicy
    from domain.models import Backend
    for name in ("GOVERNOR_RATE_LIMIT_PER_USER_PER_HOUR", "GOVERNOR_MAX_CONCURRENT",
                 "GOVERNOR_MAX_CONCURRENT_DATABRICKS"):
        monkeypatch.delenv(name, raising=False)
    p = GovernorPolicy()
    assert p.rate_limit_per_user_per_hour == 30 and p.max_concurrent_per_backend[Backend.JIRA] == 5

    monkeypatch.setenv("GOVERNOR_RATE_LIMIT_PER_USER_PER_HOUR", "300")
    monkeypatch.setenv("GOVERNOR_MAX_CONCURRENT", "8")
    monkeypatch.setenv("GOVERNOR_MAX_CONCURRENT_DATABRICKS", "4")
    p = GovernorPolicy()
    assert p.rate_limit_per_user_per_hour == 300
    assert p.max_concurrent_per_backend[Backend.DATABRICKS] == 4
    assert p.max_concurrent_per_backend[Backend.FABRIC] == 8

    monkeypatch.setenv("GOVERNOR_RATE_LIMIT_PER_USER_PER_HOUR", "lots")
    with pytest.raises(ValueError, match="not a whole number"):
        GovernorPolicy()
