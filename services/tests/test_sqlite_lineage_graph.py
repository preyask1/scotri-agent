"""adapters/lineage/sqlite_lineage_graph.py — the SQLite lineage index (OPEN_ITEMS.md item #37).

A local, credential-free adapter, so it's unit-tested directly against a temp DB (unlike the network
adapters). Covers rebuild, both traversal directions, coverage, the unverified worklist, and the
per-edge freshness-preservation rule (an unchanged edge keeps its original updated_at across a rebuild).
"""
from __future__ import annotations

from domain.lineage import LineageEdge, LineageNode
from adapters.lineage.sqlite_lineage_graph import SqliteLineageGraph


def _node(nid):
    kind, name = nid.split(":", 1)
    return LineageNode(id=nid, kind=kind, name=name)


def _graph(tmp_path):
    return SqliteLineageGraph(tmp_path / "lineage.db")


# A small chain: pipeline -> notebook -> gold <- fabric (consumes)
NB = "notebook:nb.ipynb"
GOLD = "gold_table:g"
PIPE = "adf_pipeline:p"
FAB = "fabric_table:f"


def _seed(graph):
    nodes = [_node(NB), _node(GOLD), _node(PIPE), _node(FAB)]
    edges = [
        LineageEdge(PIPE, NB, "feeds", True, "src1"),
        LineageEdge(NB, GOLD, "produces", True, "src2"),
        LineageEdge(FAB, GOLD, "consumes", False, "src3"),  # unverified (🔁)
    ]
    graph.rebuild(nodes, edges, source_file="test.yaml")


def test_coverage_counts(tmp_path):
    g = _graph(tmp_path)
    _seed(g)
    cov = g.coverage()
    assert cov.node_count == 4
    assert cov.edge_count == 3
    assert cov.verified_edge_count == 2
    assert len(cov.unverified_edges) == 1
    assert cov.unverified_edges[0].src == FAB
    assert cov.source_file == "test.yaml"
    assert cov.last_build_at is not None


def test_downstream_follows_data_flow_through_consumes(tmp_path):
    """`fabric_table consumes gold_table` is written consumer -> source; downstream still reaches
    the consumer, because a change to the pipeline does reach the Fabric table built on its output."""
    g = _graph(tmp_path)
    _seed(g)
    t = g.blast_radius(PIPE, "downstream", max_hops=5)
    assert set(t.nodes) == {PIPE, NB, GOLD, FAB}
    assert set(g.blast_radius(GOLD, "downstream", max_hops=5).nodes) == {GOLD, FAB}


def test_upstream_from_gold_reaches_producers_only(tmp_path):
    g = _graph(tmp_path)
    _seed(g)
    t = g.blast_radius(GOLD, "upstream", max_hops=5)
    assert set(t.nodes) == {GOLD, NB, PIPE}  # what feeds it; its consumer is not upstream
    assert t.unverified_edge_count == 0
    assert g.blast_radius(GOLD, "downstream", max_hops=5).unverified_edge_count == 1  # the 🔁 consumes-edge


def test_max_hops_limits_traversal(tmp_path):
    g = _graph(tmp_path)
    _seed(g)
    t = g.blast_radius(PIPE, "downstream", max_hops=1)
    assert NB in t.nodes
    assert GOLD not in t.nodes  # gold is 2 hops from the pipeline


def test_neighbors(tmp_path):
    g = _graph(tmp_path)
    _seed(g)
    out = g.neighbors(NB, "downstream")
    assert len(out) == 1 and out[0].dst == GOLD
    assert {e.src for e in g.neighbors(GOLD, "upstream")} == {NB}
    assert {e.src for e in g.neighbors(GOLD, "downstream")} == {FAB}  # stored as FAB consumes GOLD


def test_generated_rels_traverse_gold_to_report(tmp_path):
    g = _graph(tmp_path)
    col, meas, rep = "column:t/m::f[c]", "measure:t/m::[M]", "report:t/r"
    edges = [
        LineageEdge(FAB, GOLD, "consumes", True, ""),
        LineageEdge(FAB, col, "contains", True, ""),
        LineageEdge(meas, col, "depends_on", True, ""),
        LineageEdge(rep, meas, "uses", True, ""),
    ]
    g.rebuild([_node(n) for n in (GOLD, FAB, col, meas, rep)], edges, source_file="x", generated={"t": {"edges": 4}})
    assert set(g.blast_radius(GOLD, "downstream", 5).nodes) == {GOLD, FAB, col, meas, rep}
    assert set(g.blast_radius(rep, "upstream", 5).nodes) == {rep, meas, col, FAB, GOLD}
    assert g.coverage().generated == {"t": {"edges": 4}}


def test_index_built_before_flow_columns_is_migrated(tmp_path):
    import sqlite3
    conn = sqlite3.connect(tmp_path / "lineage.db")
    conn.executescript(
        "CREATE TABLE edges (src TEXT NOT NULL, dst TEXT NOT NULL, rel TEXT NOT NULL, verified INTEGER NOT NULL,"
        " source_ref TEXT, updated_at TEXT NOT NULL, PRIMARY KEY (src, dst, rel));"
        f"INSERT INTO edges VALUES ('{FAB}', '{GOLD}', 'consumes', 1, '', 'x');")
    conn.commit()
    conn.close()
    g = _graph(tmp_path)
    assert set(g.blast_radius(GOLD, "downstream", 3).nodes) == {GOLD, FAB}


def test_unverified_edges_worklist(tmp_path):
    g = _graph(tmp_path)
    _seed(g)
    uv = g.unverified_edges()
    assert len(uv) == 1 and uv[0].rel == "consumes"


def test_freshness_preserved_for_unchanged_edge(tmp_path):
    g = _graph(tmp_path)
    _seed(g)
    first = {(e.src, e.dst, e.rel): None for e in g.coverage().unverified_edges}  # noqa: F841
    import sqlite3
    def _updated(src):
        conn = sqlite3.connect(tmp_path / "lineage.db")
        row = conn.execute("SELECT updated_at FROM edges WHERE src=?", (src,)).fetchone()
        conn.close()
        return row[0]
    ts_before = _updated(NB)
    # Rebuild with the FAB edge now verified (changed), NB->GOLD unchanged.
    nodes = [_node(NB), _node(GOLD), _node(PIPE), _node(FAB)]
    edges = [
        LineageEdge(PIPE, NB, "feeds", True, "src1"),
        LineageEdge(NB, GOLD, "produces", True, "src2"),      # unchanged
        LineageEdge(FAB, GOLD, "consumes", True, "src3"),     # changed: now verified
    ]
    g.rebuild(nodes, edges, source_file="test.yaml")
    assert _updated(NB) == ts_before          # unchanged edge kept its stamp
    assert g.coverage().verified_edge_count == 3
