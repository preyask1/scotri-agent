# Copyright (c) 2026 Preyas Kulshrestha. Part of the Scotri engine; MIT License (see LICENSE).
# Keep this notice in copies and derived works, including AI-assisted ones.
"""domain/sync_policy.py — the atomic-swap sequencing the whole read-only-mirror premise rests on,
tested with fakes (no git/filesystem). Covers refresh_one ordering + flag-always-cleared, the
non-mirrored guard, refresh_all's continue-past-failure + skip-non-mirrored, the status report
shape, refresh_registry_once's is_ready_to_sync filter, and the sync lock every run path holds."""
from __future__ import annotations

import pytest

from domain.errors import TransientError
from domain.models import Environment, RepoRef
from domain.sync_policy import (
    SWAP_PERCENT,
    SyncInProgressError,
    build_status_report,
    refresh_all,
    refresh_one,
    refresh_registry_once,
    stage_percent,
)


def _ref(repo="adf", branch="release_main", mirrored=True, status=None):
    return RepoRef(repo=repo, org="acme-data", project="proj", branch=branch,
                   environment=Environment.PROD, mirrored=mirrored, status=status)


class FakeSyncTarget:
    def __init__(self, events, fail_on_swap=(), stages=()):
        self.events = events
        self._fail_on_swap = set(fail_on_swap)
        self.stages = stages  # (stage, percent) pairs reported during the fetch

    def fetch_into_staging(self, repo, branch, org, project, on_progress=None):
        self.events.append(("fetch", repo))
        for stage, pct in self.stages:
            on_progress(stage, pct)

    def swap_to_active(self, repo, branch):
        self.events.append(("swap", repo))
        if repo in self._fail_on_swap:
            raise TransientError(f"swap failed for {repo}")


class FakeFlag:
    def __init__(self, events):
        self.events = events

    def set(self, repo, eta_seconds):
        self.events.append(("set", repo))

    def clear(self, repo):
        self.events.append(("clear", repo))


class FakeLock:
    def __init__(self, events, held_by=None):
        self.events = events
        self.held_by = held_by

    def acquire(self, holder, ttl_seconds):
        if self.held_by is not None:
            return None
        self.held_by = holder
        self.events.append(("lock", holder))
        return "tok"

    def extend(self, token, ttl_seconds):
        self.events.append(("extend",))
        return True

    def release(self, token):
        self.held_by = None
        self.events.append(("unlock",))

    def holder(self):
        return self.held_by


class FakeRegistry:
    def __init__(self, mapping):
        self._mapping = mapping

    def all_repo_keys(self):
        return list(self._mapping.keys())

    def get(self, key):
        return self._mapping[key]


# --- refresh_one ---------------------------------------------------------------------------

def test_refresh_one_orders_fetch_flag_swap_clear():
    events = []
    refresh_one(_ref(), FakeSyncTarget(events), FakeFlag(events))
    assert events == [("fetch", "adf"), ("set", "adf"), ("swap", "adf"), ("clear", "adf")]


def test_refresh_one_clears_flag_even_when_swap_fails():
    events = []
    with pytest.raises(TransientError):
        refresh_one(_ref(), FakeSyncTarget(events, fail_on_swap={"adf"}), FakeFlag(events))
    assert ("clear", "adf") in events            # flag released despite the swap raising
    assert events[-1] == ("clear", "adf")        # and it was the last thing to happen


def test_refresh_one_rejects_non_mirrored_ref():
    events = []
    with pytest.raises(ValueError):
        refresh_one(_ref(mirrored=False), FakeSyncTarget(events), FakeFlag(events))


# --- refresh_all ---------------------------------------------------------------------------

def test_refresh_all_continues_past_a_single_failure():
    events = []
    refs = [_ref(repo="adf"), _ref(repo="databricks")]
    outcomes = refresh_all(refs, FakeSyncTarget(events, fail_on_swap={"adf"}), FakeFlag(events))
    assert len(outcomes) == 2
    by_repo = {o.repo: o for o in outcomes}
    assert by_repo["adf"].ok is False and by_repo["adf"].category == "transient"
    assert by_repo["databricks"].ok is True


