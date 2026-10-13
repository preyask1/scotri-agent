---
name: impact-analysis
description: "Find every ADF pipeline, Databricks notebook/table, and Fabric table/measure/report a proposed change touches, across all product repos, by reading the read-only mirror (adf-databricks-read, fabric-read) and corroborating with governed Databricks queries, then emit a scoped work order for a human implementer. Use when: impact analysis, what breaks if I change this, blast radius, before changing a gold table or measure, cross-repo impact, what does this affect, plan a change request, scope a CR."
---

# Cross-repo impact analysis — all repos, mirror-read, no write

The deliverable is a **work order**: blast radius and open questions for a human implementer (or a
write-capable workspace's own persona). The failure this skill prevents is **an incomplete blast radius
that ships a silent inconsistency across shards.**

- **Bug ticket:** runs only after `bug-investigation` returns "confirmed bug"; scope to the fix for
  that root cause, not a generic "what if we changed this table".
- **Story ticket:** runs directly after `jira-intake`.

## Helpers that don't change the completeness rule

- **Lineage graph** (`lineage-trace`/`lineage-coverage`) — query first for a fast blast radius that
  reports "N nodes reached, M edges unverified". For a gold table or column it lists every consuming
  model, measure, RLS role and report across all Fabric trees, generated from the mirror at sync time
  (`--kind report --kind measure --kind rls_role`). That list *is* the candidate set: confirm the
  edges the work order depends on by reading their `source_ref` lines, and search (`mirror-grep`)
  only where the graph has a gap: an unverified (🔁) edge, or an item in `lineage-coverage
  --unresolved` for a model or notebook in scope. Those are re-confirm tasks, not confirmed
  absences. Low coverage means "not mapped yet"; then do the full file trace.
- **Retrieval** — `retrieve --query "<table/measure>" --scope past-engagements,docs`. A ranker, never a
  candidate generator: never let a hit decide *which* consumers exist (a missed fifth consumer is
  exactly the silent inconsistency). State each cited hit's review outcome.

## Scope — every live repo, every time, Prod by default

- Resolve repos/branches from [`/docs/ai/client/repo-registry.md`](../../../docs/ai/client/repo-registry.md); never
  hardcode. Check every repo of each kind: during a migration, old and new Fabric repos are both
  live and the old→new mapping is not assumed.
- **Every live branch:** a repo with more than one mirrored Prod branch (`docs/ai/client/client.md`
  names them) holds independent content sets, not environment variants. The same folder can exist in
  several repos with different clones. Checking one branch/repo and calling it done under-reports the
  blast radius.
- Prod unless the ticket/user names Dev or UAT (only environments the registry lists exist).
  No environment named means Prod; say so. A Dev/UAT read is live, presence-checked first.
- ADF and Databricks repos via `adf-databricks-read`; Fabric repos via `fabric-read`. A component
  that is off for this deployment is out of scope: say so in the work order and skip it.
- Mirror reads are local disk, so broad text search is cheap once narrowed to the relevant
  folder/model: list/narrow candidates first, then read and search bodies.
- Corroborate Databricks facts empirically with `databricks-query` alongside the notebook logic. A
  disagreement (notebook says X, live table looks like Y) is a finding; don't silently resolve it.

## Golden rules

1. **Enumerate the blast radius mechanically, never from a doc.** Paste what the reads returned.
   `docs/ai/areas/*.md` and `docs/ai/client/lineage-map.md` are orientation, never the instance count.
2. **Classify the change layer and check it, not just the DAX.** The business rule often lives in the
   gold layer (join predicate, filter, grouping mapping in a notebook) while the measure is a bare
   `SUM`. Read the notebook; corroborate with `databricks-query`.
3. **A hand-back is a finding, not a footnote.** If the only production change is a Service dataset
   parameter or a control-table row, say so plainly: it's a human-only action.
4. **Definition of done = the ticket's Acceptance Criteria verbatim.** Missing ACs are a `[BLOCKING]`
   gap carried from `jira-intake`, not filled by inference.

## Procedure

1. **Intake and presence.** Use `jira-intake` output (run it if missing). Confirm status isn't already
   `In UAT`/`Prod Deployed`. `presence-claim` on the ticket key.
2. **Resolve the starting artifact** (pipeline, notebook, gold table, Fabric report/model/measure) to a
   concrete name. Trace upstream to its producing ADF pipeline/notebook and downstream to consuming
   Fabric models/reports.
3. **ADF facts** (`adf-databricks-read`) — pipeline/dataset/trigger/linkedService JSON: what the Copy
   activity actually maps, and the trigger's actual schedule (from its JSON, not its name).
4. **Databricks facts** — the producing `USP_*`/`NB_*`/`VW_*` notebook's logic (joins, filters,
   grouping, `MERGE INTO`/SCD2 pattern) via `adf-databricks-read`, plus live schema, row count and a
   sample via `databricks-query`. State both; flag disagreement.
