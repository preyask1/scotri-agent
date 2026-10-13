# Copyright (c) 2026 Preyas Kulshrestha. Part of the Scotri engine; MIT License (see LICENSE).
# Keep this notice in copies and derived works, including AI-assisted ones.
"""domain/retrieval.py — pure BM25 ranking + identifier-aware tokenization (OPEN_ITEMS.md item #38).

The property that matters for this workspace: a document matching no query term is DROPPED, never
returned with score 0 — so a caller can't mistake "some matches" for "everything" (the completeness
trap the design note warns about is a usage rule, but the ranker must not itself manufacture noise)."""
from __future__ import annotations

from domain.retrieval import Document, rank, tokenize


def _doc(did, text, scope="docs", title=""):
    return Document(id=did, scope=scope, title=title or did, text=text)


def test_tokenize_keeps_whole_identifier_and_parts():
    toks = tokenize("acme_prod.gold.visit_summary")
    assert "acme_prod.gold.visit_summary" in toks  # exact identifier preserved
    assert "visit" in toks and "summary" in toks and "gold" in toks  # split pieces too


def test_exact_identifier_ranks_top():
    docs = [
        _doc("a", "this is about the weather and nothing else"),
        _doc("b", "the gold table acme_prod.gold.visit_summary is produced by a notebook"),
    ]
    hits = rank("acme_prod.gold.visit_summary", docs, limit=10)
    assert hits[0].id == "b"


def test_non_matching_docs_are_dropped_not_zero_scored():
    docs = [_doc("a", "completely unrelated content about pipelines")]
    hits = rank("xyzzy_nonexistent_term", docs, limit=10)
    assert hits == []


def test_limit_respected():
    docs = [_doc(str(i), "sales report sales sales") for i in range(20)]
    hits = rank("sales", docs, limit=5)
    assert len(hits) == 5


def test_empty_query_returns_nothing():
    assert rank("   ", [_doc("a", "anything")], limit=10) == []


def test_empty_corpus_returns_nothing():
    assert rank("sales", [], limit=10) == []


def test_snippet_is_around_the_match():
    long_text = ("intro padding " * 40) + " the Visit_summary marker here " + ("tail padding " * 40)
    hits = rank("visit_summary", [_doc("a", long_text)], limit=1)
    assert "visit_summary" in hits[0].snippet.lower()


def test_scope_is_carried_through():
    hits = rank("sales", [_doc("a", "sales sales", scope="past-engagements")], limit=1)
    assert hits[0].scope == "past-engagements"


# --- markdown sectioning (docs are indexed one heading section at a time) -------------------------

from domain.retrieval import split_markdown  # noqa: E402


def test_split_on_h1_h2_with_breadcrumb_and_lines():
    md = "intro line\n# Title\nabout\n## KPIs\nkpi table\n### Detail\nsmall\n## RLS\nroles\n"
    secs = split_markdown(md)
    assert [s.heading for s in secs] == ["", "Title", "Title › KPIs", "Title › RLS"]
    kpis = secs[2]
    assert (kpis.start_line, kpis.end_line) == (4, 7)  # small ### stays inside its ## section
    assert "### Detail" in kpis.text


def test_oversized_section_resplits_on_h3():
    md = "## Big\nlead\n### A\n" + "x " * 5000 + "\n### B\nb\n"
    secs = split_markdown(md)
    assert [s.heading for s in secs] == ["Big", "Big › A", "Big › B"]


def test_headings_in_code_fences_are_ignored():
    md = "## Real\n```\n# not a heading\n```\nmore\n"
    assert [s.heading for s in split_markdown(md)] == ["Real"]


def test_no_headings_is_one_section():
    secs = split_markdown("just text\nmore text\n")
    assert len(secs) == 1 and secs[0].heading == "" and (secs[0].start_line, secs[0].end_line) == (1, 2)


def test_docs_hit_points_at_section(tmp_path):
    from adapters.retrieval.lexical_retrieval import LexicalRetriever
    from adapters.history.sqlite_engagement_history import SqliteEngagementHistory
    docs = tmp_path / "docs"
    (docs / "reports").mkdir(parents=True)
    (docs / "reports" / "Visit-Status.md").write_text(
        "# Visit-Status\n## Overview\nmonitors submissions\n## KPIs\nPending Visit Days uses NETWORKDAYS\n",
        encoding="utf-8")
    r = LexicalRetriever(SqliteEngagementHistory(tmp_path / "h.db"), docs)
    top = r.search("NETWORKDAYS", ["docs"], None, 5)[0]
    assert top.id == "docs:reports/Visit-Status.md#L4"
    assert top.meta["lines"] == "4-5" and top.meta["section"] == "Visit-Status › KPIs"
