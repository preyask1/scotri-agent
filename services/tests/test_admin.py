# Copyright (c) 2026 Preyas Kulshrestha. Part of the Scotri engine; MIT License (see LICENSE).
# Keep this notice in copies and derived works, including AI-assisted ones.
"""The admin surface (OPEN_ITEMS.md #43): the ADMIN_ROLES gate, the audit line on every outcome, the
log view's allowlist and redaction, the caller-app register, and the cross-user history queries.

The routes run against a fake container holding real SQLite/JSONL adapters on tmp_path, so the audit
log and history here never touch services/logs/ or services/data/ (the #56 mistake)."""
from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest

from adapters.admin.jsonl_admin_audit import JsonlAdminAuditLog
from adapters.admin.sqlite_caller_registry import SqliteCallerRegistry
from adapters.history.sqlite_engagement_history import SqliteEngagementHistory
from domain.admin import (
    MAX_LOG_LINES,
    CallerKind,
    PlatformActivity,
    clamp_lines,
    merge_callers,
    resolve_log_source,
    tail_log,
    validate_caller_app,
)
from domain.engagement import Engagement, EngagementStatus
from domain.errors import PolicyError
from domain.governor_policy import GovernorPolicy
from domain.models import MaintenanceStatus, Principal, TraceStep
from ports.rate_limiter import GovernorState

T0 = datetime(2026, 10, 4, 9, 0, tzinfo=timezone.utc)
ADMIN = Principal(subject="ops-1", display_name="Ops", platform="scotri-ui", roles=["scotri.admin"])
ANALYST = Principal(subject="alice", display_name="Alice", platform="scotri-ui", roles=["lead"])
AUTH = {"Authorization": "Bearer t"}


def _eng(job_id: str, subject: str, platform: str, at: datetime, status=EngagementStatus.SUCCEEDED,
         prompt: str = "investigate ACME-1") -> Engagement:
    return Engagement(job_id=job_id, prompt=prompt,
                      principal=Principal(subject=subject, display_name=subject, platform=platform, roles=[]),
                      status=status, created_at=at, conversation_id=f"c-{job_id}")


# --- domain ---------------------------------------------------------------------------------------

def test_log_sources_are_an_allowlist(tmp_path):
    assert resolve_log_source(tmp_path, "api") == tmp_path / "scotri-api.log"
    for bad in ("../.env", "scotri-api.log", "/etc/passwd", ""):
        with pytest.raises(PolicyError):
            resolve_log_source(tmp_path, bad)


def test_clamp_lines():
    assert clamp_lines(None) == 200
    assert clamp_lines(0) == 1
    assert clamp_lines(10**9) == MAX_LOG_LINES


def test_tail_keeps_the_last_lines_filters_and_redacts(tmp_path):
    log = tmp_path / "x.log"
    log.write_text("\n".join([f"line {i}" for i in range(10)]
                             + ["auth header Bearer abcdefghijklmnop", "conn AccountKey=supersecretvalue"]) + "\n",
                   encoding="utf-8")
    lines, exists = tail_log(log, 3, None)
    assert exists and lines[0] == "line 9"
    assert "abcdefghijklmnop" not in lines[1] and "[REDACTED]" in lines[1]
    assert "supersecretvalue" not in lines[2]
    filtered, _ = tail_log(log, 50, "LINE 1")
    assert filtered == ["line 1"]
    assert tail_log(tmp_path / "missing.log", 5) == ([], False)


def test_caller_app_validation():
    assert validate_caller_app("6f1c-app.id", "Scotri UI", "interactive", "data team", "") is CallerKind.INTERACTIVE
    for args in [
        ("bad id with spaces", "x", "service", "", ""),
        ("ok", "", "service", "", ""),
        ("ok", "x", "robot", "", ""),
        ("ok", "x", "service", "", "client_secret=abc12345 for the sweep"),
        ("ok", "x", "service", "", "eyJhbGciOiJSUzI1.eyJzdWIiOiIxMjM0.c2lnbmF0dXJlMTIz"),
    ]:
        with pytest.raises(PolicyError):
            validate_caller_app(*args)


