---
name: adf-databricks-read
description: "Read ADF pipeline/dataset/trigger/linkedService JSON and Databricks notebook source (USP_*, NB_*, VW_*): Prod from the read-only mirror, Dev via on-demand live read. Use when: read this pipeline definition, find the notebook that builds this table, check a linked service, read a trigger schedule, find a USP_/NB_/VW_ notebook, check the Copy activity mapping."
---

# ADF + Databricks read

## Repo layout (check `docs/ai/client/repo-registry.md` every time; don't hardcode names)

- **ADF Dev and Prod are two separate repos**, not two branches of one repo. The ADF Dev repo shares
  its name with the ADO project; that is a coincidence, not a self-reference.
- **Databricks** is one repo with two branches: `release_main` and `develop_main`.
- Neither has a UAT tier.

## Read paths

**No leading `/` in `--path`, ever.** Git Bash/MSYS silently rewrites it into a Windows path and the
call breaks. Use `""` or omit `--path` for root; otherwise a bare relative path
(`pipeline/PL_Gold_Orchestrator.json`).

**Prod (mirror): `mirror-list` / `mirror-read`.**
- `--repo-key` is the registry key (`adf`, `databricks`), not the ADO folder name. The output echoes
  the resolved `repo`/`branch`.
- A missing repo/branch/path returns `ok: false, category: not_found`, never a silent empty list.
- Mirror reads are cheap: list the folder first and narrow, rather than guessing a file name from
  convention.
- `freshness`: stale means last sync >25h ago (default); `repo_ok: false` means that repo's last sync
  failed; `available: false` means freshness is unknown, so don't assert the mirror is current either.
  The maintenance flag only covers the swap window; an old, idle mirror raises no flag.

**Dev (never mirrored): `live-list` / `live-read`.**
1. Presence check first: the Dev ADF repo and `develop_main` are where engineers actively work.
2. Only when the ticket or user explicitly names Dev. Otherwise it's Prod; say so.
3. Keep it narrow. `<dev_catalog>` and its warehouse share compute with the developer working there.
4. Never persist a live read to disk.

## ADF: what to read

- Read the root `pipeline/`, `dataset/`, `trigger/` and `linkedService/` JSON. **Never** read or edit
  the generated ARM-template folder or treat it as source; it is build output.
- A Copy activity gives source/sink dataset names and the column mapping.
- A trigger's JSON gives the real schedule (`recurrence`/`interval`). Never infer it from the name.
- List the folder to narrow to the pipelines the ticket names before reading bodies.

## ADF failures: mirror for definitions, run history for what happened

`pipeline-runs`, `activity-runs`, `trigger-runs` (flags in command-reference.md):

- `--pipeline` and `--trigger` are mandatory and non-empty. Unfiltered, the API returns only the
  newest ~100 runs across the whole factory.
- **Zero pipeline runs does not mean the pipeline failed.** A disabled, paused or non-firing trigger
  produces no runs. Follow the `hint`: run `trigger-runs` for the trigger(s) that should start it
  (get candidate names from the trigger JSON in the mirror first) and read the trigger's `enabled`
  flag from the mirror.
- **Zero trigger runs is also two-valued:** disabled/paused vs enabled-but-didn't-fire. Pair it with
  the `enabled` flag.
- Window capped at 14 days. To cover more, narrow to fewer pipelines or a smaller window; don't ask
  for a bigger cap.
- Prod only; Dev factories aren't wired up. The factories and the default are listed in
  `docs/ai/client/client.md`. A run exists only in its own factory, so find the pipeline's factory
  first (`mirror-find`, or try each) before reading an empty result as "never ran".
- 401/403 means the service principal's role grant on that factory is missing or wrong. Report that,
  not "run history unavailable".
- `pipeline-runs` says a run failed; `activity-runs` (with its `runId`) says why: per-activity
  `status`/`error`/retries, e.g. `UserErrorFileNotFound`.
- **For a failed Databricks-invoked activity (Notebook/Python/JAR/Job), read `activity-runs`'
  `error.message` fully first.** ADF relays the job's own failure text using its linked-service
  credential, so you get it with no Databricks access at all, even for a workspace this deployment
  can't reach. A Databricks access gap is usually not a blocker at this level. You need Jobs API
  access to that specific workspace only for more than the relayed text (full stack trace, task
  output). A shared storage account or metastore doesn't substitute: Jobs-API run permissions are
  per-job at the workspace level, separate from catalog/data grants.

Run history says *what* failed. These rules find the *why* and the blast radius:

- **Enumerate pipeline names, don't guess.** `mirror-list --repo-key adf --path pipeline`, then narrow
  by name (`forecast`, `sales`, ...). "The forecast pipeline" is usually several: list them, say which you
  assume, and pass that list to `--pipeline`. `Old_*`/`Old *` variants are usually dead; confirm via
  triggers before ignoring them.
