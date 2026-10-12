---
description: "Which mechanism this agent uses for which Power BI/Fabric, ADF, or Databricks task -- a local read-only mirror for repo file content, Databricks SDK (service-principal M2M, via the query governor) for gold data, Fabric REST/XMLA (service-principal, via the query governor) for DAX and live parameters. No MCP, no write-capable clone, no interactive auth anywhere."
last-validated-commit: n/a — reference doc
---

# Tool access — mirror reads + governed Databricks/Fabric queries (no MCP, no interactive auth)

Three mechanisms, one per task. No MCP server backs any of them and none uses an interactive login,
because a CLI behind `az login`, a desktop modeling tool or an OAuth-based MCP can't run unattended on
a headless server, and none of them is safe to run concurrently from many sessions without a shared
rate limit.

| Task | Tool | Skill |
|------|------|-------|
| Read repo file content (TMDL, `report.json`, `.pbir`, ADF JSON, notebook source) | **Local read-only mirror** (`mirror-active/`), refreshed by a separate sync service | `fabric-read`, `adf-databricks-read` |
| Query gold tables (schema, row counts, samples) | **Databricks Python SDK**, service-principal M2M OAuth, **through the query governor** | `databricks-query` |
| Evaluate DAX / read a live dataset parameter on a published model | **Fabric REST / XMLA**, service-principal OAuth, **through the query governor** | `dax-debug` |

---

## 1. Databricks — query gold tables

**Query workspace:** `DATABRICKS_HOST` (e.g. `<databricks host>`). Dev and
Prod share one Unity Catalog metastore, so this one connection reads either catalog.

- **Auth:** `databricks-sdk`'s `WorkspaceClient`, service-principal M2M OAuth
  (`DATABRICKS_CLIENT_ID` / `DATABRICKS_CLIENT_SECRET`, vault-sourced) — never an interactive browser
  flow; there is no browser on this host.
- **Surface:** `w.statement_execution.execute_statement(...)` for SQL; every call routed through the
  **query governor** (rate limit, concurrency cap, cache), never issued directly.
- **Warehouse:** `DATABRICKS_SQL_WAREHOUSE_ID` — ideally a dedicated, read-only warehouse for the agent.
- **Catalog:** `<prod_catalog>` by default; `<dev_catalog>` only when Dev is named. If ambiguous → prod, and say
  so.

### Guardrails
- `SELECT`/`WITH`/`DESCRIBE`/`SHOW` only, one statement per call (an allowlist on the leading keyword,
  enforced in `domain/query_guard.py`).
- No notebook execution, no job run, no pipeline trigger — ever.
- Prefer `LIMIT`ed exploratory queries.

---

## 2. Repo files — the local read-only mirror

Every product repo is read from `mirror-active/`, maintained by a separate sync service (fetch into
`mirror-staging/`, then an atomic swap). This agent only checks the maintenance flag before reading;
it never writes to the mirror and never triggers a sync.

- **Auth (for the sync service, not this agent):** a read-only PAT (`Code (Read)` scope only). No
  write-capable git credential exists anywhere in the deployment.
- **Which repo/branch:** [`repo-registry.md`](client/repo-registry.md).
- Mechanics and the narrow-before-you-read pattern: the `fabric-read` and `adf-databricks-read` skills.

---

## 3. Fabric semantic models — XMLA / Fabric REST, service-principal, read-only

One connection mode: **live, published, read-only, through the query governor.** Reading a model's
TMDL from the mirror is not the same as evaluating DAX against it; there is no offline evaluation mode
and no desktop app on this host.

- **Auth:** service-principal OAuth (`FABRIC_SP_CLIENT_ID` / `FABRIC_SP_CLIENT_SECRET`,
  `FABRIC_TENANT_ID`), client-credentials flow against `https://analysis.windows.net/powerbi/api`. The
  SP must be at least a Viewer on the target workspace.
- **Surface:** the Power BI `executeQueries` REST endpoint, routed through the governor.
- **Pick the workspace** from [`repo-map.md`](client/repo-map.md), never from a TMDL `WorkspaceName`
  parameter (it can be a stale label).
- **Guardrails:** read-only DAX and metadata only. No create/update/delete on any measure, table,
  role or partition; no publish; no refresh.

---

## Typical "validate a number" flow

1. **`fabric-read`** — read the measure's DAX and the table's M partition (gold-table name).
2. **`dax-debug`** — evaluate the DAX against the published model under the relevant filters/role.
3. **`databricks-query`** — query `<prod_catalog>.gold.<table>` with equivalent filters.
4. **`adf-databricks-read`** — read the producing notebook's logic, to check whether the live table
   (step 3) matches what the notebook claims to do.
5. **Compare.** Divergence localises to DAX/model/RLS (1–2), a notebook/live-data mismatch (3–4), or
   something upstream (ADF ingestion or source data). Trace as far as the evidence goes.

## What this workspace does NOT have

- No `az login` interactive path, no per-user token cache.
- No modeling MCP or editor extension.
- No offline PBIP *evaluation* mode.
- No View-As rendering path for reproducing a specific user's RLS-filtered view — hand that to a human.