def test_merge_callers_flags_unregistered_platforms():
    from domain.admin import CallerApp

    reg = [CallerApp("ui", "Scotri UI", CallerKind.INTERACTIVE, "", "", "ops-1", T0, T0),
           CallerApp("never-seen", "Old tool", CallerKind.SERVICE, "", "", "ops-1", T0, T0)]
    act = [PlatformActivity("ui", 5, 2, T0), PlatformActivity("mystery", 1, 1, T0)]
    rows = {r["app_id"]: r for r in merge_callers(reg, act)}
    assert rows["ui"]["registered"] and rows["ui"]["engagements"] == 5
    assert rows["never-seen"]["engagements"] == 0 and rows["never-seen"]["last_seen"] is None
    assert rows["mystery"]["registered"] is False and rows["mystery"]["subjects"] == 1


# --- adapters -------------------------------------------------------------------------------------

def test_history_cross_user_queries(tmp_path):
    h = SqliteEngagementHistory(tmp_path / "h.db")
    h.record(_eng("j1", "alice", "ui", T0))
    h.record(_eng("j2", "bob", "ui", T0 + timedelta(minutes=1), EngagementStatus.FAILED))
    h.record(_eng("j3", "bob", "mcp-client", T0 + timedelta(minutes=2)))
    assert [e.job_id for e in h.list_recent(10)] == ["j3", "j2", "j1"]
    assert [e.job_id for e in h.list_recent(10, subject="bob")] == ["j3", "j2"]
    assert [e.job_id for e in h.list_recent(10, status="failed")] == ["j2"]
    assert [e.job_id for e in h.list_recent(1)] == ["j3"]
    act = {a.platform: a for a in h.platform_activity()}
    assert act["ui"].engagements == 2 and act["ui"].subjects == 2
    assert act["mcp-client"].last_seen == T0 + timedelta(minutes=2)


def test_caller_registry_keeps_who_registered_on_update(tmp_path):
    from domain.admin import CallerApp

    r = SqliteCallerRegistry(tmp_path / "h.db")
    r.upsert(CallerApp("ui", "UI", CallerKind.INTERACTIVE, "", "", "ops-1", T0, T0))
    r.upsert(CallerApp("ui", "UI v2", CallerKind.INTERACTIVE, "team", "", "ops-2", T0 + timedelta(1), T0 + timedelta(1)))
    got = r.get("ui")
    assert got.name == "UI v2" and got.registered_by == "ops-1" and got.registered_at == T0
    assert r.delete("ui") and not r.delete("ui") and r.list() == []


def test_audit_log_tail(tmp_path):
    from domain.admin import AdminAuditEntry, AuditOutcome

    log = JsonlAdminAuditLog(tmp_path / "logs" / "a.jsonl")
    assert log.tail(5) == []
    for i in range(3):
        log.append(AdminAuditEntry(T0, "ops-1", "Ops", "ui", f"a{i}", None, AuditOutcome.OK))
    assert [e["action"] for e in log.tail(2)] == ["a1", "a2"]


# --- routes ---------------------------------------------------------------------------------------

class _Secrets:
    def __init__(self, values: dict[str, str]):
        self._v = values

    def get_optional(self, name, default=None):
        return self._v.get(name, default)


class _RateLimiter:
    def peek_state(self, user, backend, window_seconds):
        return GovernorState(rate_count_this_window=0, concurrency_in_flight=1)


class _JobStore:
    def __init__(self, jobs):
        self._jobs = {j.job_id: j for j in jobs}

    def get(self, job_id):
        return self._jobs.get(job_id)

    def list_active(self):
        return [j for j in self._jobs.values() if j.status == EngagementStatus.RUNNING]


class _NoProgress:
    def get(self, job_id):
        return None


class _Reader:
    def read_turn(self, session_id, prompt, not_before):
        return [TraceStep(kind="tool_call", tool="Bash", input_summary="ls", result_preview="ok")]


class _Registry:
    def all_repo_keys(self):
        return ["databricks"]

    def get(self, key):
        from domain.models import Environment, RepoRef

        return [RepoRef(repo="Databricks", org="o", project="p", branch="main",
                        environment=Environment("prod"), mirrored=True)]


class _MirrorReader:
    def read_status(self):
        return None


