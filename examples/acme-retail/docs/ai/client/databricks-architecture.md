---
description: "Bronze -> silver -> gold medallion architecture for the Databricks ETL repo of the fictional Acme Retail platform, catalogs/schemas, and this agent's read boundary: notebook source from the mirror, gold corroborated with governed live SQL."
last-validated-commit: n/a — reference doc for the fictional Acme Retail platform
---

# Architecture — Databricks ETL (`Acme_Databricks_WS`) — read boundary for this agent

This repo ingests source systems and builds the **`gold` analytics layer** the Fabric repos read. This
agent reads the notebook source from the local read-only mirror (`adf-databricks-read`) and verifies
`gold.*` **empirically** with live `SELECT`/`DESCRIBE` queries (`databricks-query`, through the query
governor). Where the two disagree, that disagreement is itself a finding.

```
Sources           Bronze (brnz)        Silver (slvr)        Gold (gold)            Consumers
───────           ─────────────        ─────────────        ──────────             ─────────
Store POS   ─┐    raw delta, via       cleaned, typed,      analytics-ready,       Fabric semantic
ERP         ─┼──► ADF Copy +     ──►   deduped via    ──►   SCD Type 2,      ──►   models; the Insights
E-commerce  ─┤    Auto Loader          admin mapping        business logic         app (insights schema)
Insights app   ─┘    (brnz.*)             (slvr.*)             (gold.*)
                  ▲                    ▲                    ▲
                  notebook/pipeline source read from the    NOTEBOOK SOURCE READ, PLUS
                  mirror (adf-databricks-read)              live SELECT/DESCRIBE against gold.*
```

### A second consumer, alongside Fabric: Insights

The **Insights app** (the field team's store-visit app) reads a separate `insights` schema directly, with no
serving copy in between. A Insights investigation queries `acme_prod.insights.*` live exactly as it would a
`gold` table, and reads the producing notebook from the mirror the same way.

## Catalogs & schemas

| Catalog | Branch | Use | This agent's access |
|---------|--------|-----|----------------------|
| `acme_prod` | `release_main` | production Unity Catalog — **the default, unless Dev is explicitly named** | `SELECT`/`DESCRIBE`/`SHOW` only, through the governor |
| `acme_dev` | `develop_main` | dev — may be in active use by a developer | same, **only when Dev is named**, after a presence check |

There is no UAT tier for Databricks. A "UAT" question about Databricks data is an `acme_dev` question.

| Schema | Layer | Notes | This agent's access |
|--------|-------|-------|----------------------|
| `brnz` | Bronze | raw ingestion | notebook source only |
| `slvr` | Silver | standardised, deduped | notebook source only |
| `gold` | Gold | analytics-ready; SCD Type 2 (`IS_CURRENT_FLAG`, `VALID_FROM`, `VALID_TO`, `CHECK_SUM`) | notebook source **and** live queries |
| `insights` | Insights app tables | e.g. `acme_prod.insights.store_visit` | live queries + notebook source |
| `admin` | Metadata | mapping/audit tables | not queried |

## Ingestion — Azure Data Factory

Source systems land in bronze via ADF. **ADF's Dev and Prod are two separate repos, not two branches**
— see [`repo-registry.md`](repo-registry.md). This agent reads the Prod repo's pipeline, dataset,
trigger and linkedService JSON from the mirror — the authored source, never the generated ARM-template
folder — and reads the Dev repo live only when asked. A trigger schedule, Copy mapping and dataset names
are read from the JSON, never inferred from a name.

## The pipeline (orchestration notebooks — read from the mirror)

`NB_Bronze_*`, `NB_Silver_*`, `NB_Gold_*` orchestrators and the `Gold_Notebooks/<SOURCE>/Wave<N>/USP_*`
"stored procedure" notebooks are read from `mirror-active/databricks/`. Join predicates, filters,
grouping, the `Wave<N>` build order and the SCD2 merge pattern are read directly, not inferred.

## Notes specific to this agent

- **No writes, ever, to either catalog, and none to the mirror.** `acme_dev` is read exactly like
  `acme_prod`; there is no "working catalog".
- **Notebook-variant confirmation is possible.** Since the orchestrator is readable, check whether a
  `_RT`/`_V1`/`_bkp` copy is actually invoked before calling it "the live one", and state the mirror's
  sync time, since the mirror can lag the true HEAD.

## Where to go next
- Query gold (SDK, service-principal auth, through the governor) → [`fabric-tool-access.md`](../fabric-tool-access.md)
- Gold table → producing notebook → consuming Fabric model → [`lineage-map.md`](lineage-map.md)
