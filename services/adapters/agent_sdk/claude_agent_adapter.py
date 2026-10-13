# Copyright (c) 2026 Preyas Kulshrestha. Part of the Scotri engine; MIT License (see LICENSE).
# Keep this notice in copies and derived works, including AI-assisted ones.
"""Wraps the Python Claude Agent SDK to actually run a Scotri engagement.

cwd = the workspace root (so CLAUDE.md / agent-instructions.md / .claude/skills /
.claude/agents/Scotri.md load the same way an interactive `claude` session would),
setting_sources=["project"] (required for CLAUDE.md to load at all), permission_mode=
"bypassPermissions" (this runs unattended — Scotri's own agent-instructions.md/Scotri.md hard rules
are what keep it read-only, not this flag), plus a PreToolUse hook (domain/tool_scope.py, OPEN_ITEMS.md
#28) that structurally limits file writes to scratch/proposals/investigations and Bash to app.cli calls.

CONFIRMED against the installed `claude-agent-sdk` version (2026-09-25, first real run): messages
are typed dataclasses (`ResultMessage`, `AssistantMessage`, ...) distinguished by their Python
class, not by a `.type` attribute — `ResultMessage` has no `type` field at all. Matching on
`getattr(message, "type", None) == "result"` therefore never matched anything, so every engagement
silently fell through to "no output", even on a fully successful run. Fixed to match with
`isinstance(message, ResultMessage)` instead.

Multi-turn support (2026-09-25): `ResultMessage.session_id` is captured and returned on every run,
and `resume_session_id` (when given) is passed straight through as `ClaudeAgentOptions.resume` —
the SDK's own mechanism for continuing an earlier session with its prior context intact, rather
than every call being a fresh, memory-less run.

Model wiring (fixed 2026-09-25): `ClaudeAgentOptions.model` defaults to `None`, which omits `--model`
from the underlying CLI invocation entirely — the CLI then falls back to whatever model it itself
resolves as default, independent of config/model_registry.yaml. That silently disconnected the
registry (whose one `default` entry is anthropic/claude-sonnet-5-5 since 2026-10-07, OPEN_ITEMS.md #4)
from what actually ran. `model` is now a constructor argument, resolved once by the caller (see
app/container.py, which reads `model_registry.get("default")` — the whole engagement is one
`query()` call, not a call per lifecycle stage, so there is no per-stage model switch to make here)
and passed straight through as `ClaudeAgentOptions.model`.

Cancellation (2026-09-27): `run()` blocks on its own private event loop (one `asyncio.run` per call,
in whatever worker thread the caller used), so a stop request from another thread can't just
`task.cancel()`. Each in-flight run registers its (loop, task) under the caller's `job_id`;
`cancel()` schedules `task.cancel()` onto that loop with `call_soon_threadsafe`. The CancelledError
surfaces inside `query()`, whose own `finally` (claude_agent_sdk/_internal/client.py) terminates
the CLI subprocess, and is turned into `AgentRunResult(cancelled=True)` here. The registry is
in-memory and per-process, so `cancel()` only reaches runs in this process. A cancel that arrives at
another process crosses over through the job record (OPEN_ITEMS.md #33, 2026-10-07): it writes
CANCELLED first, and app/engagement_runner.py in the process running the job sees that within a few
seconds and calls `cancel()` here.
"""
from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import shutil
import threading
from pathlib import Path
from typing import Callable

from claude_agent_sdk import (
    AssistantMessage, ClaudeAgentOptions, HookMatcher, ResultMessage, SystemMessage, TextBlock, ToolUseBlock,
    query,
)

from domain.tool_scope import DENIED_TOOLS, check_tool_call
from ports.agent_runtime import AgentEvent, AgentRunResult

logger = logging.getLogger(__name__)

