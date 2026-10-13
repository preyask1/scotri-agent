# Copyright (c) 2026 Preyas Kulshrestha. Part of the Scotri engine; MIT License (see LICENSE).
# Keep this notice in copies and derived works, including AI-assisted ones.
"""The engagement admission queue (OPEN_ITEMS.md #32): at most N runs at once, a FIFO line behind
them, per-user and length limits, expiry, cancel by owner or admin, slots that lapse when their
process dies, and what HTTP/MCP callers and the admin panel see.

The Redis adapter is tested against a real local Redis under a throwaway key prefix (skipped when
none is reachable), because its correctness is the Lua scripts. Everything above it runs on an
in-memory fake with the same contract."""
from __future__ import annotations

import time
import types
import uuid
from datetime import datetime, timezone

import pytest

import app.engagement_runner as engagement_runner
from domain.engagement import Engagement, EngagementStatus
from domain.engagement_policy import fail_interrupted_engagements, start_engagement
from domain.engagement_queue import (
    CANCEL_BUSY,
    CANCEL_QUEUE_TIMEOUT,
    CANCEL_USER,
    Admission,
    AdmissionOutcome,
    QueueSettings,
    ServerBusyError,
    admit,
    expire_waiting,
    queue_view,
    withdraw_waiting,
)
from domain.models import Principal
from ports.engagement_queue import QueueSnapshot, RunningEntry, WaitingEntry

ALICE = Principal(subject="alice", display_name="Alice", platform="web")
BOB = Principal(subject="bob", display_name="Bob", platform="web")
SETTINGS = QueueSettings(max_running=2, max_waiting_per_user=2, max_wait_seconds=60, max_length=3)


# ── fakes ───────────────────────────────────────────────────────────────────────────────────

class Jobs:
    def __init__(self):
        self.jobs: dict[str, Engagement] = {}

    def create(self, e):
        self.jobs[e.job_id] = e

    def update(self, e):
        self.jobs[e.job_id] = e

    def get(self, job_id):
        return self.jobs.get(job_id)

    def list_active(self):
        return [e for e in self.jobs.values() if e.status in (EngagementStatus.QUEUED, EngagementStatus.RUNNING)]


class History:
    def __init__(self):
        self.recorded = []

    def record(self, e):
        self.recorded.append(e)


class FakeQueue:
    """Same contract as RedisEngagementQueue, on a controllable clock."""

    def __init__(self):
        self.now = 1000.0
        self.waiting: list[tuple[str, str, float]] = []
        self.running: dict[str, float] = {}

    def _live(self):
        return [j for j, exp in self.running.items() if exp > self.now]

    def admit(self, job_id, subject, settings):
        if not self.waiting and len(self._live()) < settings.max_running:
            self.running[job_id] = self.now + settings.lease_seconds
            return Admission(AdmissionOutcome.START)
        if len(self.waiting) >= settings.max_length:
            return Admission(AdmissionOutcome.QUEUE_FULL)
        if sum(1 for _, s, _ in self.waiting if s == subject) >= settings.max_waiting_per_user:
            return Admission(AdmissionOutcome.USER_LIMIT)
        self.waiting.append((job_id, subject, self.now))
        return Admission(AdmissionOutcome.QUEUED, len(self.waiting))

    def claim_next(self, settings):
        if len(self._live()) >= settings.max_running or not self.waiting:
            return None
        job_id = self.waiting.pop(0)[0]
        self.running[job_id] = self.now + settings.lease_seconds
        return job_id

    def release(self, job_id):
        self.running.pop(job_id, None)

    def renew(self, job_ids, settings):
        for j in job_ids:
            if j in self.running:
                self.running[j] = self.now + settings.lease_seconds

    def reap_expired(self):
        lapsed = [j for j, exp in self.running.items() if exp <= self.now]
        for j in lapsed:
            del self.running[j]
        return lapsed

    def remove_waiting(self, job_id):
        before = len(self.waiting)
        self.waiting = [w for w in self.waiting if w[0] != job_id]
        return len(self.waiting) < before

    def waiting_older_than(self, seconds):
        return [j for j, _, at in self.waiting if at <= self.now - seconds]

    def position(self, job_id):
        ids = [w[0] for w in self.waiting]
        return ids.index(job_id) + 1 if job_id in ids else None

    def holds_slot(self, job_id):
        return job_id in self._live()

    def snapshot(self):
        return QueueSnapshot([WaitingEntry(j, s, at) for j, s, at in self.waiting],
                             [RunningEntry(j, exp) for j, exp in self.running.items() if exp > self.now])


