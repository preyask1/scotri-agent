# Copyright (c) 2026 Preyas Kulshrestha. Part of the Scotri engine; MIT License (see LICENSE).
# Keep this notice in copies and derived works, including AI-assisted ones.
"""What an engagement's own tool calls may touch (OPEN_ITEMS.md #28, and #60(f)).

Engagements run with permission_mode="bypassPermissions" (nobody is there to approve a prompt), so
until this existed the hard rules on writes were prose only: nothing structurally stopped a Write into
services/mirror/, a product path, or this workspace's own code, and an unrestricted Bash could reach
the shared store and plant its own run-identity entry (domain/run_context.py's "honest limit"). The
SDK adapter calls `check_tool_call` from a PreToolUse hook, which runs in every permission mode, and
denies with the returned reason. Pure: no I/O beyond resolving paths, so it's tested directly.

The rules are allowlists, so a path or command nobody thought about is refused, not admitted:

- File-writing tools (Write/Edit/MultiEdit/NotebookEdit) may write only under scratch/, proposals/
  and investigations/, plus docs/ai/client/lineage-map.md, the one doc a skill (lineage-trace) is told to
  keep up to date. Paths are resolved, symlinks included, before the check.
- Bash may run only `cd <dir>` and `<services venv python> -m app.cli <command> ...`, joined by `&&`
  or `;`. No pipes, redirects, substitution (`$`/backtick outside single quotes), background jobs or
  other programs. Every governed read, write and mirror search already goes through app.cli, which
  is what the skills document; the agent reads files with its Read/Grep/Glob tools. The ops-only
  commands (OPS_ONLY_COMMANDS) are refused here as well as by `batch`.
- PowerShell (the Windows shell tool) is refused outright: it would sidestep the Bash rules.
- File-reading tools (Read/Grep/Glob/NotebookRead) may not target the places that hold secrets or
  bypass redaction (READ_DENIED_PATHS, plus any `.env*` file but `.env.example`): the mirror is read
  through `mirror-read`/`mirror-grep`, which redact; `services/.env` holds every credential; the
  history DB and logs hold other callers' work. Grep/Glob with no path search the workspace, where
  ripgrep already skips these gitignored folders.
"""
from __future__ import annotations

import os
import shlex
from pathlib import Path

FILE_WRITE_TOOLS = {"Write": "file_path", "Edit": "file_path", "MultiEdit": "file_path",
                    "NotebookEdit": "notebook_path"}
WRITABLE_DIRS = ("scratch", "proposals", "investigations")
WRITABLE_FILES = ("docs/ai/client/lineage-map.md",)
OPS_ONLY_COMMANDS = {"governor-reset", "sync-run-once", "artifacts-purge", "lineage-generate"}
# SendMessage/ListAgents hand work to another session and end the turn; the turn ending is the
# engagement ending, so the handed-off work dies unstarted (OPEN_ITEMS.md #73).
DENIED_TOOLS = {"PowerShell", "SendMessage", "ListAgents"}
FILE_READ_TOOLS = {"Read": ("file_path",), "NotebookRead": ("notebook_path",),
                   "Grep": ("path",), "Glob": ("path", "pattern")}
READ_DENIED_PATHS = ("services/mirror", "services/data", "services/logs", "services/FRONTEND_AUTH.md")
_SEPARATORS = {"&&", ";"}
_INTERPRETERS = (".venv/bin/python", ".venv/bin/python3", ".venv/Scripts/python.exe")


def _norm(p: str | Path) -> str:
    return os.path.normcase(os.path.realpath(os.path.expanduser(str(p))))


def _git_bash_to_windows(p: str) -> str:
    """`/c/Users/...` (Git Bash) -> `C:/Users/...`, only on Windows."""
    if os.name == "nt" and len(p) >= 3 and p[0] == "/" and p[1].isalpha() and p[2] == "/":
        return f"{p[1].upper()}:{p[2:]}"
    return p


def _resolve(p: str, base: str) -> str:
    p = _git_bash_to_windows(p)
    path = Path(os.path.expanduser(p))
    return _norm(path if path.is_absolute() else Path(base) / path)


def _under(path: str, folder: str) -> bool:
    return path == folder or path.startswith(folder.rstrip(os.sep) + os.sep)


def check_write_path(file_path: str, root: str) -> str | None:
    """None if `file_path` may be written in an engagement rooted at `root`, else the reason."""
    if not isinstance(file_path, str) or not file_path:
        return "no file path given"
    target = _resolve(file_path, root)
    if any(_under(target, _norm(Path(root) / d)) for d in WRITABLE_DIRS):
        return None
    if any(target == _norm(Path(root) / f) for f in WRITABLE_FILES):
        return None
    return (f"writes are limited to {', '.join(d + '/' for d in WRITABLE_DIRS)} and "
            f"{', '.join(WRITABLE_FILES)} in this workspace (OPEN_ITEMS.md #28); "
            f"{file_path!r} is outside them")


