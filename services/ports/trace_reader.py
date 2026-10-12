"""Port for reading back what the agent actually did during an engagement turn.

The agent runtime (adapters/agent_sdk) records every run as a session transcript; nothing in the
job store or the interaction log keeps the steps, only the final answer. This port is the read path
to them. Read-only by construction — there is no write method.
"""
from __future__ import annotations

from datetime import datetime
from typing import Protocol

from domain.models import TraceStep


class TraceReader(Protocol):
    def read_turn(self, session_id: str, prompt: str, not_before: datetime) -> list[TraceStep] | None:
        """The raw steps of the turn in `session_id` that began with `prompt` at or after
        `not_before` (a session holds every follow-up turn of a conversation, so the prompt +
        start time is what picks this job's turn out of it). Returns None when the session
        transcript, or that turn within it, can't be found — distinct from an empty list, which
        means the turn exists but the agent took no steps."""
        ...
