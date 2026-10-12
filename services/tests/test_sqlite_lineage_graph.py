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


def test_downstream_from_pipeline_reaches_gold(tmp_path):
    g = _graph(tmp_path)
    _seed(g)
    t = g.blast_radius(PIPE, "downstream", max_hops=5)
    assert set(t.nodes) >= {PIPE, NB, GOLD}
    assert FAB not in t.nodes  # fabric consumes gold, it's not downstream of the pipeline


def test_upstream_from_gold_reaches_pipeline(tmp_path):
    g = _graph(tmp_path)
    _seed(g)
    t = g.blast_radius(GOLD, "upstream", max_hops=5)
    assert set(t.nodes) >= {GOLD, NB, PIPE, FAB}  # all feed into / consume gold's upstream chain
    assert t.unverified_edge_count == 1  # the fabric consumes-edge is unverified


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
    into = g.neighbors(GOLD, "upstream")
    assert {e.src for e in into} == {NB, FAB}


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
