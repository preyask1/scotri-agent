# Test Results — <TICKET-KEY> — <YYYY-MM-DD> run

| Field | Value |
|---|---|
| Ticket | <JIRA key + link> |
| Test plan | `templates/test-plan-template.md` instance for this ticket |
| Run by | Scotri (read-only, unattended) |
| Environment | `<prod_catalog>`/`<dev_catalog>` (read-only, service-principal M2M) / Fabric workspace `<name>` (published model, read-only XMLA) |

## Results

| ID | Description | Expected | Actual (real output, pasted) | Pass/Fail |
|----|-------------|----------|------------------------------|-----------|
| TC-01 | <desc> | `<expected>` | `<actual query output>` | ✅/❌ |
| TC-02 | <desc> | `<expected>` | `<actual>` | ✅/❌ |
| TC-03 | <desc> | `<expected>` | `<actual>` | ✅/❌ |

## UAT reference dashboards — values to match (Story path)

Copied from the test plan's reference table, with the expected value now filled in from the actual
result above. Nobody has checked these on screen yet: matching them is the UAT step.

| Ref | Report · page · visual | Filters / slicers to set | Expected (from test case) | Test case |
|-----|------------------------|--------------------------|---------------------------|-----------|
| UR-01 | `<report>` · `<page>` · `<visual>` | `<Slicer> = <value>` | `<TC-0N actual>` | TC-0N |

Validate in: `<UAT workspace, or the stand-in and why>`. Data as of: `<TC run time, catalog>`.

## Not executed (hand-back to human/CI)

| ID | What's needed | Exact run | Expected outcome |
|----|---------------|-----------|-------------------|
| <e.g. "notebook run"> | full notebook run in a Databricks workspace | Human-triggered via Databricks Workflows | `<table>` row count matches TC-01 |

## Failures — root cause + next step (if any)
<For each ❌: what's wrong, suspected cause, and whether the fix (if any) falls inside this agent's
read-only scope or needs a human, or a write-capable workspace, to act on it.>

## Overall verdict
- [ ] **Findings support the proposed plan** — all applicable cases pass, gaps documented and accepted
- [ ] **Needs another round** — see failures above
- [ ] **Blocked** — on: <what>

> No pull request is drafted from this file. Results feed the advisory implementation plan and/or a
> `propose-change` proposal; a human, or a write-capable workspace, carries any actual change through
> its own repo, branch, and PR process.
