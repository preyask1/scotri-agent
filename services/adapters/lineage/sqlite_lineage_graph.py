"""SQLite lineage graph adapter (OPEN_ITEMS.md item #37) — implements ports.lineage_graph.LineageGraph.

Storage is a single SQLite file with recursive-CTE traversal, exactly the design note's "do not buy
Neo4j on day one" recommendation (docs/ai/retrieval-and-graph-design.md §1): at a handful of repos this fits
in SQLite with no new infrastructure, credential or vendor. The file is a *derived index* rebuilt
from the mirror-generated lineage plus docs/ai/client/lineage-graph.yaml, not a system of record — so losing it (e.g. an AKS pod restart)
costs nothing but a rebuild.

No external service, no credential — so this adapter is unit-tested directly against a temp file
(tests/test_sqlite_lineage_graph.py), unlike the network adapters.
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from domain.lineage import FLOW_REVERSED_RELS, Coverage, LineageEdge, LineageNode, NodeNote, Traversal

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
    up         TEXT,
    down       TEXT,
    PRIMARY KEY (src, dst, rel)
);
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS node_notes (
    node_id    TEXT NOT NULL,
    kind       TEXT NOT NULL,
    title      TEXT NOT NULL,
    text       TEXT NOT NULL,
    source_ref TEXT NOT NULL,
    verified   INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_notes_node ON node_notes (node_id);
CREATE INDEX IF NOT EXISTS idx_nodes_kind ON nodes (kind);
CREATE INDEX IF NOT EXISTS idx_edges_src ON edges (src);
CREATE INDEX IF NOT EXISTS idx_edges_dst ON edges (dst);
"""
# `up`/`down` are the edge in data-flow direction (src/dst, swapped for FLOW_REVERSED_RELS), stored
# and indexed so the recursive traversal can use an index on every hop.
_FLOW_INDEXES = """
CREATE INDEX IF NOT EXISTS idx_edges_up ON edges (up);
CREATE INDEX IF NOT EXISTS idx_edges_down ON edges (down);
"""


def _flow(src: str, dst: str, rel: str) -> tuple[str, str]:
    return (dst, src) if rel in FLOW_REVERSED_RELS else (src, dst)

