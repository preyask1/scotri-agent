# Copyright (c) 2026 Preyas Kulshrestha. Part of the Scotri engine; MIT License (see LICENSE).
# Keep this notice in copies and derived works, including AI-assisted ones.
"""Live progress for a running engagement (OPEN_ITEMS.md #39, decided 2026-10-04).

A full lifecycle runs for minutes, so a polling caller needs more than "running". This is that
signal, and it is deliberately coarse: which lifecycle stage the run is in, how many tool calls it
has made, and when it last did something. Never what it is looking at. A job_id-holder can poll any
job (app/api.py's get_chat), so nothing here may name a ticket, table, notebook, pipeline, report,
query or file. The detail lives in GET .../steps, which is owner-only.

Phases are derived by the engine from the agent's own Skill tool calls, not announced by the agent:
it can't forget to emit one, and nothing it writes ends up here. Only the lifecycle-stage skills move
the phase; the support skills a stage uses (presence-claim, databricks-query, lineage-trace, ...)
run inside several stages, so they leave it where it is.

Pure data + pure transitions. Storage is ports/progress_store.py; wiring is engagement_policy.py.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, replace
from datetime import datetime

STARTING = "starting"
WORKING = "working"

PHASE_LABELS: dict[str, str] = {
    STARTING: "Starting",
    WORKING: "Working",
    "intake": "Reading the request",
    "verdict": "Investigating the reported problem",
    "impact": "Analysing impact",
    "plan": "Writing the implementation plan",
    "testing": "Running test queries",
    "proposal": "Drafting a proposed change",
    "handoff": "Writing the handoff document",
    "jira_update": "Preparing a Jira update",
}
"""Every phase a caller can see, with a label a UI can show as-is. Generic on purpose (see module
docstring): adding a phase is fine, putting anything run-specific in a label is not."""

STAGE_SKILLS: dict[str, str] = {
    "jira-intake": "intake",
    "bug-investigation": "verdict",
    "impact-analysis": "impact",
    "implementation-plan": "plan",
    "test-plan-and-results": "testing",
    "propose-change": "proposal",
    "handoff-doc": "handoff",
    "jira-write": "jira_update",
}
"""Skill name (as the agent passes it to the Skill tool) -> phase. Support skills are absent on
purpose: they don't mark a stage."""


def phase_for_skill(skill: str | None) -> str | None:
    """The phase a Skill call starts, or None if it isn't a lifecycle stage. Tolerates a
    plugin-style `namespace:skill` name."""
    if not skill:
        return None
    return STAGE_SKILLS.get(skill.rsplit(":", 1)[-1].strip())


@dataclass(frozen=True)
class EngagementProgress:
    job_id: str
    started_at: datetime
    last_activity_at: datetime
    phase: str = STARTING
    phases_seen: tuple[str, ...] = (STARTING,)
    """Every phase entered so far, in first-entered order, so a UI can tick off a stepper."""
    tool_calls: int = 0
    session_id: str | None = None
    """The agent session, captured as soon as the run reports it rather than at the end. Internal:
    it lets GET .../steps work mid-run and lets an interrupted job be resumed as a follow-up. Never
    part of public_view()."""

    @staticmethod
    def start(job_id: str, now: datetime) -> "EngagementProgress":
        return EngagementProgress(job_id=job_id, started_at=now, last_activity_at=now)

    def with_session(self, session_id: str, now: datetime) -> "EngagementProgress":
        return replace(self, session_id=session_id, last_activity_at=now)

    def with_tool_call(self, skill: str | None, now: datetime) -> "EngagementProgress":
        """One tool call. A lifecycle-stage Skill call moves the phase; the first tool call of a run
        that never uses a stage skill (a plain debugging question) moves it from STARTING to WORKING."""
        phase = phase_for_skill(skill) or (WORKING if self.phase == STARTING else self.phase)
        seen = self.phases_seen if phase in self.phases_seen else self.phases_seen + (phase,)
        return replace(self, phase=phase, phases_seen=seen, tool_calls=self.tool_calls + 1,
                       last_activity_at=now)

    def public_view(self, now: datetime, finished: bool = False) -> dict:
        """What a poller sees. Phase, counts and times only: see the module docstring for why. Once
        the job is `finished`, elapsed time stops at its last activity instead of growing with every
        poll."""
        return {
            "phase": self.phase,
            "phase_label": PHASE_LABELS.get(self.phase, PHASE_LABELS[WORKING]),
            "phases_seen": list(self.phases_seen),
            "tool_calls": self.tool_calls,
            "started_at": self.started_at.isoformat(),
            "last_activity_at": self.last_activity_at.isoformat(),
            "elapsed_seconds": max(0, int(((self.last_activity_at if finished else now) - self.started_at).total_seconds())),
        }

    def to_json(self) -> str:
        return json.dumps({
            "job_id": self.job_id,
            "started_at": self.started_at.isoformat(),
            "last_activity_at": self.last_activity_at.isoformat(),
            "phase": self.phase,
            "phases_seen": list(self.phases_seen),
            "tool_calls": self.tool_calls,
            "session_id": self.session_id,
        })

    @staticmethod
    def from_json(s: str) -> "EngagementProgress":
        data = json.loads(s)
        return EngagementProgress(
            job_id=data["job_id"],
            started_at=datetime.fromisoformat(data["started_at"]),
            last_activity_at=datetime.fromisoformat(data["last_activity_at"]),
            phase=data.get("phase", STARTING),
            phases_seen=tuple(data.get("phases_seen") or (STARTING,)),
            tool_calls=int(data.get("tool_calls", 0)),
            session_id=data.get("session_id"),
        )
