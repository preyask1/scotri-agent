"""domain/mirror_freshness.py — turns status.json into an advisory staleness verdict so a stale Prod
mirror can't be read and presented as current (OPEN_ITEMS.md #31). Pure, so tested directly."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from domain.mirror_freshness import assess_freshness

NOW = datetime(2026, 9, 30, 12, 0, 0, tzinfo=timezone.utc)


def _status(last_run_at, repos=None):
    return {"last_run_at": last_run_at, "overall_ok": True, "repos": repos or []}


def _iso(dt):
    return dt.isoformat()


def test_fresh_mirror_has_no_warning():
    st = _status(_iso(NOW - timedelta(hours=2)),
                 [{"repo": "adf", "branch": "release_main", "ok": True, "checked_at": _iso(NOW - timedelta(hours=2))}])
    f = assess_freshness(st, "adf", "release_main", NOW, warn_after_seconds=90_000)
    assert f.available is True and f.stale is False and f.warning is None
    assert f.repo_ok is True and 7000 < f.age_seconds < 7400


def test_stale_mirror_warns():
    st = _status(_iso(NOW - timedelta(hours=40)),
                 [{"repo": "adf", "branch": "release_main", "ok": True, "checked_at": _iso(NOW - timedelta(hours=40))}])
    f = assess_freshness(st, "adf", "release_main", NOW, warn_after_seconds=90_000)  # 25h threshold
    assert f.stale is True and f.warning is not None and "stale" in f.warning


def test_missing_status_is_unknown_not_fresh():
    f = assess_freshness(None, "adf", "release_main", NOW)
    assert f.available is False and f.stale is False
    assert "unknown" in f.warning


def test_failed_repo_sync_warns_even_when_recent():
    st = _status(_iso(NOW - timedelta(hours=1)),
                 [{"repo": "adf", "branch": "release_main", "ok": False, "checked_at": _iso(NOW - timedelta(hours=1))}])
    f = assess_freshness(st, "adf", "release_main", NOW, warn_after_seconds=90_000)
    assert f.repo_ok is False
    assert f.warning is not None and "FAILED" in f.warning


def test_falls_back_to_overall_timestamp_when_no_repo_row():
    st = _status(_iso(NOW - timedelta(hours=40)), repos=[])  # no per-repo row
    f = assess_freshness(st, "adf", "release_main", NOW, warn_after_seconds=90_000)
    assert f.available is True and f.repo_ok is None
    assert f.stale is True  # age still computed from last_run_at


def test_unparseable_timestamp_is_unknown():
    f = assess_freshness(_status("not-a-timestamp"), "adf", "release_main", NOW)
    assert f.available is False and "unparseable" in f.warning
