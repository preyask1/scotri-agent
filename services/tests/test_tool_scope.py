# Copyright (c) 2026 Preyas Kulshrestha. Part of the Scotri engine; MIT License (see LICENSE).
# Keep this notice in copies and derived works, including AI-assisted ones.
"""OPEN_ITEMS.md #28 and #60(f): what an engagement's own tool calls may touch."""
from __future__ import annotations

import asyncio
import os

import pytest

import adapters.agent_sdk.claude_agent_adapter as adapter
from domain.tool_scope import check_bash_command, check_tool_call, check_write_path


@pytest.fixture
def root(tmp_path):
    for d in ("scratch", "proposals", "investigations", "docs/ai", "services/.venv/bin",
              "services/mirror/mirror-active/adf/main"):
        (tmp_path / d).mkdir(parents=True)
    return str(tmp_path)


PY = "services/.venv/bin/python"


# --- file writes -------------------------------------------------------------------------------

@pytest.mark.parametrize("path", ["scratch/ACME-1-intake.md", "proposals/ACME-1/fix.patch",
                                  "investigations/x.md", "docs/ai/client/lineage-map.md", "scratch/uploads/j/a.txt"])
def test_writes_inside_the_allowlist_pass(root, path):
    assert check_write_path(path, root) is None
    assert check_write_path(os.path.join(root, path), root) is None


@pytest.mark.parametrize("path", [
    "services/mirror/mirror-active/adf/main/pipeline/p.json",  # the mirror
    "services/mirror/mirror-staging/x",
    "services/app/cli.py",                                     # Scotri's own code
    "services/.env",
    ".claude/skills/jira-write/SKILL.md",
    "agent-instructions.md",
    "docs/ai/client/lineage-graph.yaml",                              # only lineage-map.md is writable in docs/
    "scratch/../services/app/cli.py",                          # traversal out of scratch/
    "scratchpad/x.md",                                         # prefix, not the folder
    "/etc/passwd",
    "",
])
def test_writes_outside_the_allowlist_are_denied(root, path):
    assert check_write_path(path, root) is not None


def test_a_symlink_out_of_scratch_is_denied(root):
    try:
        os.symlink(os.path.join(root, "services"), os.path.join(root, "scratch", "link"),
                   target_is_directory=True)
    except OSError as e:  # Windows without Developer Mode/admin can't create symlinks (WinError 1314)
        pytest.skip(f"cannot create symlinks here: {e}")
    assert check_write_path("scratch/link/app/cli.py", root) is not None


# --- bash ----------------------------------------------------------------------------------------

def test_the_documented_invocation_passes(root):
    cmd = f'cd "{root}/services" && "{root}/{PY}" -m app.cli jira-get-issue --key ACME-1'
    assert check_bash_command(cmd, root) is None


@pytest.mark.parametrize("cmd", [
    f'cd services && {PY.replace("services/", "")} -m app.cli databricks-select --user u --catalog acme_prod '
    f'--statement "SELECT a; FROM t WHERE x = \'y\'"',  # ; inside quotes is data
    "cd services && .venv/bin/python -m app.cli dax-execute --user u --workspace w --model m "
    "--dax 'EVALUATE ROW(\"x\", [Sales $])'",  # $ inside single quotes is literal
    f"cd services; .venv/bin/python -m app.cli batch --file ../scratch/ACME-1-batch.json",
    f"cd services && .venv/bin/python -m app.cli mirror-grep --pattern 'a|b' --repo-key adf",
    f"cd services && \\\n .venv/bin/python -m app.cli lineage-coverage",
])
def test_app_cli_variants_pass(root, cmd):
    assert check_bash_command(cmd, root) is None


