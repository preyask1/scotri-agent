# Handoff — Bug — <TICKET-KEY>: <short title>

One consolidated document for whoever implements the fix next (a human, or a
write-capable workspace's own persona under its own approval gate). Everything below was produced by Scotri,
read-only — nothing here has been applied to any repo.

| Field | Value |
|---|---|
| Ticket | <JIRA key + link> |
| Change classification | `report-measure` / `pipeline-ingestion` / `scope-onboarding` / `master-data-mismatch` |
| Root-cause category | `data-source` / `etl-logic` / `ingestion-pipeline` / `semantic-model` / `report-visual` / `environment-drift` |
| Confidence in root cause | High / Medium / Low |
| Date | <YYYY-MM-DD> |
| Source documents | Investigation verdict: `<file>` · Impact analysis: `<file>` · Implementation plan: `<file>` · Test results: `<file>` |

## 1. The requirement, restated

<From `jira-intake`: the ask in plain words, acceptance criteria verbatim or flagged absent.>

### Gaps and ambiguities
<Carried from `jira-intake`, tagged `[BLOCKING]`/`[non-blocking]`.>

## 2. Root cause (from the investigation verdict)

<2-4 sentences: what's actually wrong, and the evidence that pins it to this specific
cause — see the linked investigation verdict for the full trace. Restate the confidence level and
what it's based on; don't let this section imply more certainty than §confidence above states.>

## 3. Blast radius (from impact analysis)

| Repo | What's affected | Verified how |
|---|---|---|
| | | |

- **Schema/breaking-change risk:** <yes/no, and what>
- **What could not be checked:** <carried forward, never omitted>

## 4. Recommended fix

| # | Repo | File / notebook / model / measure | Change | Risk |
|---|---|---|---|---|
| 1 | | | | Low/Med/High |

<If a concrete diff was drafted, link it: `proposals/<ticket-id>/` — state plainly that it's a
proposal, never applied, and a human applies it manually against their own clone.>

## 5. Test plan + results (already executed, read-only)

| Test case | Expected | Actual | Pass/Fail |
|---|---|---|---|

<Anything needing a write/refresh/notebook-run to validate is marked `not executed — needs human/CI
run`, with exactly what to run and what result to expect.>

## 6. Alternatives considered
| Alternative | Why not recommended |
|---|---|

## 7. Rollback approach (advisory)

<If a human applies this fix and needs to revert it, how — described as guidance, never as a step
this agent takes itself.>

## 8. Route

State plainly: this handoff is ready for a human to act on, or for a write-capable workspace's own
persona to implement under its own approval gate. Nothing described above has been applied.
