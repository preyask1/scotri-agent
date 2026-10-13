# Copyright (c) 2026 Preyas Kulshrestha. Part of the Scotri engine; MIT License (see LICENSE).
# Keep this notice in copies and derived works, including AI-assisted ones.
"""MirrorRepoContentReader.manifest_token — a tree's sync identity, keyed on manifest content.

The property that matters (OPEN_ITEMS.md #15): a mirror copied to another place, as a container
hydrating from a Blob snapshot does, gets new mtimes but is the same sync, so the caches keyed to
it (mirror-grep text cache, generated lineage) must stay valid. A new sync must change it."""
from __future__ import annotations

import json
import os
import shutil

from adapters.repo_content.mirror_reader_adapter import MirrorRepoContentReader
from domain.mirror_text import MANIFEST_NAME


def _tree(root, generated_at="2026-10-12T00:00:00+00:00", files=("a.py",)):
    tree = root / "mirror-active" / "Repo" / "main"
    tree.mkdir(parents=True)
    (tree / MANIFEST_NAME).write_text(json.dumps(
        {"repo": "Repo", "branch": "main", "generated_at": generated_at, "file_count": len(files),
         "files": list(files)}), encoding="utf-8")
    return tree


def test_same_manifest_content_same_token_whatever_the_mtime(tmp_path):
    a = _tree(tmp_path / "host1")
    shutil.copytree(tmp_path / "host1", tmp_path / "host2")
    os.utime(tmp_path / "host2" / "mirror-active" / "Repo" / "main" / MANIFEST_NAME, (1, 1))  # "hydrated" later
    t1 = MirrorRepoContentReader(tmp_path / "host1").manifest_token("Repo", "main")
    t2 = MirrorRepoContentReader(tmp_path / "host2").manifest_token("Repo", "main")
    assert t1 == t2 and t1.startswith("sha256:")
    assert a.exists()


def test_a_new_sync_changes_the_token(tmp_path):
    tree = _tree(tmp_path)
    reader = MirrorRepoContentReader(tmp_path)
    before = reader.manifest_token("Repo", "main")
    (tree / MANIFEST_NAME).write_text(json.dumps(
        {"repo": "Repo", "branch": "main", "generated_at": "2026-10-13T00:00:00+00:00", "file_count": 1,
         "files": ["a.py"]}), encoding="utf-8")
    assert reader.manifest_token("Repo", "main") != before


def test_no_manifest_no_token(tmp_path):
    (tmp_path / "mirror-active" / "Repo" / "main").mkdir(parents=True)
    assert MirrorRepoContentReader(tmp_path).manifest_token("Repo", "main") is None
    assert MirrorRepoContentReader(tmp_path).manifest_token("Nope", "main") is None
