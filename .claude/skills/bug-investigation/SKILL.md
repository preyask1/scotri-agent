---
name: bug-investigation
description: "For a Bug-type ticket (or a typed 'is this right?' question), trace the reported symptom to a verdict — confirmed bug, not a bug/expected behavior, or inconclusive — with a root-cause category and a confidence level tied to what was actually checked. Runs before impact-analysis for bugs; Stories skip it. Use when: is this a bug, investigate this reported number, why does this look wrong, triage this bug ticket, root cause this, is this expected behavior."
---

# Bug investigation — verdict first, then (only if confirmed) impact analysis

A Bug ticket isn't automatically a change to plan: it may be expected behavior, or unclear from the
evidence. Render that verdict before `impact-analysis`/`implementation-plan`, which assume a change
is needed.

## When to run

- Iff `issuetype` is `Bug` (routing rule in `agent-instructions.md`). Don't skip a Bug because its body
  "reads like a story"; a type mismatch is a Gaps remark in `jira-intake`.
- Also for a free-form typed "why does this look wrong" question with no Jira ticket (routed by content).
- `presence-claim` first. Before step 1, `retrieve` the symptom: an earlier learning is a lead to
  re-verify, never a verdict to copy.

## Procedure

### 1. Restate the symptom precisely
The specific reported value, the filter/date/division/role context it was seen under, and the expected
value if the reporter gave one. A vague restatement produces a vague trace.

### 2. Trace hop by hop, recording what's found at each
1. **Report visual → measure** — `fabric-read`, the report's `report.json`/`.pbir`.
2. **Measure → model table → gold table name** — `fabric-read`, the model's TMDL/M partition.
3. **Gold table → live schema/data** — `databricks-query`: does live data support the reported value?
4. **Gold table → producing notebook** — `adf-databricks-read`: does the logic (joins, filters,
   grouping) produce what's expected, or explain the discrepancy?
5. **DAX under the reported role/filter** — `dax-debug`: does evaluating the measure in the reporter's
   context reproduce the symptom?

Stop once the evidence supports a verdict, but not before a hop that could change it.

### 2a. Pipeline-failure symptoms ("pipeline X failed today") — trace the chain, don't just ask

Walk as far as the evidence goes before handing back. Details: `adf-databricks-read`, "ADF pipeline
failures".

1. **Enumerate candidates from the mirror** — list `pipeline/` by the reporter's name. If ambiguous,
   say which you assume and put the full candidate list in the output.
2. **Get the failing run and its activity error** — `pipeline-runs` over the window, then
   `activity-runs` on the failed run for `error.message`. If the environment/window is Dev/UAT or the
   factory isn't configured, say exactly that and continue steps 3–5 on definitions alone.
2b. **Zero pipeline runs is not "the pipeline failed" — check the trigger.** Run `trigger-runs` (names
   from the pipeline's trigger JSON) and read the trigger's `enabled` flag from the mirror; an empty
   `trigger-runs` alone can't distinguish disabled from didn't-fire, so take both. "The trigger was
   off" is a legitimate, common root cause; don't report a pipeline defect when nothing ran.
3. **Trace the producer.** For each file/dataset a candidate reads, find the pipeline that writes it.
   For a candidate not triggered directly, find the parent that calls it (`ExecutePipeline`), up to the
   scheduled trigger.
4. **Line up schedules.** Convert each trigger's `recurrence` to UTC (or use `pipeline-runs`
   timestamps) and order candidates by what feeds what. Two failures where the later consumes the
   earlier's output are one **suspected cascade**: name the suspected first cause, not two defects.
5. **Check the child, not just the parent.** A green parent doesn't clear its child. Query the named
   child with `pipeline-runs`; mark it "unchecked" only if that genuinely can't be done (e.g. beyond
   the 14-day cap).
6. **Note where the trail goes cold** — a Databricks job in a workspace this deployment doesn't cover,
   or a permission wall (401/403 on `pipeline-runs`/`activity-runs` = the SP's role grant is missing,
   not run history unavailable; report it as that). Report job name/ID/host/task and stop there.

### 2a-b. Databricks job/notebook failure — the layer between ADF (2a) and Fabric (2b)

When a 2a chain lands on a Databricks job, or the symptom is a notebook failure. Details:
`adf-databricks-read`, "Databricks job/notebook failures".

1. **Find the failing run** — `job-runs` over a bounded window (mandatory); narrow with `--job-id`.
2. **Get the actual error** — `job-run-detail --with-output` on the task run: `error`/`error_trace`/
   `notebook_output`, deeper than ADF's relayed `error.message`. Record the failure text at its hop.
