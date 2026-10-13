# Copyright (c) 2026 Preyas Kulshrestha. Part of the Scotri engine; MIT License (see LICENSE).
# Keep this notice in copies and derived works, including AI-assisted ones.
"""Admin overview additions: which code the API process runs (and whether it needs a restart), and
each mirrored tree's generated-lineage state. Both exist because on 2026-10-13 a sync started from an
API process older than the code skipped the new lineage step, and nothing on the panel showed it."""
from __future__ import annotations

from types import SimpleNamespace

from app.admin_routes import _lineage_state, git_head


def _repo(root, head="ref: refs/heads/main", ref_sha="a" * 40, packed=None):
    git = root / ".git"
    (git / "refs" / "heads").mkdir(parents=True)
    (git / "HEAD").write_text(head + "\n", encoding="utf-8")
    if ref_sha:
        (git / "refs" / "heads" / "main").write_text(ref_sha + "\n", encoding="utf-8")
    if packed:
        (git / "packed-refs").write_text(packed, encoding="utf-8")
    return git


def test_git_head_follows_a_branch_ref(tmp_path, monkeypatch):
    monkeypatch.delenv("SCOTRI_BUILD_COMMIT", raising=False)
    _repo(tmp_path)
    assert git_head(tmp_path) == "a" * 40


def test_git_head_reads_packed_refs_and_detached_heads(tmp_path, monkeypatch):
    monkeypatch.delenv("SCOTRI_BUILD_COMMIT", raising=False)
    _repo(tmp_path / "packed", ref_sha=None, packed="# pack-refs\n" + "b" * 40 + " refs/heads/main\n")
    assert git_head(tmp_path / "packed") == "b" * 40
    _repo(tmp_path / "detached", head="c" * 40, ref_sha=None)
    assert git_head(tmp_path / "detached") == "c" * 40


def test_git_head_follows_a_worktree_and_prefers_the_build_env(tmp_path, monkeypatch):
    monkeypatch.delenv("SCOTRI_BUILD_COMMIT", raising=False)
    main = _repo(tmp_path / "main")
    wt = main / "worktrees" / "wt"
    wt.mkdir(parents=True)
    (wt / "HEAD").write_text("ref: refs/heads/main\n", encoding="utf-8")
    (wt / "commondir").write_text("../..\n", encoding="utf-8")
    (tmp_path / "checkout").mkdir()
    (tmp_path / "checkout" / ".git").write_text(f"gitdir: {wt}\n", encoding="utf-8")
    assert git_head(tmp_path / "checkout") == "a" * 40
    monkeypatch.setenv("SCOTRI_BUILD_COMMIT", "d" * 40)
    assert git_head(tmp_path / "nowhere") == "d" * 40


def test_git_head_none_without_a_checkout(tmp_path, monkeypatch):
    monkeypatch.delenv("SCOTRI_BUILD_COMMIT", raising=False)
    assert git_head(tmp_path) is None


def _container(stored, token):
    return SimpleNamespace(
        lineage_generated_store=SimpleNamespace(load=lambda tree: stored),
        mirror_reader=SimpleNamespace(manifest_token=lambda repo, branch: token),
        tree_label=lambda ref: "k@main")


def test_lineage_state_mirrors_the_start_up_check():
    ref = SimpleNamespace(repo="R", branch="main")
    stored = lambda token, current=True: SimpleNamespace(manifest_token=token, current=current, generated_at="t")  # noqa: E731
    assert _lineage_state(_container(None, "x"), ref) == {"state": "missing"}
    assert _lineage_state(_container(stored("x"), "x"), ref)["state"] == "current"
    assert _lineage_state(_container(stored("old"), "x"), ref)["state"] == "stale"
    assert _lineage_state(_container(stored("x", current=False), "x"), ref)["state"] == "old_generator"