DEFAULT_MAX_TURNS = 150
"""Turn ceiling per run (OPEN_ITEMS.md #33). The largest single engagement measured on 2026-10-08
took 43 tool calls (a PROJ-2118 bug investigation): 150 leaves room for a full Story lifecycle with
every test run, and stops a looping run long before the wall clock does."""

ENGAGEMENT_WALL_CLOCK_TIMEOUT_SECONDS = 20 * 60
"""Default wall-clock ceiling for one engagement run (OPEN_ITEMS.md #33, decided 2026-10-01).

A full lifecycle (intake -> verdict -> four-repo impact -> plan -> executed tests -> handoff) is
inherently multi-minute — the PROJ-2085 run was ~10m38s — so this is ~2x headroom, not a tight budget.
Its job is to stop a *hung* run from spinning turns/time forever with no human watching (the webhook/
scheduled-sweep trigger paths, #7), not to cut short legitimate long work. On expiry the run is
stopped and reported as a non-success terminal outcome (is_error + incomplete), never a silent hang
and never a fabricated answer. Overridable per deployment via ENGAGEMENT_TIMEOUT_SECONDS (see
app/container.py). The complementary caps #33 lists are `max_turns` (ENGAGEMENT_MAX_TURNS, default
DEFAULT_MAX_TURNS, set from measured runs) and `max_budget_usd` (ENGAGEMENT_MAX_BUDGET_USD, off until a
week of the per-run cost this adapter now reports says where to put it). Cross-process
cancellation is built (2026-10-07), in app/engagement_runner.py rather than here."""

_DELEGATION_TOOLS = ["Agent"]
"""Tools denied to every engagement, because they outlive the turn that starts them.

The SDK terminates the CLI subprocess as soon as the message iterator ends (its own
``finally: await query.close()``), and the iterator ends at ResultMessage — i.e. the moment the
top-level turn returns text. A background subagent spawned via `Agent` is therefore killed the
instant Scotri says "I'll report back when it completes", and its work never happens. The turn
itself looks perfectly successful, so the engagement lands as SUCCEEDED with a promise as its
result. Observed on PROJ-2085 (2026-09-28): three engagements, two silent stalls, no presence claim
ever registered, no deliverable produced.

That pattern is fine in an interactive session, where the parent outlives the child and a
completion notification arrives later. It cannot work headless, where the turn IS the process
lifetime. So the lifecycle has to run inline, in the turn, and this denial is what enforces it —
prose in agent-instructions.md is the backup, not the guard."""

MAX_STREAM_MESSAGE_BYTES = 64 * 1024 * 1024
"""Largest single stream-json line the SDK will read from the CLI (its default is 1 MB).

A `Read` of an image or PDF comes back as one message carrying the file base64-encoded, so a
ticket attachment of a few hundred KB already crosses 1 MB, and the SDK then kills the whole run
with "JSON message exceeded maximum buffer size". Observed on PROJ-2129 (2026-10-08): the run read a
587 KB PDF attachment and crashed. 64 MB clears the API's own 32 MB request ceiling with room for
base64 overhead."""

GIT_BASH_ENV_VAR = "CLAUDE_CODE_GIT_BASH_PATH"

MISSING_BASH_MESSAGE = (
    "This engagement was stopped before it started: the agent had no Bash tool, so it could not run "
    "any app.cli command (no governed query, no mirror read). On Windows that means the Claude CLI "
    "couldn't find Git Bash. Set CLAUDE_CODE_GIT_BASH_PATH to Git's bin\bash.exe in services/.env, "
    "or start the API from the launcher, then re-run."
)