def _is_env_file(path: str) -> bool:
    name = os.path.basename(path)
    return (name == ".env" or name.startswith(".env.")) and not name.endswith(".example")


def check_read_path(file_path: str, root: str) -> str | None:
    """None if a read tool may look at `file_path` in an engagement rooted at `root`, else the reason.
    A glob pattern is checked by its fixed leading part (`services/mirror/**/*.tmdl` -> services/mirror)."""
    if not isinstance(file_path, str) or not file_path:
        return None
    fixed = file_path.split("*", 1)[0].split("?", 1)[0].split("[", 1)[0].split("{", 1)[0]
    if not fixed.strip():
        return None
    target = _resolve(fixed, root)
    if _is_env_file(target) or any(_under(target, _norm(Path(root) / d)) for d in READ_DENIED_PATHS):
        return (f"{file_path!r} holds credentials, other callers' data or unredacted mirror content; "
                f"read the mirror with `mirror-read`/`mirror-grep` and past work with `retrieve`")
    return None


def _scan(command: str) -> tuple[str | None, str]:
    """Refuse `$` and backticks outside single quotes; turn unquoted newlines into `;` and drop
    backslash-newline continuations, so the shlex split below sees what bash would run."""
    out, quote, i = [], None, 0
    while i < len(command):
        c = command[i]
        if quote == "'":
            quote = None if c == "'" else quote
        elif c in "$`":
            return f"{c!r} (substitution or expansion) is not allowed outside single quotes", ""
        elif quote == '"':
            if c == "\\" and i + 1 < len(command):
                out.append(c + command[i + 1])
                i += 2
                continue
            quote = None if c == '"' else quote
        elif c in "'\"":
            quote = c
        elif c == "\\" and command[i + 1:i + 2] == "\n":
            out.append(" ")
            i += 2
            continue
        elif c == "\\" and i + 1 < len(command):
            out.append(c + command[i + 1])
            i += 2
            continue
        elif c == "\n":
            c = ";"
        out.append(c)
        i += 1
    if quote:
        return "unbalanced quotes", ""
    return None, "".join(out)


def check_bash_command(command: str, root: str) -> str | None:
    """None if `command` is only `cd` and app.cli calls, else the reason it is refused."""
    if not isinstance(command, str) or not command.strip():
        return "empty command"
    problem, cleaned = _scan(command)
    if problem:
        return problem
    lex = shlex.shlex(cleaned, posix=True, punctuation_chars=True)
    lex.whitespace_split = True
    lex.commenters = ""
    try:
        tokens = list(lex)
    except ValueError as e:
        return f"could not parse the command ({e})"

    segments: list[list[str]] = [[]]
    for tok in tokens:
        if tok in _SEPARATORS:
            segments.append([])
        elif tok and set(tok) <= set("|&;<>()"):
            return f"shell operator {tok!r} is not allowed (only && and ; between commands)"
        else:
            segments[-1].append(tok)

    services = _norm(Path(root) / "services")
    interpreters = {_norm(Path(services) / p) for p in _INTERPRETERS}
    cwd = _norm(root)
    ran = False
    for seg in segments:
        if not seg:
            continue
        if seg[0] == "cd":
            if len(seg) != 2:
                return "cd takes exactly one directory"
            cwd = _resolve(seg[1], cwd)
            continue
        if _resolve(seg[0], cwd) not in interpreters:
            return (f"only `cd` and the services venv's `python -m app.cli ...` may run in an engagement "
                    f"(OPEN_ITEMS.md #28); {seg[0]!r} is not allowed. Read files with the Read/Grep/Glob "
                    f"tools and write them with Write")
        if seg[1:3] != ["-m", "app.cli"] or len(seg) < 4:
            return "the venv python may only run `-m app.cli <command>`"
        if cwd != services:
            return "run app.cli from the services/ directory (`cd .../services && ...`)"
        if seg[3] in OPS_ONLY_COMMANDS:
            return f"`{seg[3]}` is an ops-only command, never part of an engagement"
        ran = True
    return None if ran else "the command runs no app.cli command"


def check_tool_call(tool_name: str, tool_input: dict, root: str) -> str | None:
    """None to let the call through, else the reason to deny it."""
    if tool_name in DENIED_TOOLS:
        return f"{tool_name} is not available in an engagement; use the Bash app.cli commands"
    if tool_name in FILE_READ_TOOLS:
        values = [(tool_input or {}).get(f) for f in FILE_READ_TOOLS[tool_name]]
        if tool_name == "Glob" and isinstance(values[1], str) and not os.path.isabs(values[1]):
            values.append(os.path.join(values[0] if isinstance(values[0], str) and values[0] else root, values[1]))
        for value in values:
            reason = check_read_path(value, root)
            if reason:
                return reason
        return None
    if tool_name in FILE_WRITE_TOOLS:
        return check_write_path((tool_input or {}).get(FILE_WRITE_TOOLS[tool_name]), root)
    if tool_name == "Bash":
        return check_bash_command((tool_input or {}).get("command"), root)
    return None
