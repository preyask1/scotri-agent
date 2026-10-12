# Benchmark

Replays the fixed prompts in `client/benchmark-cases.json` (client-owned; an example is in
`examples/acme-retail/client/`) against a running Scotri API, one at a time, and records how
each run went, so a change to the instructions, skills or code comes with before/after numbers
(OPEN_ITEMS.md #68).

```bash
services/.venv/Scripts/python.exe tools/benchmark/run_benchmark.py
```

- `--case <id>` (repeatable) runs a subset. `--api` targets another deployment (default
  `http://localhost:8000`, or `SCOTRI_BENCH_API`).
- The token is `SCOTRI_BENCH_TOKEN`, else `SCOTRI_DEV_AUTH_TOKEN` from `services/.env`. It is never
  printed.
- Each run writes `results/<UTC time>.json` and a `.md` report next to it, and compares itself with the
  newest earlier results file (or `--baseline <file>`). `--report-only <file>` reprints a report.
- Exit code 1 if any case fails a check.

**What it costs.** Five real engagements, about 25–40 minutes in total, with real model usage and
governed Databricks/Jira/Fabric reads. Every case is read-only; a Jira write by any case fails the
run.

**What it records** (never the answer text, so results files are safe to commit):

| Metric | From |
|---|---|
| `agent_seconds` | first to last step of the agent transcript (`/steps`) |
| `wall_seconds`, `queued_seconds` | the harness's own clock |
| `tool_calls`, `tools`, `skills`, `phases` | `/steps` and `progress` |
| `refused_calls` | calls blocked before running: tool-scope refusals and `<tool_use_error>` blocks |
| `rate_limited_calls` | governed reads refused by the per-user rate limit (or the answer saying so) |
| `failed_calls` | a non-zero exit or `"ok": false` in the result preview, rate limits excluded |
| `whole_notebook_reads` | `mirror-read` of an `.ipynb` without `--lines` |
| `retrieve_seen` | an `app.cli retrieve` call (a call inside a batch file can read as `false`) |
| `jira_writes` | any `jira-comment`/`-edit-comment`/`-create-bug`/`jira-attach` call; must be 0 |
| `verdict`, `confidence`, `learning_recorded` | `/v1/learnings/{job_id}` |
| `diagrams`, `unchecked_mentions`, `not_executed_mentions` | the result |

**Rate limits.** Every case runs as the same user, and the governor's per-user limit (placeholder
30 calls/hour, OPEN_ITEMS.md #3) is shared with anything else that user does. Two runs back to back
exhaust it, and refused reads then make a case look worse than it is. Run it with a fresh window; a
case that lost reads to the limit is marked RATE-LIMITED in the report and isn't comparable.

**Reading the numbers.** Live data, the mirror and the tickets change between runs, so treat one
run's delta as a signal. A change that holds across two or three runs is real. Keep prompts in
`client/benchmark-cases.json` stable; editing one starts a new baseline for that case.
