# Copyright (c) 2026 Preyas Kulshrestha. Part of the Scotri engine; MIT License (see LICENSE).
# Keep this notice in copies and derived works, including AI-assisted ones.
"""Reads an engagement turn's steps back out of the Claude Agent SDK's session transcript.

The SDK's bundled CLI writes every session as `<config dir>/projects/<encoded cwd>/<session_id>.jsonl`
(config dir is `~/.claude` unless `CLAUDE_CONFIG_DIR` says otherwise). The encoded-cwd folder name is
the SDK's own convention, so rather than reproduce it, this globs for the session id across project
folders — a uuid is unique enough that there's only ever one match.

Format facts this depends on (observed against CLI v2.1.283, sdk-py entrypoint): a turn starts at a
`type: "user"` record whose `message.content` is a plain string (the prompt); each tool call is an
`assistant` record with a `tool_use` block, and its result is a later `user` record whose
`tool_result` block carries the same `tool_use_id`. Other record types (thinking blocks, attachments,
queue/latch bookkeeping) are not steps and are skipped. A follow-up turn resumed into the same
session appends to the same file, which is why the turn is found by prompt + start time.
"""
from __future__ import annotations

import json
import re
from datetime import datetime, timedelta
from pathlib import Path

from domain.grounding import TAG as GROUNDING_TAG
from domain.models import TraceStep

_SESSION_ID = re.compile(r"^[0-9a-fA-F-]{8,64}$")  # never let a stored value steer the glob elsewhere
_CLOCK_SKEW = timedelta(seconds=2)  # the queued-at time is taken before the SDK stamps the prompt
_PERSISTED = re.compile(r"Full output saved to:\s*(\S[^\n<]*?\.txt)")


