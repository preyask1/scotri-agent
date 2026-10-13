---
name: dax-debug
description: "Run a read-only DAX query against a PUBLISHED Fabric/Power BI semantic model through the governed dax-execute command, for debugging and validation only; never edits a model, never publishes or refreshes. Use when: evaluate this DAX, spot-check this measure's value, debug a wrong number in a report, validate a measure under a role, check a live parameter value."
---

# DAX debug (`dax-execute`, governed, read-only)

Read-only DAX rules are in agent-instructions.md. `dax-execute` calls the Power BI REST
`executeQueries` endpoint (no XMLA/ADOMD driver), doing SP auth, workspace/model name → GUID
resolution and the governor sequence itself. Never build a REST or XMLA call yourself.

## What it's for

What a measure returns **right now** in the **published** model: validating an impact-analysis
hypothesis, spot-checking a number a ticket disputes, or reading a live dataset parameter
(`CatalogName`, a scope list) the repo's `expressions.tmdl` may have drifted from.

- **Published models only.** TMDL read via `fabric-read` is source, never something to evaluate DAX
  against. No offline PBIP folder, no PBI Desktop session.
- If the SP isn't a workspace member, hand back; never switch to a more broadly scoped identity.

## Picking the workspace

Workspace depends on the repo and branch the model is published from. Use the workspace table in
`docs/ai/client/client.md` ("Fabric workspaces"); a repo/branch with no row there is not git-synced to
a workspace you can name, so hand back rather than guess.

Never use the model's TMDL `WorkspaceName`/`Workspace` parameter: it can be a stale label and then
returns `PowerBIEntityNotFound`.

## Tenant

`FABRIC_TENANT_ID` must be the Fabric/PBI (BI) tenant, not the data tenant that holds Azure DevOps,
ADF and Databricks, when the two differ. A wrong tenant is the
commonest silent failure: `{"ok": false}` with a message naming HTTP 203 or an HTML response. That
means a wrong tenant, **not** an empty result (see `docs/ai/client/repo-map.md`).

## Procedure

1. **Resolve the model**: via `kpi-lookup --name "<measure>"` (model, DAX location, gold sources) or
   `impact-analysis`/`lineage-trace`, know its repo + branch + workspace
   (per the table above, never the TMDL literal).
2. **Run `dax-execute`** with that workspace, the model name and a plain `EVALUATE` /
   `EVALUATE {[<Measure>]}` query; add `--role` for an RLS/effective-identity check.
3. **Read the JSON** (same shape as `databricks-select`). On `"ok": false`: `credential` usually means
   the SP isn't a workspace member yet (hand back); a 203/HTML message means tenant mismatch.
4. **Compare** with the Databricks-side fact (`lineage-trace`/`databricks-query`) to localize a wrong
   number:
   - DAX matches gold but not the expectation → expectation/filter/RLS issue
   - DAX doesn't match gold → DAX/model bug, or the producing notebook (`adf-databricks-read`) doesn't
     do what's assumed
   - gold itself looks wrong → trace further upstream with `lineage-trace`