class _Maintenance:
    def status(self, repo):
        return MaintenanceStatus(active=False)

    def set(self, repo, eta_seconds):
        pass

    def clear(self, repo):
        pass


class _MirrorSync:
    """Records refreshes; `gate`, when set, holds fetch_into_staging until released."""
    def __init__(self):
        self.calls, self.gate, self.fail = [], None, False

    def fetch_into_staging(self, repo, branch, org, project, on_progress=None):
        if on_progress:
            on_progress("receiving", 50)  # halfway through git's own receive: 36% of the repo's bar
        if self.gate is not None:
            self.gate.wait(5)
        if self.fail:
            raise RuntimeError("fetch failed")
        if on_progress:
            on_progress("checkout", 100)
        self.calls.append(("fetch", repo, branch))

    def swap_to_active(self, repo, branch):
        self.calls.append(("swap", repo, branch))


class _SyncLock:
    def __init__(self):
        self.held_by = None

    def acquire(self, holder, ttl_seconds):
        if self.held_by is not None:
            return None
        self.held_by = holder
        return "tok"

    def extend(self, token, ttl_seconds):
        return True

    def release(self, token):
        self.held_by = None

    def holder(self):
        return self.held_by


class _BrokenRedis:
    def ping(self):
        raise ConnectionError("down")


class _Container:
    def __init__(self, tmp_path, admin_roles="scotri.admin"):
        self.secrets = _Secrets({"ADMIN_ROLES": admin_roles, "DATABRICKS_CLIENT_ID": "abc-secret-id",
                                 "HISTORY_ADMIN_ROLES": "lead"})
        self.logs_dir = tmp_path / "logs"
        self.admin_audit_log = JsonlAdminAuditLog(tmp_path / "logs" / "admin-audit.jsonl")
        self.engagement_history = SqliteEngagementHistory(tmp_path / "h.db")
        self.caller_registry = SqliteCallerRegistry(tmp_path / "h.db")
        running = replace(_eng("run1", "bob", "ui", T0, EngagementStatus.RUNNING, prompt="x" * 500),
                          session_id="sess")
        self.job_store = _JobStore([running])
        self.progress_store = _NoProgress()
        self.trace_reader = _Reader()
        self.rate_limiter = _RateLimiter()
        self.registry = _Registry()
        self.mirror_reader = _MirrorReader()
        self.maintenance_flag = _Maintenance()
        self.mirror_sync = _MirrorSync()
        self.sync_lock = _SyncLock()
        self.mirror_root = tmp_path / "mirror"
        self.mirror_root.mkdir()
        self._redis_client = _BrokenRedis()

    def role_set(self, name):
        return {r.strip() for r in (self.secrets.get_optional(name, "") or "").split(",") if r.strip()}


@pytest.fixture
def setup(monkeypatch, tmp_path):
    from fastapi.testclient import TestClient

    import app.api as api

    fake = _Container(tmp_path)
    caller = {"p": ADMIN}
    monkeypatch.setattr(api, "container", fake)
    monkeypatch.setattr(api, "_authenticate", lambda authorization: caller["p"])
    return TestClient(api.app, raise_server_exceptions=False), fake, caller


ADMIN_GETS = ["/v1/admin/overview", "/v1/admin/engagements", "/v1/admin/engagements/run1/steps",
              "/v1/admin/logs", "/v1/admin/logs/api", "/v1/admin/audit", "/v1/admin/apps"]


def test_every_admin_route_refuses_a_non_admin_and_audits_it(setup):
    client, fake, caller = setup
    caller["p"] = ANALYST
    for path in ADMIN_GETS:
        assert client.get(path, headers=AUTH).status_code == 403, path
    assert client.put("/v1/admin/apps/x", headers=AUTH, json={"name": "x", "kind": "service"}).status_code == 403
    assert client.delete("/v1/admin/apps/x", headers=AUTH).status_code == 403
    entries = fake.admin_audit_log.tail(100)
    assert len(entries) == len(ADMIN_GETS) + 2
    assert {e["outcome"] for e in entries} == {"denied"} and {e["subject"] for e in entries} == {"alice"}
    assert fake.caller_registry.list() == []