def test_refresh_all_skips_non_mirrored_refs():
    events = []
    refs = [_ref(repo="adf", mirrored=True), _ref(repo="devrepo", mirrored=False)]
    outcomes = refresh_all(refs, FakeSyncTarget(events), FakeFlag(events))
    assert [o.repo for o in outcomes] == ["adf"]   # non-mirrored produced no outcome at all


# --- build_status_report -------------------------------------------------------------------

def test_status_report_overall_ok_true_when_all_ok():
    events = []
    outcomes = refresh_all([_ref(repo="adf"), _ref(repo="databricks")], FakeSyncTarget(events), FakeFlag(events))
    report = build_status_report(outcomes)
    assert report["overall_ok"] is True
    assert "last_run_at" in report and len(report["repos"]) == 2
    assert all("checked_at" in row for row in report["repos"])


def test_status_report_overall_ok_false_on_any_failure():
    events = []
    outcomes = refresh_all([_ref(repo="adf"), _ref(repo="databricks")],
                           FakeSyncTarget(events, fail_on_swap={"adf"}), FakeFlag(events))
    assert build_status_report(outcomes)["overall_ok"] is False


# --- refresh_registry_once -----------------------------------------------------------------

def test_refresh_registry_once_syncs_only_ready_refs():
    events = []
    registry = FakeRegistry({
        "adf": [_ref(repo="adf", mirrored=True)],                              # ready
        "planned": [_ref(repo="planned", mirrored=True, status="planned_not_live")],  # not ready
        "dev": [_ref(repo="dev", mirrored=False)],                             # not ready
    })
    outcomes = refresh_registry_once(registry, FakeSyncTarget(events), FakeFlag(events),
                                     FakeLock(events), holder="test")
    assert [o.repo for o in outcomes] == ["adf"]


# --- sync lock -----------------------------------------------------------------------------

def test_refresh_registry_once_holds_the_lock_for_the_whole_run_and_renews_per_repo():
    events = []
    registry = FakeRegistry({"adf": [_ref(repo="adf")], "databricks": [_ref(repo="databricks")]})
    lock = FakeLock(events)
    refresh_registry_once(registry, FakeSyncTarget(events), FakeFlag(events), lock, holder="sync-service")
    assert events[0] == ("lock", "sync-service") and events[-1] == ("unlock",)
    assert events.count(("extend",)) == 2
    assert lock.held_by is None


def test_refresh_registry_once_refuses_when_another_run_holds_the_lock():
    events = []
    registry = FakeRegistry({"adf": [_ref(repo="adf")]})
    with pytest.raises(SyncInProgressError) as exc:
        refresh_registry_once(registry, FakeSyncTarget(events), FakeFlag(events),
                              FakeLock(events, held_by="admin:ops-1"), holder="cli:sync-run-once")
    assert "admin:ops-1" in exc.value.user_message
    assert events == []  # nothing fetched, nothing swapped, the other run's lock untouched


def test_refresh_registry_once_releases_the_lock_when_a_repo_fails():
    events = []
    registry = FakeRegistry({"adf": [_ref(repo="adf")]})
    lock = FakeLock(events)
    outcomes = refresh_registry_once(registry, FakeSyncTarget(events, fail_on_swap={"adf"}),
                                     FakeFlag(events), lock, holder="t")
    assert not outcomes[0].ok and lock.held_by is None and events[-1] == ("unlock",)


def test_refresh_registry_once_releases_the_lock_on_an_unexpected_error():
    events = []

    class BrokenRegistry(FakeRegistry):
        def all_repo_keys(self):
            raise RuntimeError("registry unreadable")

    lock = FakeLock(events)
    with pytest.raises(RuntimeError):
        refresh_registry_once(BrokenRegistry({}), FakeSyncTarget(events), FakeFlag(events), lock, holder="t")
    assert lock.held_by is None


# --- RedisSyncLock (adapter) ---------------------------------------------------------------