def _parse_ts(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _is_prompt(record: dict) -> bool:
    return (
        record.get("type") == "user"
        and not record.get("isSidechain")
        and isinstance((record.get("message") or {}).get("content"), str)
    )


def _is_turn_for(sent: str, prompt: str) -> bool:
    """Whether the prompt the agent was sent is this job's prompt. The engine puts its own blocks in
    front of the caller's prompt, each separated by a blank line: the run context, the pre-fetch,
    attached files (domain/engagement_policy.py). So the stored prompt is either the whole of what
    was sent or the part after the last such separator. Requiring the blank line, not a bare
    suffix, keeps a short prompt like "yes" from matching a turn that merely ends in "yes"
    (OPEN_ITEMS.md #61)."""
    sent, prompt = sent.strip(), prompt.strip()
    return sent == prompt or sent.endswith("\n\n" + prompt)


def _result_text(block: dict) -> str:
    content = block.get("content")
    if isinstance(content, list):
        return " ".join(part.get("text", "") for part in content if isinstance(part, dict))
    return content if isinstance(content, str) else ""


def _tool_input_summary(tool_input: dict) -> str:
    """The one field that says what the call did, falling back to the whole input as JSON."""
    for key in ("command", "file_path", "path", "pattern", "url", "description", "query", "prompt"):
        if isinstance(tool_input.get(key), str):
            return tool_input[key]
    return json.dumps(tool_input, ensure_ascii=False)


class ClaudeTranscriptReader:
    """Implements ports.trace_reader.TraceReader."""

    def __init__(self, config_dir: Path) -> None:
        self._projects = config_dir / "projects"

    def _find(self, session_id: str) -> Path | None:
        if not _SESSION_ID.match(session_id):
            return None
        matches = list(self._projects.glob(f"*/{session_id}.jsonl"))
        return matches[0] if matches else None

    def read_turn(self, session_id: str, prompt: str, not_before: datetime) -> list[TraceStep] | None:
        path = self._find(session_id)
        if path is None:
            return None

        records: list[dict] = []
        with path.open(encoding="utf-8") as f:
            for line in f:
                try:
                    records.append(json.loads(line))
                except json.JSONDecodeError:
                    continue  # a half-written trailing line of a session still in flight

        start = self._find_turn_start(records, prompt, not_before)
        if start is None:
            return None

        steps: list[TraceStep] = []
        by_call_id: dict[str, int] = {}  # tool_use_id -> index into steps, to attach the result
        for record in records[start + 1:]:
            if _is_prompt(record):
                if record["message"]["content"].startswith(GROUNDING_TAG):
                    continue  # the engine's correction turn belongs to the same job
                break  # the next turn's prompt
            message = record.get("message") or {}
            content = message.get("content")
            if not isinstance(content, list) or record.get("isSidechain"):
                continue
            for block in content:
                kind = block.get("type")
                if record.get("type") == "assistant" and kind == "tool_use":
                    by_call_id[block["id"]] = len(steps)
                    steps.append(
                        TraceStep(
                            kind="tool_call",
                            timestamp=record.get("timestamp"),
                            tool=block.get("name"),
                            input_summary=_tool_input_summary(block.get("input") or {}),
                        )
                    )
                elif record.get("type") == "assistant" and kind == "text" and block.get("text"):
                    steps.append(TraceStep(kind="message", timestamp=record.get("timestamp"), text=block["text"]))
                elif record.get("type") == "user" and kind == "tool_result":
                    index = by_call_id.get(block.get("tool_use_id"))
                    if index is not None:
                        steps[index] = TraceStep(
                            kind="tool_call",
                            timestamp=steps[index].timestamp,
                            tool=steps[index].tool,
                            input_summary=steps[index].input_summary,
                            result_preview=_result_text(block),
                        )
        return steps

    def read_turn_evidence(self, session_id: str, prompt: str, not_before: datetime) -> str | None:
        """Everything the agent was shown or ran in its session up to now, as one text, for the
        grounding check (domain/grounding.py): every prompt it was sent (pre-fetch included), every
        tool result in full
        (a result too large to inline is read from the file the CLI saved it to), every Bash command,
        and the JSON files it wrote (batch inputs, where its queries live). Its own prose and the
        markdown it wrote are left out on purpose: they are the claims, not the evidence. None when
        the transcript or turn can't be found."""
        path = self._find(session_id)
        if path is None:
            return None
        records: list[dict] = []
        with path.open(encoding="utf-8") as f:
            for line in f:
                try:
                    records.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
        if self._find_turn_start(records, prompt, not_before) is None:
            return None

        # The whole session so far, not just this turn: a follow-up turn may rightly cite what an
        # earlier turn of the same conversation queried.
        parts: list[str] = []
        for record in records:
            if record.get("isSidechain"):
                continue
            content = (record.get("message") or {}).get("content")
            if record.get("type") == "user" and isinstance(content, str):
                if not content.startswith(GROUNDING_TAG):  # it lists the claims; never evidence for them
                    parts.append(content)
                continue
            if not isinstance(content, list):
                continue
            for block in content:
                kind = block.get("type")
                if record.get("type") == "assistant" and kind == "tool_use":
                    tool_input = block.get("input") or {}
                    if block.get("name") == "Bash" and isinstance(tool_input.get("command"), str):
                        parts.append(tool_input["command"])
                    elif (block.get("name") == "Write" and str(tool_input.get("file_path", "")).endswith(".json")
                          and isinstance(tool_input.get("content"), str)):
                        parts.append(tool_input["content"])
                elif record.get("type") == "user" and kind == "tool_result":
                    text = _result_text(block)
                    parts.append(text)
                    parts.extend(self._persisted_outputs(text, path.parent))
        return "\n".join(parts)

    def _persisted_outputs(self, text: str, session_dir: Path) -> list[str]:
        """A tool result too large to inline says where the CLI saved it. Read it back, but only from
        under the transcripts folder: a stored path must never steer a read elsewhere."""
        out = []
        for match in _PERSISTED.finditer(text):
            candidate = Path(match.group(1).strip())
            try:
                resolved = candidate.resolve()
                if resolved.is_relative_to(self._projects.resolve()) and resolved.is_file():
                    out.append(resolved.read_text(encoding="utf-8", errors="replace"))
            except OSError:
                continue
        return out

    @staticmethod
    def _find_turn_start(records: list[dict], prompt: str, not_before: datetime) -> int | None:
        for i, record in enumerate(records):
            if not _is_prompt(record) or not _is_turn_for(record["message"]["content"], prompt):
                continue
            ts = _parse_ts(record.get("timestamp"))
            if ts is None or ts >= not_before - _CLOCK_SKEW:
                return i
        return None