def test_empty_admin_roles_means_nobody(setup):
    client, fake, _ = setup
    fake.secrets._v["ADMIN_ROLES"] = ""
    assert client.get("/v1/admin/overview", headers=AUTH).status_code == 403
    assert client.get("/v1/admin/me", headers=AUTH).json()["is_admin"] is False


def test_me_is_open_and_unaudited(setup):
    client, fake, caller = setup
    assert client.get("/v1/admin/me", headers=AUTH).json()["is_admin"] is True
    caller["p"] = ANALYST
    assert client.get("/v1/admin/me", headers=AUTH).json() == {
        "subject": "alice", "display_name": "Alice", "platform": "scotri-ui", "roles": ["lead"], "is_admin": False}
    assert fake.admin_audit_log.tail(10) == []


def test_overview_reports_each_section_and_never_a_setting_value(setup):
    client, fake, _ = setup
    body = client.get("/v1/admin/overview", headers=AUTH).json()
    assert body["redis"] == {"ok": False, "error": "ConnectionError"}  # a broken section, not a 500
    assert body["governor"]["ok"] and {b["backend"] for b in body["governor"]["backends"]} >= {"databricks", "jira"}
    assert body["governor"]["rate_limit_per_user_per_hour"] == GovernorPolicy().rate_limit_per_user_per_hour
    assert body["active_jobs"]["count"] == 1 and len(body["active_jobs"]["jobs"][0]["prompt_preview"]) <= 201
    assert body["mirror"]["repos"][0]["freshness"]["available"] is False
    assert body["config"]["settings"]["DATABRICKS_CLIENT_ID"] is True
    assert body["config"]["settings"]["AZURE_AD_TENANT_ID"] is False
    assert body["config"]["roles"]["HISTORY_ADMIN_ROLES"] == ["lead"]
    assert "abc-secret-id" not in str(body)
    assert fake.admin_audit_log.tail(1)[0]["action"] == "overview"


def test_engagements_are_cross_user_and_filterable(setup):
    client, fake, _ = setup
    fake.engagement_history.record(_eng("j1", "alice", "ui", T0))
    fake.engagement_history.record(_eng("j2", "bob", "ui", T0 + timedelta(minutes=1), EngagementStatus.FAILED))
    body = client.get("/v1/admin/engagements", headers=AUTH).json()
    assert [e["job_id"] for e in body["engagements"]] == ["j2", "j1"]
    body = client.get("/v1/admin/engagements?subject=alice", headers=AUTH).json()
    assert [e["subject"] for e in body["engagements"]] == ["alice"]
    assert fake.admin_audit_log.tail(1)[0]["target"] == "subject=alice"


def test_admin_can_read_someone_elses_steps_and_it_is_audited(setup):
    client, fake, _ = setup
    body = client.get("/v1/admin/engagements/run1/steps", headers=AUTH).json()
    assert body["owner"] == "bob" and body["steps"][0]["tool"] == "Bash"
    entry = fake.admin_audit_log.tail(1)[0]
    assert entry == {**entry, "action": "read_steps", "target": "run1", "outcome": "ok", "subject": "ops-1"}
    assert "Bash" not in str(entry)  # never the payload
    assert client.get("/v1/admin/engagements/nope/steps", headers=AUTH).status_code == 404
    assert fake.admin_audit_log.tail(1)[0]["outcome"] == "error"


def test_log_view(setup):
    client, fake, _ = setup
    fake.logs_dir.mkdir(parents=True, exist_ok=True)
    (fake.logs_dir / "scotri-api.log").write_text("a\nb password=hunter2222\nc\n", encoding="utf-8")
    sources = {s["source"]: s for s in client.get("/v1/admin/logs", headers=AUTH).json()["sources"]}
    assert sources["api"]["exists"] and not sources["cli"]["exists"]
    body = client.get("/v1/admin/logs/api?lines=2", headers=AUTH).json()
    assert body["lines"][1] == "c" and "hunter2222" not in body["lines"][0]
    assert client.get("/v1/admin/logs/..%2F.env", headers=AUTH).status_code == 404
    assert client.get("/v1/admin/logs/secrets", headers=AUTH).status_code == 404


