"""Port for the lineage graph store (OPEN_ITEMS.md item #37).

The query surface impact-analysis / lineage-trace use instead of re-deriving reachability by reading
files: a traversal (blast radius), plus the two reports that make completeness *provable* rather than
hoped-for — coverage (how much is mapped) and the unverified-edge set (the 🔁 rows, now queryable).

`rebuild` is the populate side: it replaces the whole graph from a freshly-loaded source. Kept on the
same port so the storage choice (SQLite today; Azure SQL/Postgres or a per-sync NetworkX rebuild
later) stays a one-adapter swap — though note the graph is a *derived, rebuildable index*, not a
system of record, so an ephemeral SQLite file that is rebuilt on startup is fine even on multi-replica
AKS (unlike durable engagement history, item #19/#25, which needs a managed DB).
"""
from __future__ import annotations

from typing import Protocol

from domain.lineage import Coverage, LineageEdge, LineageNode, NodeNote, Traversal


class LineageGraph(Protocol):
    def rebuild(self, nodes: list[LineageNode], edges: list[LineageEdge], source_file: str | None,
                generated: dict | None = None, notes: list[NodeNote] | None = None,
                meta: dict | None = None) -> None:
        """Replace the entire graph. An edge's freshness stamp (updated_at) is preserved when the
        edge is unchanged since the last rebuild and bumped only when its rel/verified/source_ref
        changed — so per-edge freshness is real, not just a single build timestamp."""
        ...

    def neighbors(self, node_id: str, direction: str) -> list[LineageEdge]:
        """Immediate edges out of (`downstream`) or into (`upstream`) a node, in data-flow
        direction (domain.lineage.FLOW_REVERSED_RELS)."""
        ...

    def blast_radius(self, node_id: str, direction: str, max_hops: int) -> Traversal:
        """The subgraph reachable from `node_id` within `max_hops`, `downstream` (what this change
        touches) or `upstream` (what feeds this). Reports its own unverified-edge count so a caller
        never treats a traversal that crossed a 🔁 edge as fully confirmed."""
        ...

    def coverage(self) -> Coverage:
        """Totals + the full unverified-edge set + when the graph was last built and from what —
        the numbers impact-analysis quotes to state coverage instead of implying it."""
        ...

    def find_nodes(self, kind: str, needle: str, limit: int) -> list[str]:
        """Node ids of `kind` whose name, or a note title, contains `needle`; exact names first."""
        ...

    def notes_for(self, node_ids: list[str]) -> list[NodeNote]:
        """Every note on these nodes (definition location, KPI definitions)."""
        ...

    def unverified_edges(self) -> list[LineageEdge]:
        """Every edge with verified=false (the 🔁 rows) — the missing-edge / re-confirm worklist."""
        ...
