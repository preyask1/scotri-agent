# Investigation Verdict — <TICKET-KEY>: <short title>

| Field | Value |
|---|---|
| Ticket | <JIRA key + link> |
| Reported symptom | <what the user/ticket says is wrong, in their own words> |
| Investigated by | Scotri (read-only) |
| Date | <YYYY-MM-DD> |
| **Verdict** | **Confirmed bug** / **Not a bug — expected behavior** / **Inconclusive** |
| Confidence | High / Medium / Low — see §3 for what that's based on |
| Root-cause category (if confirmed bug) | `data-source` / `etl-logic` / `ingestion-pipeline` / `semantic-model` / `report-visual` / `environment-drift` / `unknown` |

## 1. What was traced

The chain actually followed, hop by hop — not the theoretical full chain, the one this session
actually walked:

| Hop | What | Read via | What was found |
|---|---|---|---|
| 1 | Report visual → measure | `fabric-read` | |
| 2 | Measure → model table → gold table name | `fabric-read` | |
| 3 | Gold table → live schema/data | `databricks-query` | |
| 4 | Gold table → producing notebook logic | `adf-databricks-read` | |
| 5 | DAX under the reported filter/role | `dax-debug` | |

## 2. The verdict, argued

<2-4 sentences: what the evidence shows, and why it points to this verdict specifically — not just
"the number is X," but "the number is X because <mechanism>, confirmed by <which hop above>.">

## 3. Why this confidence level, specifically

- **High** — the root cause (or its absence) was *directly observed* at one of the hops above.
- **Medium** — strong circumstantial evidence, but state exactly what couldn't be ruled out (e.g. a
  stale mirror, a deploy lag, an unread notebook variant).
- **Low** — inferred from a known pattern/convention without directly verifying this specific
  instance — say which convention, and why direct verification wasn't possible this session.

<State which one applies here, and exactly what was/wasn't checked that leads to that level — never
round up confidence to sound more certain than the evidence supports.>

## 4. What happens next, depending on the verdict

- **Confirmed bug** → proceeds to `impact-analysis` (scoped to the fix for this root cause), then
  `implementation-plan`, then `test-plan-and-results` — all folded into the Bug handoff doc
  (`templates/handoff-bug-template.md`) once those stages complete.
- **Not a bug** → stops here. A plain-language explanation for whoever reported it is drafted below,
  in §5. No dev handoff, no further stages.
- **Inconclusive** → stops here. State exactly what would resolve it (a credential this workspace
  doesn't have, a system this workspace can't read, a human with domain knowledge this workspace
  lacks) — hand back rather than guessing. Always include **ranked leads** (most likely cause first,
  each with its evidence and confidence) and **the smallest next ask** — the single concrete lookup
  someone could do to resolve it (system, name, time window, what to read out).

## 5. Drafted reply (only if verdict is "Not a bug")

<Plain-language explanation for the reporter — what they're seeing, and why it's the expected
behavior, not a defect. Written for someone who isn't going to read the trace above.>

## 6. What could not be checked, and why

<Never omit this section — an unreachable system, a maintenance-flag window, a credential gap,
anything that limits how far this trace actually went.>
