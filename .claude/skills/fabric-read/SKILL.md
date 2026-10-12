---
name: fabric-read
description: "Read Fabric repo files (TMDL, report.json, .pbir, definition.pbism, etc.) for every Fabric repo/branch in the registry: Prod from the read-only mirror, Dev/UAT via on-demand live read. Use when: read this TMDL file, fetch report.json, list files in this Fabric folder, get the M partition for this table, read definition.pbir, check what's in this model's roles folder."
---

# Fabric read

Other skills (`impact-analysis`, `lineage-trace`) read Fabric files through this one rather than
re-deriving paths.

## Repos and branches

Read `docs/ai/client/repo-registry.md` first, every time; never hardcode a repo, branch or workspace name.
It lists every Fabric repo (old and new ones during a migration are all in scope), and which
branches are mirrored vs live-read. If Power BI/Fabric is off for this deployment
(`docs/ai/client/client.md`), there is nothing to read: say so.

**Which branch:** pick the one the analysis concerns. If the ticket doesn't say, check every
applicable one. A repo key with two mirrored Prod branches (`docs/ai/client/client.md` names them)
means **both**: both are live and can diverge substantially; neither is a staging copy of the other.

## Read paths

**No leading `/` in `--path`, ever.** Git Bash/MSYS silently rewrites it into a Windows path and the
call breaks. Use `""` or omit `--path` for root; otherwise a bare relative path
(`Sales/Model.SemanticModel/...`).

**Prod (mirror): `mirror-list` / `mirror-read`.**
- `--repo-key` is the registry key (from `docs/ai/client/client.md`), not the ADO folder name. The output
  echoes the resolved `repo`/`branch`; confirm it.
- Pass `--branch` only to pick a non-default branch when a repo key has two for the same
  environment (`docs/ai/client/client.md` gives the default and the `--branch` value for the other).
- A missing repo/branch/path returns `ok: false, category: not_found`. An empty `[]` means "nothing
  here" only with `ok: true`.
- The maintenance flag is checked internally. Mid-swap you get `ok: false, category: transient,
  "repo sync in progress..."`: wait, don't retry around it. The flag doesn't apply to live reads.
- Mirror reads are cheap: list and narrow first, then read the candidates. Don't read every file.
- `freshness`: stale means last sync >25h ago (default); `repo_ok: false` means that repo's last sync
  failed; `available: false` means freshness is unknown. The maintenance flag only covers the swap
  window; an old, idle mirror raises no flag.

**Dev/UAT (never mirrored): `live-list` / `live-read`.**
1. Presence check first. Surface any claim plainly: *"Data.Engineer2 has an active claim on this
   in Dev — proceed anyway?"*
2. Only when the ticket or user explicitly names Dev or UAT, never in a routine scan. Otherwise
   assume Prod and say so (or ask, per "Ask for scope" in agent-instructions.md).
3. Governed; never persist the result to disk.

## Refresh failures: `refresh-history`

The mirror shows what a model is defined to do, never whether last night's refresh worked.

- **Start here** for "did the refresh fail" or "why hasn't this report updated". Without
  `--refresh-id` it lists the last `--top` (default 10) attempts: `status`
  (Completed/Failed/Disabled/Unknown), start/end, `refreshType`, and `serviceExceptionJson` on
  failure.
- `--refresh-id` gives per-partition/table detail **only for Enhanced Refresh datasets**. For a
  Standard-refresh dataset (most of them) it returns `{"available": false, "reason": ...}`. That is
  expected, not an error; don't retry. The top-level status is all there is.
- Prod-published models only. Dev/UAT refresh history isn't wired up.
- 401/403/not-found means the service principal is not at least Viewer on the workspace
  (`docs/ai/fabric-tool-access.md`). Report a workspace-membership gap, not "refresh history isn't
  available".
- Don't stop at the status. A `Failed` refresh with a service exception is often enough. A
  `Completed` refresh that still looks wrong calls for `dax-debug` (is the value stale/wrong?) and a
  read of the model's M partition (did the source change?).

## Rules

- Cite repo, branch, exact path and read path (mirror/live) for every fact.
- Never write anything on either path, including persisting a live read.
