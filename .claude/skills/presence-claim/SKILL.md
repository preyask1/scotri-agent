---
name: presence-claim
description: "Check and register a presence claim (ticket, notebook, table) in the shared coordination store before starting an investigation, to surface when another engineer is already on the same thing. A duplicate-effort guard, not a merge-conflict guard. Use when: starting jira-intake or impact-analysis on a ticket, about to investigate a specific notebook or table, checking if someone else is already on this."
---

# Presence claim

This agent never edits files, so there is no merge conflict to prevent. The claim stops two engineers
from tracing the same bug or drafting conflicting plans for one ticket without knowing it.

## Claim shape

Stored in the same coordination store as the query governor's state:

```
{ ticket_or_entity: "PROJ-1234" | "gold.sales_summary" | "USP_BuildSalesGold",
  user: "<requesting user>",
  started_at: "<timestamp>",
  last_heartbeat: "<timestamp>" }
```

`ticket_or_entity` is the investigation's real subject: a Jira key, gold table, notebook, or Fabric
model/report name.

## Procedure

1. Check for a claim on the ticket/entity.
2. If one exists and is fresh (heartbeat within the TTL), surface it and ask before proceeding:
   > "Data.Engineer2 has been investigating PROJ-1234 for the last 12 minutes — proceed anyway?"
3. If the user proceeds, note in the work order/report that a concurrent investigation was in
   progress, so the findings can be reconciled later.
4. If there is no claim, or it has expired, register one. Inside an engagement `--owner` is
   replaced by the caller (like `--user`), and the owner's own live claim is refreshed, not reported
   as a collision, so a person repeating a question isn't asked to proceed against themselves.
5. While working, refresh the heartbeat so the claim doesn't look abandoned.
6. When the run ends, the engine releases every claim it registered (the prefetch's ticket claim and
   any you made). Outside an engagement, release your own with `presence-release`; otherwise the
   TTL (30 minutes) expires it.

## Rules

- **Advisory, never a lock.** It informs; the human decides whether to proceed.
- If the store is unreachable, say `presence check unavailable — <reason>` and let the user decide.
  Never assume nobody else is working on it.
