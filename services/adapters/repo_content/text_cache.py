# Copyright (c) 2026 Preyas Kulshrestha. Part of the Scotri engine; MIT License (see LICENSE).
# Keep this notice in copies and derived works, including AI-assisted ones.
"""A per-tree text cache for mirror-grep (OPEN_ITEMS.md #46).

An all-tree mirror-grep reads ~27,700 files (462 MB, 354 MB of it Power BI report JSON) and took
~54 s, almost all of it in per-file opens: Windows small-file I/O, not the regex (a sequential read
of the same files took 121 s; the search itself is cheap). So each tree's searchable files are
written once, in their text view (notebooks rendered as cells, so line numbers match
`mirror-read --as-text --lines`), into one file, and a grep becomes a single memory-mapped regex
scan.

The cache is keyed to the tree's sync manifest (mtime and size): the sync service swaps a new
manifest in with every new tree, so a sync invalidates it and the next grep rebuilds it. It lives
under services/data/, never in the mirror, which only the sync service writes. A tree without a
manifest (synced before manifests existed) is never cached; the caller falls back to reading files.
"""
from __future__ import annotations

import json
import mmap
import os
import re
from bisect import bisect_right
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from domain.mirror_text import MAX_GREP_FILE_BYTES, as_text, is_searchable


@dataclass(frozen=True)
class CachedHit:
    path: str
    line: int
    text: str


def _key(repo: str, branch: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]", "_", f"{repo}@{branch}")


class MirrorTextCache:
    def __init__(self, cache_dir: Path) -> None:
        self._dir = cache_dir

    def _paths(self, repo: str, branch: str) -> tuple[Path, Path]:
        key = _key(repo, branch)
        return self._dir / f"{key}.txt", self._dir / f"{key}.json"

    def load_index(self, repo: str, branch: str, token: str | None) -> dict | None:
        """The cache's index if it matches the tree's current manifest `token`, else None."""
        if token is None:
            return None
        blob, index = self._paths(repo, branch)
        try:
            data = json.loads(index.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        return data if data.get("token") == token and blob.exists() else None

    def build(self, repo: str, branch: str, token: str, files: list[str],
              read: Callable[[str], bytes | None], map_ordered) -> dict:
        """Write the cache for one tree. `read` returns a file's bytes (None to skip it), and
        `map_ordered(fn, items)` maps in input order, typically a thread pool's map. Written to temp
        names and swapped in, so a concurrent reader never sees half a cache."""
        self._dir.mkdir(parents=True, exist_ok=True)
        blob, index = self._paths(repo, branch)
        tmp_blob, tmp_index = blob.with_suffix(f".txt.{os.getpid()}.tmp"), index.with_suffix(f".json.{os.getpid()}.tmp")

        def render(path: str) -> bytes | None:
            data = read(path)
            if data is None:
                return None
            # Normalised to "\n" so counting newlines gives the same line numbers as splitlines(),
            # which mirror-read --lines and the uncached grep use (a lone \r is a line break there).
            text = as_text(path, data.decode("utf-8", errors="replace"))
            return ("\n".join(text.splitlines()) + "\n").encode("utf-8")

        paths, offsets, offset = [], [], 0
        with open(tmp_blob, "wb") as out:
            for path, encoded in zip(files, map_ordered(render, files)):
                if encoded is None:
                    continue
                paths.append(path)
                offsets.append(offset)
                out.write(encoded)
                offset += len(encoded)
        data = {"token": token, "paths": paths, "offsets": offsets, "size": offset}
        tmp_index.write_text(json.dumps(data), encoding="utf-8")
        os.replace(tmp_blob, blob)
        os.replace(tmp_index, index)
        return data

    def search(self, repo: str, branch: str, index: dict, byte_regex: re.Pattern, line_regex: re.Pattern,
               keep: Callable[[str], bool], limit: int, max_chars: int) -> tuple[list[CachedHit], bool]:
        """Lines matching `line_regex`, found by scanning the blob with `byte_regex` and confirming
        each candidate on its own line, so results equal a line-by-line search. Only files for
        which `keep(path)` is true count. Returns (hits, whether more matches exist past `limit`)."""
        blob, _ = self._paths(repo, branch)
        paths, offsets = index["paths"], index["offsets"]
        hits: list[CachedHit] = []
        if not paths or index.get("size", 0) == 0:
            return hits, False
        with open(blob, "rb") as fh, mmap.mmap(fh.fileno(), 0, access=mmap.ACCESS_READ) as mm:
            pos = 0
            while True:
                m = byte_regex.search(mm, pos)
                if m is None:
                    return hits, False
                start = mm.rfind(b"\n", 0, m.start()) + 1
                end = mm.find(b"\n", m.start())
                end = len(mm) if end < 0 else end
                pos = end + 1
                i = bisect_right(offsets, start) - 1
                path = paths[i]
                if not keep(path):
                    # Skip the rest of a file nobody asked about.
                    pos = offsets[i + 1] if i + 1 < len(offsets) else len(mm)
                    continue
                line = mm[start:end].decode("utf-8", errors="replace")
                if not line_regex.search(line):
                    continue
                if len(hits) == limit:
                    return hits, True
                number = mm[offsets[i]:start].count(b"\n") + 1
                hits.append(CachedHit(path, number, line.strip()[:max_chars]))


def ensure_index(cache: MirrorTextCache, reader, repo: str, branch: str, map_ordered,
                 files: list[str] | None = None) -> dict | None:
    """The tree's cache index, built now if it's missing or from an older sync. None when the tree
    has no manifest to key it to. Used by mirror-grep and, right after each swap, by the sync."""
    token = reader.manifest_token(repo, branch) if hasattr(reader, "manifest_token") else None
    if token is None:
        return None
    index = cache.load_index(repo, branch, token)
    if index is not None:
        return index
    if files is None:
        files, _ = reader.list_files(repo, branch)
    searchable = [p for p in files if is_searchable(p)]
    return cache.build(repo, branch, token, searchable,
                       lambda p: reader.read_bytes_capped(repo, branch, p, MAX_GREP_FILE_BYTES), map_ordered)
