"""SQLite lineage graph adapter (OPEN_ITEMS.md item #37) — implements ports.lineage_graph.LineageGraph.

Storage is a single SQLite file with recursive-CTE traversal, exactly the design note's "do not buy
Neo4j on day one" recommendation (docs/ai/retrieval-and-graph-design.md §1): at a handful of repos this fits
in SQLite with no new infrastructure, credential or vendor. The file is a *derived index* rebuilt
from docs/ai/client/lineage-graph.yaml, not a system of record — so losing it (e.g. an AKS pod restart)
costs nothing but a rebuild.

No external service, no credential — so this adapter is unit-tested directly against a temp file
(tests/test_sqlite_lineage_graph.py), unlike the network adapters.
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from domain.lineage import Coverage, LineageEdge, LineageNode, Traversal

_SCHEMA = """
CREATE TABLE IF NOT EXISTS nodes (
    id   TEXT PRIMARY KEY,
    kind TEXT NOT NULL,
    name TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS edges (
    src        TEXT NOT NULL,
    dst        TEXT NOT NULL,
    rel        TEXT NOT NULL,
    verified   INTEGER NOT NULL,
    source_ref TEXT,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (src, dst, rel)
);
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
CREATE INDEX IF NOT EXISTS idx_edges_src ON edges (src);
CREATE INDEX IF NOT EXISTS idx_edges_dst ON edges (dst);
"""

_VALID_DIRECTIONS = ("downstream", "upstream")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class SqliteLineageGraph:
    def __init__(self, db_path: Path) -> None:
        self._db_path = db_path
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as conn:
            conn.executescript(_SCHEMA)

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self._db_path)
        conn.row_factory = sqlite3.Row
        return conn

    @staticmethod
    def _row_to_edge(row: sqlite3.Row) -> LineageEdge:
        return LineageEdge(
            src=row["src"], dst=row["dst"], rel=row["rel"],
            verified=bool(row["verified"]), source_ref=row["source_ref"] or "",
        )

    # --- populate --------------------------------------------------------------------------

    def rebuild(self, nodes: list[LineageNode], edges: list[LineageEdge], source_file: str | None) -> None:
        now = _now()
        with self._connect() as conn:
            conn.executescript(_SCHEMA)
            # Preserve per-edge freshness: keep the old updated_at for an edge whose meaning
            # (verified/source_ref) is unchanged; bump it only for a new or changed edge.
            prior = {
                (r["src"], r["dst"], r["rel"]): (r["verified"], r["source_ref"] or "", r["updated_at"])
                for r in conn.execute("SELECT src, dst, rel, verified, source_ref, updated_at FROM edges")
            }
            conn.execute("DELETE FROM edges")
            conn.execute("DELETE FROM nodes")
            conn.executemany(
                "INSERT OR REPLACE INTO nodes (id, kind, name) VALUES (?, ?, ?)",
                [(n.id, n.kind, n.name) for n in nodes],
            )
            edge_rows = []
            for e in edges:
                key = (e.src, e.dst, e.rel)
                verified_int = 1 if e.verified else 0
                prev = prior.get(key)
                if prev is not None and prev[0] == verified_int and prev[1] == (e.source_ref or ""):
                    updated_at = prev[2]  # unchanged — keep its original freshness stamp
                else:
                    updated_at = now
                edge_rows.append((e.src, e.dst, e.rel, verified_int, e.source_ref, updated_at))
            conn.executemany(
                "INSERT OR REPLACE INTO edges (src, dst, rel, verified, source_ref, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                edge_rows,
            )
            conn.execute("INSERT OR REPLACE INTO meta (key, value) VALUES ('last_build_at', ?)", (now,))
            conn.execute("INSERT OR REPLACE INTO meta (key, value) VALUES ('source_file', ?)", (source_file or "",))
            conn.commit()

    # --- query -----------------------------------------------------------------------------

    def neighbors(self, node_id: str, direction: str) -> list[LineageEdge]:
        self._assert_direction(direction)
        col = "src" if direction == "downstream" else "dst"
        with self._connect() as conn:
            rows = conn.execute(
                f"SELECT src, dst, rel, verified, source_ref FROM edges WHERE {col} = ?", (node_id,)
            ).fetchall()
        return [self._row_to_edge(r) for r in rows]

    def blast_radius(self, node_id: str, direction: str, max_hops: int) -> Traversal:
        self._assert_direction(direction)
        # downstream follows src->dst; upstream follows dst->src.
        from_col, to_col = ("src", "dst") if direction == "downstream" else ("dst", "src")
        cte = f"""
            WITH RECURSIVE reach(node, depth) AS (
                SELECT ?, 0
                UNION
                SELECT e.{to_col}, r.depth + 1
                FROM edges e JOIN reach r ON e.{from_col} = r.node
                WHERE r.depth < ?
            )
            SELECT DISTINCT node FROM reach
        """
        with self._connect() as conn:
            reached = [r["node"] for r in conn.execute(cte, (node_id, max_hops)).fetchall()]
            reached_set = set(reached)
            edge_rows = conn.execute(
                "SELECT src, dst, rel, verified, source_ref FROM edges"
            ).fetchall()
        # Edges of the reached subgraph: both endpoints reached.
        sub_edges = [
            self._row_to_edge(r) for r in edge_rows
            if r["src"] in reached_set and r["dst"] in reached_set
        ]
        unverified = sum(1 for e in sub_edges if not e.verified)
        return Traversal(
            root=node_id, direction=direction,
            nodes=sorted(reached_set), edges=sub_edges,
            max_hops=max_hops, unverified_edge_count=unverified,
        )

    def coverage(self) -> Coverage:
        with self._connect() as conn:
            node_count = conn.execute("SELECT COUNT(*) FROM nodes").fetchone()[0]
            edge_count = conn.execute("SELECT COUNT(*) FROM edges").fetchone()[0]
            verified_count = conn.execute("SELECT COUNT(*) FROM edges WHERE verified = 1").fetchone()[0]
            unverified_rows = conn.execute(
                "SELECT src, dst, rel, verified, source_ref FROM edges WHERE verified = 0"
            ).fetchall()
            meta = {r["key"]: r["value"] for r in conn.execute("SELECT key, value FROM meta")}
        return Coverage(
            node_count=node_count,
            edge_count=edge_count,
            verified_edge_count=verified_count,
            unverified_edges=[self._row_to_edge(r) for r in unverified_rows],
            last_build_at=meta.get("last_build_at"),
            source_file=meta.get("source_file"),
        )

    def unverified_edges(self) -> list[LineageEdge]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT src, dst, rel, verified, source_ref FROM edges WHERE verified = 0"
            ).fetchall()
        return [self._row_to_edge(r) for r in rows]

    @staticmethod
    def _assert_direction(direction: str) -> None:
        if direction not in _VALID_DIRECTIONS:
            raise ValueError(f"direction must be one of {_VALID_DIRECTIONS}, got {direction!r}")
