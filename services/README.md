# Scotri services — hexagonal (ports & adapters)

Real, runnable Python backing the behavior described in `../.claude/skills/*.md`. Those skill files
tell Claude *what* must happen (rate limiting, presence checks, mirror-vs-live reads); this code is
*how* it actually happens, deterministically, outside the LLM's own judgment — see the "LLM vs
scripts" split discussed in the system-design doc.

## Layout

```
domain/      pure business logic — no redis / databricks-sdk / requests / git imports, ever
ports/       abstract interfaces (typing.Protocol) the domain layer depends on
adapters/    concrete, technology-specific implementations of each port:
               databricks, fabric (DAX + refresh-history), adf (pipeline/activity run-history),
               jira (read + write), mirror_sync, redis, registry, repo_content (mirror + live ADO),
               secrets, azure_ad (agent-service auth), interaction_log (agent-service audit trail),
               agent_sdk (Claude Agent SDK runtime)
app/         composition root (container.py) + three real entry points:
               cli.py           — the skill-driven CLI (`python -m app.cli <command>`)
               sync_service.py  — the scheduled mirror-refresh daemon
               api.py           — the agent-service HTTP API (FastAPI; see OPEN_ITEMS.md, 2026-09-22)
config/      repo_registry.yaml + model_registry.yaml + adf_factories.yaml — machine-readable twins
             of the docs/ai/*.md registries of the same name (model_registry.yaml sets the
             model — see docs/ai/model-registry.md; adf_factories.yaml has no docs/ai/ twin yet — it's small
             enough that its own header is the full doc, see OPEN_ITEMS.md)
tests/       domain-layer + CLI-boundary tests — no live Redis/Databricks/Fabric/Jira needed (see below)
```

**Dependency rule:** `domain/` and `ports/` never import from `adapters/` or `app/`. `adapters/`
never import each other. Only `app/container.py` imports concrete adapter classes — everything else
depends on the port (the `Protocol`), not the implementation. This is what makes each swap
(env vars → Key Vault, local Redis → managed Redis) a change in exactly one file.

## Status

Every file here is real, working code that ran against a production estate. To run it yourself you
need:

1. **Redis** (local is fine) for the governor, presence claims, the job store and the queue.
2. **Credentials** for whichever backends you point it at. `services/.env.example` lists every
   variable the adapters expect; copy it to `.env` and fill in real values (a read-only ADO PAT, a
   Databricks service principal, a Fabric service principal, a Jira service-account token, an
   Anthropic API key). **Never commit `.env`** — it's gitignored.
3. **Your own client profile.** `config/*.yaml` and `../docs/ai/client/` are generated from
   `../client/profile.yaml` by `python -m app.setup` (see [`../SETUP.md`](../SETUP.md)). A worked
   example for a fictional retailer is in `../examples/acme-retail/`.

The test suite needs none of this.

## Setup

```bash
cd services
python -m venv .venv
.venv\Scripts\activate            # or: source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env               # then fill in real values
```

## Testing

`tests/` covers `domain/` (pure logic — rate-limit/concurrency boundary conditions, presence-claim
staleness, the SELECT-only guard) and the `app/cli.py` boundary (the `--confirmed` gate in front of
every Jira write, the maintenance-flag short-circuit in front of every mirrored read, and the
read-only `governor-check` reporting path) using lightweight fakes for `RateLimiter`/`PresenceStore`/
`MaintenanceFlag`/`mirror_reader`/`jira_writer` — no live Redis, Databricks, Fabric, or Jira needed to
run these. Requires no setup beyond `pip install -r requirements.txt` (`pytest` is already listed):

```bash
cd services
python -m pytest -v
```

## Running

