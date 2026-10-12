---
description: "End-to-end Power BI / Fabric architecture for the fictional Acme Retail platform -- source systems -> Databricks medallion -> gold -> semantic model M partitions -> DAX -> report visuals -> the two consumption paths (Insights app embed vs PBI Service). Read mechanics: the local read-only mirror, never a write."
last-validated-commit: n/a — reference doc for the fictional Acme Retail platform
---

# Architecture — how data reaches a Power BI report

Reports and semantic models live in the Fabric repos listed in [`repo-registry.md`](repo-registry.md)
(two legacy repos plus three new ones during a migration). This agent never writes to any of them;
all are read from the local read-only mirror (see the `fabric-read` skill).

| Repo | What's in it |
|---|---|
| `fabric_core` (`main`) | company-wide sales and category reports, landing pages |
| `fabric_core` (`hq_live`) | head-office faces of the same report families |
| `fabric_secondary` (`regional_main`) | **Rep Scorecard**, **Visit Status**, store-coverage reports |
| `fabric_hq` / `fabric_stores` / `fabric_regional` | the new audience-scoped structure, filling up as reports migrate |

🔴 **`Insights/` exists in more than one repo** with the same subfolder names (`Sales/`, `Field/`). A
bare path like `Insights/Sales/…` is ambiguous — always name the repo (and, for `fabric_core`, the
branch).

```
Source systems      Databricks (lakehouse, medallion)          Power BI (Fabric repos)
──────────────      ─────────────────────────────────         ───────────────────────
Store POS     ─┐                                               Semantic models (.tmdl)
ERP           ─┼─►  brnz.* ──► slvr.* ──► gold.*     ─────►     ├─ M partitions (import from gold.*)
E-commerce    ─┤    (raw)      (clean)    (analytics,           ├─ DAX measures
Insights app     ─┘                          SCD Type 2)           └─ RLS roles
                                                                     │
                                                                     ▼
                                                               Reports (.pbir / report.json)
                                                                     │
                                                         ┌───────────┴───────────┐
                                                         ▼                       ▼
                                                  Insights app (embedded)    Power BI Service
```

**This agent's read boundary:** ADF ingestion and the bronze/silver notebooks upstream of gold are read
from the mirror (`adf-databricks-read`) but not re-verified against live bronze/silver data. `gold.*`
is both read as notebook source and queried live (schema + data) — flag any disagreement. Everything
from `gold.*` rightward (M partitions, DAX, RLS, visuals) is read from the mirror via `fabric-read`.

## The layers

| Layer | Where | What it is | This agent's access |
|-------|-------|-----------|----------------------|
| Source | external | store POS, ERP, e-commerce platform, the Insights field app (own MySQL store) | none |
| Ingestion | ADF | pipelines land raw into ADLS / bronze | pipeline JSON via `adf-databricks-read` (mirror) |
| Bronze / Silver / Gold | Databricks (`acme_prod` / `acme_dev`) | medallion ETL | notebook source via the mirror; **gold also corroborated live** via governed `SELECT`/`DESCRIBE` |
| Semantic model | Fabric repo, `*.SemanticModel/definition/` | TMDL: tables, DAX measures, M partitions, relationships, RLS roles | `fabric-read` (mirror) |
| Report | Fabric repo, `*.Report/` | visual layout bound to a semantic model via `definition.pbir` | `fabric-read`, `report-bindings` (mirror) |

## How a semantic model pulls data

Each fact/dim table has an **M partition** that imports from a gold table via the
`Databricks.Catalogs(...)` connector. Example (`fabric_core`,
`Insights/Sales/Sales Overview.SemanticModel`, table `sales_daily`):

```m
Source = Databricks.Catalogs("adb-2222222222222222.4.azuredatabricks.net", Compute, [...]),
acme_dev_Database = Source{[Name=CatalogName, Kind="Database"]}[Data],   // CatalogName param
test_Schema      = acme_dev_Database{[Name=SchemaName, Kind="Schema"]}[Data],  // SchemaName ("gold")
table            = test_Schema{[Name="sales_daily", Kind="Table"]}[Data]
```

> ⚠️ Read the M **parameters**, not the variable names. `acme_dev_Database`/`test_Schema` are leftover
> identifiers from a dev-era copy; the catalog/schema come from `CatalogName`/`SchemaName` (or a
> hardcoded literal). Never conclude "this reads dev" from a variable name.

**Two partition styles coexist** — check which one you're looking at:

| Style | Where the catalog lives |
|---|---|
| **Parameterised** | `definition/expressions.tmdl` → `CatalogName`, `SchemaName`, `Compute` |
| **Hardcoded inline** | literal `Name="acme_prod"` / `Name="gold"` in each table's M |

For a hardcoded model the catalog can drift **per table** — check every partition, not one sample.

### Offline file vs live Service parameter
Parameters in `expressions.tmdl` can be overridden as dataset parameters in the Power BI Service. Read
the live value over XMLA (`dax-debug`) before a finding depends on it.

> ℹ️ **Two Databricks hosts, one Unity Catalog.** Reports' M queries may reference a different
> workspace host from the one `databricks-query` connects to. Two workspaces sharing one Unity Catalog
> metastore see the same `gold.*` data, so cross-checks are valid. A `WorkspaceName` parameter in TMDL
> can be a **stale label** — never pass it to a connection call.

## Cross-repo lineage

**This agent traces the full chain**: report visual → DAX measure (`dax-debug`) → model table →
gold-table name (`fabric-read`) → the producing notebook's logic (`adf-databricks-read`) → the gold
table's live schema/data (`databricks-query`) → the ADF pipeline feeding bronze. Record any
notebook-vs-live-data disagreement explicitly — see [`lineage-map.md`](./lineage-map.md).

## Where to go next

- Folder map, naming, PBIP file formats, parameters → [`fabric-conventions.md`](./fabric-conventions.md)
- Fetch Fabric files / query Databricks / evaluate DAX → [`fabric-tool-access.md`](../fabric-tool-access.md)
- **Guardrail:** any change to an M partition, DAX measure, or RLS role is a semantic-model edit —
  this agent never makes one, never publishes one, and only reads the mirror.