class _FakeRedis:
    """Just enough of redis.Redis for RedisSyncLock: get, set(nx, ex) and the adapter's two scripts,
    emulated by what they do (compare the value, then expire or delete)."""

    def __init__(self):
        self.data, self.ttl = {}, {}

    def get(self, key):
        v = self.data.get(key)
        return v.encode() if v is not None else None

    def set(self, key, value, nx=False, ex=None):
        if nx and key in self.data:
            return None
        self.data[key], self.ttl[key] = value, ex
        return True

    def eval(self, script, numkeys, key, value, *args):
        if self.data.get(key) != value:
            return 0
        if "expire" in script:
            self.ttl[key] = int(args[0])
        else:
            del self.data[key]
        return 1


def test_redis_sync_lock_is_exclusive_and_token_checked():
    from adapters.redis.sync_lock_adapter import RedisSyncLock

    r = _FakeRedis()
    lock = RedisSyncLock(r)
    first = lock.acquire("sync-service", 60)
    assert first and lock.holder() == "sync-service"
    assert lock.acquire("admin:ops-1", 60) is None
    assert lock.extend(first, 120) and r.ttl["mirror:sync-lock"] == 120
    lock.release("someone-elses-token")       # a stale run can't drop the current lock
    assert lock.holder() == "sync-service"
    assert lock.extend("someone-elses-token", 60) is False
    lock.release(first)
    assert lock.holder() is None
    second = lock.acquire("admin:ops-1", 60)
    assert second and second != first and lock.holder() == "admin:ops-1"



# --- progress, partial runs ----------------------------------------------------------------

class _Recorder:
    def __init__(self):
        self.calls = []

    def ref_started(self, ref):
        self.calls.append(("start", ref.repo))

    def ref_progress(self, ref, phase, percent):
        self.calls.append((phase, ref.repo, percent))

    def ref_finished(self, ref, outcome):
        self.calls.append(("finish", ref.repo, outcome.ok))


def test_stage_percent_maps_each_git_stage_onto_its_slice():
    assert stage_percent("receiving", 0) == 2 and stage_percent("receiving", 100) == 70
    assert stage_percent("resolving", 50) == 77
    assert stage_percent("checkout", 100) == 93 and stage_percent("indexing", 100) == 96 == SWAP_PERCENT
    assert stage_percent("receiving", 250) == 70  # clamped


def test_progress_never_moves_backwards_and_ends_at_swap():
    events, rec = [], _Recorder()
    # git's fetch then reset each report a pass; the second, lower "checkout" must not pull the bar back
    target = FakeSyncTarget(events, stages=[("receiving", 50), ("checkout", 100), ("checkout", 10), ("bogus", 99)])
    refresh_all([_ref(repo="adf")], target, FakeFlag(events), progress=rec)
    assert rec.calls == [("start", "adf"), ("fetching", "adf", 0), ("fetching", "adf", 36),
                         ("fetching", "adf", 93), ("swapping", "adf", SWAP_PERCENT), ("finish", "adf", True)]


def test_progress_reports_a_failed_repo_finished_not_ok():
    events, rec = [], _Recorder()
    refresh_all([_ref(repo="adf")], FakeSyncTarget(events, fail_on_swap={"adf"}), FakeFlag(events), progress=rec)
    assert rec.calls[-1] == ("finish", "adf", False)


def test_only_narrows_a_run_and_never_widens_it():
    events = []
    registry = FakeRegistry({
        "adf": [_ref(repo="adf")],
        "databricks": [_ref(repo="databricks")],
        "planned": [_ref(repo="planned", status="planned_not_live")],
    })
    outcomes = refresh_registry_once(registry, FakeSyncTarget(events), FakeFlag(events), FakeLock(events),
                                     holder="t", only=lambda ref: ref.repo in ("databricks", "planned"))
    assert [o.repo for o in outcomes] == ["databricks"]