def test_caller_app_lifecycle(setup):
    client, fake, _ = setup
    fake.engagement_history.record(_eng("j1", "alice", "unknown-app", T0))
    r = client.put("/v1/admin/apps/ui-app", headers=AUTH,
                   json={"name": "Scotri UI", "kind": "interactive", "owner": "data team"})
    assert r.status_code == 200 and r.json()["created"] is True
    r = client.put("/v1/admin/apps/ui-app", headers=AUTH, json={"name": "Scotri UI 2", "kind": "interactive"})
    assert r.json()["created"] is False and r.json()["registered_by"] == "ops-1"
    assert client.put("/v1/admin/apps/x", headers=AUTH,
                      json={"name": "x", "kind": "service", "notes": "token=abcdefgh"}).status_code == 422
    body = client.get("/v1/admin/apps", headers=AUTH).json()
    assert body["unregistered"] == 1
    assert {a["app_id"]: a["registered"] for a in body["apps"]} == {"ui-app": True, "unknown-app": False}
    assert client.delete("/v1/admin/apps/ui-app", headers=AUTH).json() == {"app_id": "ui-app", "deleted": True}
    assert client.delete("/v1/admin/apps/ui-app", headers=AUTH).status_code == 404
    actions = [(e["action"], e["outcome"], e["detail"]) for e in fake.admin_audit_log.tail(10)]
    assert ("put_app", "ok", "created") in actions and ("put_app", "ok", "updated") in actions
    assert ("put_app", "error", "http 422") in actions and ("delete_app", "error", "not_found") in actions
    assert "abcdefgh" not in str(actions)


def test_audit_view_is_newest_first_and_itself_audited(setup):
    client, fake, _ = setup
    client.get("/v1/admin/apps", headers=AUTH)
    body = client.get("/v1/admin/audit", headers=AUTH).json()
    assert [e["action"] for e in body["entries"]] == ["list_apps"]
    assert fake.admin_audit_log.tail(1)[0]["action"] == "read_audit"


def test_an_admin_action_that_cannot_be_audited_fails(setup):
    client, fake, _ = setup

    class _Broken:
        def append(self, entry):
            raise OSError("disk full")

    fake.admin_audit_log = _Broken()
    assert client.get("/v1/admin/apps", headers=AUTH).status_code == 500


def test_admin_page_is_served_without_data(setup):
    client, _, _ = setup
    r = client.get("/admin")
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/html")
    assert r.headers["x-frame-options"] == "DENY"


def test_overview_mirror_reports_a_redis_outage_once(setup):
    client, fake, _ = setup
    from domain.errors import TransientError

    calls = []

    class _Down:
        def status(self, repo):
            calls.append(repo)
            raise TransientError("Redis unavailable")

    fake.maintenance_flag = _Down()
    fake.registry.get = lambda key: [r for b in ("main", "develop_main") for r in _Registry().get(key)]
    mirror = client.get("/v1/admin/overview", headers=AUTH).json()["mirror"]
    assert mirror["maintenance_error"] == "Redis unavailable" and len(calls) == 1
    assert [r["maintenance"] for r in mirror["repos"]] == [None, None]


# --- manual mirror sync ---------------------------------------------------------------------------

def _wait_for_sync():
    import app.api as api

    thread = api.admin_router.manual_sync.thread
    if thread is not None:
        thread.join(5)
        assert not thread.is_alive()


def test_manual_mirror_sync_runs_the_scheduled_path_and_audits_start_and_finish(setup):
    import json

    client, fake, _ = setup
    _wait_for_sync()
    r = client.post("/v1/admin/mirror/sync", headers=AUTH)
    assert r.status_code == 202 and r.json()["started_by"] == "ops-1"
    _wait_for_sync()
    assert fake.mirror_sync.calls == [("fetch", "Databricks", "main"), ("swap", "Databricks", "main")]
    status = json.loads((fake.mirror_root / "status.json").read_text())
    assert status["overall_ok"] is True and status["repos"][0]["repo"] == "Databricks"
    state = client.get("/v1/admin/mirror/sync", headers=AUTH).json()
    assert state["running"] is False and state["checked"] == 1 and state["failed"] == 0
    actions = [(e["action"], e["outcome"], e["detail"]) for e in fake.admin_audit_log.tail(10)]
    assert ("sync_mirror", "ok", "started") in actions
    assert ("mirror_sync_finished", "ok", "checked=1 failed=0") in actions