```bash
# One-off mirror refresh (also what a human/ops would run manually, per the "scheduled + manual"
# design — this is the exact same code path the scheduled daemon below calls). An admin can do the
# same from the admin panel's "Sync now" button (POST /v1/admin/mirror/sync). All three take one
# Redis lock (mirror:sync-lock) for the whole run, so a second one is refused, naming the holder:
python -m app.cli sync-run-once

# The scheduled daemon (reads SYNC_SCHEDULE_CRON from .env, default: once daily at 03:00):
python -m app.sync_service

# The agent-service HTTP API (needs AZURE_AD_TENANT_ID/AZURE_AD_API_AUDIENCE filled in — see
# .env.example's Azure AD section; requires uvicorn, already in requirements.txt):
uvicorn app.api:app --host 0.0.0.0 --port 8000

# Or start Redis, the API and scotri-ui from the launcher (tools/launcher/, "Start Scotri
# Launcher.command" on macOS, ".bat" on Windows); its "Open Admin Panel" button opens /admin once
# the API is up.

# Windows: engagements need Git Bash. Every governed read is an app.cli call through the agent's Bash
# tool, and when the Claude CLI can't find Git Bash it starts with no Bash tool at all. The adapter
# sets CLAUDE_CODE_GIT_BASH_PATH itself (from the `git` on PATH, Scoop, or Program Files), and stops
# an engagement that still starts without Bash, with an error saying so, rather than letting it run
# blind. If Git lives somewhere else, set CLAUDE_CODE_GIT_BASH_PATH=<Git>\bin\bash.exe in .env.
# Seen 2026-10-07 with the API started from a Claude Code session's shell; the launcher was fine.

# Local/test-frontend run with no Entra app registration at all — set SCOTRI_AUTH_MODE=dev and
# SCOTRI_DEV_AUTH_TOKEN in .env (see .env.example's "Dev/testing auth bypass" section), then send
# `Authorization: Bearer <SCOTRI_DEV_AUTH_TOKEN>` from the test frontend instead of a real AAD token.

# Check the repo registry resolves what you expect:
python -m app.cli registry-resolve --repo-key databricks --environment prod

# Governor state / presence checks (mostly exercised indirectly by the databricks/fabric adapters —
# these are exposed directly mainly for inspection/testing):
# governor-check is READ-ONLY reporting — it never increments the rate counter or acquires a
# concurrency slot, so calling it never consumes real quota. The actual accept/reject decision only
# happens inside a real governed call (databricks-select, dax-execute) via domain/governed_call.py.
python -m app.cli governor-check --user alice --backend databricks
python -m app.cli presence-check --entity PROJ-1234 --owner alice

# Release a claim early instead of waiting out its 30-minute TTL (no-op if --owner doesn't hold it):
python -m app.cli presence-release --entity PROJ-1234 --owner alice

# Refresh a held claim's last_heartbeat + TTL for long-running work (refuses if --owner doesn't hold it):
python -m app.cli presence-heartbeat --entity PROJ-1234 --owner alice

# ADF pipeline/activity run history (OPEN_ITEMS.md: decided 2026-09-27, Prod factory only).
# --pipeline is mandatory and non-empty (domain/adf_run_policy.py) and the window is capped at
# 14 days — both refuse the "unfiltered query only returns the newest ~100 runs" gotcha that cost
# time on an earlier failure investigation. Needs
# config/adf_factories.yaml's subscription_id filled in before it can actually reach ARM.
python -m app.cli pipeline-runs --user alice --pipeline PL_Forecast_Sales PL_Forecast_Extract \
  --start 2026-09-26T00:00:00Z --end 2026-09-27T00:00:00Z
python -m app.cli activity-runs --user alice --run-id <runId-from-pipeline-runs> \
  --start 2026-09-26T00:00:00Z --end 2026-09-27T00:00:00Z

# Fabric/Power BI dataset refresh history (OPEN_ITEMS.md: decided 2026-09-27, Prod-published
# models only). Without --refresh-id, lists recent attempts; with it, drills into per-partition
# detail — only ever populated for a dataset on Enhanced Refresh (a Standard-refresh dataset
# reports `available: false`, which is the expected answer, not an error):
python -m app.cli refresh-history --user alice --workspace "<Fabric workspace>" --dataset "Sales Model" --top 10
python -m app.cli refresh-history --user alice --workspace "<Fabric workspace>" --dataset "Sales Model" --refresh-id <id-from-the-list-above>

# The steps the agent took for an engagement job_id (read-only; from its session transcript on this
# host, so tool inputs/outputs are truncated and secret-redacted). Same data over HTTP, owner-only:
# GET /v1/chat/{job_id}/steps  and  GET /v1/engagements/{job_id}/steps
python -m app.cli job-trace --job-id <job_id>

# Several independent READ commands in one process, in parallel (default 4 workers, max 8, max 25
# items). Each item runs its normal cmd_* — still governed, rate-counted and redacted; write
# commands are refused (app/cli.py BATCHABLE_COMMANDS). Measured 2026-10-01: 3 uncached Databricks
# queries + mirror-list + jira-get-issue + registry-resolve took 20.6s as six calls, 6.0s as one batch.
echo '[["jira-get-issue","--key","PROJ-2085"],["mirror-list","--repo-key","databricks"]]' > /tmp/b.json
python -m app.cli batch --file /tmp/b.json      # or --file - to read stdin
```

## Why `config/repo_registry.yaml` duplicates `docs/ai/client/repo-registry.md`

`docs/ai/client/repo-registry.md` is what Claude reads — prose, tables, reasoning about *why*. This YAML
file is what Python code parses — structured, no markdown to interpret. They describe the same
facts from two different consumers' point of view. **Keep them in sync by hand for now**; if they
ever drift, `docs/ai/client/repo-registry.md` is the source of truth to reconcile toward. A later pass
could generate one from the other — not done yet, since it's not worth the complexity until the
registry is changing often enough for hand-sync to actually become a burden.

`config/model_registry.yaml` follows the same twin-of-a-docs-file convention (its prose counterpart
is `docs/ai/model-registry.md`). `app/container.py` reads its `default.model` and passes it to every
engagement's Agent SDK call.
