---
name: implementation-plan
description: "Turn a confirmed impact analysis into an advisory implementation plan (files to change in order, risk, alternatives, assumptions) for a human, or a write-capable workspace's own persona, to execute. Never edits files, never proposes running anything, has no approval checkpoint. Use when: write an implementation plan, plan the change, how should this be implemented, propose an approach, what files would need to change and in what order."
---

# Implementation plan — advisory only, never applied

The plan is a document a human acts on outside this agent, in their own editor and clone. Once it's
written, this skill is done: there is no next stage and nothing to hand off to.

## Inputs
- The restated requirement from `jira-intake` (scope, ACs, gaps).
- **A completed `impact-analysis`** — findings across all repos, including any notebook-vs-live-data
  disagreement. 🔴 Never write the plan before it: without the blast radius the plan will miss the
  second Fabric repo, a live-branch-only file, an ADF trigger, or a schema-breaking column.

## Writing the plan

Fill [`/templates/implementation-plan-template.md`](../../../templates/implementation-plan-template.md).
Every row and sentence is advice to a human, never a step this agent will take.

- **Requirement recap** — 1-2 sentences.
- **Approach** — recommended approach in plain language, and why, given the impact analysis.
- **Files to change, in order** — one row per file, per repo, with the specific edit (measure, TMDL
  table, parameter, notebook transformation, pipeline activity). Each row is backed by a mirror read or
  governed query from impact analysis; a row that genuinely couldn't be verified (mirror unreachable,
  maintenance window, query declined) is marked `not independently verified — <reason>`. Don't treat
  the whole plan as unverifiable by default.
- **Alternatives considered** — or explicitly N/A.
- **Assumptions & unknowns** — anything not confirmed by a read or query, and how a human would confirm it.
- **Risk & blast radius** — carried from impact analysis across all repos, with a reason. Restate any
  notebook-vs-live-data disagreement here: a reader of only the plan must see it where they act.
- **Rollback approach** — "if a human applies this plan and it needs reverting, here is how they would
  do that"; never something this agent executes.
- **Test approach** — pointer to `test-plan-and-results`.

## Ending: no approval checkpoint

End the document with:

```
This is an advisory plan for a human (or a write-capable workspace's own persona, under its own
approval gate) to execute. This agent has not made, and will not make, any change described above.
```

Don't solicit an "approved". If the user says "approved, go ahead", reply that this workspace has no
write path and the plan is ready for a human (or a separate write-capable workspace) to act on.

## Checklist before calling it done

- [ ] Impact analysis exists and its findings across all repos are reflected accurately
- [ ] Every file row is specific (path/cell/parameter/measure/notebook transformation)
- [ ] Unverifiable rows marked "not independently verified — <reason>"
- [ ] Risk stated with a reason; alternatives filled or N/A
- [ ] Ends with the advisory-only statement, not an approval solicitation
- [ ] Saved to a file, not left only in chat
