# Handoff — Story — <TICKET-KEY>: <short title>

One consolidated document for whoever implements this next (a human, or a
write-capable workspace's own persona under its own approval gate). Everything below was produced by Scotri,
read-only — nothing here has been applied to any repo. No root-cause section — nothing is broken,
this is additive.

| Field | Value |
|---|---|
| Ticket | <JIRA key + link> |
| Change classification | `report-measure` / `pipeline-ingestion` / `scope-onboarding` / `master-data-mismatch` |
| Date | <YYYY-MM-DD> |
| Source documents | Impact analysis: `<file>` · Implementation plan: `<file>` · Test results: `<file>` |

## 1. The requirement, restated

<From `jira-intake`: the ask in plain words, acceptance criteria verbatim or flagged absent,
explicitly in-scope vs out-of-scope.>

### Gaps and ambiguities
<Carried from `jira-intake`, tagged `[BLOCKING]`/`[non-blocking]`.>

## 2. Blast radius (from impact analysis)

| Repo | What's affected | Verified how |
|---|---|---|
| | | |

- **Schema/breaking-change risk:** <yes/no, and what>
- **What could not be checked:** <carried forward, never omitted>

## 3. Recommended approach

<Chosen approach in plain language, and why, given what impact analysis found.>

## 4. Files to change, in order

| # | Repo | File / model / measure / pipeline | Change | Risk |
|---|---|---|---|---|
| 1 | | | | Low/Med/High |

## 5. Alternatives considered
| Alternative | Why not recommended |
|---|---|

## 6. Assumptions & unknowns
<Anything not confirmed via an actual mirror read or governed query, and how a human would confirm it.>

## 7. Test plan + results (already executed, read-only)

| Test case | Expected | Actual | Pass/Fail |
|---|---|---|---|

<Anything needing a write/refresh/notebook-run to validate is marked `not executed — needs human/CI
run`, with exactly what to run and what result to expect.>

### 7a. Where to validate in UAT (reference dashboards)

For developers and business users: open these, set these filters, and expect these numbers. Each
expected value is the actual result of the named test case above.

| Ref | Report | Page | Visual | Dimensions | Filters / slicers to set | Expected | Test case | Visual status |
|---|---|---|---|---|---|---|---|---|

- **Validate in:** <UAT workspace from the registry, or `none registered` + the stand-in and why>
- **Reference gaps:** <carried verbatim from the test plan; `none` if none>
- **Data as of:** <TC run time and catalog; match only after the dataset has refreshed past it>
- **Planned visuals:** <any row whose visual doesn't exist yet: which plan step adds it, and the
  existing visual, if any, that already shows the same base number>

## 8. Rollback approach (advisory)

<If a human applies this plan and needs to revert it, how — described as guidance, never as a step
this agent takes itself.>

## 9. Route

State plainly: this handoff is ready for a human to act on, or for a write-capable workspace's own
persona to implement under its own approval gate. Nothing described above has been applied.