- **A failing Copy activity's file is a symptom; find who writes it.** For a missing-file error,
  search `pipeline/*.json` for the file/path string to find its producer. Flag files (`trigger.csv`,
  `*_successfull.csv`) mean "an upstream stage finished", so the producer's health is the real
  question.
- **Find the caller of a pipeline that isn't scheduled directly.** Search `pipeline/` and `trigger/`
  for its name. The usual caller is an `ExecutePipeline` activity in a parent, which is often the one
  on a schedule. Keep walking up until you reach a `trigger/*.json`.
- **A parent's status is not proof its child was fine.** `PL_Store_Sales_Master` once reported
  Succeeded while its child `Store_Sales_Process` had failed, even though the `ExecutePipeline`
  activity had `waitOnCompletion: true`. The mechanism is unexplained (possibly a run-window
  mismatch); don't assume one. Query the child with `pipeline-runs` separately. If you can't (e.g.
  outside the 14-day cap), list it as "unchecked", not clean.
- **Repeated `Failed` entries for one `activityName` are retries** of one incident (per its
  `policy.retry`), not separate failures.
- **Line trigger schedules up against failure times.** Read `typeProperties.recurrence` (`schedule`
  hours/minutes, `timeZone`) and convert to UTC, or compare `runStart`/`runEnd` directly. If failure A
  precedes B and B's trigger depends on A's output, report a **suspected cascade**, not two unrelated
  defects.
- **Trail goes cold** (a Databricks job/workspace this deployment's host doesn't cover, or a
  permission wall): report job name/ID, workspace host and task name, and hand off. Don't try other
  auth routes. A secondary workspace named in `docs/ai/client/client.md` is limited as
  agent-instructions.md says.

## Databricks job failures: `job-runs` / `job-run-detail`

These give the task's real error and notebook output, deeper than ADF's relayed message.

- `job-runs`: runs in a window, newest first, optionally one `--job-id`. Window mandatory and capped
  at 14 days, `--limit` capped; an unbounded pull is refused.
- `job-run-detail`: one run's structure (per-task state, cluster/notebook). `--with-output` returns a
  task run's `error`/`error_trace`/`logs`/`notebook_output`. Pass the **task-level** `run_id`, not the
  parent job run id.
- Read-only, governed on `Backend.DATABRICKS`, the query workspace in `docs/ai/client/client.md` only. No
  submit/cancel/repair. A 403 means the SP lacks a Jobs-API read grant (View/CAN_VIEW) on this
  workspace; its Unity Catalog `SELECT` grant doesn't cover the Jobs API. Report it as that, not as
  a missing credential.
- Doesn't cover any other workspace (including a secondary one); a chain landing there hands off with
  job id, workspace host and task name.
- **Which job wrote this table?** Ask the table, not the job list: `SELECT version, timestamp,
  operation, job.jobId, job.runId, notebook.notebookId, clusterId FROM (DESCRIBE HISTORY <table>)
  ORDER BY version DESC LIMIT 10` names the writer of each version, and when it ran. Then
  `job-run-detail --run-id` on that run. An empty `job-runs` window proves nothing on its own.
- **Jobs on another workspace.** When the jobs that write a table run on a workspace this agent
  can't read (`docs/ai/client/client.md` says whether a secondary workspace hosts them), `job-runs`/
  `job-run-detail` won't show them: a `not_found` for a job or run the table's history names means
  exactly that. Report the job and run ids from `DESCRIBE HISTORY` as a hand-off ("needs a run-detail
  read on <that workspace>"), lower confidence accordingly, and never conclude the job doesn't exist.

## Databricks: what to read

- Find the `USP_*`/`NB_*`/`VW_*` notebook that produces a gold table with `table-usage --table
  <name>` (writers, with cell and line numbers), not repeated greps. Then read the body for the real logic: join predicates, filters,
  grouping/mapping, not just the target name.
- Respect the notebook's `Wave<N>` build order when reasoning about when a table is populated.
- For SCD2 gold tables, follow `docs/ai/databricks-conventions.md` if present, or the notebook's own
  `MERGE INTO ... WHEN MATCHED` clause.
- Default to `release_main` (matches live `gold.*`). Read `develop_main` only for a Dev-branch change in
  progress.

## Procedure

1. Resolve repo, branch and read path from the registry.
2. List the relevant folder before reading file bodies.
3. Read and search. Broad search is fine on the mirror; keep live Dev reads targeted.
4. Cite source, branch and read path (mirror/live) for every finding.
5. Corroborate with `databricks-query` (`<prod_catalog>` for Prod, `<dev_catalog>` for Dev). The notebook shows
   intended logic; the query shows what the table holds now. Deploys lag, so a disagreement is
   itself a finding.

Don't stop at "I can't see run history": the mirror's definitions already narrow the candidate
pipelines, the producer chain and the trigger timing.
