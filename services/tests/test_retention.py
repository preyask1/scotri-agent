"""OPEN_ITEMS.md #6(a): expired uploads, staged copies and published deliverables are purged, and
only when an operator asks."""
from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from adapters.artifact_store.local_fs_adapter import LocalFsArtifactStore
from domain.errors import ConfigurationError
from domain.retention import RetentionPolicy, parse_days, purge_expired
from domain.tool_scope import check_bash_command

NOW = datetime(2026, 10, 6, tzinfo=timezone.utc)


def _backdate(store_root: Path, key: str, days: int) -> None:
    meta = store_root / "meta" / (key + ".json")
    data = json.loads(meta.read_text())
    data["created_at"] = (NOW - timedelta(days=days)).isoformat()
    meta.write_text(json.dumps(data))


@pytest.fixture
def setup(tmp_path):
    root = tmp_path / "store"
    store = LocalFsArtifactStore(root)
    for key, age in [("uploads/old1/a.md", 40), ("uploads/new1/b.md", 2),
                     ("engagements/oldjob/scratch/ACME-1-handoff.md", 100),
                     ("engagements/newjob/scratch/ACME-2-handoff.md", 5)]:
        store.put(key, b"x" * 10, "text/markdown", {"owner": "alice"})
        _backdate(root, key, age)
    ws = tmp_path / "ws"
    for job, age in [("oldjob", 40), ("newjob", 1)]:
        d = ws / "scratch" / "uploads" / job
        d.mkdir(parents=True)
        (d / "brd.pdf").write_bytes(b"y" * 7)
        t = (NOW - timedelta(days=age)).timestamp()
        os.utime(d, (t, t))
    return store, root, ws


def test_dry_run_lists_without_deleting(setup):
    store, _, ws = setup
    report = purge_expired(store, ws, RetentionPolicy(upload_days=30, shared_file_days=90), now=NOW)
    assert report.uploads == ["uploads/old1/a.md"]
    assert report.shared_files == ["engagements/oldjob/scratch/ACME-1-handoff.md"]
    assert report.staged_dirs == ["scratch/uploads/oldjob"]
    assert report.bytes_freed == 27
    assert store.head("uploads/old1/a.md") is not None and (ws / "scratch/uploads/oldjob").is_dir()


def test_confirmed_run_deletes_only_what_expired(setup):
    store, root, ws = setup
    purge_expired(store, ws, RetentionPolicy(upload_days=30, shared_file_days=90), now=NOW, dry_run=False)
    assert store.head("uploads/old1/a.md") is None
    assert store.head("engagements/oldjob/scratch/ACME-1-handoff.md") is None
    assert store.head("uploads/new1/b.md") is not None
    assert store.head("engagements/newjob/scratch/ACME-2-handoff.md") is not None
    assert not (ws / "scratch/uploads/oldjob").exists() and (ws / "scratch/uploads/newjob").is_dir()
    assert not (root / "blobs" / "uploads" / "old1").exists()  # empty folders are pruned


def test_each_setting_is_independent(setup):
    store, _, ws = setup
    report = purge_expired(store, ws, RetentionPolicy(shared_file_days=90), now=NOW, dry_run=False)
    assert report.uploads == [] and report.staged_dirs == []
    assert store.head("uploads/old1/a.md") is not None


@pytest.mark.parametrize("raw,expected", [(None, None), ("", None), ("0", None), (" 30 ", 30)])
def test_parse_days(raw, expected):
    assert parse_days("X", raw) == expected


@pytest.mark.parametrize("raw", ["-1", "1.5", "thirty"])
def test_bad_settings_are_refused(raw):
    with pytest.raises(ConfigurationError):
        parse_days("X", raw)


def test_delete_reports_whether_anything_was_there(tmp_path):
    store = LocalFsArtifactStore(tmp_path / "s")
    store.put("uploads/u/a.md", b"1", "text/markdown")
    assert store.delete("uploads/u/a.md") is True
    assert store.delete("uploads/u/a.md") is False


def test_engagements_cannot_run_the_purge(tmp_path):
    (tmp_path / "services/.venv/bin").mkdir(parents=True)
    cmd = "cd services && .venv/bin/python -m app.cli artifacts-purge --confirmed"
    assert "ops-only" in check_bash_command(cmd, str(tmp_path))
