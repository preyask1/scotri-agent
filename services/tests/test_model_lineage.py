"""domain/model_lineage.py — lineage edges generated from TMDL and report definitions.

Fixtures are cut-down copies of shapes found in the real mirror (2026-10-12): ``` and indented
measure bodies, calculated columns, navigation partitions (literal and parameter names), native SQL,
a query referencing another table, an "Enter data" table, a composite-model entity partition, an RLS
role, and report field strings in report_bindings notation."""
from __future__ import annotations

import json

from adapters.lineage.generated_store import GeneratedLineageStore
from domain.lineage import FLOW_REVERSED_RELS, canonical_node_id, load_graph, merge
from domain.model_lineage import dax_refs, generate_tree, m_source, parse_tmdl, resolve_field

M = "Field/Work Habit/Visit-Status.SemanticModel"
TREE = "fabric_secondary@regional_main"
BASE = f"{TREE}/Field/Work Habit/Visit-Status"

Visit = """table visit_summary
\tlineageTag: x

\tmeasure 'Avg Visit Submission Days' = ```
\t\t\t
\t\t\tAVERAGEX(
\t\t\t    'visit_summary',
\t\t\t    DATEDIFF('visit_summary'[VISITDATE], 'visit_summary'[SUBMITDATETIME], DAY)  // "[NOTACOL]"
\t\t\t)
\t\t\t```
\t\tformatString: #,0.0

\tmeasure 'Row Color' =
\t\t\tVAR PendingDays = [Avg Visit Submission Days]
\t\t\tRETURN IF(PendingDays >= 4, "#fc7272", BLANK())
\t\tdisplayFolder: Measures

\tcolumn SAPCODE
\t\tdataType: string
\t\tsourceColumn: SAPCODE

\tcolumn VISITDATE
\t\tdataType: dateTime
\t\tsourceColumn: VISITDATE

\tcolumn SUBMITDATETIME
\t\tsourceColumn: SUBMITDATETIME

\tcolumn 'Month Sort' = MONTH([VISITDATE])
\t\tsortByColumn: 'FY Sort'

\tpartition visit_summary = m
\t\tmode: import
\t\tsource = ```
\t\t\t\tlet
\t\t\t\t    Source = Databricks.Catalogs("host", "/sql/1.0/warehouses/x", [Catalog=null]),
\t\t\t\t    db = Source{[Name="acme_prod", Kind="Database"]}[Data],
\t\t\t\t    sch = db{[Name=SchemaName, Kind="Schema"]}[Data],
\t\t\t\t    t = sch{[Name="visit_summary", Kind="Table"]}[Data]
\t\t\t\tin
\t\t\t\t    t
\t\t\t\t```

\tannotation PBI_ResultType = Table
"""

USERS = """table users
\tcolumn EmailAddress
\t\tsourceColumn: EmailAddress

\tpartition users = m
\t\tmode: import
\t\tsource =
\t\t\t\tlet
\t\t\t\t    Source = Value.NativeQuery(Databricks.Catalogs("h", Compute, [Catalog="acme_prod"]){[Name="acme_prod",Kind="Database"]}[Data], "select EmailAddress from acme_prod.gold.user_access_email u#(lf)join gold.staff_hierarchy s on 1=1", null)
\t\t\t\tin
\t\t\t\t    Source
"""

DERIVED = """table Bridge
\tcolumn SAPCODE
\t\tsourceColumn: SAPCODE

\tpartition Bridge = m
\t\tmode: import
\t\tsource =
\t\t\t\tlet
\t\t\t\t    Source = visit_summary,
\t\t\t\t    #"Kept" = Table.SelectColumns(Source, {"SAPCODE"})
\t\t\t\tin
\t\t\t\t    #"Kept"
"""

ENTERED = """table New
\tcolumn Category
\t\tsourceColumn: Category

\tpartition New = m
\t\tmode: import
\t\tsource = Table.FromRows(Json.Document(Binary.Decompress(Binary.FromText("abc", BinaryEncoding.Base64), Compression.Deflate)))
"""

COMPOSITE = """table scm
\tcolumn qty
\t\tsourceColumn: qty

\tpartition scm = entity
\t\tmode: directQuery
\t\tsource
\t\t\tentityName: scm
\t\t\texpressionSource: 'DirectQuery to AS - SCM Model'
"""

EXPRESSIONS = """expression SchemaName = "gold" meta [IsParameterQuery=true, Type="Text"]
\tlineageTag: a

expression Compute = "/sql/1.0/warehouses/x" meta [IsParameterQuery=true]
"""

