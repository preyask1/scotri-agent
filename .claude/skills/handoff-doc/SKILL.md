---
name: handoff-doc
description: "Final lifecycle stage: assemble what earlier stages produced (requirement + gaps, bug verdict if any, impact analysis, implementation plan, test results) into ONE markdown file for whoever implements the change, using the Bug or Story template per the ticket's actual path. Use when: create the handoff doc, consolidate this ticket, package this for implementation, finalize this work order, hand this off."
---

# Handoff doc: one file, assembled from what already exists

This skill never re-does analysis. It assembles the outputs of `jira-intake`, `bug-investigation`
(bugs only), `impact-analysis`, `implementation-plan` and `test-plan-and-results` into the one document
the implementer reads.

## Template: set by the path taken

| Path taken | Template |
|---|---|
| Story: `jira-intake` → `impact-analysis` → `implementation-plan` → `test-plan-and-results` | [`/templates/handoff-story-template.md`](../../../templates/handoff-story-template.md) |
| Bug, verdict confirmed bug: `jira-intake` → `bug-investigation` → `impact-analysis` → `implementation-plan` → `test-plan-and-results` | [`/templates/handoff-bug-template.md`](../../../templates/handoff-bug-template.md) |
| Bug, verdict not a bug or inconclusive | **No handoff doc.** The verdict document is the deliverable. |

A path matching neither of the first two rows means a stage was skipped: run it rather than assemble
around the gap. Check the verdict field before picking the Bug template, every time.

## Procedure

1. **Confirm every prerequisite document exists, is for this ticket, and is complete** (not stale from
   another ticket, not a draft marked incomplete, tests actually run). If one is missing, say so and
   stop.
2. **Pick the template** from the table.
3. **Fill every field from the source documents, verbatim where the template says so** (acceptance
   criteria, confidence and its justification, test results). Assemble; don't re-summarize.
4. **Carry forward every "could not be checked" note** from intake, investigation (bugs) and impact
   analysis.
5. **Story path: carry the UAT reference dashboards** from the test results into section 7a: every row,
   its expected value as the test case's actual result, its `planned`/`none registered` notes and its
   **Reference gaps** list intact. If the test results have no such table, write `not established this
   phase — see <test results file>`; don't pick a report here.
6. **Link to the source documents** (the table at the top of both templates) instead of copying their
   full content where the template says a summary is enough.
7. **Save as `<ticket-key>-handoff.md`** alongside the source documents, and say where.
8. **Optionally offer** to post a summary as a Jira comment via `jira-write` (its permission rules
   apply). Offered, never assumed.

## Hard boundaries

- **No finding that isn't in a source document.** A field with nothing to fill it gets
  `not established this phase — see <source doc>`, never a plausible guess.
- **Never soften or upgrade a verdict, confidence level or wording.** "Medium confidence,
  environment-drift" stays exactly that; "believed" never becomes "confirmed".
- **No handoff doc for a not-a-bug or inconclusive ticket.**

## Output contract

1. One file, correct template, this ticket.
2. Every section traceable to a specific source document.
3. Every caveat and gap from every prior stage carried forward.
4. The templates' final-section statement that nothing has been applied: this is a handoff, not a
   completion record.
5. The final message ends with one `scotri-learning` block (format in `agent-instructions.md`).
