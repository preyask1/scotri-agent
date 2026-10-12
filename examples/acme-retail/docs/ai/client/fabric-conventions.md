---
description: "Fabric / Power BI conventions for the fictional Acme Retail platform: folder map, PBIP file formats (legacy report.json vs PBIR definition/pages), connection parameters, PBI table + RLS naming, the shard-clone duplication pattern. All paths are read from the local read-only mirror (fabric-read), never edited."
last-validated-commit: n/a — reference doc for the fictional Acme Retail platform
---

# Conventions — Fabric / Power BI

## Repo folder map

🔴 **There is more than one Fabric repo.** Before asserting where anything lives, check which one.
Several contain a top-level `Insights/` with identically-named subfolders.

### `Acme-PowerBIReports` (`fabric_core`)

| Folder | Domain |
|---|---|
| `Insights/Sales/` | Sales Overview, Category Trends, Store Performance — one clone per region shard (North, South, West) |
| `Insights/Field/` | Visit Tracker, My Targets |
| `Finance/` | Margin and Shrink reports |
| repo root | `Home Page`, `HQ Landing Page` |

### `AcmeFabricReports` (`fabric_secondary`)
**Everything is under `Insights/`.**

| Folder | Domain |
|---|---|
| `Insights/Scorecard/` | **Rep Scorecard** — one model, several report faces (base, per level, HQ) |
| `Insights/Field/` | Visit Status, Store Coverage |

> **Consumption rule.** The Insights family is consumed **both** embedded in the Insights app **and** in the
> Power BI Service, with different RLS handling. The signal is the presence of `Field`/`HQ` roles, not
> the folder. If a finding needs to reproduce one user's RLS slice, hand it back to a human with a
> View-As capability rather than approximating it.

## The shard-clone duplication pattern (read before analysing any report)

One logical report is often hand-cloned per region shard, each clone bound to its own model copy
holding one slice of the data. A clone set can span repos.

🔴 **Clones drift.** Over time individual clones get edited, and some get converted to PBIR while
others stay legacy. Never assume a clone set is byte-identical or single-format: when enumerating one
via `fabric-read`, re-derive each instance's format and content fresh.

## PBIP file formats

| File | Purpose | Key fields |
|------|---------|-----------|
| `definition.pbir` | Report → model binding | `datasetReference.byPath.path`. Can point **cross-folder**. |
| `definition.pbism` | Semantic model wrapper | points to the `definition/` TMDL folder |
| `definition/model.tmdl` | Model root | model-level settings, table list |
| `definition/tables/*.tmdl` | Table defs | columns, **DAX measures**, **M partition** (gold source), annotations |
| `definition/expressions.tmdl` | Connection parameters | `CatalogName`, `SchemaName`, `Compute`, `RegionList`… — **absent** in hardcoded-source models |
| `definition/relationships.tmdl` | Relationships | keys, cardinality, cross-filter direction |
| `definition/roles/*.tmdl` | RLS roles | one file per role; DAX `tablePermission` filters |
| **Report layout** | pages + visuals | **two layouts, both live:** legacy → one big `<Report>/report.json`; PBIR → `<Report>/definition/report.json` + `definition/pages/<id>/page.json` + `visuals/<id>/visual.json` |

To detect the layout: if `definition/pages/` exists it's PBIR; otherwise read `<Report>/report.json`.
For "what does this report show", use `report-bindings`, never a whole `report.json`.

## Connection parameters (`expressions.tmdl`)

| Parameter | Typical value | Meaning |
|-----------|------|---------|
| `CatalogName` | `acme_prod` | Unity Catalog |
| `SchemaName` | `gold` | reports read the gold layer only |
| `Compute` | `/sql/1.0/warehouses/<id>` | SQL warehouse path |
| `WorkspaceName` | e.g. `Acme-QA` | ⚠️ **can be a stale label**, not a real workspace. Pick the workspace from [`repo-map.md`](./repo-map.md). |
| `RegionList` / `Regions` / `RegionCodes` | comma-list of region codes | shard scope — **three names for one concept** in different model families; check all three when a change touches region scope |
| `RangeStart` / `RangeEnd` | `#datetime(...)` | incremental-refresh window |

## Naming conventions

### PBI tables
| Pattern | Meaning | Example |
|---------|---------|---------|
| `pulse_*` | fact/dim from the Insights app | `pulse_store_visit` |
| `*_rls` | RLS-filtered fact table | `visit_tracker_rls` |
| `param_*` | parameter / config table (slicers, toggles) | `param_kpis` |
| `*_bi` | BI-optimised view | `rep_hierarchy_bi` |
| `vw_*_scorecard` | gold view built for the Scorecard model | `vw_fact_rep_scorecard` |

⚠️ Production models can carry work-in-progress tables (`duplicate table`, `…_testing`) — don't
assume every table is load-bearing.

### RLS hierarchy columns
Level order 0 = top → 4 = rep: `NATIONAL_HEAD_CODE` (0) · `REGION_HEAD_CODE` (1) ·
`AREA_MANAGER_CODE` (2) · `TEAM_LEAD_CODE` (3) · `REP_CODE` (4).

### Databricks notebooks (ETL repo, read via `adf-databricks-read`)
| Pattern | Meaning |
|---------|---------|
| `NB_*` | orchestration / utility notebook |
| `USP_*` | gold-layer "stored procedure" style notebook |
| `VW_*` | view definition |

A naming table is a convention reference, not a substitute for reading the notebook's logic.

## Scratch / ignored paths
Don't spend a read on `_local_stash_*/`, `_preserved_archive_*/`, `_sync_backup_*/`, `*.log`,
`*.csv`, `*.zip` or `~$*` if they turn up in a listing.