3. **No Jobs-API grant?** `DESCRIBE HISTORY <table>` / `system.query_history` via `databricks-select`
   still give last-write time and query-level failure history.
4. **Where the trail goes cold** — the query workspace only; a 403 is a missing Jobs-API grant on
   the SP (not "credential missing"). A chain landing on a secondary workspace
   (`docs/ai/client/client.md`) still hands off under the same trail-goes-cold rule.

### 2b. Report/dataset refresh symptoms ("why hasn't this report updated")

Details: `fabric-read`, "Fabric refresh failures".

1. **Get the refresh history** — `refresh-history`. A `Failed` refresh with `serviceExceptionJson` is
   a direct answer.
2. **Drill in only for Enhanced Refresh** — `--refresh-id` gives per-partition detail; for Standard
   refresh it returns `available: false`, which is the expected answer. Don't retry it.
3. **"Completed" but the number still looks wrong?** The source may have changed: check the M
   partition (`fabric-read`) and spot-check the value (`dax-debug`), as in hops 3/5.
4. **Where the trail goes cold** — a Dev/UAT-published model (not wired up), or 401/403 (the SP isn't
   a workspace member). Report it as that, not "refresh history unavailable".

### 2c. Working with partial information

An unreadable hop makes the verdict **Inconclusive**, but the document still carries:

- **Ranked leads** — likely first-cause candidates, each with its evidence and confidence (`Low` is
  usually right).
- **The smallest next ask** — one concrete lookup someone can do in a minute (e.g. "run history for
  `<pipeline>` between `<start>` and `<end>`, activity errors only"), not "please provide more info".
- Everything not checked, listed explicitly.

Ask the user only if the answer changes what you'd trace next; otherwise give leads and ask together.

### 3. Render the verdict
Exactly one of three, per [`/templates/investigation-verdict-template.md`](../../../templates/investigation-verdict-template.md):

- **Confirmed bug** — a concrete mechanism produces the wrong result, and it's genuinely wrong (not
  just different from what the reporter expected).
- **Not a bug — expected behavior** — the system does what it's designed to; the gap is the reporter's
  expectation. Draft a plain-language explanation for them (template §5).
- **Inconclusive** — evidence supports neither. Say exactly what's missing (unreadable system,
  maintenance-flag window, credential gap); don't force a verdict past what was checked.

### 4. Categorize the root cause (confirmed bug only)
From the template's table: `data-source` / `etl-logic` / `ingestion-pipeline` / `semantic-model` /
`report-visual` / `environment-drift` / `unknown`. `unknown` is legitimate when the mechanism is
confirmed but doesn't fit; don't force a fit.

### 5. Assign confidence, tied to what was checked
- **High** — root cause directly observed at a step-2 hop.
- **Medium** — strong circumstantial evidence, with a named thing that couldn't be ruled out (stale
  mirror, deploy lag, an unread notebook variant).
- **Low** — inferred from a known pattern/convention without verifying this instance.

Never round up. "Matches a pattern I've seen" is `Low`, not `High`.

### 6. Route
- **Confirmed bug** → `impact-analysis` (scoped to the fix for this root cause), `implementation-plan`,
  `test-plan-and-results`, then `handoff-doc` (Bug format). If this started from a plain complaint and
  no ticket covers it, `jira-write` may create a Bug from the verdict.
- **Not a bug** → stop. The verdict document (with drafted reply) is the deliverable. `jira-write` may
  post the reply as a comment.
- **Inconclusive** → stop. Hand back to a human with exactly what's missing.

## Output contract
1. The verdict document, saved to a file, following the template exactly.
2. Every hop's finding cites its source (path/branch/query); never present inference as observed fact.
3. Confidence justified with specifics.
4. Routing stated explicitly: what happens next, and why.
5. If the lifecycle stops here (not a bug / inconclusive), end the final message with one
   `scotri-learning` block. A confirmed bug leaves its learning at the handoff.

## Common failure modes
- Jumping to `impact-analysis` for a Bug without a verdict.
- Stopping at "no access to run history/logs" and asking the user, without first walking the mirror
  definitions (candidates → producers → callers → schedules); the mirror usually narrows it a long way.
- Reading a green parent pipeline as "upstream was fine" when the child was never checked.
- Treating "not a bug" as failing to find something, rather than a complete answer.
- Tracing past the point where the evidence already supports a verdict, padding the document.
