# Copyright (c) 2026 Preyas Kulshrestha. Part of the Scotri engine; MIT License (see LICENSE).
# Keep this notice in copies and derived works, including AI-assisted ones.
"""domain/kpi_docs.py — KPI definitions from the report docs attached to lineage nodes, and the
index queries kpi-lookup uses (find_nodes, notes_for)."""
from __future__ import annotations

from adapters.lineage.sqlite_lineage_graph import SqliteLineageGraph
from domain.kpi_docs import attach, kpi_rows, model_path
from domain.lineage import LineageNode, NodeNote

TREE = "fabric_secondary@regional_main"
BASE = f"{TREE}/Field/Work Habit/Visit-Status"

DOC = """# Power BI Report Documentation
**Semantic model:** `Field\\Work Habit\\Visit-Status.SemanticModel`

## 1. Report Overview

| KPI | Definition | Source | Notes |
|---|---|---|---|
| Not section 2 | ignored | `Pending Visit Days` | |

## 2. Business Requirements & KPI Definitions (BRD)

| Business Question | Answered By |
|---|---|
| How late are visit reports? | `Pending Visit Days` |

| KPI | Definition | Source Column / Measure | Notes |
|-----|-----------|------------------------|-------|
| Avg Visit Submission Days | Average days from Visit date to submission | `Avg Visit Submission Days` — `AVERAGEX` of `DATEDIFF([VISITDATE], [SUBMITDATETIME], DAY)` | Promptness |
| Morning / Evening Visits | Visits by time of day | `MORNING_VISITS`, `EVENING_VISITS` | |
| Pending Visit Days | Working days since the last Visit | `NETWORKDAYS(...)` | Core metric |
| Dynamic Title | A report measure | `Dynamic Title` | |

## 3. Pages

| KPI | Definition | Source |
|---|---|---|
| Also ignored | x | `MORNING_VISITS` |
"""

NODES = {
    f"fabric_model:{BASE}",
    f"measure:{BASE}::[Avg Visit Submission Days]",
    f"measure:{BASE}::[Pending Visit Days]",
    f"column:{BASE}::visit_summary[VISITDATE]",
    f"column:{BASE}::visit_summary[MORNING_VISITS]",
    f"column:{BASE}::visit_summary[EVENING_VISITS]",
}


def test_model_path_and_rows_come_from_section_two_definition_tables_only():
    assert model_path(DOC) == "Field/Work Habit/Visit-Status"
    rows = kpi_rows(DOC)
    assert [r.name for r in rows] == ["Avg Visit Submission Days", "Morning / Evening Visits",
                                      "Pending Visit Days", "Dynamic Title"]
    assert rows[0].notes == "Promptness" and rows[0].line == 18


def test_attach_prefers_measures_and_never_pins_formula_columns():
    r = attach("docs/x.md", DOC, TREE, NODES)
    by_node = {n.node_id: n for n in r.notes}
    assert set(by_node) == {
        f"measure:{BASE}::[Avg Visit Submission Days]",   # whole token, not VISITDATE from the formula
        f"column:{BASE}::visit_summary[MORNING_VISITS]", f"column:{BASE}::visit_summary[EVENING_VISITS]",
        f"measure:{BASE}::[Pending Visit Days]",          # matched by the KPI name itself
    }
    assert by_node[f"measure:{BASE}::[Pending Visit Days]"].definition == "Working days since the last Visit (Note: Core metric)"
    assert by_node[f"measure:{BASE}::[Pending Visit Days]"].source_ref == "docs/x.md:20"
    assert [u["kpi"] for u in r.unmatched] == ["Dynamic Title"]


def test_attach_tolerates_a_trailing_underscore_in_the_repo_folder():
    nodes = {n.replace("Visit-Status", "Visit-Status_") for n in NODES}
    r = attach("docs/x.md", DOC, TREE, nodes)
    assert len(r.notes) == 4


