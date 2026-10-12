---
name: test-plan-and-results
description: "Write concrete test cases for a proposed data-platform change (SQL SELECT assertions against Databricks, DAX spot-checks against a published Fabric semantic model), then execute the read-only ones and record pass/fail with real output. Use when: write a test plan, create test cases, execute/run test cases, record test results, validate this change, what should be tested."
---

# Test plan & results (write, then run, read-only)

These tests validate the platform's **current** behavior (to support impact analysis or spot-check a
reported number) or the hypothesis behind a `propose-change` proposal. There is never a change made
by this agent to validate.

A governed `SELECT` and a read-only DAX `Execute` change nothing, so **write each case and run it in
the same session**. Don't defer one to a human unless it genuinely needs a write, a notebook run, a
pipeline trigger, a model refresh/publish or an interactive login; those are never attempted.

## Writing the test plan

Fill `templates/test-plan-template.md`. At least one case per `impact-analysis` finding:

- **Databricks (gold)** via `databricks-query`: a `SELECT` with an expected row count/value/set,
  filtered `IS_CURRENT_FLAG = 1` unless testing history. Cover the happy path, a null/missing-mapping
  edge case and, if relevant, a boundary the ticket describes.
- **Fabric (DAX/measure/RLS)** via `dax-debug`: exact DAX against the **published** model and its
  expected value under a named role/effective identity, not just "no errors". If RLS is touched, test
  one role that should see a row and one that shouldn't.
- **Cross-check**: one case stating the full chain (ADF → notebook logic → gold value → Fabric value)
  so a reviewer sees it checked end to end. If `impact-analysis` flagged a notebook-logic vs
  live-table disagreement, add a case re-checking exactly that.
- **Not executable** (notebook run, pipeline trigger, model refresh/publish): mark `not executed —
  needs human/CI run` and state exactly what to run and what to expect.

Each case: id, one-line description, exact query/DAX, expected result, status. "Verify the numbers look
right" is not a test case.

## UAT reference dashboards

**Story path: required. Bug path: optional**, add it when the fix changes a number someone sees. Fill
the template's "UAT reference dashboards" table: report, page and visual a person opens in UAT, the
filters and slicers to set, and the number to expect, so Scotri's SQL/DAX and what people see on
screen are the same check.

Read reports only through `report-bindings`, never `visual.json`/`report.json` whole.

1. **A report the user or ticket names comes first.** Resolve it (`mirror-find --pattern
   "*<name>*.Report*"`), then `report-bindings --repo-key <key> --report "<path>" --table <T>` for
   each changed model table `T`. Use it, but check it, and list every problem under **Reference gaps**
   below the table. Never quietly swap in another report. Gap types:
   - **Not found** in any mirrored tree: say so and give the closest `mirror-find` matches.
   - **Wrong model**: its `model` isn't one `impact-analysis` found affected, so it can't show this
     change.
   - **Doesn't show the change**: `tables_not_used` lists a changed table, or the only visuals using
     it are `hidden` or on `hidden` pages. Say "reached by navigation, not the tab bar", not
     "unreachable".
   - **Report measure**: the visual binds `T.F (report measure)`, defined in the report, so a change
     to the model measure of the same name won't reach it.
   - **Filters it can't set**: a filter the matching test case needs has no slicer on that page; name
     the field the person must set in the filter pane.
   - **Several copies**: the same report path exists in several trees (legacy and new repos,
     another mirrored branch), sometimes in different layouts. Say which copy the row means.

   Then still add candidates (step 2) for any changed table the named report doesn't cover.
2. **Candidates by changed tables.** Run `report-bindings --table <T> [--table <T2>] --model
   "<model>"` once per affected model from `impact-analysis` step 5 (`--model` matters: names like
   `CalendarDate` repeat across models). Take the top one or two, then run step 1's single-report call
   on each to pick the visual. One visual per changed measure or column (a card or table showing the
   number plainly beats a chart), plus a second only when the change behaves differently by
   dimension. Same gap checks apply.

   **Name what to set** for every row: dimensions = fields on the visual's rows/axis/legend roles;
   filters/slicers = the values the matching test case filters on. Name each slicer by on-screen
   `title` **and** field, since they often differ (e.g. a slicer titled "Zone" on
   `CalendarDate.Month Name`). A slicer's `selected` is its saved selection, what a person sees on open.
3. **Tie each row to a test case.** Expected value = that case's **executed** result, never a new
   number. If no case computes what the visual shows under those filters, write one (usually a
   gold-table `SELECT` grouped and filtered the same way) and run it. Record when it ran and against
   which catalog: the dashboard matches only once its dataset has refreshed past that point.
4. **New visuals.** If the change adds a measure or visual that doesn't exist yet, mark the row
   `planned`, cite the implementation-plan step that adds it, and name any existing visual that
   already shows the same base number for a side-by-side.
5. **Where to validate.** UAT workspace from `docs/ai/client/repo-registry.md`. The reference is located in
   the Prod mirror, but UAT isn't read unless the user named UAT. If the repo has no registered UAT
   branch (the new repos today) or the registered one is broken, write `none registered` and name the
   stand-in: Dev workspace for a change still in development, Prod after release. Never invent a UAT
   location.
6. **Names, not links.** Record report, page and visual names as shown in Power BI, plus repo · branch
   · path where found. Don't build report URLs (service report ids aren't in the repo; the new repos'
   workspace ids aren't known).

## Executing

- Run every SQL assertion via `databricks-query` and every DAX check via `dax-debug`; paste the actual
  output/value. A governor queue is a wait to surface, not a reason to skip.
- Needs a write, refresh or interactive login: don't attempt it; `not executed — needs human/CI run`
  with exactly what to run and expect.
- Don't defer a read-only check as "unsafe"; it isn't.

## Recording results

Save to `scratch/<ticket-id>-test-results.md` using `templates/test-results-template.md`, one
file per ticket (a re-run updates it and notes what changed): pass/fail per case with real output, everything not executed and why, and an overall
verdict (findings support the proposed plan / need another round / blocked on X).

Results feed a `propose-change` patch or go back as-is to a human for implementation. There is no PR
step.

## Done when

- [ ] Every impact-analysis finding has at least one test case
- [ ] Every read-only case (SQL, DAX) was executed through the governor, real output pasted
- [ ] Every non-executable case is `not executed — needs human/CI run` with what to run and expect
- [ ] Any notebook-vs-live-data disagreement from impact analysis was re-checked
- [ ] Results saved to `scratch/<ticket-id>-test-results.md` with an overall verdict
- [ ] Story path: every changed measure or column has a UAT reference row tied to an executed test
      case, with filters/slicers to set and where to validate (or `none registered` + the stand-in)