def _job(jobs, principal=ALICE, prompt="ACME-1"):
    return jobs.get(start_engagement(jobs, principal, prompt))


# ── Redis adapter (real Redis, throwaway keys) ──────────────────────────────────────────────

@pytest.fixture
def redis_queue():
    redis = pytest.importorskip("redis")
    from adapters.redis.engagement_queue_adapter import RedisEngagementQueue

    client = redis.Redis(host="localhost", port=6379, db=0, socket_connect_timeout=0.5)
    try:
        client.ping()
    except Exception:  # noqa: BLE001
        pytest.skip("no local Redis to test the Lua scripts against")
    prefix = f"test-engagement-queue:{uuid.uuid4().hex}:"
    yield RedisEngagementQueue(client, key_prefix=prefix)
    for key in client.scan_iter(match=prefix + "*"):
        client.delete(key)


def test_redis_admits_up_to_the_limit_then_queues_in_order(redis_queue):
    q = redis_queue
    assert q.admit("a", "alice", SETTINGS).outcome == AdmissionOutcome.START
    assert q.admit("b", "bob", SETTINGS).outcome == AdmissionOutcome.START
    assert q.admit("c", "carol", SETTINGS) == Admission(AdmissionOutcome.QUEUED, 1)
    assert q.admit("d", "dave", SETTINGS) == Admission(AdmissionOutcome.QUEUED, 2)
    assert q.claim_next(SETTINGS) is None  # both slots taken
    q.release("a")
    assert q.claim_next(SETTINGS) == "c"  # first in, first out
    assert q.position("d") == 1 and q.position("c") is None
    assert q.holds_slot("c") and not q.holds_slot("a")
    snap = q.snapshot()
    assert [w.job_id for w in snap.waiting] == ["d"] and {r.job_id for r in snap.running} == {"b", "c"}


def test_redis_refuses_per_user_and_when_full(redis_queue):
    q = redis_queue
    settings = QueueSettings(max_running=1, max_waiting_per_user=1, max_length=2)
    assert q.admit("run", "x", settings).outcome == AdmissionOutcome.START
    assert q.admit("a1", "alice", settings).outcome == AdmissionOutcome.QUEUED
    assert q.admit("a2", "alice", settings).outcome == AdmissionOutcome.USER_LIMIT
    assert q.admit("b1", "bob", settings).outcome == AdmissionOutcome.QUEUED
    assert q.admit("c1", "carol", settings).outcome == AdmissionOutcome.QUEUE_FULL
    assert q.remove_waiting("a1") and not q.remove_waiting("a1")
    assert q.admit("a2", "alice", settings) == Admission(AdmissionOutcome.QUEUED, 2)


def test_redis_lapsed_leases_are_reaped_and_free_their_slot(redis_queue):
    q = redis_queue
    short = QueueSettings(max_running=1, lease_seconds=1)
    assert q.admit("a", "alice", short).outcome == AdmissionOutcome.START
    assert q.admit("b", "bob", short).outcome == AdmissionOutcome.QUEUED
    time.sleep(1.2)
    assert q.reap_expired() == ["a"] and q.reap_expired() == []
    assert q.claim_next(short) == "b"
    q.renew(["a"], short)  # a reaped job is never re-added by a late renewal
    assert not q.holds_slot("a")
    assert q.waiting_older_than(0) == []


def test_redis_waiting_older_than(redis_queue):
    q = redis_queue
    settings = QueueSettings(max_running=1)
    q.admit("a", "alice", settings)
    q.admit("b", "bob", settings)
    assert q.waiting_older_than(3600) == [] and q.waiting_older_than(0) == ["b"]


# ── domain rules ────────────────────────────────────────────────────────────────────────────

def test_refused_at_the_door_is_recorded_cancelled_busy_with_the_prompt():
    jobs, q = Jobs(), FakeQueue()
    settings = QueueSettings(max_running=1, max_waiting_per_user=1, max_length=5)
    admit(jobs, q, _job(jobs), settings)
    admit(jobs, q, _job(jobs), settings)
    third = _job(jobs, prompt="ACME-3")
    with pytest.raises(ServerBusyError) as exc:
        admit(jobs, q, third, settings)
    assert exc.value.reason == "user_limit" and exc.value.prompt == "ACME-3"
    stored = jobs.get(third.job_id)
    assert stored.status == EngagementStatus.CANCELLED and stored.cancel_reason == CANCEL_BUSY
    assert "1 requests waiting" in stored.error