_VALID_DIRECTIONS = ("downstream", "upstream")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class SqliteLineageGraph:
    def __init__(self, db_path: Path) -> None:
        self._db_path = db_path
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as conn:
            conn.executescript(_SCHEMA)
            cols = {r["name"] for r in conn.execute("PRAGMA table_info(edges)")}
            if "up" not in cols:  # an index built before flow columns existed
                conn.execute("ALTER TABLE edges ADD COLUMN up TEXT")
                conn.execute("ALTER TABLE edges ADD COLUMN down TEXT")
            if conn.execute("SELECT 1 FROM edges WHERE up IS NULL LIMIT 1").fetchone():
                rows = conn.execute("SELECT src, dst, rel FROM edges").fetchall()
                conn.executemany("UPDATE edges SET up = ?, down = ? WHERE src = ? AND dst = ? AND rel = ?",
                                 [(*_flow(r["src"], r["dst"], r["rel"]), r["src"], r["dst"], r["rel"])
                                  for r in rows])
            conn.executescript(_FLOW_INDEXES)

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

    def rebuild(self, nodes: list[LineageNode], edges: list[LineageEdge], source_file: str | None,
                generated: dict | None = None, notes: list[NodeNote] | None = None,
                meta: dict | None = None) -> None:
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
                edge_rows.append((e.src, e.dst, e.rel, verified_int, e.source_ref, updated_at,
                                  *_flow(e.src, e.dst, e.rel)))
            conn.executemany(
                "INSERT OR REPLACE INTO edges (src, dst, rel, verified, source_ref, updated_at, up, down) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                edge_rows,
            )
            conn.execute("INSERT OR REPLACE INTO meta (key, value) VALUES ('last_build_at', ?)", (now,))
            conn.execute("INSERT OR REPLACE INTO meta (key, value) VALUES ('source_file', ?)", (source_file or "",))
            conn.execute("INSERT OR REPLACE INTO meta (key, value) VALUES ('generated', ?)",
                         (json.dumps(generated or {}),))
            for key, value in (meta or {}).items():
                conn.execute("INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)", (key, json.dumps(value)))
            conn.execute("DELETE FROM node_notes")
            conn.executemany(
                "INSERT INTO node_notes (node_id, kind, title, text, source_ref, verified) VALUES (?, ?, ?, ?, ?, ?)",
                [(n.node_id, n.kind, n.title, n.text, n.source_ref, 1 if n.verified else 0) for n in notes or []])
            conn.commit()

    # --- query -----------------------------------------------------------------------------

    def neighbors(self, node_id: str, direction: str) -> list[LineageEdge]:
        self._assert_direction(direction)
        col = "up" if direction == "downstream" else "down"
        with self._connect() as conn:
            rows = conn.execute(
                f"SELECT src, dst, rel, verified, source_ref FROM edges WHERE {col} = ?", (node_id,)
            ).fetchall()
        return [self._row_to_edge(r) for r in rows]

    def blast_radius(self, node_id: str, direction: str, max_hops: int) -> Traversal:
        self._assert_direction(direction)
        # Data-flow direction: downstream follows up->down; upstream follows down->up.
        from_col, to_col = ("up", "down") if direction == "downstream" else ("down", "up")
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
            reached_set = {r["node"] for r in conn.execute(cte, (node_id, max_hops)).fetchall()}
            # Edges of the reached subgraph: both endpoints reached.
            conn.execute("CREATE TEMP TABLE reached (node TEXT PRIMARY KEY)")
            conn.executemany("INSERT INTO reached (node) VALUES (?)", [(n,) for n in reached_set])
            sub_edges = [self._row_to_edge(r) for r in conn.execute(
                "SELECT src, dst, rel, verified, source_ref FROM edges "
                "WHERE src IN (SELECT node FROM reached) AND dst IN (SELECT node FROM reached)")]
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
            generated=json.loads(meta.get("generated") or "{}"),
            kpi=json.loads(meta.get("kpi") or "{}"),
        )

    def find_nodes(self, kind: str, needle: str, limit: int) -> list[str]:
        """Node ids of `kind` whose id contains `needle` (case-insensitive), plus nodes with a note
        whose title contains it (a KPI name that differs from the measure's), exact name first."""
        escaped = needle.lower().replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        like = f"%{escaped}%"  # a KPI name like `Growth%` is literal text, not a wildcard
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT id FROM nodes WHERE kind = ? AND lower(name) LIKE ? ESCAPE '\\' "
                "UNION SELECT n.id FROM nodes n JOIN node_notes nn ON nn.node_id = n.id "
                "WHERE n.kind = ? AND lower(nn.title) LIKE ? ESCAPE '\\'", (kind, like, kind, like)).fetchall()
        ids = [r[0] for r in rows]
        exact = f"[{needle.lower()}]"
        ids.sort(key=lambda i: (not i.lower().endswith(exact), len(i), i))
        return ids[:limit]

    def notes_for(self, node_ids: list[str]) -> list[NodeNote]:
        if not node_ids:
            return []
        with self._connect() as conn:
            conn.execute("CREATE TEMP TABLE IF NOT EXISTS wanted (node TEXT PRIMARY KEY)")
            conn.execute("DELETE FROM wanted")
            conn.executemany("INSERT OR IGNORE INTO wanted (node) VALUES (?)", [(n,) for n in node_ids])
            rows = conn.execute(
                "SELECT node_id, kind, title, text, source_ref, verified FROM node_notes "
                "WHERE node_id IN (SELECT node FROM wanted)").fetchall()
        return [NodeNote(r["node_id"], r["kind"], r["title"], r["text"], r["source_ref"], bool(r["verified"]))
                for r in rows]

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
