---
description: "Cross-repo lineage map for the fictional Acme Retail platform: ADF pipeline <-> producing notebook <-> gold table <-> consuming Power BI model. Every leg is re-verifiable by this agent (databricks-query for live gold facts, adf-databricks-read for notebook/ADF source, fabric-read for Fabric consumers)."
last-validated-commit: n/a — reference doc for the fictional Acme Retail platform
---

# Lineage map (ADF pipeline ⇄ notebook ⇄ gold table ⇄ Fabric model)

The human-readable narrative of [`lineage-graph.yaml`](lineage-graph.yaml), which is authoritative
and is what `lineage-trace` / `lineage-coverage` query. Keep the two in sync.

> Status legend: ✅ verified (re-confirmed from the mirror or a live query) · 🔁 inherited, not yet
> re-confirmed — re-verify before a plan depends on it · ⬜ not yet mapped.

## How this agent extends this file
1. **Gold table → live schema/data.** `databricks-query`: `DESCRIBE`/`COUNT`/sample. Record what you
   queried and when.
2. **Gold table → producing notebook.** `table-usage --table <name>` lists writers; read the writer's
   logic with `mirror-read --as-text --lines`. Compare with step 1 and flag any disagreement.
3. **Gold table → consuming Fabric model(s).** `table-usage` also lists semantic models whose M
   partition names the table; confirm each with `fabric-read`.
4. **ADF pipeline.** Read the pipeline/trigger JSON that lands the source data the notebook consumes.
5. **Promote 🔁 to ✅** once you have re-read the file yourself. Never delete an unconfirmed fact — mark
   it 🔁.
6. Add the edge to `lineage-graph.yaml` (agents may write this doc and the graph file only).

## Sales Overview (🔁 inherited)

| PBI model table | Gold table | Producing notebook | ADF pipeline | Status |
|---|---|---|---|---|
| `sales_daily` | `acme_prod.gold.sales_daily` | `Gold_Notebooks/SALES/Wave3/USP_SALES_DAILY.ipynb` | `PL_POS_Daily_Ingest` | 🔁 |
| `store_dim` | `acme_prod.gold.store_dim` | `Gold_Notebooks/MASTER/Wave1/USP_STORE_DIM.ipynb` | _to map_ | 🔁 |
| `rep_hierarchy_monthly` | _to confirm_ | _to map_ | _to map_ | ⬜ |
| `category_trend_weekly` | _to confirm_ | _to map_ | _to map_ | ⬜ |

## Rep Scorecard (`scorecard/gold/`)

| Gold table / view | Producing notebook |
|---|---|
| `fact_rep_kpi_scorecard` | `scorecard/gold/1. Fact Rep Scorecard.ipynb` 🔁 |
| `vw_fact_rep_scorecard` | `scorecard/gold/2. PBI Views Scorecard.ipynb` 🔁 |

Consumed by the **Rep Scorecard** model in `fabric_secondary` (`Insights/Scorecard/`), feeding several
report faces. The consumer side is ✅ via `fabric-read`; the notebook column is 🔁.

## Visit Status (added by ACME-2085's impact analysis)

| PBI model table | Gold table | Producing notebook | ADF pipeline | Status |
|---|---|---|---|---|
| `visit_summary` (`fabric_secondary`, `Insights/Field/Visit-Status.SemanticModel`) | `acme_prod.gold.visit_summary` | `Gold_Notebooks/FIELD/Visit summary report.ipynb` | none found — likely a Databricks Job outside ADF, **unconfirmed** | ✅ gold/notebook/Fabric legs; ADF leg absent |

`SHELFCHECK_WITHREP` is a direct passthrough column (no DAX transform), confirmed from the table's
`.tmdl` M partition.

## Other areas
⬜ Not yet mapped. Every `impact-analysis` that touches an unmapped table should add a full row here
and the matching edges to the graph.
