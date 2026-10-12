"""domain/presence_policy.py — TTL staleness + collision-notice wording (presence-claim SKILL.md).

Advisory only, never a hard lock — these tests cover the staleness boundary and the notice text a
skill surfaces to the user, not any blocking behavior (there is none)."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from domain.models import Claim
from domain.presence_policy import CLAIM_INACTIVITY_TIMEOUT, format_collision_notice, is_claim_stale

NOW = datetime(2026, 9, 24, 12, 0, 0, tzinfo=timezone.utc)


def _claim(started_minutes_ago: int, heartbeat_minutes_ago: int) -> Claim:
    return Claim(
        entity="ACME-1234",
        owner="Data.Engineer2",
        started_at=NOW - timedelta(minutes=started_minutes_ago),
        last_heartbeat=NOW - timedelta(minutes=heartbeat_minutes_ago),
    )


def test_fresh_claim_is_not_stale():
    claim = _claim(started_minutes_ago=12, heartbeat_minutes_ago=1)
    assert is_claim_stale(claim, NOW) is False


def test_claim_exactly_at_the_timeout_boundary_is_not_yet_stale():
    """is_claim_stale uses a strict `>`, so a heartbeat exactly CLAIM_INACTIVITY_TIMEOUT old is the
    last moment it still counts as active — must not flip to stale one tick early."""
    boundary_minutes = int(CLAIM_INACTIVITY_TIMEOUT.total_seconds() // 60)
    claim = _claim(started_minutes_ago=boundary_minutes + 5, heartbeat_minutes_ago=boundary_minutes)
    assert is_claim_stale(claim, NOW) is False


def test_claim_past_the_timeout_is_stale():
    boundary_minutes = int(CLAIM_INACTIVITY_TIMEOUT.total_seconds() // 60)
    claim = _claim(started_minutes_ago=boundary_minutes + 5, heartbeat_minutes_ago=boundary_minutes + 1)
    assert is_claim_stale(claim, NOW) is True


def test_collision_notice_names_the_owner_and_minutes_active():
    claim = _claim(started_minutes_ago=12, heartbeat_minutes_ago=1)
    notice = format_collision_notice(claim, NOW)
    assert "Data.Engineer2" in notice
    assert "ACME-1234" in notice
    assert "12 minute" in notice


def test_collision_notice_uses_started_at_not_last_heartbeat_for_duration():
    """The notice reports how long the OTHER engineer has been investigating overall (started_at),
    not how recently they last heartbeat — getting this backwards would understate a long-running
    concurrent investigation."""
    claim = _claim(started_minutes_ago=45, heartbeat_minutes_ago=2)
    notice = format_collision_notice(claim, NOW)
    assert "45 minute" in notice