ROLE = """role Field
\tmodelPermission: read

\ttablePermission visit_summary = ```
\t\t\tVAR U = USERPRINCIPALNAME()
\t\t\tRETURN 'visit_summary'[SAPCODE] = U || 'gone_table'[X] = U
\t\t\t```
"""


def _files():
    d = f"{M}/definition"
    return {
        f"{d}/tables/visit_summary.tmdl": Visit, f"{d}/tables/users.tmdl": USERS,
        f"{d}/tables/Bridge.tmdl": DERIVED, f"{d}/tables/New.tmdl": ENTERED,
        f"{d}/tables/scm.tmdl": COMPOSITE, f"{d}/expressions.tmdl": EXPRESSIONS,
        f"{d}/roles/Field.tmdl": ROLE,
    }


def _model():
    return parse_tmdl(_files())


# ── parsing ─────────────────────────────────────────────────────────────────────────────────

def test_parse_tables_columns_measures():
    m = _model()
    t = m.tables["visit_summary"]
    assert set(t.measures) == {"Avg Visit Submission Days", "Row Color"}
    assert t.columns["SAPCODE"] is None and t.source_columns["SAPCODE"] == "SAPCODE"
    assert t.columns["Month Sort"].text == "MONTH([VISITDATE])"  # sortByColumn property not captured
    assert "formatString" not in t.measures["Avg Visit Submission Days"].text
    assert "displayFolder" not in t.measures["Row Color"].text
    assert t.measures["Row Color"].line == 13
    assert m.roles["Field"][0][0] == "visit_summary"


def test_dax_refs_resolve_and_ignore_comments_and_strings():
    m = _model()
    refs, gaps = dax_refs(m.tables["visit_summary"].measures["Avg Visit Submission Days"].text, m, "visit_summary")
    assert {(r.kind, r.table, r.name) for r in refs} == {
        ("table", "visit_summary", ""), ("column", "visit_summary", "VISITDATE"), ("column", "visit_summary", "SUBMITDATETIME")}
    assert gaps == []
    refs, _ = dax_refs(m.tables["visit_summary"].measures["Row Color"].text, m, "visit_summary")
    assert {(r.kind, r.name) for r in refs} == {("measure", "Avg Visit Submission Days")}


def test_dax_missing_table_is_reported():
    m = _model()
    _, gaps = dax_refs(m.roles["Field"][0][1].text, m, "visit_summary")
    assert gaps == ["table 'gone_table' not in model (via 'gone_table'[X])"]


def test_m_source_navigation_with_parameter_and_native_sql():
    m = _model()
    from domain.model_lineage import string_params
    params = string_params(m.expressions)
    assert params == {"SchemaName": "gold", "Compute": "/sql/1.0/warehouses/x"}
    nav = m_source(m.tables["visit_summary"].partitions[0][1].text, set(m.tables), "visit_summary", params)
    assert {t.node for t in nav.tables} == {"gold_table:visit_summary"} and nav.navigation_only
    sql = m_source(m.tables["users"].partitions[0][1].text, set(m.tables) | set(m.expressions), "users", params)
    assert {t.node for t in sql.tables} == {"gold_table:user_access_email", "gold_table:staff_hierarchy"}
    assert not sql.navigation_only
    assert "visit_summary" not in sql.queries  # names inside M strings are not query references


def test_resolve_report_field_strings():
    m = _model()
    assert resolve_field("visit_summary.SAPCODE", m).name == "SAPCODE"
    assert resolve_field("Sum(visit_summary.VISITDATE)", m).name == "VISITDATE"
    assert resolve_field("visit_summary.Row Color (measure)", m).kind == "measure"
    assert resolve_field("visit_summary.SAPCODE in [A; B]", m).name == "SAPCODE"
    assert resolve_field("visit_summary.Date Hierarchy.SUBMITDATETIME", m).name == "SUBMITDATETIME"
    assert resolve_field("visit_summary.flag (report measure) = 1", m) is None
    assert resolve_field("visit_summary.NOPE", m) is None


# ── a whole tree ────────────────────────────────────────────────────────────────────────────