def find_git_bash() -> str | None:
    """Git for Windows' bash.exe, or None. Tried in order: next to the `git` on PATH (Git's
    cmd/git.exe or mingw64/bin/git.exe, both two levels under Git's root), Scoop's git, then the
    standard install folders."""
    candidates: list[Path] = []
    git = shutil.which("git")
    if git:
        git_dir = Path(git).resolve().parent
        candidates += [git_dir.parent / "bin" / "bash.exe", git_dir.parent.parent / "bin" / "bash.exe"]
    candidates.append(Path.home() / "scoop" / "apps" / "git" / "current" / "bin" / "bash.exe")
    for base in (os.environ.get("ProgramFiles"), os.environ.get("ProgramFiles(x86)"),
                 os.environ.get("LOCALAPPDATA") and os.path.join(os.environ["LOCALAPPDATA"], "Programs")):
        if base:
            candidates.append(Path(base) / "Git" / "bin" / "bash.exe")
    return next((str(c) for c in candidates if c.is_file()), None)


def _git_bash_env() -> dict[str, str]:
    """On Windows, point the Claude CLI at Git Bash explicitly, unless the deployment already does.

    Without it the CLI looks for Git Bash itself, and when that lookup fails it starts with no Bash
    tool at all, silently: every app.cli call becomes impossible and the agent can only guess.
    Observed 2026-10-07 with the API started from a Claude Code session's Git Bash shell (the same
    machine started from the launcher was fine); setting this variable alone restored the tool."""
    if os.name != "nt" or os.environ.get(GIT_BASH_ENV_VAR):
        return {}
    path = find_git_bash()
    if path is None:
        logger.warning("Git Bash not found; engagements may start without a Bash tool. Set %s.", GIT_BASH_ENV_VAR)
        return {}
    return {GIT_BASH_ENV_VAR: path}


def _init_tools(message) -> list[str] | None:
    """The tool names from the CLI's opening `init` message, or None for any other message."""
    if isinstance(message, SystemMessage) and message.subtype == "init":
        tools = (message.data or {}).get("tools")
        return list(tools) if isinstance(tools, list) else None
    return None


def _tool_scope_hook(root: str):
    """A PreToolUse hook enforcing domain/tool_scope.py (OPEN_ITEMS.md #28, #60(f)). Hooks run even
    under bypassPermissions, which is why this is a hook and not a `can_use_tool` callback (that one
    is only consulted when a permission prompt would appear, which bypassPermissions never does). A
    denial goes back to the agent with the reason, so it can take the allowed route instead."""

    async def hook(hook_input, tool_use_id, context):
        if hook_input.get("hook_event_name") != "PreToolUse":
            return {}
        tool = hook_input.get("tool_name", "")
        reason = check_tool_call(tool, hook_input.get("tool_input") or {}, root)
        if reason is None:
            return {}
        logger.warning("tool_scope denied %s: %s", tool, reason)
        return {"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny",
                                       "permissionDecisionReason": f"Denied by Scotri's tool scope: {reason}"}}

    return hook


_FILE_WRITING_TOOLS = {"Write": "file_path", "Edit": "file_path", "MultiEdit": "file_path",
                       "NotebookEdit": "notebook_path"}
"""Tool name -> the input field naming the file it writes. Used to report AgentRunResult.files_written,
which is how an engagement's markdown deliverables get published (domain/shared_files.py). A file
written through Bash isn't seen here; the skills write their outputs with Write."""


def _written_paths(message) -> list[str]:
    if not isinstance(message, AssistantMessage):
        return []
    paths = []
    for block in message.content:
        if isinstance(block, ToolUseBlock) and block.name in _FILE_WRITING_TOOLS:
            path = (block.input or {}).get(_FILE_WRITING_TOOLS[block.name])
            if isinstance(path, str) and path:
                paths.append(path)
    return paths


def _top_level_text(message) -> list[str]:
    """The text blocks of one top-level assistant message. ResultMessage.result carries only the
    turn's LAST text block, so an answer given mid-run, before more tool calls, was dropped from the
    stored result. Engagement 2577baf2 (PROJ-2052, 2026-10-05) answered the user's question, then ran a
    batch and summarised that, and only the summary was kept. Subagent messages (parent_tool_use_id
    set) are skipped: they aren't the turn's own words."""
    if not isinstance(message, AssistantMessage) or message.parent_tool_use_id is not None:
        return []
    return [block.text for block in message.content if isinstance(block, TextBlock) and block.text.strip()]


