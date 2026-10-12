---
name: propose-change
description: "Given a concrete fix found during impact analysis or debugging, in any product repo, write a unified diff (.patch) and a full modified copy of the affected file into proposals/<ticket-id>/ — never into the mirror or a product repo's working tree — and state that it is advisory and a human must apply it by hand. Use when: show what a fix would look like, propose a fix, draft a patch, illustrate the change, write a proposal for this ticket."
---

# Propose a change (diff + modified copy, in `proposals/` only)

Read the current file from the mirror, compute the fixed version, and write the diff and the full fixed
file into `proposals/<ticket-id>/` for a human to review and apply by hand.

## When to use

Only after `impact-analysis` (and, if relevant, a `dax-debug`/`databricks-query` test) has identified a
specific fix: a measure's DAX, a TMDL parameter, a role filter, notebook transformation logic, an ADF
Copy activity mapping. `implementation-plan` still gives the prose plan; this is for when the literal
text of the fix is more useful than a description.

## Procedure

1. **Read the current file** via `fabric-read` (Fabric) or `adf-databricks-read` (ADF, Databricks),
   with the maintenance flag clear. If the read failed or happened during a maintenance window and
   wasn't retried after it cleared, say so and stop; never fabricate content you didn't read.
2. **Compute the smallest, most surgical change.** Preserve structure and formatting exactly outside
   the changed lines: a malformed TMDL/`report.json`/`.pbir`/pipeline JSON/notebook breaks the artifact,
   and reformatting untouched lines makes the diff unreviewable.
3. **Write all three files** to `proposals/<ticket-id>/` and nowhere else (not `mirror-active/`,
   `mirror-staging/`, or anything resembling a product-repo working tree):
   - `<safe-file-name>.patch` — unified diff (`diff -u` style) with `--- a/<path>` / `+++ b/<path>`
     headers using the path **inside its own repo**, e.g.
     `Sales/Sales_Overview_Model.SemanticModel/definition/tables/Sales.tmdl`, so a
     human can `git apply` it against their clone of the right repo.
   - `<safe-file-name>` — the **full modified file**, not just changed lines, so a human whose clone
     has moved on can diff or copy it in when the patch doesn't apply cleanly.
   - `README.md` — one entry per proposed file: repo, branch, path, the ticket/finding, what changed
     and why, and the disclaimer below verbatim.
4. **State the disclaimer every time** (in the README and your reply):

   ```
   This is a PROPOSAL, not an applied change. Nothing in the mirror, or in any product repo, has been
   modified. A human must review this diff, apply it manually (e.g. `git apply patch-file` or a
   direct copy-paste of the modified file) against their own clone of <repo>@<branch>, and carry it
   through that workspace's normal review/PR process before it reaches production.
   ```

5. **Never apply it anywhere**, including the mirror: no `git apply`, `patch`, or any command that
   mutates the mirror or a product repo. To sanity-check the diff, re-derive the modified content by
   hand and compare it with the file read in step 1.

Never describe a proposal as "applied", "merged" or "pushed": it stays in `proposals/` until a human
takes it from there.
