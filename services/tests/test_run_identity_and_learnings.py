"""Run identity (the caller travels with the run, not as a guessed --user) and shared learnings
(knowledge from one person's or service's investigation is reusable by the next caller)."""
from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone

import pytest

from adapters.history.sqlite_engagement_history import SqliteEngagementHistory
from adapters.redis.run_context_adapter import RedisRunContextStore
from adapters.retrieval.lexical_retrieval import LexicalRetriever
from app import cli
from domain.engagement import EngagementStatus
from domain.engagement_policy import run_engagement, start_engagement
from domain.errors import PolicyError
from domain.learning import extract_learning
from domain.models import Principal
from domain.run_context import RUN_TOKEN_ENV, RunContext, resolve_user, token_digest
from domain.verdict_review import ReviewOutcome, VerdictReview
from ports.agent_runtime import AgentRunResult
from tests.test_engagement_cancel import FakeJobStore, FakeLog, FakeRuntime

BOB = Principal(subject="bob-oid", display_name="Bob", platform="web", roles=[])
WEBHOOK = Principal(subject="svc-jira-webhook", display_name="", platform="jira-webhook", roles=[])
SIA = "sia-oid"

LEARNING_BLOCK = """Final answer text for the user.

```scotri-learning
{"title": "Brand New Brand report download fails", "ticket_key": "ACME-2096", "verdict": "confirmed_bug",
 "symptom": "Export to Excel times out on the Brand New Brand report",
 "checked": ["report.json visual export settings", "acme_prod.gold.brand_sales row count"],
 "root_cause_category": "report-visual", "root_cause": "table visual exceeds the export row cap",
 "confidence": "high",
 "takeaways": ["Download failures on this report come from the export row cap, not the gold table"],
 "entities": ["Brand New Brand", "acme_prod.gold.brand_sales"]}
```
"""


class FakeRedis:
    def __init__(self):
        self.data: dict[str, str] = {}

    def set(self, key, value, ex=None):
        self.data[key] = value

    def get(self, key):
        return self.data.get(key)

    def delete(self, key):
        self.data.pop(key, None)


# --- run identity ----------------------------------------------------------------------------------

def test_store_keeps_only_a_hash_of_the_token():
    r = FakeRedis()
    store = RedisRunContextStore(r)
    token = store.issue(RunContext.for_principal(BOB, "j1"), 60)
    assert token not in "".join(r.data)  # the raw token appears in no key
    assert list(r.data) == ["runctx:" + token_digest(token)]
    assert store.resolve(token).subject == "bob-oid"
    store.revoke(token)
    assert store.resolve(token) is None


def test_resolve_user_ignores_the_claimed_user_when_a_token_is_present():
    ctx = RunContext.for_principal(BOB, "j1")
    assert resolve_user("tok", "alice-oid", lambda t: ctx) == ("bob-oid", ctx)
    assert resolve_user(None, "alice-oid", lambda t: ctx) == ("alice-oid", None)


def test_an_unknown_token_is_refused_not_ignored():
    with pytest.raises(PolicyError):
        resolve_user("stale", "alice-oid", lambda t: None)


def test_cli_replaces_user_from_the_run_token(monkeypatch):
    store = RedisRunContextStore(FakeRedis())
    token = store.issue(RunContext.for_principal(BOB, "j1"), 60)
    monkeypatch.setenv(RUN_TOKEN_ENV, token)
    container = argparse.Namespace(run_context_store=store)
    args = argparse.Namespace(user="Scotri:ACME-2096")
    cli._apply_run_identity(args, container)
    assert args.user == "bob-oid"
    untouched = argparse.Namespace(path="x")  # a command with no --user never needs the store
    cli._apply_run_identity(untouched, None)


def test_engine_issues_passes_and_revokes_the_token(tmp_path):
    store, log, runtime = FakeJobStore(), FakeLog(), FakeRuntime()
    ctx_store = RedisRunContextStore(FakeRedis())
    job_id = start_engagement(store, BOB, "investigate")
    seen = {}

    def on_run():
        token = runtime.run_calls[-1]["env"][RUN_TOKEN_ENV]
        seen["ctx"] = ctx_store.resolve(token)
        seen["token"] = token

    runtime.on_run = on_run
    run_engagement(store, runtime, log, job_id, "/ws", run_context_store=ctx_store)
    assert seen["ctx"].subject == "bob-oid" and seen["ctx"].job_id == job_id
    assert ctx_store.resolve(seen["token"]) is None  # revoked once the run ended
    assert "You are working for Bob" in runtime.run_calls[-1]["prompt"]


# --- learnings: extraction -------------------------------------------------------------------------

def test_extraction_strips_the_block_and_validates_it():
    ex = extract_learning(LEARNING_BLOCK)
    assert ex.problem is None
    assert "scotri-learning" not in ex.cleaned_text
    assert ex.cleaned_text.startswith("Final answer text")
    assert ex.fields["verdict"] == "confirmed_bug"
    assert ex.fields["entities"] == ["Brand New Brand", "acme_prod.gold.brand_sales"]