def test_withdraw_and_expire_keep_the_prompt_and_record_history():
    jobs, q, history = Jobs(), FakeQueue(), History()
    settings = QueueSettings(max_running=1, max_wait_seconds=60)
    admit(jobs, q, _job(jobs), settings)
    waiting = _job(jobs, prompt="ACME-2")
    admit(jobs, q, waiting, settings)
    stale = _job(jobs, BOB, prompt="ACME-3")
    admit(jobs, q, stale, settings)

    cancelled = withdraw_waiting(jobs, q, history, waiting.job_id, CANCEL_USER, "Cancelled.")
    assert cancelled.status == EngagementStatus.CANCELLED and cancelled.cancel_reason == CANCEL_USER
    assert cancelled.prompt == "ACME-2" and history.recorded == [cancelled]
    assert withdraw_waiting(jobs, q, history, waiting.job_id, CANCEL_USER, "x") is None  # not waiting now

    q.now += 61
    assert expire_waiting(jobs, q, history, settings) == [stale.job_id]
    expired = jobs.get(stale.job_id)
    assert expired.cancel_reason == CANCEL_QUEUE_TIMEOUT and "waited 1 minutes" in expired.error
    assert q.snapshot().waiting == []


def test_queue_view_shows_the_place_in_line():
    jobs, q = Jobs(), FakeQueue()
    settings = QueueSettings(max_running=1)
    first = _job(jobs)
    admit(jobs, q, first, settings)
    second = _job(jobs, BOB)
    admit(jobs, q, second, settings)
    assert queue_view(q, second.job_id, settings) == {"position": 1, "waiting": 1, "running": 1, "max_running": 1}
    assert queue_view(q, first.job_id, settings) is None


def test_startup_sweep_spares_waiting_jobs_and_live_runs():
    jobs, q = Jobs(), FakeQueue()
    settings = QueueSettings(max_running=1)
    live = jobs.get(start_engagement(jobs, ALICE, "a", origin="api"))
    admit(jobs, q, live, settings)
    jobs.update(live.with_status(EngagementStatus.RUNNING))
    waiting = jobs.get(start_engagement(jobs, BOB, "b", origin="api"))
    admit(jobs, q, waiting, settings)
    orphan = jobs.get(start_engagement(jobs, BOB, "c", origin="api"))  # queued before the queue existed
    assert fail_interrupted_engagements(jobs, "api", queue=q) == 1
    assert jobs.get(orphan.job_id).status == EngagementStatus.FAILED
    assert jobs.get(live.job_id).status == EngagementStatus.RUNNING
    assert jobs.get(waiting.job_id).status == EngagementStatus.QUEUED


def test_settings_from_secrets_with_defaults_and_bad_values():
    secrets = types.SimpleNamespace(get_optional=lambda name, default=None: {"MAX_CONCURRENT_ENGAGEMENTS": "5",
                                                                             "ENGAGEMENT_QUEUE_MAX_LENGTH": "x"}.get(name, default))
    s = QueueSettings.from_secrets(secrets)
    assert (s.max_running, s.max_waiting_per_user, s.max_wait_seconds, s.max_length) == (5, 2, 1800, 20)


# ── runner ──────────────────────────────────────────────────────────────────────────────────

@pytest.fixture
def runner_env(monkeypatch):
    """A runner whose threads run inline and whose runs are recorded, not executed."""
    jobs, q, history = Jobs(), FakeQueue(), History()
    ran: list[str] = []

    def fake_run(job_store, agent_runtime, interaction_log, job_id, *a, **kw):
        ran.append(job_id)
        e = job_store.get(job_id)
        job_store.update(e.with_status(EngagementStatus.SUCCEEDED, result="done"))

    deferred: list = []
    monkeypatch.setattr(engagement_runner, "run_engagement", fake_run)
    container = types.SimpleNamespace(
        job_store=jobs, engagement_queue=q, queue_settings=QueueSettings(max_running=1, max_wait_seconds=60),
        engagement_history=history, agent_runtime=None, interaction_log=None, run_context_store=None,
        run_context_ttl_seconds=60, artifact_store=None, progress_store=None, engagement_prefetch=lambda root: None,
    )
    runner = engagement_runner.EngagementRunner(lambda: container, "/ws", "test",
                                                spawn=lambda fn, name: deferred.append(fn))

    def drain():
        while deferred:
            deferred.pop(0)()

    return runner, jobs, q, ran, drain


