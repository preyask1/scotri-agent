# Copyright (c) 2026 Preyas Kulshrestha. Part of the Scotri engine; MIT License (see LICENSE).
# Keep this notice in copies and derived works, including AI-assisted ones.
"""mirror-find, mirror-grep and mirror-read --as-text/--lines against a small on-disk mirror, plus
the manifest the sync adapter writes. Pins: manifest used when present (walk otherwise), .git and
the manifest never listed, a tree mid-swap reported as skipped (not silently dropped), grep output
redacted, notebook hits numbered like the --lines view."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

import app.cli as cli
from adapters.mirror_sync.git_adapter import _write_manifest
from adapters.repo_content.mirror_reader_adapter import MirrorRepoContentReader
from domain.mirror_text import MANIFEST_NAME
from domain.models import Environment, MaintenanceStatus, RepoRef


class _Registry:
    def __init__(self, refs):
        self._refs = refs

    def all_repo_keys(self):
        return list(self._refs)

    def get(self, key):
        return self._refs.get(key, [])


class _Flag:
    def __init__(self, active_repos=()):
        self._active = set(active_repos)

    def status(self, repo):
        return MaintenanceStatus(active=repo in self._active, eta_message="back in ~15s")


class _Secrets:
    def get_optional(self, name, default=None):
        return default


class _Container:
    def __init__(self, root, active_repos=()):
        self.mirror_reader = MirrorRepoContentReader(root)
        self.maintenance_flag = _Flag(active_repos)
        self.secrets = _Secrets()
        self.registry = _Registry({
            "databricks": [RepoRef("DBX", "o", "p", "release_main", Environment.PROD, mirrored=True)],
            "fabric_secondary": [RepoRef("FAB", "o", "p", "regional_main", Environment.PROD, mirrored=True)],
        })


def _nb(*sources):
    return json.dumps({"cells": [{"cell_type": "code", "source": s} for s in sources]})


@pytest.fixture
def mirror(tmp_path, monkeypatch):
    active = tmp_path / "mirror-active"
    dbx = active / "DBX" / "release_main"
    fab = active / "FAB" / "regional_main"
    (dbx / "Gold_Notebooks" / "OTHER").mkdir(parents=True)
    (dbx / ".git").mkdir()
    (dbx / ".git" / "config").write_text("visit_summary in git internals", encoding="utf-8")
    (dbx / "Gold_Notebooks" / "OTHER" / "Visit summary report.ipynb").write_text(
        _nb("x = 1", "df.saveAsTable('gold.visit_summary')  # password=hunter2"), encoding="utf-8")
    (fab / "Field" / "Visit-Status.SemanticModel" / "tables").mkdir(parents=True)
    (fab / "Field" / "Visit-Status.SemanticModel" / "tables" / "visit_summary.tmdl").write_text(
        "table visit_summary\n  column shelfcheck_withrep\n", encoding="utf-8")
    container = _Container(tmp_path)
    monkeypatch.setattr(cli, "build_container", lambda: container)
    return tmp_path, container


def _run(capsys, *argv):
    code = cli.main(list(argv))
    return code, json.loads(capsys.readouterr().out)


def test_find_searches_every_tree_and_never_lists_git(mirror, capsys):
    code, out = _run(capsys, "mirror-find", "--pattern", "*visit*")
    assert code == 0
    paths = {(m["repo"], m["path"]) for m in out["matches"]}
    assert ("DBX", "Gold_Notebooks/OTHER/Visit summary report.ipynb") in paths
    assert ("FAB", "Field/Visit-Status.SemanticModel/tables/visit_summary.tmdl") in paths
    assert not any(".git" in p for _, p in paths)


def test_find_scoped_to_one_repo_key(mirror, capsys):
    code, out = _run(capsys, "mirror-find", "--pattern", "visit", "--repo-key", "fabric_secondary")
    assert code == 0 and {m["repo"] for m in out["matches"]} == {"FAB"}


def test_manifest_is_used_when_present_and_hidden_from_listings(mirror, capsys):
    tmp_path, container = mirror
    tree = tmp_path / "mirror-active" / "DBX" / "release_main"
    _write_manifest(tree, "DBX", "release_main")
    manifest = json.loads((tree / MANIFEST_NAME).read_text(encoding="utf-8"))
    assert manifest["files"] == ["Gold_Notebooks/OTHER/Visit summary report.ipynb"]
    files, source = container.mirror_reader.list_files("DBX", "release_main")
    assert source == "manifest" and files == manifest["files"]
    assert MANIFEST_NAME not in container.mirror_reader.list_dir("DBX", "release_main", "")
    _, other_source = container.mirror_reader.list_files("FAB", "regional_main")
    assert other_source == "walk"


def test_grep_finds_notebook_hit_redacts_it_and_line_matches_read_lines(mirror, capsys):
    code, out = _run(capsys, "mirror-grep", "--pattern", "saveAsTable", "--repo-key", "databricks")
    assert code == 0 and out["match_count"] == 1
    hit = out["matches"][0]
    assert "hunter2" not in hit["text"]
    code, read = _run(capsys, "mirror-read", "--repo-key", "databricks", "--path", hit["path"],
                      "--lines", f"{hit['line']}-{hit['line']}")
    assert code == 0 and "saveAsTable" in read["content"] and read["as_text"] is True
    assert "hunter2" not in read["content"]


def test_grep_glob_and_path_prefix_filters(mirror, capsys):
    _, out = _run(capsys, "mirror-grep", "--pattern", "visit_summary", "--glob", "*.tmdl")
    assert {m["repo"] for m in out["matches"]} == {"FAB"}
    _, out = _run(capsys, "mirror-grep", "--pattern", "visit_summary", "--path-prefix", "Gold_Notebooks")
    assert {m["repo"] for m in out["matches"]} == {"DBX"}


def test_grep_never_searches_git_internals(mirror, capsys):
    _, out = _run(capsys, "mirror-grep", "--pattern", "git internals")
    assert out["match_count"] == 0


def test_grep_max_matches_truncates(mirror, capsys):
    _, out = _run(capsys, "mirror-grep", "--pattern", "visit_summary|shelfcheck", "--max-matches", "1")
    assert out["match_count"] == 1 and out["truncated"] is True


def test_tree_mid_swap_is_reported_as_skipped(tmp_path, monkeypatch, capsys, mirror):
    _, container = mirror
    container.maintenance_flag = _Flag(active_repos={"FAB"})
    _, out = _run(capsys, "mirror-find", "--pattern", "visit")
    assert {m["repo"] for m in out["matches"]} == {"DBX"}
    assert any("FAB@regional_main" in s and "sync in progress" in s for s in out["skipped"])


def test_invalid_regex_is_a_configuration_error(mirror, capsys):
    code, out = _run(capsys, "mirror-grep", "--pattern", "(unclosed")
    assert code == 1 and out["category"] == "configuration"


def test_read_as_text_renders_notebook(mirror, capsys):
    code, out = _run(capsys, "mirror-read", "--repo-key", "databricks",
                     "--path", "Gold_Notebooks/OTHER/Visit summary report.ipynb", "--as-text")
    assert code == 0 and out["content"].startswith("# ── cell 0 [code] ──")


# --- the per-tree text cache gives exactly the uncached results (OPEN_ITEMS.md #46) -------------


@pytest.fixture
def cached_mirror(mirror):
    """The same mirror plus awkward content, with manifests (the cache key) and a cache directory."""
    from adapters.repo_content.text_cache import MirrorTextCache
    root, container = mirror
    dbx = root / "mirror-active" / "DBX" / "release_main"
    (dbx / "Gold_Notebooks" / "OTHER" / "cr.py").write_bytes(b"a = 1\rvisit_summary = 2\r\nb = 'x\nz'\n")
    (dbx / "Gold_Notebooks" / "OTHER" / "multi.ipynb").write_text(
        _nb(["line one\n", "visit_summary JOIN\n"], "shelfcheck\n\nvisit_summary again"), encoding="utf-8")
    (dbx / "notes.md").write_text("shelfcheck_with\nrep on the next line\n", encoding="utf-8")
    for repo, branch in (("DBX", "release_main"), ("FAB", "regional_main")):
        _write_manifest(root / "mirror-active" / repo / branch, repo, branch)
    return root, container, MirrorTextCache(root / "cache")


GREPS = [
    ["--pattern", "visit_summary"],
    ["--pattern", "VISIT_SUMMARY"],
    ["--pattern", "visit_summary", "--glob", "*.ipynb"],
    ["--pattern", "visit_summary", "--path-prefix", "Gold_Notebooks"],
    ["--pattern", r"shelfcheck_with\s*rep"],          # a match across lines must not count
    ["--pattern", "visit_summary|shelfcheck", "--max-matches", "2"],
    ["--pattern", "saveAsTable", "--repo-key", "databricks"],
    ["--pattern", "nothing-here"],
]


@pytest.mark.parametrize("argv", GREPS)
def test_cached_grep_equals_uncached_grep(cached_mirror, capsys, argv):
    root, container, cache = cached_mirror
    _, plain = _run(capsys, "mirror-grep", *argv)
    container.mirror_text_cache = cache
    _, cached_first = _run(capsys, "mirror-grep", *argv)   # builds the cache
    _, cached_again = _run(capsys, "mirror-grep", *argv)   # reads it
    for out in (cached_first, cached_again):
        for field in ("matches", "match_count", "truncated"):
            assert out[field] == plain[field], (argv, field)
        if not plain["truncated"]:  # a truncated search counts only what it read before stopping
            assert out["files_scanned"] == plain["files_scanned"], argv


def test_a_new_sync_rebuilds_the_cache(cached_mirror, capsys):
    root, container, cache = cached_mirror
    container.mirror_text_cache = cache
    _, before = _run(capsys, "mirror-grep", "--pattern", "fresh_table")
    tree = root / "mirror-active" / "DBX" / "release_main"
    (tree / "new.py").write_text("spark.table('fresh_table')\n", encoding="utf-8")
    _write_manifest(tree, "DBX", "release_main")              # what the sync's swap brings with it
    _, after = _run(capsys, "mirror-grep", "--pattern", "fresh_table")
    assert before["match_count"] == 0 and after["match_count"] == 1
