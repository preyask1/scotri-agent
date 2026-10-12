"""docs/ai/command-reference.md is loaded into every session so the agent never runs --help. That
only helps while it's accurate: this fails when a CLI command or flag is added, renamed or removed
without updating the page."""
from __future__ import annotations

import argparse
from pathlib import Path

import app.cli as cli

REFERENCE = Path(__file__).resolve().parents[2] / "docs" / "ai" / "command-reference.md"


def _subcommands():
    parser = cli.build_parser()
    action = next(a for a in parser._actions if isinstance(a, argparse._SubParsersAction))
    return action.choices


def test_every_command_and_flag_is_documented():
    text = REFERENCE.read_text(encoding="utf-8")
    missing = []
    for name, sub in _subcommands().items():
        line = next((ln for ln in text.splitlines() if ln.startswith(f"- `{name}")), None)
        if line is None:
            missing.append(name)
            continue
        for action in sub._actions:
            flags = [o for o in action.option_strings if o.startswith("--") and o != "--help"]
            if flags and flags[-1] not in line:
                missing.append(f"{name} {flags[-1]}")
    assert not missing, f"docs/ai/command-reference.md is missing: {missing}"


def test_claude_md_imports_the_reference():
    claude_md = (REFERENCE.parents[2] / "CLAUDE.md").read_text(encoding="utf-8")
    assert "@docs/ai/command-reference.md" in claude_md
