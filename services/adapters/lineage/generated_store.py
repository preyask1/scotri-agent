"""Per-tree store of lineage edges generated from the mirror (domain/model_lineage.py).

One gzipped JSON file per mirrored tree under LINEAGE_GENERATED_DIR (default
services/data/lineage-generated/, gitignored with the rest of services/data/). Written after each
sync's swap, read by every index rebuild: the SQLite index (sqlite_lineage_graph.py) is rebuilt from
docs/ai/client/lineage-graph.yaml plus these files, so either side can change without regenerating the
other. Derived data: losing a file costs one `lineage-generate`.
"""
from __future__ import annotations

import gzip
import json
import os
from dataclasses import dataclass, field
from pathlib import Path

from domain.lineage import LineageEdge
from domain.model_lineage import GENERATOR_VERSION, TreeGraph


@dataclass(frozen=True)
class StoredTree:
    tree: str
    manifest_token: str | None
    generated_at: str
    stats: dict
    edges: list[LineageEdge]
    unresolved: list[dict]
    locations: dict[str, str]
    descriptions: dict[str, str] = field(default_factory=dict)
    version: int = 0  # domain.model_lineage.GENERATOR_VERSION it was generated with

    @property
    def current(self) -> bool:
        return self.version == GENERATOR_VERSION


def _file_name(tree: str) -> str:
    return "".join(c if c.isalnum() or c in "@_-." else "_" for c in tree) + ".json.gz"


class GeneratedLineageStore:
    def __init__(self, root: Path) -> None:
        self._root = root

    def save(self, graph: TreeGraph, manifest_token: str | None, generated_at: str) -> Path:
        self._root.mkdir(parents=True, exist_ok=True)
        doc = {
            "version": GENERATOR_VERSION,
            "tree": graph.tree, "manifest_token": manifest_token, "generated_at": generated_at,
            "stats": graph.stats,
            "edges": [[e.src, e.dst, e.rel, e.source_ref] for e in graph.edges],
            "unresolved": [u.to_dict() for u in graph.unresolved],
            "locations": graph.locations,
            "descriptions": graph.descriptions,
        }
        path = self._root / _file_name(graph.tree)
        tmp = path.with_suffix(".tmp")
        with gzip.open(tmp, "wt", encoding="utf-8") as fh:
            json.dump(doc, fh)
        os.replace(tmp, path)
        return path

    def load(self, tree: str) -> StoredTree | None:
        return self._read(self._root / _file_name(tree))

    def load_all(self) -> list[StoredTree]:
        if not self._root.is_dir():
            return []
        out = [self._read(p) for p in sorted(self._root.glob("*.json.gz"))]
        return [t for t in out if t is not None]

    @staticmethod
    def _read(path: Path) -> StoredTree | None:
        try:
            with gzip.open(path, "rt", encoding="utf-8") as fh:
                doc = json.load(fh)
        except (OSError, ValueError):
            return None
        return StoredTree(
            tree=doc["tree"], manifest_token=doc.get("manifest_token"),
            generated_at=doc.get("generated_at", ""), stats=doc.get("stats", {}),
            edges=[LineageEdge(src=s, dst=d, rel=r, verified=True, source_ref=ref)
                   for s, d, r, ref in doc.get("edges", [])],
            unresolved=doc.get("unresolved", []),
            locations=doc.get("locations", {}),
            descriptions=doc.get("descriptions", {}),
            version=doc.get("version", 0),
        )
