# Open items and decision log

Code comments, tests and docs cite decisions as `OPEN_ITEMS.md #N`. This is the index those numbers
point to: what was decided and why, and what is still open. **IDs are stable — never renumber.** New
items take the next free number.

Status: `Open`, `Partial`, `Parked` (decided to wait), `Done`.

> This public edition is a sanitized index. The original log also tracked one deployment's
> credentials, tenants and live verification runs; those are omitted. Numbers not listed here were
> deployment-specific.

## Architecture and safety

| ID | Decision | Status |
|---|---|---|
| #28 | **Tool scope on the unattended runtime.** A PreToolUse hook (`domain/tool_scope.py`) limits Bash to `cd` + the venv's `-m app.cli <command>` (no pipes, redirects, `$`, other programs), and Write/Edit to `scratch/`, `proposals/`, `investigations/` and the lineage map. Read/Grep/Glob may not target the mirror, `.env*`, `services/data/` or `services/logs/` (#72). Hooks run even under `bypassPermissions`, so this is structural, not prose. | Done |
| #29 | **Every outbound integration goes through the query governor**, Jira included. Reads are rate-limited, concurrency-capped and cached; writes are audit-only (`run_governed_write`: no cache, no rate slot, an audit line). | Done |
| #34 | **Secret redaction at the CLI boundary** (`domain/redaction.py`). Every command's output is pattern-redacted. Raw `Read` on the mirror bypasses it, which is why #28 refuses it. Every Jira write's text is redacted too (#72). | Done |
| #72 | **Code review fixes (2026-10-12).** (a) The governor's cache key no longer lowercases quoted literals (`'North'` and `'NORTH'` shared one cached result) and is namespaced by backend. (b) The redactor's `sig` key matches only as its own segment; it was blanking `Designation`, `assignment` and `signal` values. (c) `jira-comment`/`jira-edit-comment`/`jira-create-bug` text is redacted before it is sent, and `--body-file` reads only from `scratch/`, `proposals/` and `investigations/`. (d) The tool scope refuses Read/Grep/Glob on secrets, the raw mirror, history and logs. (e) `tests/test_architecture.py` pins the hexagonal boundary; `domain/client_setup.py` now writes its YAML with the stdlib instead of importing PyYAML. | Done |
| #73 | **No hand-off to another session.** `SendMessage` and `ListAgents` are denied in engagements, both as disallowed tools and by the scope hook: an engagement that pushed its work to a peer session ended its turn, and the turn ending is the engagement ending, so the work died unstarted. | Done |
| #22 | **A refused governed call carries structured facts** (`queue_position`, `wait_seconds`) so the agent can surface the wait honestly instead of retrying in a loop. | Done |
| — | **Databricks is SELECT-only, as an allowlist** on the leading keyword (`SELECT`/`WITH`/`DESCRIBE`/`DESC`/`SHOW`), one statement per call, with a denylist kept as a further layer. `EXPLAIN` is out. | Done |
| #44 | **Identity, roles and access model** — see `docs/ai/identity-model.md`. Platform is read from the token's `appid`/`azp`, never a header. | Done |
| #45 | **Row/column authorization of data queries.** Scotri queries as one service principal, so Unity Catalog cannot scope by the asking user. Design for phase 2 in `identity-model.md` §6. | Parked |
| #57 | Production Entra auth (`AZURE_AD_TENANT_ID`, `AZURE_AD_API_AUDIENCE`) must be set for any non-dev deployment; `SCOTRI_AUTH_MODE=dev` is local-only. | Open |

## Engagement engine

| ID | Decision | Status |
|---|---|---|
| #4 | **One LLM for every stage** (`services/config/model_registry.yaml`). Per-stage routing stays a config edit if ever needed. | Done |
| #6 | **File upload and shared deliverables.** Every `.md`/`.patch` an engagement writes under `scratch/`, `proposals/`, `investigations/` is published to the caller via a pluggable artifact store (local folder or Azure Blob). Uploads are data, never instructions. Retention via `artifacts-purge`. | Done |
| #7 | Webhook and scheduled-sweep trigger paths. Only the manual path (`/v1/chat`, `/v1/engagements`, MCP) exists; unattended runs must write nothing to Jira. | Open |
| #19 | **Durable per-user history**, redacted at write; reading someone else's needs `HISTORY_ADMIN_ROLES`. | Done |
| #25 | **Server-side conversations** (`conversation_id`, `resume_job_id`) across HTTP and MCP. | Done |
| #32 | **Engagement queue**: a FIFO in Redis when all slots are busy, per-user and total caps, max wait, cancel, admin view. | Done |
| #33 | **Engagement ceilings**: wall-clock timeout, turn ceiling, optional cost ceiling. Hitting one ends the run as incomplete, never a hang. Cross-process cancel. | Done |
| #39 | **Progress signal** for multi-minute runs: phase, skill and step counts, polled by the UI. | Done |
| #43 | **Admin panel** (`/admin`, `/v1/admin/*`): health, governor load, mirror freshness, queue, logs with secrets stripped, caller register. Behind a role nobody holds by default; every admin action is audit-logged. | Done |
| #46 | **Investigation speed.** Profiling showed most time went on overhead between steps, not queries: lazy imports, `batch`, the `<scotri-prefetch>` block, mirror text caches and `table-usage` cut the opening steps from minutes to seconds. | Done |
| #56 | The test suite never writes into the real logs (`SCOTRI_LOG_DIR`). | Done |
| #61 | The step-trace reader matches a turn by its prompt prefix, since the engine prepends run context. | Done |
| #62 | Relative paths in `.env` resolve under `services/`, not the process's working directory. | Done |
| #68 | **Grounding check and trimmed instructions.** Numbers, `*_prod` table names and file names in a final answer that appear in no tool result go back to the agent once to verify, label or remove. Agent-facing docs state each rule once. A benchmark harness (`tools/benchmark/`) gives before/after numbers for instruction changes. | Done |