def test_find_nodes_and_notes(tmp_path):
    g = SqliteLineageGraph(tmp_path / "l.db")
    ids = [f"measure:{BASE}::[Growth%]", f"measure:{BASE}::[growth asc]", f"measure:{BASE}::[Growth% Div]",
           f"measure:{BASE}::[Calls]"]
    nodes = [LineageNode(i, "measure", i.split(":", 1)[1]) for i in ids]
    notes = [NodeNote(ids[3], "kpi_definition", "Store Calls", "Total calls", "docs/x.md:3", False),
             NodeNote(ids[0], "defined_at", "", f"{TREE}:m.tmdl:4", f"{TREE}:m.tmdl:4", True)]
    g.rebuild(nodes, [], "x", notes=notes, meta={"kpi": {"definitions": 1}})
    assert g.find_nodes("measure", "Growth%", 10) == [ids[0], ids[2]]  # `%` is literal; exact first
    assert g.find_nodes("measure", "store calls", 10) == [ids[3]]     # via the KPI title
    assert {n.kind for n in g.notes_for([ids[0], ids[3]])} == {"defined_at", "kpi_definition"}
    assert g.coverage().kpi == {"definitions": 1}


def test_definitions_apply_to_listed_copies_with_their_source_marked(tmp_path):
    from types import SimpleNamespace

    from app.container import Container

    ai = tmp_path / "docs" / "ai"
    (ai / "reports" / "regional").mkdir(parents=True)
    (ai / "reports" / "regional" / "Visit-Status-Report-Document.md").write_text(DOC, encoding="utf-8")
    (ai / "reports" / "sources.yaml").write_text(
        f"regional:\n  tree: {TREE}\n  also_trees: [copy@main, absent@main]\n", encoding="utf-8")
    copy = {n.replace(TREE, "copy@main") for n in NODES if "EVENING" not in n}  # the copy drifted
    fake = SimpleNamespace(lineage_source_path=ai / "lineage-graph.yaml")
    notes, stats = Container._kpi_notes(fake, NODES | copy)
    on_copy = [n for n in notes if "copy@main" in n.node_id]
    assert len(on_copy) == 3 and all(f"(doc describes {TREE}; applied to this copy)" in n.source_ref for n in on_copy)
    assert not any("copy@main" in n.source_ref for n in notes if TREE in n.node_id)
    assert {(u["tree"], u["kpi"]) for u in stats["unmatched"]} >= {("copy@main", "Dynamic Title"), (TREE, "Dynamic Title")}
    assert any(u["tree"] == "absent@main" and u["kpi"] == "*" for u in stats["unmatched"])


def test_direct_gold_stops_at_the_first_gold_node(tmp_path):
    """kpi-lookup's gold_sources/producers: the gold tables a measure reads, not every table the
    producing notebook reads (a plain upstream walk crosses the notebook into the gold layer)."""
    from app.cli import _direct_gold
    from domain.lineage import LineageEdge

    m, col, gcol, gt = f"measure:{BASE}::[M]", f"column:{BASE}::t[c]", "gold_column:t.c", "gold_table:t"
    nb, other = "notebook:nb.py", "gold_table:unrelated_input"
    edges = [LineageEdge(m, col, "depends_on", True, ""), LineageEdge(col, gcol, "consumes", True, ""),
             LineageEdge(gt, gcol, "contains", True, ""), LineageEdge(nb, gt, "produces", True, "databricks@x:nb.py:9"),
             LineageEdge(nb, other, "consumes", True, "")]
    g = SqliteLineageGraph(tmp_path / "l.db")
    g.rebuild([LineageNode(n, n.split(":", 1)[0], n.split(":", 1)[1]) for n in {m, col, gcol, gt, nb, other}], edges, "x")
    assert _direct_gold(g, m, 8) == {gcol}
    assert other not in _direct_gold(g, m, 8)
