# Impact Analysis — <TICKET-KEY>: <short title>

| Field | Value |
|---|---|
| Ticket | <JIRA key + link> |
| Date | <YYYY-MM-DD> |
| Starting artifact | <gold table / PBI table / measure / report named in the requirement> |
| Scope this run | **Every repo in the registry** (list the repo keys checked) — read from the local read-only mirror, corroborated by governed live Databricks queries |
| Presence check | <existing claim found on this ticket/entity? who, since when — or "none"> |

## 1. ADF — pipeline/trigger facts (read from the mirror via `adf-databricks-read`)

| Pipeline/trigger | What it does | Schedule (from trigger JSON, not name) | Notes |
|---|---|---|---|
| `<pipeline>` | <Copy activity source→sink, mapping> | <actual recurrence/interval> | |

## 2. Databricks — notebook logic + live gold-table facts

| Gold table | Producing notebook (read via `adf-databricks-read`) | Notebook's claimed logic | Live schema/row-count/sample (via `databricks-query`) | Agreement? |
|---|---|---|---|---|
| `<table>` | `<USP_*/NB_*/VW_* path>` | <join/filter/grouping summary, Wave<N>, SCD2 pattern if any> | <from `DESCRIBE`/`COUNT`/sample> | Yes / **No — see note** |

## 3. Fabric — downstream consumers (EVERY Fabric repo, EVERY mirrored branch)

| Instance | Repo | Branch | Model path | Table | Format (legacy/PBIR) | Measure(s) affected | Report(s) |
|---|---|---|---|---|---|---|---|
| 1 | `<repo_key>` | `<branch>` | `<Area>/<Model>.SemanticModel` | `<table>` | <legacy/PBIR> | `<measure>` | `<report>` |

*Mirror paths read to produce the above table, pasted:*
```
<the actual mirror paths read, and their essential content>
```

## 4. Live Service parameters (if the change touches shard scope or catalog)

| Parameter | Value | Source | Read date |
|---|---|---|---|
| `CatalogName` | <value> | live Service read / mirror-committed fallback | <date> |
| `<scope parameter>` | <value> | live Service read / mirror-committed fallback | <date> |

## 5. Schema / breaking-change check
- Columns added: <list, or none>
- Columns renamed/removed: <list, or none — flag high-risk>
- Type changes: <list, or none>
- Referenced by a Fabric M-partition, DAX measure, or downstream ADF/Databricks consumer? <yes/no + which>

## 6. What could NOT be checked, and why (never omit this section)
- <anything declined, unreachable, mid-maintenance-window, or credential-blocked, with the reason>
- <any notebook-vs-live-data disagreement from §2, restated here for visibility>

## 7. Gaps and ambiguities (carried from `jira-intake`, plus any new ones)
- <gap> — `[BLOCKING]` / `[non-blocking]`

## 8. Overall blast-radius summary
<2-3 sentences: how contained is the change across every repo, and whether anything (a
notebook/live-data disagreement, an unreachable read) affects confidence in the picture.>

## 9. Lineage-map updates made
- <row added/updated in docs/ai/client/lineage-map.md, or "none needed — already mapped">
