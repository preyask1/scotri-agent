"""domain/engagement_prefetch.py and its hook in run_engagement. Pins: ticket keys extracted (not
UTF-8 & co.), the ticket fetched through the governor with the CLI's cache key, a presence collision
reported instead of overwritten, a failing step reported as "not fetched" rather than failing the
block, scope matched from the prompt and ticket, and run_engagement passing the block to the agent
while storing the caller's prompt unchanged."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from domain.engagement import EngagementStatus
from domain.engagement_policy import run_engagement, start_engagement
from domain.engagement_prefetch import (
    PrefetchDeps,
    ScopeItem,
    adf_to_text,
    build_prefetch_context,
    extract_ticket_keys,
    scope_lines,
)
from domain.models import Claim, Environment, MaintenanceStatus, Principal, RepoRef
from ports.agent_runtime import AgentRunResult

NOW = datetime(2026, 10, 1, 9, 0, tzinfo=timezone.utc)

ISSUE = {
    "key": "ACME-2085",
    "fields": {
        "summary": "Blank JOINTWORK_WITHREP in Region 7",
        "issuetype": {"name": "Story"}, "status": {"name": "In Dev"}, "priority": {"name": "Medium"},
        "labels": [],
        "description": {"type": "doc", "content": [
            {"type": "paragraph", "content": [{"type": "text", "text": "The Visit-Status report shows blanks."}]}]},
        "comment": {"comments": [{"id": "1", "author": {"displayName": "Ana"}, "created": "2026-09-28",
                                  "body": {"type": "doc", "content": [{"type": "paragraph", "content": [
                                      {"type": "text", "text": "token password=hunter2"}]}]}}]},
        "attachment": [],
    },
}


class _Reader:
    def __init__(self, fail=False):
        self.calls = 0
        self.fail = fail

    def get_issue(self, key):
        self.calls += 1
        if self.fail:
            raise RuntimeError("jira down")
        return {**ISSUE, "key": key}


class _Presence:
    def __init__(self, claim=None):
        self.claim = claim
        self.set_calls = []

    def get_claim(self, entity):
        return self.claim

    def set_claim(self, entity, owner):
        self.set_calls.append((entity, owner))


class _Limiter:
    def enter_concurrency_slot(self, backend):
        return 1

    def exit_concurrency_slot(self, backend):
        pass

    def increment_and_get_count(self, user, window_seconds):
        return 1


class _Cache:
    def __init__(self):
        self.store = {}

    def get(self, key):
        return self.store.get(key)

    def set(self, key, value, ttl_seconds=None):
        self.store[key] = value


class _Flag:
    def status(self, repo):
        return MaintenanceStatus(active=False)


class _Registry:
    def all_repo_keys(self):
        return ["fabric_secondary", "fabric_hq"]

    def get(self, key):
        if key == "fabric_hq":
            return [RepoRef("ACME-PBI-HO", "o", "p", "main", Environment.PROD, mirrored=True, status="planned_not_live")]
        return [RepoRef("FAB", "o", "p", "regional_main", Environment.PROD, mirrored=True)]


def _deps(tmp_path, reader=None, presence=None, files=None):
    return PrefetchDeps(
        ticket_reader=lambda: reader or _Reader(),
        presence_store=lambda: presence or _Presence(),
        rate_limiter=lambda: _Limiter(),
        cache=lambda: _Cache(),
        maintenance_flag=lambda: _Flag(),
        registry=_Registry(),
        mirror_status=lambda: None,
        scratch_dir=tmp_path / "scratch",
        stale_warn_seconds=25 * 3600,
        list_tree_files=lambda repo, branch: files if files is not None else [
            "Insights/Field/Visit-Status.SemanticModel/definition/tables/visit_summary.tmdl",
            "Insights/Field/Visit-Status.Report/report.json",
        ],
    )


def test_extract_ticket_keys():
    assert extract_ticket_keys("see https://x.atlassian.net/browse/ACME-2063 and ACME-2085, ACME-2063 again") == [
        "ACME-2063", "ACME-2085"]
    assert extract_ticket_keys("UTF-8 and SHA-256 aren't tickets") == []
    assert extract_ticket_keys("ACME-1 SUP-2", frozenset({"SUP"})) == ["SUP-2"]
    assert extract_ticket_keys("ACME-1 ACME-2 ACME-3 ACME-4") == ["ACME-1", "ACME-2", "ACME-3"]


def test_adf_to_text_keeps_words_and_structure():
    doc = {"type": "doc", "content": [
        {"type": "heading", "attrs": {"level": 2}, "content": [{"type": "text", "text": "Overview"}]},
        {"type": "bulletList", "content": [{"type": "listItem", "content": [
            {"type": "paragraph", "content": [{"type": "text", "text": "one", "marks": [{"type": "strong"}]}]}]}]},
    ]}
    text = adf_to_text(doc)
    assert "## Overview" in text and "- one" in text


def test_ticket_block_writes_redacted_ticket_and_registers_claim(tmp_path):
    presence = _Presence()
    block = build_prefetch_context(_deps(tmp_path, presence=presence), "investigate ACME-2085", "alice", NOW)
    assert block.startswith("<scotri-prefetch>") and block.endswith("</scotri-prefetch>")
    assert "ACME-2085 — Story · In Dev" in block
    ticket = (tmp_path / "scratch" / "ACME-2085-ticket.md").read_text(encoding="utf-8")
    assert "The Visit-Status report shows blanks." in ticket and "hunter2" not in ticket
    assert presence.set_calls == [("ACME-2085", "alice")]
    assert 'owner "alice"' in block


def test_presence_collision_is_reported_not_overwritten(tmp_path):
    other = Claim("ACME-2085", "bob", NOW - timedelta(minutes=5), NOW - timedelta(minutes=1))
    presence = _Presence(claim=other)
    block = build_prefetch_context(_deps(tmp_path, presence=presence), "ACME-2085", "alice", NOW)
    assert presence.set_calls == []
    assert "NOT registered" in block and "bob" in block and "--force" in block


def test_failed_jira_step_is_reported_and_the_rest_still_runs(tmp_path):
    block = build_prefetch_context(_deps(tmp_path, reader=_Reader(fail=True)), "ACME-2085", "alice", NOW)
    assert "jira-get-issue ACME-2085: not fetched — RuntimeError: jira down; run it yourself" in block
    assert "presence: claim registered" in block and "Mirror: no sync in progress." in block


def test_scope_matches_report_from_ticket_and_ignores_status_and_planned_trees(tmp_path):
    block = build_prefetch_context(_deps(tmp_path), "ACME-2085", "alice", NOW)
    assert 'matches report/model "Visit-Status" → --repo-key fabric_secondary' in block
    # "In Dev" is the ticket's status, not a statement about the environment
    assert "environment: not named" in block
    assert "this is Prod; say so" in block
    assert "ACME-PBI-HO" not in block  # planned_not_live trees are neither listed nor searched


def test_free_form_prompt_gets_scope_only_when_something_matches(tmp_path):
    assert build_prefetch_context(_deps(tmp_path), "say hello", "alice", NOW) == ""
    block = build_prefetch_context(_deps(tmp_path), "why is Visit-Status blank in uat?", "alice", NOW)
    assert "environment named: UAT" in block and "Ticket" not in block


def test_scope_says_to_ask_when_nothing_narrows_it():
    lines = "\n".join(scope_lines("numbers look wrong", [ScopeItem("report/model", "Visit-Status", "k", "t", "p")]))
    assert "environment: not named" in lines and "ask for the environment and the report or dashboard name" in lines


class _JobStore:
    def __init__(self):
        self.items = {}

    def create(self, e):
        self.items[e.job_id] = e

    def get(self, job_id):
        return self.items.get(job_id)

    def update(self, e):
        self.items[e.job_id] = e


class _Runtime:
    def __init__(self):
        self.prompts = []

    def run(self, prompt, cwd, resume_session_id=None, job_id=None, env=None):
        self.prompts.append(prompt)
        return AgentRunResult(text="done", session_id="s", is_error=False)


class _Log:
    def record(self, entry):
        pass


@pytest.mark.parametrize("prefetch,expected_prefix", [
    (lambda e: "<scotri-prefetch>X</scotri-prefetch>", "<scotri-prefetch>X</scotri-prefetch>\n\nACME-2085"),
    (lambda e: "", "ACME-2085"),
])
def test_run_engagement_prepends_block_but_stores_raw_prompt(prefetch, expected_prefix):
    store, runtime = _JobStore(), _Runtime()
    job_id = start_engagement(store, Principal("u", "U", "web"), "ACME-2085")
    run_engagement(store, runtime, _Log(), job_id, "C:/ws", prefetch=prefetch)
    assert runtime.prompts == [expected_prefix]
    assert store.get(job_id).prompt == "ACME-2085"
    assert store.get(job_id).status == EngagementStatus.SUCCEEDED


def test_run_engagement_survives_a_crashing_prefetch():
    store, runtime = _JobStore(), _Runtime()
    job_id = start_engagement(store, Principal("u", "U", "web"), "ACME-2085")

    def boom(engagement):
        raise RuntimeError("redis down")

    run_engagement(store, runtime, _Log(), job_id, "C:/ws", prefetch=boom)
    assert runtime.prompts == ["ACME-2085"] and store.get(job_id).status == EngagementStatus.SUCCEEDED