def _tree():
    files = _files()
    rep = "Field/Work Habit/Visit-Status.Report"
    files[f"{rep}/definition.pbir"] = json.dumps({"datasetReference": {"byPath": {"path": "../Visit-Status.SemanticModel"}}})
    visual = {"visual": {"visualType": "tableEx", "query": {"queryState": {"Values": {"projections": [
        {"field": {"Column": {"Expression": {"SourceRef": {"Entity": "visit_summary"}}, "Property": "SAPCODE"}}},
        {"field": {"Measure": {"Expression": {"SourceRef": {"Entity": "visit_summary"}}, "Property": "Row Color"}}},
        {"field": {"Column": {"Expression": {"SourceRef": {"Entity": "visit_summary"}}, "Property": "DROPPED"}}},
    ]}}}}}
    files[f"{rep}/definition/pages/p1/page.json"] = json.dumps({"displayName": "Team View"})
    files[f"{rep}/definition/pages/p1/visuals/v1/visual.json"] = json.dumps(visual)
    return generate_tree(TREE, sorted(files), files.get)


def _has(g, src, dst, rel):
    return any(e.src == src and e.dst == dst and e.rel == rel for e in g.edges)


def test_generate_tree_edges():
    g = _tree()
    tbl = f"fabric_table:{BASE}::visit_summary"
    assert _has(g, tbl, f"fabric_model:{BASE}", "part_of")
    assert _has(g, tbl, "gold_table:visit_summary", "consumes")
    assert _has(g, f"column:{BASE}::visit_summary[SAPCODE]", "gold_column:visit_summary.sapcode", "consumes")
    assert _has(g, f"measure:{BASE}::[Avg Visit Submission Days]", f"column:{BASE}::visit_summary[VISITDATE]", "depends_on")
    assert _has(g, f"column:{BASE}::visit_summary[Month Sort]", f"column:{BASE}::visit_summary[VISITDATE]", "depends_on")
    assert _has(g, f"fabric_table:{BASE}::Bridge", tbl, "consumes")  # Source = visit_summary
    assert _has(g, f"rls_role:{BASE}::Field", f"column:{BASE}::visit_summary[SAPCODE]", "depends_on")
    rep = f"report:{TREE}/Field/Work Habit/Visit-Status"
    assert _has(g, rep, f"measure:{BASE}::[Row Color]", "uses")
    assert _has(g, rep, f"column:{BASE}::visit_summary[SAPCODE]", "uses")
    assert all(e.verified and e.source_ref.startswith(TREE + ":") for e in g.edges)
    assert {e.rel for e in g.edges if e.rel in FLOW_REVERSED_RELS} == {"consumes", "depends_on", "uses"}


def test_generate_tree_reports_what_it_could_not_resolve():
    g = _tree()
    reasons = {(u.node.rsplit("::", 1)[-1], u.reason.split(":")[0]) for u in g.unresolved}
    assert ("scm", "entity partition over another model (DirectQuery to AS - SCM Model); its source is not in this tree") in reasons
    assert any("DROPPED" in u.reason for u in g.unresolved)
    assert any("gone_table" in u.reason for u in g.unresolved)
    assert not any(u.node.endswith("::New") for u in g.unresolved)  # entered data: nothing to trace
    assert g.stats["models"] == 1 and g.stats["reports"] == 1 and g.stats["measures"] == 2


# ── store and merge ─────────────────────────────────────────────────────────────────────────

def test_store_roundtrip_and_merge_with_curated(tmp_path):
    g = _tree()
    store = GeneratedLineageStore(tmp_path)
    store.save(g, "tok", "2026-10-12T00:00:00Z")
    [loaded] = store.load_all()
    assert loaded.tree == TREE and len(loaded.edges) == len(g.edges) and loaded.manifest_token == "tok"
    curated = load_graph({"edges": [
        {"src": "notebook:nb", "dst": "gold_table:Visit_Summary", "rel": "produces", "verified": False}]})
    merged = merge(curated, loaded.edges)
    ids = {n.id for n in merged.nodes}
    assert "gold_table:visit_summary" in ids and "gold_table:Visit_Summary" not in ids  # case-folded, one node
    assert canonical_node_id("fabric_table:Keep_Case") == "fabric_table:Keep_Case"


# ── automatic catch-up (Container.ensure_lineage_current) ──────────────────────────────────

