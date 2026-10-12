---
description: "Cross-repo lineage map for $name: ADF pipeline <-> notebook <-> table <-> Power BI model. Human-readable narrative of lineage-graph.yaml; extended by impact analyses."
---

# Lineage map (pipeline ⇄ notebook ⇄ table ⇄ model)

The narrative form of [`lineage-graph.yaml`](lineage-graph.yaml), which is authoritative and is what
`lineage-trace` / `lineage-coverage` query. Keep the two in sync. This is the one doc the agent may
edit itself: every impact analysis that maps a new chain adds a row here.

> Status legend: ✅ verified (re-confirmed from the mirror or a live query) · 🔁 inherited, not yet
> re-confirmed · ⬜ not yet mapped.

## How to extend it

1. **Table → live schema/data:** `databricks-query` (`DESCRIBE`, counts, a sample).
2. **Table → producing notebook:** `table-usage --table <name>`, then read the writer's logic.
3. **Table → consuming models:** `table-usage` lists semantic models whose M partition names it.
4. **Pipeline:** the pipeline/trigger JSON that lands the notebook's source data.
5. Add the row below and the matching edges to `lineage-graph.yaml`.

## Mapped chains

| Model table | Source table (`$prod_catalog`) | Producing notebook | Pipeline | Status |
|---|---|---|---|---|
| ⬜ | | | | |
