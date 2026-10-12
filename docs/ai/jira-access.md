---
description: "How this agent connects to Jira (Atlassian Cloud) to read stories/BRDs/bugs -- REST with a service-account API token, not an interactive OAuth MCP. One account serves reads and the one narrow, confirm-gated write exception (comment / edit-own-comment / create-Bug / attach-file), via the jira-write skill."
last-validated-commit: n/a — reference doc
---

# Jira access — REST, single service account (no OAuth MCP)

Scoped to the data team's primary project **PROJ** plus any projects listed in
`SCOTRI_JIRA_PROJECTS`. An OAuth Atlassian MCP server (browser sign-in) would carry write scopes gated
only by a behavioural rule. This workspace uses a plain REST service account instead: no browser
sign-in, and every write goes through the confirm-first `jira-write` path in the app.

## Connection

- **Auth:** Basic auth against the Atlassian API gateway
  (`https://api.atlassian.com/ex/jira/<cloudId>/rest/api/3/...`), credential `JIRA_EMAIL` +
  `JIRA_API_TOKEN`, both vault-sourced. `JIRA_CLOUD_ID` comes from
  `https://<site>.atlassian.net/_edge/tenant_info` (no auth needed).
- **No MCP, no browser OAuth, no per-user token cache.**
- Every call goes through the governed `jira-get-issue` / `jira-get-attachment` commands — never a raw
  `curl`.

## Reading a story

1. `jira-get-issue --key <KEY>` returns summary, description, comments, attachment metadata, issue
   type, status and priority. Read the description, not just the summary line.
2. **Attachments** (BRDs, mockups, samples): `jira-get-attachment --key <KEY> [--attachment-id <id>]`
   saves readable, redacted copies under `scratch/jira-attachments/<KEY>/`. `.docx`/`.xlsx`/`.pptx`
   arrive as extracted text; raw-data exports are refused rather than copied.
3. **Bug vs Story** comes from the `issuetype` field, never from re-reading the body.

## Handy JQL
```sql
-- Open dev stories/CRs
project = PROJ AND issuetype IN (Story, Task) AND statusCategory != Done

-- Bugs needing investigation
project = PROJ AND issuetype = Bug AND status IN ("Info Required", "In Dev")
```

## Writing back — one narrow, confirm-gated exception

The same account writes, but only through `jira-write`, and only four actions exist: **comment**,
**edit a comment Scotri posted**, **create a Bug** (from a confirmed-bug verdict, if no ticket already
covers it) and **attach a file** Scotri wrote or the caller uploaded. Each is drafted, shown, and
submitted only after explicit approval of that exact content (`--confirmed`). Nothing else exists: no
field/status edits, no Story creation, no transitions, no deletes. On an unattended run, nothing is
written. Full rules: [`../../.claude/skills/jira-write/SKILL.md`](../../.claude/skills/jira-write/SKILL.md).

## Tie-in to the rest of this workspace

After reading a ticket:
1. Restate the requirement with an explicit gaps/ambiguities section (`jira-intake`).
2. For a Bug, render a verdict first (`bug-investigation`).
3. Run `impact-analysis` across every repo in the registry.
4. Write and run the test plan; optionally an advisory implementation plan or a `propose-change`
   patch. Never a PR, never a transition.