def test_manual_mirror_sync_reports_a_failed_repo(setup):
    client, fake, _ = setup
    _wait_for_sync()
    fake.mirror_sync.fail = True
    assert client.post("/v1/admin/mirror/sync", headers=AUTH).status_code == 202
    _wait_for_sync()
    state = client.get("/v1/admin/mirror/sync", headers=AUTH).json()
    assert state["failed"] == 1 and state["failures"][0]["repo"] == "Databricks"
    finished = [e for e in fake.admin_audit_log.tail(10) if e["action"] == "mirror_sync_finished"]
    assert finished[-1]["outcome"] == "error"


def test_manual_mirror_sync_refuses_a_second_run_while_one_is_running(setup):
    import threading

    client, fake, _ = setup
    _wait_for_sync()
    fake.mirror_sync.gate = threading.Event()
    try:
        assert client.post("/v1/admin/mirror/sync", headers=AUTH).status_code == 202
        assert client.get("/v1/admin/mirror/sync", headers=AUTH).json()["running"] is True
        assert client.post("/v1/admin/mirror/sync", headers=AUTH).status_code == 409
    finally:
        fake.mirror_sync.gate.set()
        _wait_for_sync()


def test_manual_mirror_sync_is_admin_only(setup):
    client, fake, caller = setup
    _wait_for_sync()
    caller["p"] = ANALYST
    assert client.post("/v1/admin/mirror/sync", headers=AUTH).status_code == 403
    assert client.get("/v1/admin/mirror/sync", headers=AUTH).status_code == 403
    assert fake.mirror_sync.calls == []
    assert {e["outcome"] for e in fake.admin_audit_log.tail(10)} == {"denied"}


def test_manual_mirror_sync_holds_the_shared_lock_while_running(setup):
    import threading

    client, fake, _ = setup
    _wait_for_sync()
    fake.mirror_sync.gate = threading.Event()
    try:
        assert client.post("/v1/admin/mirror/sync", headers=AUTH).status_code == 202
        for _ in range(100):  # the thread takes the lock as it starts
            if fake.sync_lock.held_by:
                break
            threading.Event().wait(0.01)
        assert fake.sync_lock.held_by == "admin:ops-1"
        assert client.get("/v1/admin/mirror/sync", headers=AUTH).json()["lock_holder"] == "admin:ops-1"
    finally:
        fake.mirror_sync.gate.set()
        _wait_for_sync()
    assert fake.sync_lock.held_by is None


def test_manual_mirror_sync_is_refused_while_another_path_holds_the_lock(setup):
    client, fake, _ = setup
    _wait_for_sync()
    fake.sync_lock.held_by = "sync-service"
    r = client.post("/v1/admin/mirror/sync", headers=AUTH)
    assert r.status_code == 409 and "sync-service" in r.text
    assert fake.mirror_sync.calls == []
    assert client.get("/v1/admin/mirror/sync", headers=AUTH).json()["lock_holder"] == "sync-service"
    assert fake.sync_lock.held_by == "sync-service"  # the other run's lock is left alone



# --- per-repo sync and progress -------------------------------------------------------------------

class _TwoRepos:
    def all_repo_keys(self):
        return ["adf", "databricks"]

    def get(self, key):
        from domain.models import Environment, RepoRef

        repo, branch = {"adf": ("adf", "release_main"), "databricks": ("Databricks", "main")}[key]
        return [RepoRef(repo=repo, org="o", project="p", branch=branch,
                        environment=Environment("prod"), mirrored=True)]


class _StatusReader:
    def __init__(self, status):
        self.status = status

    def read_status(self):
        return self.status


