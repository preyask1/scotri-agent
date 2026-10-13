# Copyright (c) 2026 Preyas Kulshrestha. Part of the Scotri engine; MIT License (see LICENSE).
# Keep this notice in copies and derived works, including AI-assisted ones.
"""Filesystem adapter reading from the local read-only mirror (fabric-read / adf-databricks-read
SKILL.md, Path A — Prod). No network, no credentials — just mirror-active/<repo>/<branch>/<path>.

This adapter never writes. There is no method here that could write even by accident — that's a
deliberate omission, not an oversight.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

from domain.errors import NotFoundError
from domain.mirror_text import MANIFEST_NAME


class MirrorRepoContentReader:
    """Implements ports.repo_content.RepoContentReader, reading from mirror-active/."""

    def __init__(self, mirror_root: Path) -> None:
        # Must be resolved (absolute) — the escape-guard below compares against resolved.parents,
        # which are always absolute; an unresolved/relative _active_root could never match, making
        # the guard permanently (and silently) reject every legitimate path. Confirmed 2026-09-17,
        # the first time this method was actually exercised against real mirrored content.
        self._active_root = (mirror_root / "mirror-active").resolve()
        self._token_cache: dict[tuple, str] = {}  # (manifest path, mtime_ns, size) -> content hash

    def _resolve(self, repo: str, branch: str, path: str) -> Path:
        # A leading "/" (meaning "the repo/branch root itself", e.g. from a caller passing "/" for
        # the top-level listing) must NOT be treated as an absolute-path anchor by pathlib's `/`
        # operator — on Windows that collapses the whole joined path down to the drive root.
        # Confirmed 2026-09-17. Strip it so "/" and "" both mean "this directory itself."
        relative = path.lstrip("/\\")
        resolved = (self._active_root / repo / branch / relative).resolve()
        if self._active_root not in resolved.parents and resolved != self._active_root:
            raise ValueError(f"Path escapes the mirror root: {resolved}")
        return resolved

    def list_dir(self, repo: str, branch: str, path: str) -> list[str]:
        # A missing repo/branch/path must be a loud NOT_FOUND, never a silent `[]` — a caller (a
        # skill, an impact analysis) cannot otherwise tell "this directory is genuinely empty" from
        # "you asked for a repo/branch/path that doesn't exist," and the latter silently narrowing
        # an analysis without saying so is exactly the failure agent-instructions.md warns against.
        target = self._resolve(repo, branch, path)
        if not target.exists():
            raise NotFoundError(f"not found in mirror: {repo}/{branch}/{path or '.'}")
        if not target.is_dir():
            raise NotFoundError(f"not a directory in mirror: {repo}/{branch}/{path or '.'}")
        return sorted(p.name for p in target.iterdir() if p.name != MANIFEST_NAME)

    def read_file(self, repo: str, branch: str, path: str) -> str:
        target = self._resolve(repo, branch, path)
        if not target.exists():
            raise NotFoundError(f"not found in mirror: {repo}/{branch}/{path}")
        return target.read_text(encoding="utf-8")

    def has_branch(self, repo: str, branch: str) -> bool:
        return (self._active_root / repo / branch).is_dir()

    def list_files(self, repo: str, branch: str) -> tuple[list[str], str]:
        """Every file in the tree as a repo-relative POSIX path, sorted, plus where the list came
        from: "manifest" (written by the sync service before the swap) or "walk" (a tree synced
        before manifests existed, or one whose manifest is unreadable). .git is never included."""
        root = self._resolve(repo, branch, "")
        if not root.is_dir():
            raise NotFoundError(f"not found in mirror: {repo}/{branch}")
        try:
            manifest = json.loads((root / MANIFEST_NAME).read_text(encoding="utf-8"))
            files = manifest["files"]
            if isinstance(files, list):
                return files, "manifest"
        except (OSError, ValueError, KeyError, TypeError):
            pass
        return walk_tree(root), "walk"

    def manifest_token(self, repo: str, branch: str) -> str | None:
        """Identifies the tree's current sync, for caches keyed to it (the mirror-grep text cache,
        generated lineage): a hash of the manifest's content, which names the sync time and every
        file. Content, not mtime, so a mirror copied elsewhere (hydrated from a Blob snapshot on
        Azure, OPEN_ITEMS.md #15) keeps its identity and its caches. Hashed once per manifest
        version (stat), so repeated calls cost a stat. None when the tree has no manifest."""
        try:
            path = self._resolve(repo, branch, "") / MANIFEST_NAME
            st = path.stat()
        except (OSError, ValueError):
            return None
        key = (str(path), st.st_mtime_ns, st.st_size)
        token = self._token_cache.get(key)
        if token is None:
            try:
                token = "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()[:32]
            except OSError:
                return None
            self._token_cache[key] = token
        return token

    def read_bytes_capped(self, repo: str, branch: str, path: str, max_bytes: int) -> bytes | None:
        """The file's bytes, or None if it is larger than `max_bytes` or can't be opened. One open
        and one read — no separate stat — because on Windows the per-file open is what a search
        over ~20k mirror files actually pays for."""
        try:
            with open(self._resolve(repo, branch, path), "rb") as fh:
                data = fh.read(max_bytes + 1)
        except OSError:
            return None
        return None if len(data) > max_bytes else data

    def read_status(self) -> dict | None:
        """The sync health-status snapshot (`status.json`, sibling of `mirror-active/`, written by
        the sync service via app/status_writer.py) — or None if it's absent/unreadable/not JSON.
        Read-only, best-effort: a mirror read must never fail just because freshness can't be read
        (OPEN_ITEMS.md #31). The judgment lives in domain/mirror_freshness.py, not here."""
        status_path = self._active_root.parent / "status.json"
        try:
            return json.loads(status_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None


def walk_tree(root: Path) -> list[str]:
    """Repo-relative POSIX paths of every file under `root`, skipping .git and the manifest. Shared
    with the sync adapter, which writes the manifest from this same walk."""
    files = []
    for directory, dirs, names in os.walk(root):
        if ".git" in dirs:
            dirs.remove(".git")
        rel_dir = Path(directory).relative_to(root).as_posix()
        for name in names:
            if name == MANIFEST_NAME:
                continue
            files.append(name if rel_dir == "." else f"{rel_dir}/{name}")
    return sorted(files)
