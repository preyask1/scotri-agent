# `proposals/` — advisory patches only, never applied by this agent

Everything under `proposals/<ticket-id>/` is output from the `propose-change` skill: a suggested fix,
written down for a human to review and apply **by hand**, in their own editor, against their own
clone of the relevant product repo. Nothing here has been applied to any product repo, committed, or
pushed — this workspace has no write access to any product repo and no push-capable git credential
configured for it, ever (see [`../agent-instructions.md`](../agent-instructions.md)).

## Layout

```
proposals/
  <TICKET-KEY>/
    README.md                 — one entry per proposed file: repo, branch, path, why, what changed
    <file-name>.patch         — a unified diff, `git apply`-able against the right repo/branch
    <file-name>               — the FULL modified file (not just the diff), for a manual copy-paste
                                 if the patch doesn't apply cleanly against a since-moved-on clone
```

One subfolder per ticket. A ticket with multiple proposed files gets multiple `.patch` +
full-copy pairs inside its one folder, all indexed by that ticket's `README.md`.

## How to use a proposal (for the human applying it)

1. Read the ticket's `README.md` here first — it names the exact repo, branch, and path the patch
   targets, and the reasoning behind the change.
2. Clone (or use your existing clone of) that repo, on that branch.
3. Either `git apply <file-name>.patch` from the repo root, or open the full modified file here and
   copy/diff it against your own working copy manually.
4. Review the change like any other code change — this agent did not review it against a running
   system beyond the read-only test cases recorded in the matching `test-results/` file, if any.
5. Carry it through that repo's own branch → PR → review → merge process. This workspace has no part
   in that process and takes no further action once the proposal is written.

## What this is not

- **Not a queue of pending changes this agent will apply later.** There is no "apply" step anywhere
  in this workspace, ever, for any proposal, no matter how small or how explicitly a user asks.
- **Not committed or pushed by this agent** — files land here via the local filesystem only; nothing
  in `propose-change` runs `git add`/`commit`/`push`, and no credential capable of doing so exists in
  this deployment.
- **Not a substitute for impact analysis or the implementation plan** — a proposal illustrates one
  concrete edit; the plan document (`templates/implementation-plan-template.md`) is still the record
  of the broader recommended approach, risk, and what wasn't independently verified.
