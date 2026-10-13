# Copyright (c) 2026 Preyas Kulshrestha. Part of the Scotri engine; MIT License (see LICENSE).
# Keep this notice in copies and derived works, including AI-assisted ones.
"""Lineage graph — pure domain types and the loader that turns the machine-readable lineage source
(docs/ai/client/lineage-graph.yaml) into validated nodes and edges (OPEN_ITEMS.md item #37).

Why a graph at all: docs/ai/client/lineage-map.md already *is* a graph (gold table ⇄ producing notebook ⇄
consuming Fabric model), hand-maintained in markdown with 🔁 markers on edges nobody has
re-verified. impact-analysis re-derives that reachability by reading files every time. A queryable
graph answers "what does changing X touch" as a traversal, and — the property that actually matters
for an unattended agent — can report *"47 nodes reached, 3 edges unverified since <date>"*, turning
the always-trace-all-four-repos behavioural promise into something structural (design note
docs/ai/retrieval-and-graph-design.md §1).

Decision recorded here (OPEN_ITEMS.md #37 decision 3, resolved 2026-09-29): the YAML is the
machine-readable source for the *hand-curated* edges; docs/ai/client/lineage-map.md stays the narrative.
Since 2026-10-12 most of the graph is generated from the mirror instead (domain/model_lineage.py,
domain/code_lineage.py) and merged with the YAML by `merge`; the YAML keeps only legs the mirror
can't show. The heterogeneous markdown tables are not reliably machine-parseable, so making them the
parse source would silently drop rows — the exact failure this graph exists to prevent. `verified:
false` here is the queryable form of the map's 🔁 marker.

Pure domain: no sqlite, no yaml library import here. The loader takes an already-parsed dict (the CLI
layer reads the file); traversal itself lives in the SQLite adapter (recursive CTE), not here.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from domain.errors import ConfigurationError

# Relationship vocabulary from the design note (§1). Not a hard allowlist — an unknown rel loads with
# a recorded warning rather than failing, so the graph can grow a new edge kind without a code change
# — but the known set is what traversal and coverage are documented against.
KNOWN_RELS = frozenset(
    {"produces", "consumes", "feeds", "references", "triggers", "tested_by", "investigated_in",
     "depends_on", "uses", "contains", "part_of", "includes"}
)

# Rels written consumer -> source (`fabric_table consumes gold_table`, `measure depends_on column`,
# `report uses measure`, `notebook includes notebook` for a `%run`). Traversal flips them so `downstream` always follows data flow: from a gold
# table downstream reaches the Fabric tables, measures and reports built on it; `upstream` reaches
# what feeds it. Every other rel (produces, feeds, contains, part_of, ...) already reads source ->
# consumer.
FLOW_REVERSED_RELS = frozenset({"consumes", "depends_on", "uses", "includes"})

# Databricks object kinds: Unity Catalog names are case-insensitive, so their node names are
# lower-cased on load, letting a hand-written `gold_table:Sales_trend_X` meet a generated
# `gold_table:sales_trend_x`.
_CASE_INSENSITIVE_KINDS = ("gold_table", "silver_table", "bronze_table", "gold_column")


_TABLE_KINDS = ("gold_table", "silver_table", "bronze_table")


def canonical_node_id(node_id: str) -> str:
    """Lower-case for Databricks kinds; a table also drops its catalog/schema prefix
    (`gold_table:acme_prod.gold.visit_summary` -> `gold_table:visit_summary`), the form generated edges
    use, so a hand-written edge and a generated one meet on one node."""
    kind, _, name = node_id.partition(":")
    if kind not in _CASE_INSENSITIVE_KINDS:
        return node_id
    name = name.lower()
    if kind in _TABLE_KINDS:
        name = name.rsplit(".", 1)[-1]
    return f"{kind}:{name}"


@dataclass(frozen=True)
class LineageNode:
    id: str  # "kind:name", e.g. "gold_table:<prod_catalog>.gold.visit_summary"
    kind: str
    name: str


@dataclass(frozen=True)
class LineageEdge:
    src: str  # a node id
    dst: str  # a node id
    rel: str
    verified: bool
    source_ref: str = ""  # where this edge came from (a ticket, a session, a mirror path) — for audit


@dataclass(frozen=True)
class NodeNote:
    """Something known about a node that isn't an edge: where it is defined in the mirror
    (`defined_at`, verified) or a KPI's business definition from the report docs
    (`kpi_definition`, verified=False: the docs are a dated snapshot)."""
    node_id: str
    kind: str        # "defined_at" | "kpi_definition"
    title: str       # the KPI name for a definition; "" for a location
    text: str        # the definition, or the "<tree>:<path>:<line>" location
    source_ref: str  # where the note came from
    verified: bool


@dataclass(frozen=True)
class LoadedGraph:
    nodes: list[LineageNode]
    edges: list[LineageEdge]
    warnings: list[str] = field(default_factory=list)  # non-fatal: unknown rel, node named twice, etc.


@dataclass(frozen=True)
class Traversal:
    """One blast-radius result — the subgraph reachable from `root` within `max_hops`."""
    root: str
    direction: str  # "downstream" | "upstream"
    nodes: list[str]
    edges: list[LineageEdge]
    max_hops: int
    unverified_edge_count: int

    def nodes_by_kind(self) -> dict[str, int]:
        out: dict[str, int] = {}
        for n in self.nodes:
            kind = n.split(":", 1)[0]
            out[kind] = out.get(kind, 0) + 1
        return dict(sorted(out.items()))


@dataclass(frozen=True)
class Coverage:
    node_count: int
    edge_count: int
    verified_edge_count: int
    unverified_edges: list[LineageEdge]
    last_build_at: str | None
    source_file: str | None
    generated: dict = field(default_factory=dict)  # per-tree stats of mirror-generated edges
    kpi: dict = field(default_factory=dict)        # KPI definitions attached from the report docs


def _node_id_parts(node_id: str) -> tuple[str, str]:
    """A node id is "kind:name"; the name itself may contain ':' (a DAX path, a URL), so split once."""
    if ":" not in node_id:
        raise ConfigurationError(
            f"lineage node id {node_id!r} must be 'kind:name' (e.g. 'gold_table:<prod_catalog>.gold.x') — "
            f"the kind prefix is what lets coverage group nodes by type."
        )
    kind, name = node_id.split(":", 1)
    if not kind or not name:
        raise ConfigurationError(f"lineage node id {node_id!r} has an empty kind or name.")
    return kind, name


def load_graph(raw: dict) -> LoadedGraph:
    """Validate a parsed lineage-graph.yaml dict into nodes + edges.

    Nodes are auto-derived from every edge endpoint (so the YAML only has to list edges), with an
    optional top-level `nodes:` list to give a node a custom display name or to declare a not-yet-
    connected node (an ⬜ 'unmapped' placeholder). Fails loudly on a structurally broken edge; records
    a non-fatal warning for an unknown rel or a duplicate node declaration.
    """
    if not isinstance(raw, dict):
        raise ConfigurationError("lineage-graph source must be a mapping with an 'edges' key.")

    warnings: list[str] = []
    nodes: dict[str, LineageNode] = {}

    def ensure_node(node_id: str, name_override: str | None = None) -> None:
        kind, name = _node_id_parts(node_id)
        if node_id in nodes and name_override and nodes[node_id].name != name_override:
            warnings.append(f"node {node_id!r} declared more than once with differing names")
        nodes[node_id] = LineageNode(id=node_id, kind=kind, name=name_override or name)

    for decl in raw.get("nodes", []) or []:
        if not isinstance(decl, dict) or "id" not in decl:
            raise ConfigurationError(f"each 'nodes' entry must be a mapping with an 'id': got {decl!r}")
        ensure_node(canonical_node_id(str(decl["id"])), decl.get("name"))

    edges: list[LineageEdge] = []
    raw_edges = raw.get("edges")
    if raw_edges is None:
        raise ConfigurationError("lineage-graph source has no 'edges' key.")
    if not isinstance(raw_edges, list):
        raise ConfigurationError("'edges' must be a list.")

    for i, e in enumerate(raw_edges):
        if not isinstance(e, dict):
            raise ConfigurationError(f"edge #{i} must be a mapping: got {e!r}")
        for required in ("src", "dst", "rel"):
            if not e.get(required):
                raise ConfigurationError(f"edge #{i} is missing required field {required!r}: {e!r}")
        src, dst, rel = canonical_node_id(str(e["src"])), canonical_node_id(str(e["dst"])), str(e["rel"])
        if "verified" not in e:
            raise ConfigurationError(
                f"edge #{i} ({src} -{rel}-> {dst}) must state verified: true/false explicitly — an "
                f"unstated verification status is exactly the ambiguity 🔁 exists to make visible."
            )
        verified = bool(e["verified"])
        if rel not in KNOWN_RELS:
            warnings.append(f"edge #{i} uses unknown rel {rel!r} (known: {sorted(KNOWN_RELS)})")
        ensure_node(src)
        ensure_node(dst)
        edges.append(LineageEdge(src=src, dst=dst, rel=rel, verified=verified,
                                 source_ref=str(e.get("source_ref", ""))))

    return LoadedGraph(nodes=list(nodes.values()), edges=edges, warnings=warnings)


def merge(curated: LoadedGraph, generated: list[LineageEdge]) -> LoadedGraph:
    """The hand-curated graph plus edges generated from the mirror. An edge in both keeps the
    generated (verified, mirror-sourced) copy; a curated node keeps its display name."""
    edges = {(e.src, e.dst, e.rel): e for e in curated.edges}
    for e in generated:
        edges[(e.src, e.dst, e.rel)] = e
    nodes = {n.id: n for n in curated.nodes}
    for e in generated:
        for nid in (e.src, e.dst):
            if nid not in nodes:
                kind, name = _node_id_parts(nid)
                nodes[nid] = LineageNode(id=nid, kind=kind, name=name)
    return LoadedGraph(nodes=list(nodes.values()), edges=list(edges.values()), warnings=curated.warnings)
