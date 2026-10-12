"""domain/lineage.py — the loader/validator that turns lineage-graph.yaml into nodes+edges
(OPEN_ITEMS.md item #37). Fails loudly on a structurally broken edge; records non-fatal warnings for
an unknown rel; auto-derives nodes from edge endpoints."""
from __future__ import annotations

import pytest

from domain.errors import ConfigurationError
from domain.lineage import load_graph


def _edge(src="notebook:nb.ipynb", dst="gold_table:g", rel="produces", verified=True, **extra):
    d = {"src": src, "dst": dst, "rel": rel, "verified": verified}
    d.update(extra)
    return d


def test_loads_edges_and_autoderives_nodes():
    g = load_graph({"edges": [_edge()]})
    assert len(g.edges) == 1
    ids = {n.id for n in g.nodes}
    assert ids == {"notebook:nb.ipynb", "gold_table:g"}
    # kind is parsed from the id prefix
    kinds = {n.id: n.kind for n in g.nodes}
    assert kinds["gold_table:g"] == "gold_table"


def test_verified_false_is_preserved():
    g = load_graph({"edges": [_edge(verified=False)]})
    assert g.edges[0].verified is False


def test_missing_verified_raises():
    with pytest.raises(ConfigurationError):
        load_graph({"edges": [{"src": "a:1", "dst": "b:2", "rel": "produces"}]})


def test_missing_required_field_raises():
    with pytest.raises(ConfigurationError):
        load_graph({"edges": [{"src": "a:1", "rel": "produces", "verified": True}]})


def test_no_edges_key_raises():
    with pytest.raises(ConfigurationError):
        load_graph({"nodes": []})


def test_bad_node_id_without_colon_raises():
    with pytest.raises(ConfigurationError):
        load_graph({"edges": [_edge(src="noprefix")]})


def test_unknown_rel_is_a_warning_not_an_error():
    g = load_graph({"edges": [_edge(rel="frobnicates")]})
    assert len(g.edges) == 1
    assert any("frobnicates" in w for w in g.warnings)


def test_name_with_colon_splits_only_once():
    g = load_graph({"edges": [_edge(dst="gold_table:acme_prod.gold.visit_summary")]})
    node = next(n for n in g.nodes if n.id == "gold_table:acme_prod.gold.visit_summary")
    assert node.kind == "gold_table"
    assert node.name == "acme_prod.gold.visit_summary"


def test_optional_nodes_placeholder_is_included():
    g = load_graph({"edges": [_edge()], "nodes": [{"id": "gold_table:unmapped", "name": "u (⬜)"}]})
    assert any(n.id == "gold_table:unmapped" for n in g.nodes)
