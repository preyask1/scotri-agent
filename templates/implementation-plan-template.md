# Implementation Plan (Advisory) — <TICKET-KEY>: <short title>

| Field | Value |
|---|---|
| Ticket | <JIRA key + link> |
| Author | Scotri (advisory only — no code authored by this agent) |
| Date | <YYYY-MM-DD> |
| Status | Draft / Advisory — for human execution |
| Related impact analysis | `<file>` |

> This document is advisory. Nothing in it has been applied by this agent. A human (or
> a write-capable workspace's own persona, under its own approval gate) executes it against their own
> clone of the relevant repo(s).

## 1. Requirement recap
<1-2 sentences.>

## 2. Recommended approach
<Chosen approach in plain language, and why, given the impact analysis's Fabric findings.>

## 3. Files a human would need to change, in order

| # | Repo | File / model / measure / parameter | Change | Risk | Independently verified this phase? |
|---|------|--------------------------------------|--------|------|--------------------------------------|
| 1 | Fabric (`<repo_key>`) | `<Area>/<Model>.SemanticModel/definition/tables/<t>.tmdl` | <measure/column/RLS change> | Low/Med/High | Yes — fetched via `fabric-read` |
| 2 | Databricks | `<notebook path, e.g. Gold_Notebooks/<SOURCE>/Wave<N>/USP_*.ipynb>` | <transformation-logic change, backed by the notebook's actual join/filter/grouping read> | Low/Med/High | Yes — read via `adf-databricks-read`, corroborated by a live `databricks-query` schema/sample |
| 3 | ADF | `<pipeline/dataset/trigger path>` | <Copy-activity mapping / trigger-schedule change, backed by the actual JSON read> | Low/Med/High | Yes — read via `adf-databricks-read` |

Every row above should read "Yes" in the ordinary case — `impact-analysis` reads ADF and Databricks
facts from the mirror on every run, the same as Fabric. Mark a row `not independently verified —
<reason>` only for a genuine gap this run (mirror unreachable, maintenance window, a credential
declined) — never as the template's default expectation for a repo.

## 4. Alternatives considered
| Alternative | Why not recommended |
|---|---|

## 5. Assumptions & unknowns
- <assumption — and how a human would confirm it>
- <anything not independently verified this phase, repeated from §3's rightmost column>

## 6. Risk & blast radius (from impact analysis)
- **Fabric reports/models affected:** <list>
- **RLS/security impact:** <none / describe>
- **Schema-breaking?** <yes/no>
- **Anything not independently verified this run, and why:** <carried from §3's rightmost column, or
  "none — ADF, Databricks, and Fabric facts were all confirmed this run">, plus any notebook-vs-live-data
  disagreement the impact analysis flagged (see its §5)

## 7. Rollback approach (advisory)
<One paragraph: if a human applies this plan and needs to revert it, how would they — described as
guidance for them, not as a step this agent would ever take itself.>

## 8. Test approach (see `test-plan-and-results` for the concrete, executed cases)
<What kinds of checks validate this — SQL assertions, DAX spot-checks — and which of them this agent
already ran vs. which need a human/CI run.>

---

**This is an advisory plan for a human (or a write-capable workspace's own persona, under its own
approval gate) to execute. This agent has not made, and will not make, any change described above.**