5. **Fabric consumers (every repo, every live branch).** List semantic-model folders, narrow to models
   plausibly related (per [`/docs/ai/client/fabric-conventions.md`](../../../docs/ai/client/fabric-conventions.md)'s
   folder map), read each candidate's `definition/tables/*.tmdl` and check the M partition for the gold
   table. Record per hit: `repo · branch · model path · table · M-partition style
   (parameterised/hardcoded) · measures referencing it`.
   Then find bound reports: read each report's `definition.pbir`, check
   `datasetReference.byPath.path`. Classify each instance's layout (`legacy` `<Report>/report.json` vs
   `pbir` `<Report>/definition/report.json` + per-visual files). This varies within one clone set and
   changes over time: re-derive it every time, never inherit it from a prior work order.
6. **Live Service parameters** (if the change touches shard scope or catalog). `CatalogName` and
   any shard-scope parameter (a region or division keep-list; `docs/ai/client/fabric-conventions.md`
   names this client's) live as dataset parameters in the Power BI Service,
   not reliably in the repo's `expressions.tmdl`. Read the live value via `dax-debug` (a metadata read,
   same connection). If unavailable, write `live parameter unread: <reason>` and use the
   mirror-committed value with a "may be stale" caveat.
7. **Schema / breaking-change check.** Which columns does the change add/rename/remove/retype, and is
   any referenced by an M partition or DAX measure in any Fabric repo, or by a downstream ADF/Databricks
   consumer? A renamed or dropped gold column breaks a model silently at next refresh.
8. **Emit the work order** per [`/templates/impact-analysis-template.md`](../../../templates/impact-analysis-template.md),
   saved to a file. Every finding states its source (file/path/branch or query).
9. **Route explicitly:** "Findings above are ready for a human to act on, or for a write-capable
   workspace's own persona to implement under its own approval gate."

## Output contract

1. **CR/ticket** — key, type, status, the ask in plain words, ACs verbatim or flagged absent.
2. **Scope statement** — every repo/branch checked per the registry, and whether Dev/UAT was touched
   (only on explicit request) or the run was Prod-only by default.
3. **ADF-side facts** — actually read, not inferred.
4. **Databricks-side facts** — notebook logic, live schema/row count/sample, disagreement flagged.
5. **Fabric-side blast radius** — reads made, results, then the table:
   `instance · repo · branch · folder · format · bound model · measures affected`.
6. **Live parameter read** — value, source (live Service read vs mirror fallback), and date.
7. **Schema/breaking-change check.**
8. **Gaps and ambiguities** carried from intake, plus new ones.
9. **Work order document** saved to a file.
10. **What could not be checked, and why** — never omitted, never buried.

## Checklist before calling it done

- [ ] Presence checked before starting (and before any Dev/UAT read)
- [ ] Scope resolved from `repo-registry.md`; every live repo and every mirrored branch checked
      (a repo with two Prod branches counts twice)
- [ ] Prod by default; Dev/UAT only if explicitly requested
- [ ] Every finding cites its path/branch/query and read path (mirror/live)
- [ ] Notebook logic and live Databricks facts both captured; disagreement flagged
- [ ] Schema/breaking-change question answered against actual reads
- [ ] Live Service parameter read attempted; mirror fallback labeled
- [ ] Work order saved to a file
- [ ] Maintenance flag clear (or waited out) before mirror reads were trusted (it does not gate an explicit Dev/UAT live read)