def _combined_text(texts: list[str], result_text: str) -> str:
    """Every top-level text block in order, ending with the result text. The result text is added
    only when it isn't already the last block, so a one-message run reads exactly as before."""
    parts = [t.strip() for t in texts]
    if result_text.strip() and (not parts or parts[-1] != result_text.strip()):
        parts.append(result_text.strip())
    return "\n\n".join(parts)


def _events(message) -> list[AgentEvent]:
    """The progress events in one streamed message (OPEN_ITEMS.md #39). The CLI's opening `init`
    system message carries the session id; each tool_use block in an assistant message is a call."""
    if isinstance(message, SystemMessage):
        session_id = (message.data or {}).get("session_id") if message.subtype == "init" else None
        return [AgentEvent(kind="session", session_id=session_id)] if isinstance(session_id, str) else []
    if not isinstance(message, AssistantMessage):
        return []
    events = []
    for block in message.content:
        if isinstance(block, ToolUseBlock):
            skill = (block.input or {}).get("skill") if block.name == "Skill" else None
            events.append(AgentEvent(kind="tool_call", tool=block.name,
                                     skill=skill if isinstance(skill, str) else None))
    return events


def _emit(on_event: Callable[[AgentEvent], None] | None, message) -> None:
    if on_event is None:
        return
    for event in _events(message):
        try:
            on_event(event)
        except Exception:  # noqa: BLE001 — progress is an extra; it must never fail the run
            logger.exception("on_event raised; ignoring")