def test_partial_status_keeps_other_rows_and_the_last_full_run():
    previous = {"last_run_at": "2026-10-01T03:00:00+00:00", "overall_ok": True, "repos": [
        {"repo": "adf", "branch": "release_main", "ok": True, "checked_at": "2026-10-01T03:00:00+00:00"},
        {"repo": "databricks", "branch": "release_main", "ok": False, "checked_at": "2026-10-01T03:00:00+00:00"},
    ]}
    from domain.sync_policy import SyncOutcome

    report = build_status_report([SyncOutcome("databricks", "release_main", ok=True)], previous)
    assert report["last_run_at"] == "2026-10-01T03:00:00+00:00" and "last_partial_run_at" in report
    rows = {r["repo"]: r for r in report["repos"]}
    assert rows["adf"]["checked_at"] == "2026-10-01T03:00:00+00:00"
    assert rows["databricks"]["ok"] is True and report["overall_ok"] is True


# --- the mirror-grep cache is rebuilt after each swap (OPEN_ITEMS.md #46) -------------------------


def test_after_swap_runs_once_per_swapped_tree_after_the_flag_clears():
    events = []
    registry = FakeRegistry({"adf": [_ref(repo="adf")], "dbx": [_ref(repo="dbx")]})
    refresh_registry_once(registry, FakeSyncTarget(events), FakeFlag(events), FakeLock(events), holder="t",
                          after_swap=lambda ref: events.append(("cache", ref.repo)))
    for repo in ("adf", "dbx"):
        assert events.index(("clear", repo)) < events.index(("cache", repo))
    assert [e for e in events if e[0] == "cache"] == [("cache", "adf"), ("cache", "dbx")]


def test_a_failed_swap_gets_no_cache_rebuild_and_a_failed_rebuild_fails_no_sync():
    events = []
    registry = FakeRegistry({"adf": [_ref(repo="adf")], "dbx": [_ref(repo="dbx")]})

    def rebuild(ref):
        events.append(("cache", ref.repo))
        raise OSError("disk full")

    outcomes = refresh_registry_once(registry, FakeSyncTarget(events, fail_on_swap=("adf",)), FakeFlag(events),
                                     FakeLock(events), holder="t", after_swap=rebuild)
    assert [(o.repo, o.ok) for o in outcomes] == [("adf", False), ("dbx", True)]
    assert [e for e in events if e[0] == "cache"] == [("cache", "dbx")]


def test_every_sync_entry_point_passes_the_post_swap_step():
    """sync-run-once, the scheduled service and the admin panel's Sync now all run the post-swap step
    (search cache, then generated lineage)."""
    from pathlib import Path
    app = Path(__file__).resolve().parents[1] / "app"
    for name in ("cli.py", "sync_service.py", "admin_routes.py"):
        text = (app / name).read_text(encoding="utf-8")
        call = text[text.index("refresh_registry_once("):]
        call = call[:call.index(")\n")]
        assert "after_swap=" in call and "after_mirror_swap" in call, name
        assert "after_run=" in call and "after_mirror_sync" in call, name


def test_after_run_runs_once_per_sync_only_when_something_swapped_and_never_fails_it():
    events, runs = [], []
    registry = FakeRegistry({"adf": [_ref(repo="adf")], "dbx": [_ref(repo="dbx")]})

    def after_run(outcomes):
        runs.append([(o.repo, o.ok) for o in outcomes])
        raise RuntimeError("generator crashed")

    outcomes = refresh_registry_once(registry, FakeSyncTarget(events, fail_on_swap=("adf",)), FakeFlag(events),
                                     FakeLock(events), holder="t", after_run=after_run)
    assert [(o.repo, o.ok) for o in outcomes] == [("adf", False), ("dbx", True)]
    assert runs == [[("adf", False), ("dbx", True)]]  # once, with every outcome; its crash is swallowed

    runs.clear()
    refresh_registry_once(registry, FakeSyncTarget(events, fail_on_swap=("adf", "dbx")), FakeFlag(events),
                          FakeLock(events), holder="t", after_run=after_run)
    assert runs == []  # nothing swapped, nothing to rebuild