@pytest.mark.parametrize("cmd", [
    "redis-cli SET scotri:run:abc '{}'",                                     # #60(f): writing the store
    "cd services && .venv/bin/python -c 'import redis'",                    # arbitrary python
    "cd services && .venv/bin/python -m app.cli lineage-coverage | head",   # pipe
    "cd services && .venv/bin/python -m app.cli lineage-coverage > ../x",   # redirect
    "cd services && .venv/bin/python -m app.cli lineage-coverage & sleep 1",
    "cd services && .venv/bin/python -m app.cli lineage-coverage || rm -rf x",
    "cd services && .venv/bin/python -m app.cli $(redis-cli get k)",        # substitution
    "cd services && .venv/bin/python -m app.cli `id`",
    'cd services && .venv/bin/python -m app.cli retrieve --query "$HOME"',  # expansion inside double quotes
    "cd services && .venv/bin/python -m app.cli lineage-coverage\nredis-cli FLUSHALL",
    "cd services && .venv/bin/python -m app.cli lineage-coverage; rm x",
    "cd services && .venv/bin/python -m app.cli governor-reset --backend jira --confirmed",
    "cd services && .venv/bin/python -m app.cli sync-run-once",
    ".venv/bin/python -m app.cli lineage-coverage",                         # wrong directory
    "cd services && python -m app.cli lineage-coverage",                    # PATH python
    "cd scratch && ../services/.venv/bin/python -m app.cli lineage-coverage",
    "cd services && .venv/bin/python -m app.cli",
    "cd services",
    "ls scratch",
    "cd services && .venv/bin/python -m app.cli x 'unclosed",
    "",
])
def test_everything_else_is_denied(root, cmd):
    assert check_bash_command(cmd, root) is not None


def test_powershell_is_denied_and_other_tools_pass(root):
    assert check_tool_call("PowerShell", {"command": "Get-ChildItem"}, root) is not None
    assert check_tool_call("Read", {"file_path": f"{root}/docs/ai/repo-registry.md"}, root) is None
    assert check_tool_call("Write", {"file_path": "services/app/cli.py"}, root) is not None
    assert check_tool_call("NotebookEdit", {"notebook_path": "scratch/n.ipynb"}, root) is None


# --- the SDK hook ---------------------------------------------------------------------------------

def _call(hook, tool, tool_input):
    return asyncio.run(hook({"hook_event_name": "PreToolUse", "tool_name": tool, "tool_input": tool_input},
                            "id", None))


def test_hook_denies_with_a_reason_and_allows_silently(root):
    hook = adapter._tool_scope_hook(root)
    denied = _call(hook, "Write", {"file_path": "services/mirror/mirror-active/adf/main/x.json"})
    out = denied["hookSpecificOutput"]
    assert out["permissionDecision"] == "deny" and "#28" in out["permissionDecisionReason"]
    assert _call(hook, "Write", {"file_path": "scratch/a.md"}) == {}


def test_query_options_carry_the_hook_and_deny_powershell(monkeypatch, root):
    seen = {}

    async def fake_query(prompt, options):
        seen["options"] = options
        if False:
            yield None

    monkeypatch.setattr(adapter, "query", fake_query)
    asyncio.run(adapter.ClaudeAgentSdkRuntime()._query("hi", root, None))
    opts = seen["options"]
    assert "PowerShell" in opts.disallowed_tools and "Agent" in opts.disallowed_tools
    [matcher] = opts.hooks["PreToolUse"]
    assert matcher.matcher is None and len(matcher.hooks) == 1


# --- file reads (secrets, other callers' data, unredacted mirror) --------------------------------

@pytest.mark.parametrize("tool,tool_input", [
    ("Read", {"file_path": "services/.env"}),
    ("Read", {"file_path": "services/.env.bak-20261006-190107"}),
    ("Read", {"file_path": "services/FRONTEND_AUTH.md"}),
    ("Read", {"file_path": "services/mirror/mirror-active/adf/main/pipeline/p.json"}),
    ("Read", {"file_path": "services/data/history.db"}),
    ("Read", {"file_path": "services/logs/scotri-cli.log"}),
    ("Grep", {"pattern": "AccountKey", "path": "services/mirror/mirror-active"}),
    ("Glob", {"pattern": "services/mirror/**/*.tmdl"}),
    ("Glob", {"pattern": "mirror-active/**/*.json", "path": "services/mirror"}),
    ("Glob", {"pattern": "**/*.json", "path": "services/mirror"}),
])
def test_reads_of_secrets_and_the_raw_mirror_are_denied(root, tool, tool_input):
    assert check_tool_call(tool, tool_input, root) is not None


@pytest.mark.parametrize("tool,tool_input", [
    ("Read", {"file_path": "services/.env.example"}),
    ("Read", {"file_path": "scratch/uploads/j/brd.pdf"}),
    ("Read", {"file_path": "docs/ai/repo-registry.md"}),
    ("Grep", {"pattern": "gold"}),                      # no path: the workspace, gitignore applies
    ("Grep", {"pattern": "gold", "path": "docs/ai"}),
    ("Glob", {"pattern": "**/*.md"}),
    ("Glob", {"pattern": "*.md", "path": "scratch"}),
])
def test_ordinary_reads_pass(root, tool, tool_input):
    assert check_tool_call(tool, tool_input, root) is None