class ClaudeAgentSdkRuntime:
    """Implements ports.agent_runtime.AgentRuntime."""

    def __init__(
        self,
        model: str | None = None,
        timeout_seconds: float = ENGAGEMENT_WALL_CLOCK_TIMEOUT_SECONDS,
        max_turns: int | None = None,
        max_budget_usd: float | None = None,
    ) -> None:
        self._model = model
        self._timeout_seconds = timeout_seconds
        self._max_turns = max_turns
        self._max_budget_usd = max_budget_usd
        self._active: dict[str, tuple[asyncio.AbstractEventLoop, asyncio.Task]] = {}
        self._active_lock = threading.Lock()

    def run(
        self,
        prompt: str,
        cwd: str,
        resume_session_id: str | None = None,
        job_id: str | None = None,
        env: dict[str, str] | None = None,
        on_event: Callable[[AgentEvent], None] | None = None,
    ) -> AgentRunResult:
        return asyncio.run(self._run_async(prompt, cwd, resume_session_id, job_id, env, on_event))

    def cancel(self, job_id: str) -> bool:
        with self._active_lock:
            entry = self._active.get(job_id)
        if entry is None:
            return False
        loop, task = entry
        try:
            loop.call_soon_threadsafe(task.cancel)
        except RuntimeError:  # loop already closed — the run finished between the lookup and here
            return False
        return True

    async def _run_async(
        self, prompt: str, cwd: str, resume_session_id: str | None, job_id: str | None,
        env: dict[str, str] | None = None,
        on_event: Callable[[AgentEvent], None] | None = None,
    ) -> AgentRunResult:
        if job_id is not None:
            task = asyncio.current_task()
            assert task is not None
            with self._active_lock:
                self._active[job_id] = (asyncio.get_running_loop(), task)
        written: list[str] = []  # owned here so a timeout or cancel still reports what was written
        try:
            # Wall-clock ceiling (OPEN_ITEMS.md #33). On expiry, wait_for cancels the inner _query,
            # whose cancellation propagates into the SDK's query() finally and terminates the CLI
            # subprocess — so a timeout tears the run down cleanly, no orphaned process. A timeout is
            # a run-side failure, so per the AgentRuntime contract it is returned as a non-success
            # AgentRunResult (is_error + incomplete), never raised; an externally-requested cancel()
            # still surfaces as CancelledError below and is reported distinctly as cancelled=True.
            return await asyncio.wait_for(
                self._query(prompt, cwd, resume_session_id, env, written, on_event), timeout=self._timeout_seconds
            )
        except asyncio.TimeoutError:
            minutes = self._timeout_seconds / 60
            return AgentRunResult(
                text=(
                    f"Engagement exceeded the {minutes:.0f}-minute time limit and was stopped. "
                    "No complete answer was produced — re-run, or narrow the request."
                ),
                is_error=True,
                incomplete=True,
                files_written=tuple(written),
            )
        except asyncio.CancelledError:
            return AgentRunResult(text="Cancelled.", is_error=True, cancelled=True, files_written=tuple(written))
        finally:
            if job_id is not None:
                with self._active_lock:
                    self._active.pop(job_id, None)

    async def _query(
        self, prompt: str, cwd: str, resume_session_id: str | None, env: dict[str, str] | None = None,
        written: list[str] | None = None, on_event: Callable[[AgentEvent], None] | None = None,
    ) -> AgentRunResult:
        written = written if written is not None else []
        # Turn and cost ceilings (OPEN_ITEMS.md #33): a run that hits either ends with a non-success
        # subtype and is reported incomplete below, like the wall-clock timeout.
        options = ClaudeAgentOptions(
            max_turns=self._max_turns,
            max_budget_usd=self._max_budget_usd,
            cwd=cwd,
            permission_mode="bypassPermissions",
            setting_sources=["project"],
            resume=resume_session_id,
            model=self._model,
            disallowed_tools=_DELEGATION_TOOLS + sorted(DENIED_TOOLS),
            hooks={"PreToolUse": [HookMatcher(matcher=None, hooks=[_tool_scope_hook(cwd)])]},
            env={**_git_bash_env(), **(env or {})},
            max_buffer_size=MAX_STREAM_MESSAGE_BYTES,
        )

        final_text = ""
        is_error = False
        incomplete = False
        num_turns: int | None = None
        cost_usd: float | None = None
        session_id: str | None = None
        texts: list[str] = []
        # aclosing: leaving the loop early (no Bash, below) closes the stream in this task, which runs
        # the SDK's own cleanup and terminates the CLI subprocess.
        async with contextlib.aclosing(query(prompt=prompt, options=options)) as stream:
            async for message in stream:
                tools = _init_tools(message)
                if tools is not None and "Bash" not in tools:
                    # Every governed read is an app.cli call through Bash. Without it the agent can only
                    # guess (it marked every column `unchecked` on 2026-10-07), so stop before it starts.
                    logger.error("engagement started without a Bash tool; stopping it. tools=%s", sorted(tools))
                    return AgentRunResult(text=MISSING_BASH_MESSAGE, is_error=True, files_written=tuple(written))
                written.extend(_written_paths(message))
                texts.extend(_top_level_text(message))
                _emit(on_event, message)
                if isinstance(message, ResultMessage):
                    is_error = message.is_error
                    if is_error:
                        final_text = message.result or "\n".join(message.errors or [])
                    else:
                        final_text = _combined_text(texts, message.result or "")
                    session_id = message.session_id
                    # "success" is the CLI's normal completion; error_max_turns / error_during_execution
                    # / an API failure all arrive here too, and NOT reliably with is_error set — a run
                    # that stops at the turn limit can carry partial text and is_error=False.
                    incomplete = message.subtype != "success"
                    num_turns, cost_usd = message.num_turns, message.total_cost_usd

        if not final_text:
            return AgentRunResult(text="Scotri produced no output for this run.", is_error=True,
                                  session_id=session_id, files_written=tuple(written))
        return AgentRunResult(
            text=final_text, is_error=is_error, session_id=session_id, incomplete=incomplete,
            num_turns=num_turns, cost_usd=cost_usd, files_written=tuple(written),
        )