def test_runner_starts_the_next_waiting_job_when_a_run_ends(runner_env):
    runner, jobs, q, ran, drain = runner_env
    first, second = _job(jobs), _job(jobs, BOB)
    assert runner.submit(first).outcome == AdmissionOutcome.START
    assert runner.submit(second) == Admission(AdmissionOutcome.QUEUED, 1)
    assert runner.running_here() == [first.job_id]
    drain()  # first finishes, frees its slot, second is claimed and runs
    assert ran == [first.job_id, second.job_id]
    assert q.snapshot().running == [] and q.snapshot().waiting == []
    assert jobs.get(second.job_id).status == EngagementStatus.SUCCEEDED


def test_runner_withdraw_cancels_only_waiting_jobs(runner_env):
    runner, jobs, q, ran, drain = runner_env
    first, second = _job(jobs), _job(jobs, BOB, prompt="ACME-9")
    runner.submit(first)
    runner.submit(second)
    assert runner.withdraw(first.job_id) is None  # running, not waiting
    cancelled = runner.withdraw(second.job_id)
    assert cancelled.cancel_reason == CANCEL_USER and cancelled.prompt == "ACME-9"
    drain()
    assert ran == [first.job_id]


def test_tick_fails_a_run_whose_process_died_and_starts_the_next(runner_env):
    runner, jobs, q, ran, drain = runner_env
    orphan = _job(jobs)
    q.running[orphan.job_id] = q.now + 5  # held by some other, now dead, process
    jobs.update(orphan.with_status(EngagementStatus.RUNNING))
    waiting = _job(jobs, BOB)
    runner.submit(waiting)
    q.now += 10
    runner.tick()
    assert jobs.get(orphan.job_id).status == EngagementStatus.FAILED
    assert "restarted" in jobs.get(orphan.job_id).error
    drain()
    assert ran == [waiting.job_id]


def test_tick_renews_its_own_runs_and_expires_long_waits(runner_env):
    runner, jobs, q, ran, drain = runner_env
    mine, waiting = _job(jobs), _job(jobs, BOB)
    runner.submit(mine)
    runner.submit(waiting)
    q.now += 61  # past the 60 s wait limit, and past mine's lease were it not renewed
    q.running[mine.job_id] = q.now + 1
    runner.tick()
    assert q.holds_slot(mine.job_id)
    assert jobs.get(waiting.job_id).cancel_reason == CANCEL_QUEUE_TIMEOUT


def test_a_cancel_from_another_process_interrupts_the_run_here(runner_env):
    """OPEN_ITEMS.md #33: the job record says CANCELLED (written by whichever process got the
    request); the process actually running the job notices and interrupts its own run, once."""
    runner, jobs, q, ran, drain = runner_env
    cancelled_here = []
    runner._get_container().agent_runtime = types.SimpleNamespace(
        cancel=lambda job_id: cancelled_here.append(job_id) or True)
    job, other = _job(jobs), _job(jobs, BOB)
    runner.submit(job)                      # running here (its run is deferred, so still in flight)
    runner.submit(other)                    # waiting, not running here: never signalled
    assert runner.check_cancelled() == []   # nothing cancelled yet

    jobs.update(jobs.get(job.job_id).with_status(EngagementStatus.CANCELLED, cancel_reason=CANCEL_USER))
    jobs.update(jobs.get(other.job_id).with_status(EngagementStatus.CANCELLED))
    assert runner.check_cancelled() == [job.job_id]
    assert runner.check_cancelled() == []   # signalled once only
    assert cancelled_here == [job.job_id]


# ── HTTP, MCP and admin ─────────────────────────────────────────────────────────────────────

@pytest.fixture
def api(monkeypatch, runner_env):
    from fastapi.testclient import TestClient

    import app.api as api_module

    runner, jobs, q, ran, drain = runner_env
    container = runner._get_container()
    container.engagement_history.get = lambda job_id: None
    container.engagement_history.get_conversation = lambda cid: []
    monkeypatch.setattr(api_module, "container", container)
    monkeypatch.setattr(api_module, "runner", runner)
    monkeypatch.setattr(api_module, "_authenticate",
                        lambda authorization: BOB if authorization == "Bearer bob" else ALICE)
    return TestClient(api_module.app), jobs, q, drain


