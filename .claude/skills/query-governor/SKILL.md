---
name: query-governor
description: "The shared Redis-backed service every outbound integration passes through (Databricks, Fabric XMLA, ADO live reads, ADF/Databricks run history, Jira; reads governed, writes audit-only): per-user rate limits, a per-backend concurrency cap, a result cache and an audit log. Use when: about to run a databricks-query/dax-debug/jira call, checking whether a call is rate-limited, checking current concurrency, explaining a queued/wait response to a user."
---

# Query governor

The governor is the only thing that talks to the backends on this agent's behalf. `databricks-query`
and `dax-debug` never call the Databricks SDK or the Fabric XMLA endpoint directly. The never-bypass
rule is in `agent-instructions.md`.

- **Reads** are fully governed: rate limit, concurrency cap, a reads-only cache and an audit line.
- **Writes** (Jira only) are audit-only (`run_governed_write`): no cache (a mutation must never be
  served or deduplicated from cache), no rate slot.

State lives in one shared Redis because each user's session is a separate process and can't see the
others' load otherwise.

## What it holds

- **Rate-limit counters**: per user, per time window (e.g. queries/hour). Configurable.
- **Concurrency semaphore**: in-flight calls, **per backend**, so a Fabric burst can't starve
  Databricks users or the reverse.
- **Result cache**: keyed on the canonicalized query, short TTL; metadata/schema lookups get a longer
  TTL than data queries.
- **Audit log**: who ran what, against which environment, how long it took.

## Call sequence

1. Canonicalize the query; check the cache.
2. If not cached, check concurrency, then this user's rate limit, for the backend.
3. Within limits → execute. Otherwise surface the queue position/wait and hold the turn on it.
4. On completion, cache the result (with the right TTL) and append to the audit log.

## When a limit is hit

The CLI returns a `GovernorRejection` (category `transient`) with structured `queue_position` and
`wait_seconds`. Relay them; never retry silently and never fail without saying why. The two kinds differ:

| Kind | Fields | Say |
|---|---|---|
| Concurrency cap (too many in flight on a backend) | `queue_position` set, `wait_seconds` **null** (no honest ETA exists) | "Queued — 2 requests ahead of you on Databricks." |
| Rate limit (user's quota used up) | `wait_seconds` = real time until the window rolls over, `queue_position` null | "Rate limit reached (30/hour); quota frees in ~18 minutes." |

A concurrency rejection doesn't consume a rate-limit slot. There's no automatic block-and-resume: you
surface the rejection and decide whether to wait and retry.

## Leaked concurrency slot

A session that dies mid-query (killed, crashed, lost Redis connection) leaves the in-flight counter
incremented. The counter carries a generous TTL (`CONCURRENCY_SLOT_TTL_SECONDS`,
`adapters/redis/rate_limiter_adapter.py`), set only by the first concurrent holder, so it clears
eventually. If `governor-check` shows a `concurrency_in_flight` count that can't be real,
`governor-reset --backend <backend> --confirmed` clears it to 0 now; it is ops-only (never part of an
investigation), and only after confirming nothing is actually in flight, never reflexively.

## Hard boundaries

- **Never invent a queue position or wait.** If the governor's state can't be read, say
  `governor state unavailable — <reason>`.
- **The governor enforces limits, not query permission.** `SELECT`-only and read-only DAX are enforced
  by `databricks-query`/`dax-debug`, independently.

## Pending

- Rate-limit numbers (queries/hour/user, max concurrency per backend), whether the rate cap is
  per-backend, and automatic block-and-resume: to be set from observed usage.
- Redis connection details: from the vault/env-var pattern like every credential, never hardcoded.