@pytest.mark.parametrize("body", [
    '{"title": "x", "verdict": "maybe", "takeaways": ["t"]}',
    '{"title": "x", "verdict": "not_a_bug", "takeaways": []}',
    '{"verdict": "not_a_bug", "takeaways": ["t"]}',
    'not json',
])
def test_invalid_blocks_record_nothing_but_are_still_stripped(body):
    ex = extract_learning(f"answer\n\n```scotri-learning\n{body}\n```\n")
    assert ex.fields is None and ex.problem
    assert ex.cleaned_text == "answer"


def test_no_block_means_no_learning_and_text_unchanged():
    ex = extract_learning("just an answer")
    assert (ex.fields, ex.cleaned_text) == (None, "just an answer")


def test_secrets_in_a_learning_are_redacted():
    body = ('{"title": "conn", "verdict": "not_a_bug", "takeaways": ["uses '
            'Server=x;Password=hunter2secret;"]}')
    ex = extract_learning(f"a\n```scotri-learning\n{body}\n```")
    assert "hunter2secret" not in ex.fields["takeaways"][0]


# --- learnings: engine + team-wide retrieval -------------------------------------------------------

def _run(history, principal, text, status_ok=True):
    store, log = FakeJobStore(), FakeLog()
    runtime = FakeRuntime(result=AgentRunResult(text=text, is_error=not status_ok, session_id="s"))
    job_id = start_engagement(store, principal, "investigate the download issue")
    run_engagement(store, runtime, log, job_id, "/ws", engagement_history=history)
    return job_id, store.get(job_id)


def test_a_webhook_run_learning_is_found_by_another_user(tmp_path):
    history = SqliteEngagementHistory(tmp_path / "h.db")
    job_id, final = _run(history, WEBHOOK, LEARNING_BLOCK)
    assert final.status == EngagementStatus.SUCCEEDED
    assert "scotri-learning" not in final.result  # the user never sees the machine block

    retriever = LexicalRetriever(history, tmp_path / "no-docs")
    hits = retriever.search("Brand New Brand download", ["learnings"], subject=SIA, limit=5)
    assert [h.meta["job_id"] for h in hits] == [job_id]
    assert hits[0].meta["verdict"] == "confirmed_bug"
    assert hits[0].meta["review_outcome"] == "unreviewed"
    # Sia sees the learning, never the webhook run's full result.
    assert retriever.search("Brand", ["past-engagements"], subject=SIA, limit=5) == []


def test_failed_runs_record_no_learning(tmp_path):
    history = SqliteEngagementHistory(tmp_path / "h.db")
    _run(history, BOB, LEARNING_BLOCK, status_ok=False)
    assert history.list_learnings(10) == []


def test_an_overturned_learning_stops_being_served_to_the_team(tmp_path):
    history = SqliteEngagementHistory(tmp_path / "h.db")
    job_id, _ = _run(history, BOB, LEARNING_BLOCK)
    history.add_review(VerdictReview(job_id=job_id, outcome=ReviewOutcome.OVERTURNED,
                                     reviewer_subject="lead", reviewed_at=datetime.now(timezone.utc),
                                     reason="row cap was not the cause"))
    retriever = LexicalRetriever(history, tmp_path / "no-docs")
    assert retriever.search("Brand New Brand", ["learnings"], subject=SIA, limit=5) == []
    assert len(retriever.search("Brand New Brand", ["learnings"], subject=SIA, limit=5,
                                include_overturned=True)) == 1


# --- figures never travel in a learning (OPEN_ITEMS.md #60(b)) -----------------------------------


def test_data_figures_are_scrubbed_but_locators_and_names_survive():
    block = (
        '{"title": "Duplicate rows: 41,208 rep rows doubled", "verdict": "confirmed_bug",'
        ' "symptom": "SUM(net_sale) 31415926.25 on both sides; ~17.5% orphans",'
        ' "checked": ["databricks-select: manager/DivisionA 6312, rep/DivisionA 41208",'
        ' "Store_Visit.ipynb cell 27 line 1609; VERSION AS OF 760/761; HTTP 403; since 2026-09-01"],'
        ' "root_cause": "time travel is limited to 168 hours", "confidence": "medium",'
        ' "takeaways": ["dim_org_hierarchy has 18,404 rows for 17,092 keys: dedupe it"],'
        ' "entities": ["acme_prod.gold.store_visit_report", "Wave3"]}')
    ex = extract_learning(f"a\n```scotri-learning\n{block}\n```")
    f = ex.fields
    text = json.dumps(f)
    for leaked in ("41,208", "31415926", "17.5", "6312", "41208", "18,404", "17,092"):
        assert leaked not in text
    assert f["checked"][1] == "Store_Visit.ipynb cell 27 line 1609; VERSION AS OF 760/761; HTTP 403; since 2026-09-01"
    assert f["root_cause"] == "time travel is limited to 168 hours"
    assert f["takeaways"] == ["dim_org_hierarchy has <n> rows for <n> keys: dedupe it"]
    assert f["entities"] == ["acme_prod.gold.store_visit_report", "Wave3"]