def test_manual_mirror_sync_reports_each_repos_progress(setup):
    import threading

    client, fake, _ = setup
    _wait_for_sync()
    fake.registry = _TwoRepos()
    fake.mirror_sync.gate = threading.Event()
    try:
        started = client.post("/v1/admin/mirror/sync", headers=AUTH).json()
        assert started["scope"] == "all"
        assert [(r["repo_key"], r["phase"], r["percent"]) for r in started["repos"]] in (
            [("adf", "queued", 0), ("databricks", "queued", 0)],
            [("adf", "fetching", 0), ("databricks", "queued", 0)],
            [("adf", "fetching", 36), ("databricks", "queued", 0)],
        )
        for _ in range(100):
            rows = client.get("/v1/admin/mirror/sync", headers=AUTH).json()["repos"]
            if rows[0]["percent"] == 36:
                break
            threading.Event().wait(0.01)
        assert rows[0]["phase"] == "fetching" and rows[0]["percent"] == 36
        assert rows[1]["phase"] == "queued"
    finally:
        fake.mirror_sync.gate.set()
        _wait_for_sync()
    rows = client.get("/v1/admin/mirror/sync", headers=AUTH).json()["repos"]
    assert [(r["phase"], r["percent"], r["ok"]) for r in rows] == [("done", 100, True), ("done", 100, True)]


def test_manual_mirror_sync_of_one_repo_merges_its_status_row(setup):
    import json

    client, fake, _ = setup
    _wait_for_sync()
    fake.registry = _TwoRepos()
    fake.mirror_reader = _StatusReader({
        "last_run_at": "2026-10-01T03:00:00+00:00", "overall_ok": True,
        "repos": [{"repo": "adf", "branch": "release_main", "ok": True, "checked_at": "2026-10-01T03:00:00+00:00"},
                  {"repo": "Databricks", "branch": "main", "ok": True, "checked_at": "2026-10-01T03:00:00+00:00"}]})
    r = client.post("/v1/admin/mirror/sync", headers=AUTH, json={"repo_key": "databricks", "branch": "main"})
    assert r.status_code == 202 and r.json()["scope"] == "databricks@main"
    assert [row["repo_key"] for row in r.json()["repos"]] == ["databricks"]
    _wait_for_sync()
    assert fake.mirror_sync.calls == [("fetch", "Databricks", "main"), ("swap", "Databricks", "main")]
    status = json.loads((fake.mirror_root / "status.json").read_text())
    assert status["last_run_at"] == "2026-10-01T03:00:00+00:00"  # one fresh repo doesn't freshen the rest
    rows = {r["repo"]: r for r in status["repos"]}
    assert rows["adf"]["checked_at"] == "2026-10-01T03:00:00+00:00"
    assert rows["Databricks"]["checked_at"] > "2026-10-01T03:00:00+00:00"
    actions = [(e["action"], e["target"]) for e in fake.admin_audit_log.tail(10)]
    assert ("sync_mirror", "databricks@main") in actions and ("mirror_sync_finished", "databricks@main") in actions


def test_manual_mirror_sync_of_one_repo_names_it_on_the_lock(setup):
    import threading

    client, fake, _ = setup
    _wait_for_sync()
    fake.registry = _TwoRepos()
    fake.mirror_sync.gate = threading.Event()
    try:
        assert client.post("/v1/admin/mirror/sync", headers=AUTH, json={"repo_key": "adf"}).status_code == 202
        for _ in range(100):
            if fake.sync_lock.held_by:
                break
            threading.Event().wait(0.01)
        assert fake.sync_lock.held_by == "admin:ops-1 (adf)"
        r = client.post("/v1/admin/mirror/sync", headers=AUTH, json={"repo_key": "databricks"})
        assert r.status_code == 409 and "admin:ops-1 (adf)" in r.text
    finally:
        fake.mirror_sync.gate.set()
        _wait_for_sync()


def test_manual_mirror_sync_refuses_an_unknown_repo(setup):
    client, fake, _ = setup
    _wait_for_sync()
    assert client.post("/v1/admin/mirror/sync", headers=AUTH, json={"repo_key": "nope"}).status_code == 404
    assert client.post("/v1/admin/mirror/sync", headers=AUTH,
                       json={"repo_key": "databricks", "branch": "dev"}).status_code == 404
    assert fake.mirror_sync.calls == [] and fake.sync_lock.held_by is None


def test_a_failed_repo_shows_failed_on_its_row(setup):
    client, fake, _ = setup
    _wait_for_sync()
    fake.mirror_sync.fail = True
    client.post("/v1/admin/mirror/sync", headers=AUTH)
    _wait_for_sync()
    row = client.get("/v1/admin/mirror/sync", headers=AUTH).json()["repos"][0]
    assert row["phase"] == "failed" and row["ok"] is False and row["message"]