def test_http_queued_job_shows_its_position_and_can_be_cancelled(api):
    client, jobs, q, drain = api
    first = client.post("/v1/chat", json={"prompt": "one"}).json()
    assert first["queued"] is False
    second = client.post("/v1/chat", json={"prompt": "two"}).json()
    assert second["queued"] is True and second["queue_position"] == 1

    polled = client.get(f"/v1/chat/{second['job_id']}").json()
    assert polled["status"] == "queued" and polled["queue"]["position"] == 1 and polled["cancel_reason"] is None

    assert client.post(f"/v1/chat/{second['job_id']}/cancel", headers={"Authorization": "Bearer bob"}).status_code == 403
    cancelled = client.post(f"/v1/chat/{second['job_id']}/cancel").json()
    assert cancelled["status"] == "cancelled" and cancelled["cancel_reason"] == "user"
    assert cancelled["prompt"] == "two" and cancelled["queue"] is None
    drain()
    assert jobs.get(first["job_id"]).status == EngagementStatus.SUCCEEDED


def test_http_busy_is_429_or_503_with_the_prompt(api):
    client, jobs, q, drain = api
    client.post("/v1/chat", json={"prompt": "one"})
    client.post("/v1/chat", json={"prompt": "two"})
    client.post("/v1/chat", json={"prompt": "three"})
    res = client.post("/v1/chat", json={"prompt": "four"})
    assert res.status_code == 429 and res.headers["retry-after"] == "60"
    body = res.json()
    assert body["reason"] == "user_limit" and body["prompt"] == "four" and body["job_id"]
    assert jobs.get(body["job_id"]).cancel_reason == CANCEL_BUSY

    q.waiting.extend((f"x{i}", f"other{i}", q.now) for i in range(20))  # fill the line
    res = client.post("/v1/chat", json={"prompt": "five"}, headers={"Authorization": "Bearer bob"})
    assert res.status_code == 503 and res.json()["reason"] == "queue_full"


def test_mcp_start_status_and_cancel(monkeypatch, runner_env):
    import app.mcp_server as mcp_server

    runner, jobs, q, ran, drain = runner_env
    container = runner._get_container()
    container.engagement_history.get = lambda job_id: None
    container.engagement_history.get_conversation = lambda cid: []
    monkeypatch.setattr(mcp_server, "container", container)
    monkeypatch.setattr(mcp_server, "runner", runner)
    monkeypatch.setattr(mcp_server, "_authenticate", lambda ctx: ALICE)

    assert mcp_server.scotri_start("one", None)["queued"] is False
    second = mcp_server.scotri_start("two", None)
    assert second["queued"] is True and second["queue_position"] == 1
    status = mcp_server.scotri_status(second["job_id"], None)
    assert status["status"] == "queued" and status["queue"]["position"] == 1 and status["prompt"] == "two"
    cancelled = mcp_server.scotri_cancel(second["job_id"], None)
    assert cancelled["status"] == "cancelled" and cancelled["cancel_reason"] == "user"


def test_admin_queue_view_and_cancel(monkeypatch, api):
    import app.api as api_module

    client, jobs, q, drain = api
    container = api_module.container
    container.role_set = lambda name: {"scotri.admin"} if name == "ADMIN_ROLES" else set()
    audit = []
    container.admin_audit_log = types.SimpleNamespace(append=audit.append)
    admin = Principal(subject="ada", display_name="Ada", platform="web", roles=["scotri.admin"])
    monkeypatch.setattr(api_module, "_authenticate", lambda authorization: admin if authorization == "Bearer admin" else ALICE)

    client.post("/v1/chat", json={"prompt": "one"})
    waiting = client.post("/v1/chat", json={"prompt": "two"}).json()["job_id"]
    view = client.get("/v1/admin/queue", headers={"Authorization": "Bearer admin"}).json()
    assert [w["job_id"] for w in view["waiting"]] == [waiting] and view["waiting"][0]["position"] == 1
    assert len(view["running"]) == 1 and view["limits"]["max_running"] == 1
    assert client.get("/v1/admin/queue").status_code == 403  # not an admin

    res = client.post(f"/v1/admin/queue/{waiting}/cancel", headers={"Authorization": "Bearer admin"})
    assert res.status_code == 200 and res.json()["cancelled"]["cancel_reason"] == "admin"
    assert jobs.get(waiting).cancel_reason == "admin" and "by an admin" in jobs.get(waiting).error
    assert client.post(f"/v1/admin/queue/{waiting}/cancel", headers={"Authorization": "Bearer admin"}).status_code == 409
    assert any(e.action == "cancel_waiting" and e.detail == "owner=alice" for e in audit)
