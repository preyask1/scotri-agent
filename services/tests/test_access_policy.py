"""domain/access_policy.py — pure role/ownership checks, tested with fake Principals (no FastAPI,
no Redis, no token). Locks down the fail-closed invariant and each of the four gate categories from
docs/ai/identity-model.md."""
from __future__ import annotations

import pytest

from domain.access_policy import (
    can_read_owned,
    has_any_role,
    require_human_caller,
    require_owner_or_role,
    require_role,
)
from domain.errors import ErrorCategory, PolicyError
from domain.models import Principal


def _p(subject="alice", platform="web-portal", roles=None) -> Principal:
    return Principal(subject=subject, display_name=subject, platform=platform, roles=list(roles or []))


# --- has_any_role / fail-closed --------------------------------------------------------------

def test_has_any_role_intersection():
    assert has_any_role(_p(roles=["lead"]), {"lead", "admin"}) is True
    assert has_any_role(_p(roles=["analyst"]), {"lead", "admin"}) is False


def test_empty_allowed_is_always_false_fail_closed():
    """An empty allowed-set means 'no role satisfies this', never 'everyone passes'."""
    assert has_any_role(_p(roles=["admin"]), set()) is False
    assert has_any_role(_p(roles=[]), set()) is False


def test_role_list_with_only_blanks_is_false():
    assert has_any_role(_p(roles=["admin"]), ["", "  "]) is False  # after filtering, empty


# --- category 2: capability gate -------------------------------------------------------------

def test_require_role_allows_holder_raises_for_non_holder():
    require_role(_p(roles=["admin"]), {"admin"})  # no raise
    with pytest.raises(PolicyError) as ei:
        require_role(_p(roles=["analyst"]), {"admin"}, action="reset the governor")
    assert ei.value.category is ErrorCategory.POLICY
    assert "reset the governor" in ei.value.user_message


# --- category 1: ownership-scoped access -----------------------------------------------------

def test_owner_always_reads_own_regardless_of_roles():
    assert can_read_owned(_p(subject="alice", roles=[]), owner_subject="alice", admin_roles={"lead"}) is True


def test_non_owner_without_role_denied():
    assert can_read_owned(_p(subject="bob", roles=["analyst"]), owner_subject="alice", admin_roles={"lead"}) is False


def test_non_owner_with_widening_role_allowed():
    assert can_read_owned(_p(subject="carol", roles=["lead"]), owner_subject="alice", admin_roles={"lead", "admin"}) is True


def test_empty_admin_roles_is_strictly_owner_only():
    """The safe default: with no widening roles configured, even an admin can't read another's."""
    assert can_read_owned(_p(subject="carol", roles=["admin"]), owner_subject="alice", admin_roles=set()) is False


def test_require_owner_or_role_raises_for_stranger():
    require_owner_or_role(_p(subject="alice"), "alice", {"lead"})  # owner, no raise
    with pytest.raises(PolicyError) as ei:
        require_owner_or_role(_p(subject="bob", roles=[]), "alice", {"lead"}, resource="conversation c1")
    assert "conversation c1" in ei.value.user_message


# --- category 4: caller-shape gate -----------------------------------------------------------

def test_human_caller_passes_service_caller_refused():
    require_human_caller(_p(platform="teams-app-id"), {"scheduled-sweep", "jira-webhook"})  # human, no raise
    with pytest.raises(PolicyError) as ei:
        require_human_caller(
            _p(platform="scheduled-sweep"), {"scheduled-sweep", "jira-webhook"}, action="post a Jira comment"
        )
    assert "scheduled-sweep" in ei.value.user_message
    assert ei.value.category is ErrorCategory.POLICY