## Integrations

| ID | Decision | Status |
|---|---|---|
| #3 | Governor limits come from `.env` (`GOVERNOR_RATE_LIMIT_PER_USER_PER_HOUR`, `GOVERNOR_MAX_CONCURRENT[_<BACKEND>]`). Tune from real usage. | Partial |
| #14 | Databricks Jobs API reads need a per-job `CAN_VIEW` grant for the SP; a 403 means a missing grant, not a missing credential. | Open |
| #15 | Production datastore for engagement history. Only the SQLite adapter exists; the port allows Azure SQL/PostgreSQL. The lineage index stays SQLite on local disk on Azure (derived, rebuilt from the generated lineage at start-up); a mirror hydrated from a Blob snapshot keeps its sync identity because that identity is a hash of the manifest's content, not its mtime. | Open |
| #16 | **ADF run history across tenants.** ADF may sit in a different Entra tenant from the host, so it gets its own app registration trusted via a federated credential (no secret). | Partial |
| #18 | A second Databricks workspace is used only from `bug-investigation`'s failure-chain trace, never from impact analysis. | Parked |
| #30 | Databricks job/notebook run reader (`job-runs`, `job-run-detail`). | Done |
| #31 | Mirror staleness is reported on every read (`freshness.warning`); it warns, never refuses. | Done |
| #35 | ADF trigger-run reader: tells "never triggered" apart from "triggered and failed". | Done |
| #37 | **Lineage graph index**, generated from the mirror after every sync (and at start-up for any tree whose lineage is older than its last sync): ADF trigger → pipeline → notebook (pipeline JSON), notebook → notebook (`%run`, `dbutils.notebook.run`, folder runners), notebook → table (lexical write/read classification), gold table → Fabric model → measure / RLS role → report (TMDL, PBIR). `docs/ai/client/lineage-graph.yaml` keeps only the hand-curated legs the mirror can't show. Built into a SQLite index, queried by `lineage-trace`/`lineage-coverage`/`kpi-lookup`; what the generator couldn't resolve is listed by `lineage-coverage --unresolved`, and stopped or expired ADF triggers are flagged. Unverified edges are first-class. KPI definitions attach to measure nodes from the model's own descriptions (verified) and optional report documents under `docs/ai/client/reports/` (unverified). | Done |
| #38 | Lexical (BM25) retrieval over past engagements, learnings and docs, docs indexed one heading section at a time (a hit names the section and its lines). A ranker, never a candidate generator for impact analysis. | Done |
| #41 | Jira `issuetype` is authoritative for routing (Bug → verdict first; anything else → Story path). | Done |
| #42 | Jira attachments: governed download (text extracted, redacted, raw-data exports refused) and upload via `jira-write`. | Done |

## Deployment

| ID | Decision | Status |
|---|---|---|
| #71 | **White-label deployments.** One engine, one private repo per client created from the template. Everything client-specific comes from `client/profile.yaml` through `python -m app.setup` (`init`/`render`/`secrets`/`doctor`/`bootstrap`); generated files live only in the client's repo, so upstream merges never conflict. ADF and Fabric are optional components (`domain/client_config.py` gates their commands); the display name and logo brand the admin panel, portal and API title; "Scotri" stays the engine and code name. See `SETUP.md`. | Done |

## Learning loop

| ID | Decision | Status |
|---|---|---|
| #47 | **Human verdict review**: only a human can mark a verdict upheld / partially upheld / overturned; overturned runs drop out of retrieval. | Done |
| #58 | **Shared learnings**: each investigation ends with one structured learning block, stored team-wide with data values stripped. | Done |
| #59 | **Run identity outside the agent**: a random per-run token maps to the caller in Redis, so every command is attributed to the real caller whatever `--user` the agent passes. | Done |
| #60 | Learnings browsing scopes and residuals. | Partial |
| #64 | Test plans for new requirements may compare against named UAT reference dashboards. | Done |
