---
name: lineage-trace
description: "Trace the chain between a Fabric/Power BI table and its Databricks gold-table source, in either direction: from a semantic-model table down to the gold table, producing notebook and live data, or from a gold table up to every consuming Fabric model. Reads TMDL and notebook source from the mirror, corroborates with a governed Databricks query. Use when: where does this table come from, what gold table feeds this model, what notebook builds this, what does this gold table look like, which reports use this table, upstream source, impact analysis before changing a gold table or a model table, build/extend the lineage map."
---

# Lineage trace (Fabric table ⇄ gold table ⇄ producing notebook ⇄ Databricks facts)

Both directions read the Databricks repo from the mirror via `adf-databricks-read` and Fabric files
via `fabric-read`.

## Fast path: the lineage graph

Query the graph before sweeping files. It answers reachability in one call and reports its own gaps:

```bash
python -m app.cli lineage-trace --node "gold_table:<name>" --direction upstream   # what feeds it
python -m app.cli lineage-trace --node "gold_table:<name>" --direction downstream --kind report --kind measure --kind rls_role
python -m app.cli lineage-trace --node "gold_column:<table>.<column>"              # one column's consumers
python -m app.cli lineage-coverage --unresolved 20 --repo-key <key>  # + what the generator couldn't resolve
python -m app.cli kpi-lookup --name "<measure or KPI>"   # one measure: DAX location, definition, gold sources, reports
```

- The whole chain is **generated from the mirror after every sync**, each edge `verified: true` with
  the mirror path and line in `source_ref`: ADF trigger → pipeline → notebook (pipeline JSON),
  notebook → notebook (`%run` as `includes`, `dbutils.notebook.run` as `triggers`), notebook → table
  it writes / table it reads (notebook code), and gold table/column → model table →
  column → measure / calculated column / RLS role → report (every Fabric tree). The hand-curated
  `docs/ai/client/lineage-graph.yaml` holds only what the mirror can't show (its unverified ADF legs are
  pipelines that pick notebooks by parameter). Node ids: `adf_trigger:<name>`, `adf_pipeline:<name>`,
  `notebook:<repo path>`, `gold_table:<name>` (lower-case; other schemas `<schema>_table`), Fabric
  nodes with `<repo_key>@<branch>/<model path>`, so the same model in two trees is two nodes.
- Notebook edges are read from the code lexically: a schema variable (`{gold_schema}`) resolves from
  NB_Utils or the notebook's own assignment; a target built only from variables is listed as
  unresolved. `upstream` from a report fans out wide (every table each notebook reads): use `--kind`.
  A shared notebook reaches everything that `%run`s it: `downstream` from `notebook:NB_Utils.py` is
  most of the platform, which is the honest blast radius of changing it.
- Generated edges are lexical (DAX/M/SQL read, not evaluated). What they couldn't resolve is listed by
  `lineage-coverage --unresolved`: check that list for the model you're tracing before calling the
  set complete.
- Every traversal reports `unverified_edge_count`. An edge with `verified: false` is the YAML form of
  the map's 🔁 marker.
- **After a graph answer, read; don't search.** Confirm each edge your conclusion rests on by
  reading its `source_ref` lines (`mirror-read --as-text --lines <line>-<line+40>`, batched), not by
  grepping for names the graph already listed. `mirror-grep` is for three cases only: a node in scope
  appears in `lineage-coverage --unresolved`, an edge is `verified: false`, or the question is about
  logic the graph doesn't model (a filter, a join condition, a CASE inside a notebook). The few
  `verified: false` edges are hand-curated legs the mirror can't show; you can't edit the YAML, so
  say in the answer and the learning which of them you confirmed or refuted.
- An empty or low-coverage result means "not mapped yet", never "nothing there": check
  `lineage-coverage --unresolved` for the model or notebook, then do the file trace.

## Starting point

- **Semantic-model table** (or a report visual: resolve it to a table via the model's
  `report.json`/`.pbir` first) → trace **down**: steps 1, 2, 3.
- **Gold table** (or a Fabric table resolved to one via step 1) → trace **up**: steps 2, 3, 4.

Either way, finish with all four facts: Fabric table(s), gold table, producing notebook, live
Databricks facts.

## Procedure

### 1. Fabric table → gold table name (skip if you started from a gold table)
- The model may be in **any** Fabric repo, and on any of a repo's mirrored branches.
  List candidate `*.SemanticModel` folders, then read
  `<repo>/<Area>/<Model>.SemanticModel/definition/tables/<table>.tmdl` and its M partition.
- The `Databricks.Catalogs(...)` block names the gold table and uses `CatalogName`/`SchemaName`
  params (`<prod_catalog>`/`<dev_catalog>` . `gold`). Where the change depends on them, read the live values via
  `dax-debug`: the mirror's `expressions.tmdl` copy is routinely stale.
- Record the exact gold table name **and the repo and branch**.
- Skip `param_*`, `Role Parameter *` and RLS tables: model-internal, not lineage.

### 2. Gold table → producing notebook
Run `table-usage --table <gold table>`: its `writers` are the notebooks whose `saveAsTable`/`MERGE
INTO` target is the table, with cell and line numbers. A writer outside the daily path (an older
copy) can look the same: confirm which one the job runs. Read its real logic: join predicates, filters, grouping/mapping, its `Wave<N>` build-order
position, and its SCD2 merge pattern if any. Don't guess from naming convention.

### 3. Gold table → live Databricks facts
Via `databricks-query`:
```sql
DESCRIBE TABLE <prod_catalog>.gold.<table>;
SELECT COUNT(*) FROM <prod_catalog>.gold.<table>;
SELECT * FROM <prod_catalog>.gold.<table> LIMIT 20;  -- optional, to test a specific hypothesis
```
Compare with what the notebook (step 2) claims to produce and **state plainly whether they agree**.
A disagreement (stale deploy, in-flight change, bug) is a finding; don't quietly reconcile it in
favor of one source.

### 4. Gold table → every consuming Fabric model
- `table-usage`'s `readers` list every semantic model that names the gold table, one entry per
  model, across every tree. Check each model's M partition in every Fabric repo and every mirrored
  branch (a repo with two Prod branches holds two independent live content sets).
- Record `repo · branch · model path · table`. Each model's `.pbir` files give the bound report(s).
- Never assume one repo/branch represents another. Never skip this step because you started from a
  Fabric table: a gold table often feeds more than one model.

## Record it in `docs/ai/client/lineage-map.md` (only what the graph can't show)

The map is narrative now; the graph holds every generated leg. Add or update a row only for a leg
the mirror can't show (a Databricks Job outside ADF, a notebook a pipeline picks by parameter, a
cross-repo link you established): `PBI model table | Gold table | Producing notebook | ADF
pipeline/trigger | Status`.

- Producing-notebook cell: from step 2's actual read. ADF-pipeline cell: from `adf-databricks-read` /
  `impact-analysis` step 3.
- ✅ only when every cell was independently confirmed this session; otherwise 🔁. Never promote to ✅
  on inference alone, and never delete a row just because it wasn't re-confirmed.

## For impact analysis

Before calling a gold-table or model change safe: read the notebook logic (step 2), confirm schema
and a data sample and flag any disagreement (step 3), and find every consuming model (step 4). A
schema/column change is **breaking** if any consuming M partition or DAX measure references the
changed column.