def test_ensure_lineage_current_regenerates_missing_and_stale_trees_only(tmp_path):
    from types import SimpleNamespace

    from adapters.lineage.sqlite_lineage_graph import SqliteLineageGraph
    from app.container import Container
    from domain.model_lineage import TreeGraph

    refs = {"a": [SimpleNamespace(repo="A", branch="main", mirrored=True)],
            "b": [SimpleNamespace(repo="B", branch="main", mirrored=True)],
            "dev": [SimpleNamespace(repo="A", branch="dev", mirrored=False)]}
    tokens = {("A", "main"): "t1", ("B", "main"): "t1"}
    store = GeneratedLineageStore(tmp_path / "gen")
    graph = SqliteLineageGraph(tmp_path / "lineage.db")
    calls = []

    def generate(ref):
        calls.append(ref.repo)
        store.save(TreeGraph(f"{ref.repo}@{ref.branch}", stats={"models": 1}), tokens[(ref.repo, ref.branch)], "now")
        return {"models": 1}

    fake = SimpleNamespace(
        registry=SimpleNamespace(all_repo_keys=lambda: list(refs), get=lambda k: refs[k]),
        mirror_reader=SimpleNamespace(has_branch=lambda r, b: True, manifest_token=lambda r, b: tokens[(r, b)]),
        maintenance_flag=SimpleNamespace(status=lambda r: SimpleNamespace(active=False)),
        lineage_generated_store=store, lineage_graph=graph,
        tree_label=lambda ref: f"{ref.repo}@{ref.branch}", generate_tree_lineage=generate,
        rebuild_lineage_index=lambda: graph.rebuild([], [], "x", generated={
            t.tree: {"generated_at": t.generated_at} for t in store.load_all()}),
    )
    first = Container._ensure_lineage_current(fake)
    assert sorted(calls) == ["A", "B"] and first["rebuilt"]  # fresh deployment: everything
    calls.clear()
    assert Container._ensure_lineage_current(fake) == {"regenerated": {}, "skipped": [], "rebuilt": False}
    tokens[("B", "main")] = "t2"  # B was synced since
    Container._ensure_lineage_current(fake)
    assert calls == ["B"]


def test_report_with_stale_model_path_binds_by_unique_folder_name():
    files = _files()
    rep = "Moved/Reports/Visit View.Report"  # its pbir still points at the model's old location
    files[f"{rep}/definition.pbir"] = json.dumps(
        {"datasetReference": {"byPath": {"path": "../Model/Visit-Status.SemanticModel"}}})
    files[f"{rep}/definition/pages/p1/page.json"] = json.dumps({"displayName": "P"})
    files[f"{rep}/definition/pages/p1/visuals/v1/visual.json"] = json.dumps({"visual": {
        "visualType": "card", "query": {"queryState": {"Values": {"projections": [
            {"field": {"Column": {"Expression": {"SourceRef": {"Entity": "visit_summary"}}, "Property": "SAPCODE"}}}]}}}}})
    g = generate_tree(TREE, sorted(files), files.get)
    node = f"report:{TREE}/Moved/Reports/Visit View"
    assert _has(g, node, f"column:{BASE}::visit_summary[SAPCODE]", "uses")
    assert any(u.node == node and u.reason.startswith("definition.pbir path is stale") for u in g.unresolved)


def test_lineage_lock_is_exclusive_and_taken_over_when_stale(tmp_path, monkeypatch):
    import os
    import time

    from app.container import Container

    monkeypatch.setenv("LINEAGE_GENERATED_DIR", str(tmp_path / "gen"))
    c = Container()
    with c.lineage_lock() as first:
        assert first
        with c.lineage_lock() as second:  # another writer: refused at once
            assert not second
    with c.lineage_lock() as again:       # released on exit
        assert again
    lock = tmp_path / "gen" / ".lock"
    lock.write_text("12345 0")
    old = time.time() - 3600
    os.utime(lock, (old, old))            # its process died an hour ago
    with c.lineage_lock(stale_seconds=1800) as taken:
        assert taken
    assert not lock.exists()


def test_model_descriptions_are_captured_for_measures_and_columns():
    tmdl = ("table sales\n"
            "\t/// Net sales after returns, in INR.\n"
            "\t/// Excludes samples.\n"
            "\tmeasure 'Net Sales' = SUM(sales[amount])\n"
            "\t\tformatString: #,0\n\n"
            "\t/// Invoice amount\n"
            "\tcolumn amount\n"
            "\t\tsourceColumn: amount\n\n"
            "\tcolumn undescribed\n"
            "\t\tsourceColumn: undescribed\n")
    files = {"M.SemanticModel/definition/tables/sales.tmdl": tmdl}
    m = parse_tmdl(files)
    assert m.tables["sales"].descriptions == {"measure:Net Sales": "Net sales after returns, in INR. Excludes samples.",
                                              "column:amount": "Invoice amount"}
    g = generate_tree("t@main", sorted(files), files.get)
    assert g.descriptions == {"measure:t@main/M::[Net Sales]": "Net sales after returns, in INR. Excludes samples.",
                              "column:t@main/M::sales[amount]": "Invoice amount"}
