# Copyright (c) 2026 Preyas Kulshrestha. Part of the Scotri engine; MIT License (see LICENSE).
# Keep this notice in copies and derived works, including AI-assisted ones.
"""Presence-claim policy (presence-claim SKILL.md) — TTL rules and the collision-notice wording.

Advisory only, never a hard lock (presence-claim SKILL.md's own hard boundary) — this module never
raises to block a caller; it only decides whether to surface a notice and what it should say.
"""
from __future__ import annotations

from datetime import datetime, timedelta

from domain.models import Claim

# PENDING: tune from observed usage. 30 minutes of inactivity is a starting guess, not a measured value.
CLAIM_INACTIVITY_TIMEOUT = timedelta(minutes=30)


def is_claim_stale(claim: Claim, now: datetime) -> bool:
    return (now - claim.last_heartbeat) > CLAIM_INACTIVITY_TIMEOUT


def format_collision_notice(claim: Claim, now: datetime) -> str:
    minutes_active = int((now - claim.started_at).total_seconds() // 60)
    return (
        f"{claim.owner} has been investigating {claim.entity} for the last {minutes_active} "
        f"minute(s) — proceed anyway?"
    )
