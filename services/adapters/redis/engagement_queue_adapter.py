# Copyright (c) 2026 Preyas Kulshrestha. Part of the Scotri engine; MIT License (see LICENSE).
# Keep this notice in copies and derived works, including AI-assisted ones.
"""Redis-backed EngagementQueue (domain/engagement_queue.py, OPEN_ITEMS.md #32).

Keys, under one prefix:
- `waiting`: sorted set of job ids scored by a sequence number, so order is strictly first come,
  first served.
- `owners` / `enqueued`: hashes job id -> subject / enqueue time, for the per-user limit, expiry
  and the admin view.
- `running`: sorted set of job ids scored by lease expiry (Unix seconds).
- `seq`: the sequence counter.

Every step that reads and then writes runs as one Lua script, so two processes can't both take the
last slot or both start the same waiting job. Time comes from the Redis server (TIME), not each
host's clock, so leases and expiry agree across processes.
"""
from __future__ import annotations

import redis

from adapters.redis.errors import translate_redis_errors
from domain.engagement_queue import Admission, AdmissionOutcome, QueueSettings
from ports.engagement_queue import QueueSnapshot, RunningEntry, WaitingEntry

_ADMIT = """
local now = tonumber(ARGV[3])
local live = redis.call('zcount', KEYS[2], '(' .. ARGV[3], '+inf')
local nwait = redis.call('zcard', KEYS[1])
if nwait == 0 and live < tonumber(ARGV[5]) then
  redis.call('zadd', KEYS[2], ARGV[4], ARGV[1])
  return {1, 0}
end
if nwait >= tonumber(ARGV[6]) then return {3, 0} end
local mine = 0
for _, m in ipairs(redis.call('zrange', KEYS[1], 0, -1)) do
  if redis.call('hget', KEYS[3], m) == ARGV[2] then mine = mine + 1 end
end
if mine >= tonumber(ARGV[7]) then return {4, 0} end
local seq = redis.call('incr', KEYS[5])
redis.call('zadd', KEYS[1], seq, ARGV[1])
redis.call('hset', KEYS[3], ARGV[1], ARGV[2])
redis.call('hset', KEYS[4], ARGV[1], ARGV[3])
return {2, redis.call('zrank', KEYS[1], ARGV[1]) + 1}
"""

_CLAIM = """
local live = redis.call('zcount', KEYS[2], '(' .. ARGV[1], '+inf')
if live >= tonumber(ARGV[3]) then return false end
local head = redis.call('zrange', KEYS[1], 0, 0)
if #head == 0 then return false end
redis.call('zrem', KEYS[1], head[1])
redis.call('hdel', KEYS[3], head[1])
redis.call('hdel', KEYS[4], head[1])
redis.call('zadd', KEYS[2], ARGV[2], head[1])
return head[1]
"""

_REMOVE_WAITING = """
local removed = redis.call('zrem', KEYS[1], ARGV[1])
redis.call('hdel', KEYS[2], ARGV[1])
redis.call('hdel', KEYS[3], ARGV[1])
return removed
"""

_REAP = """
local expired = redis.call('zrangebyscore', KEYS[1], '-inf', ARGV[1])
if #expired > 0 then redis.call('zremrangebyscore', KEYS[1], '-inf', ARGV[1]) end
return expired
"""


def _s(value) -> str:
    return value.decode() if isinstance(value, bytes) else str(value)


class RedisEngagementQueue:
    """Implements ports.engagement_queue.EngagementQueue."""

    def __init__(self, client: redis.Redis, key_prefix: str = "engagement-queue:") -> None:
        self._client = client
        self._waiting = key_prefix + "waiting"
        self._owners = key_prefix + "owners"
        self._enqueued = key_prefix + "enqueued"
        self._running = key_prefix + "running"
        self._seq = key_prefix + "seq"

    def _now(self) -> float:
        seconds, micros = self._client.time()
        return int(seconds) + int(micros) / 1_000_000

    @translate_redis_errors
    def admit(self, job_id: str, subject: str, settings: QueueSettings) -> Admission:
        now = self._now()
        code, position = self._client.eval(
            _ADMIT, 5, self._waiting, self._running, self._owners, self._enqueued, self._seq,
            job_id, subject, repr(now), repr(now + settings.lease_seconds),
            settings.max_running, settings.max_length, settings.max_waiting_per_user,
        )
        outcome = {1: AdmissionOutcome.START, 2: AdmissionOutcome.QUEUED,
                   3: AdmissionOutcome.QUEUE_FULL, 4: AdmissionOutcome.USER_LIMIT}[int(code)]
        return Admission(outcome, int(position) if outcome == AdmissionOutcome.QUEUED else None)

    @translate_redis_errors
    def claim_next(self, settings: QueueSettings) -> str | None:
        now = self._now()
        claimed = self._client.eval(_CLAIM, 4, self._waiting, self._running, self._owners, self._enqueued,
                                    repr(now), repr(now + settings.lease_seconds), settings.max_running)
        return _s(claimed) if claimed else None

    @translate_redis_errors
    def release(self, job_id: str) -> None:
        self._client.zrem(self._running, job_id)

    @translate_redis_errors
    def renew(self, job_ids: list[str], settings: QueueSettings) -> None:
        if not job_ids:
            return
        expiry = self._now() + settings.lease_seconds
        pipe = self._client.pipeline()
        for job_id in job_ids:
            pipe.zadd(self._running, {job_id: expiry}, xx=True)  # never re-adds a reaped job
        pipe.execute()

    @translate_redis_errors
    def reap_expired(self) -> list[str]:
        return [_s(j) for j in self._client.eval(_REAP, 1, self._running, repr(self._now()))]

    @translate_redis_errors
    def remove_waiting(self, job_id: str) -> bool:
        return bool(self._client.eval(_REMOVE_WAITING, 3, self._waiting, self._owners, self._enqueued, job_id))

    @translate_redis_errors
    def waiting_older_than(self, seconds: int) -> list[str]:
        cutoff = self._now() - seconds
        return [_s(j) for j, at in self._client.hgetall(self._enqueued).items() if float(_s(at)) <= cutoff]

    @translate_redis_errors
    def position(self, job_id: str) -> int | None:
        rank = self._client.zrank(self._waiting, job_id)
        return None if rank is None else int(rank) + 1

    @translate_redis_errors
    def holds_slot(self, job_id: str) -> bool:
        expiry = self._client.zscore(self._running, job_id)
        return expiry is not None and float(expiry) > self._now()

    @translate_redis_errors
    def snapshot(self) -> QueueSnapshot:
        now = self._now()
        pipe = self._client.pipeline()
        pipe.zrange(self._waiting, 0, -1)
        pipe.hgetall(self._owners)
        pipe.hgetall(self._enqueued)
        pipe.zrangebyscore(self._running, f"({now!r}", "+inf", withscores=True)
        waiting_ids, owners, enqueued, running = pipe.execute()
        owners = {_s(k): _s(v) for k, v in owners.items()}
        enqueued = {_s(k): float(_s(v)) for k, v in enqueued.items()}
        waiting = [WaitingEntry(_s(j), owners.get(_s(j), "?"), enqueued.get(_s(j), now)) for j in waiting_ids]
        return QueueSnapshot(waiting, [RunningEntry(_s(j), float(score)) for j, score in running])
