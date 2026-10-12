# Test Plan — <TICKET-KEY>: <short title>

| Field | Value |
|---|---|
| Ticket | <JIRA key + link> |
| Implementation plan | `<file>` (advisory) |
| Date written | <YYYY-MM-DD> |

## Test cases

| ID | Area | Description | Query / DAX (exact) | Expected result | Status |
|----|------|-------------|------------------------|------------------|--------|
| TC-01 | Databricks | Happy path — current gold-table state | `SELECT ... FROM <prod_catalog>.gold.<table> WHERE ... AND IS_CURRENT_FLAG = 1` | `<value/row count>` | Not yet run |
| TC-02 | Databricks | Edge case — null/missing mapping | `SELECT COUNT(*) FROM ... WHERE <key> IS NULL` | `<expected count>` | Not yet run |
| TC-03 | Fabric | DAX spot-check via XMLA `Execute` against the published model | `EVALUATE {[<Measure>]}`, workspace `<name>` (per `/docs/ai/client/repo-map.md`) | `<expected value>` | Not yet run |
| TC-04 | Fabric | RLS — role X sees the row, role Y doesn't | Evaluate under both effective identities | X: visible; Y: not visible | Not yet run |
| TC-05 | Cross-check | Full chain agrees: notebook logic (`adf-databricks-read`) → gold value (TC-01) → Fabric value (TC-03) for the same entity | Combine TC-01 + TC-03; compare against the notebook's actual transformation logic | Values match, and match what the notebook's logic implies | Not yet run |

Add/remove rows to match what the impact analysis actually found.

## UAT reference dashboards (Story path)

Where a person checks this change on screen during UAT. Each row maps one visual to the test case
whose result is its expected value, so the number Scotri checked and the number a person reads are
the same number. Bug path: optional, add it when the fix changes a number someone sees.

| Ref | Report (as shown in Power BI) | Located at (repo · branch · report path) | Page | Visual (title · type) | Dimensions on the visual | Filters / slicers to set | Expected value | Test case | Visual status |
|-----|-------------------------------|------------------------------------------|------|-----------------------|--------------------------|--------------------------|----------------|-----------|---------------|
| UR-01 | `<report name>` | `<repo_key>` · `<branch>` · `<Area>/<Report>.Report` | `<page displayName>` | `<title>` · `<card/table/matrix/...>` | `<column(s) on rows/axis>` | `<Slicer> = <value>`; `<Slicer> = <value>` | Same as TC-0N's actual result | TC-0N | existing / planned (added by step N of the plan) / no visual yet |

- **Where to validate:** UAT workspace `<name>` from `docs/ai/client/repo-registry.md`, or `none registered`
  and the stand-in to use instead. <State the stand-in and why.>
- **How the reference was chosen:** named by the user or ticket / ranked by `report-bindings --table
  ... --model ...` (top candidates by changed tables used).
- **Reference gaps:** <every problem with a named or chosen report: not found, wrong model, doesn't
  show the change, only on hidden pages, report-level measure, no slicer for a needed filter, several
  copies. `none` if none.>
- **Data as of:** the expected values are from the TC runs on `<date/time>` against `<catalog>`; the
  dashboard matches only once its dataset has refreshed past that point.

## Explicitly out of scope for this agent to execute
- Any Databricks notebook/job run, ADF pipeline trigger, or Power BI model refresh/publish — hand
  back to a human/CI with the exact run and expected outcome.
- Anything requiring an interactive login of any kind.

## Sign-off
- [ ] All Databricks/Fabric read-only test cases actually executed with real output recorded
- [ ] Any hand-back items clearly listed with what to run and what to expect
- [ ] Every cross-check case states whether the notebook hop was actually read this run (via
      `adf-databricks-read`) — and if it genuinely couldn't be, that gap is carried forward explicitly,
      not silently assumed either way
