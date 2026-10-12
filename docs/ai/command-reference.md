---
description: "Every services/ CLI command's exact flags, the valid --repo-key values, where output files go, and how to search the mirror. Loaded into every session via CLAUDE.md so no run starts with --help discovery. services/tests/test_command_reference.py fails if a CLI command or flag is missing from this page."
---

# Command reference

Every command below is run the same way (see agent-instructions.md, "How to actually invoke"):

```bash
cd "<this workspace root>/services" && "<this workspace root>/<python>" -m app.cli <command> [flags]
```

Don't run `--help` to rediscover these. This page is generated from the parser and checked by a test.
Every data command prints one JSON object: `{"ok": true, ...}` or
`{"ok": false, "category": ..., "message": ...}` with exit code 1.

When ADF is off for this deployment, `pipeline-runs`/`activity-runs`/`trigger-runs` answer
`not enabled for this deployment`; when Power BI/Fabric is off, so do `dax-execute`,
`refresh-history` and `report-bindings` (see `docs/ai/client/client.md`).

## Valid `--repo-key` values

The repo keys of this deployment are listed in `docs/ai/client/client.md` (repo table), with any
repo that has more than one mirrored branch and the `--branch` value that selects the other one.
Keys use underscores; a hyphenated ADO folder name is refused. `docs/ai/client/repo-registry.md`
says what each one is.

## Where files go

Write every working file for a ticket to `scratch/<ticket-id>-<stage>.md` at the workspace root, for
example `scratch/PROJ-2085-intake.md`, `-verdict.md`, `-impact-analysis.md`, `-implementation-plan.md`,
`-test-results.md` and `-handoff.md`. Batch files go to `scratch/<ticket-id>-batch.json`. `scratch/` is
gitignored. Never use `/tmp`: Git Bash and Windows Python resolve it to different folders. Templates
are in `templates/`: `investigation-verdict`, `impact-analysis`, `implementation-plan`, `test-plan`,
`test-results`, `handoff-bug` and `handoff-story` (each `-template.md`). Proposals go to
`proposals/<ticket-id>/` (see `propose-change`).

## Searching the mirror: use these, not Grep/Read/ls on the mirror

| Need | Command |
|---|---|
| Where is the file named like X? | `mirror-find --pattern "*visit*summary*"`. Searches every tree; add `--repo-key` to scope it. Uses the sync-time manifest and lists no directories. |
| Which files mention X? | `mirror-grep --pattern "shelfcheck_withrep" --repo-key databricks --glob "*.ipynb"`. Searches notebooks as text. Without `--repo-key` it scans every tree: about 4 s from a per-tree text cache that every mirror sync rebuilds (about 40 s only if a cache is missing). Scope it when you know the tree. |
| Read a notebook | `mirror-read --repo-key databricks --path "<path>.ipynb" --as-text` gives the cells as text with outputs dropped. Add `--lines 540-600` for the part around a grep hit. Line numbers match `mirror-grep`. |
| Browse a folder | `mirror-list --repo-key <key> --path "<folder>"`. |
| What does a report show? | `report-bindings --repo-key <key> --report "<Area>/<Name>.Report" [--table <T>]` gives pages, visuals (type, title, fields per role), slicers with their saved selection, and filters, for PBIR and legacy layouts alike, in a few KB. Never read `visual.json`/`report.json` whole for this. |
| Which notebook builds table X, and what reads it? | `table-usage --table <prod_catalog>.gold.x` lists writers, then readers (notebooks, ADF pipelines/datasets, semantic models: one entry per model), each hit with its notebook cell and the line number `mirror-read --as-text --lines` uses. One call, about 10 s across every tree. Reports are left out: use `report-bindings --table` for those. |
| Which reports use these tables? | `report-bindings --table <T> [--table <T2>] --model "<model name>"` ranks reports by tables used, then visible data visuals. `--model` keeps it to reports bound to that model (table names repeat across models) and cuts an all-tree scan from ~100 s to ~12 s. |

These go through redaction. The raw `Grep`/`Read` tools on `services/mirror/` don't (OPEN_ITEMS.md
#34), and PowerShell `Get-ChildItem`/`ls` over the mirror took 20 to 55 s per call on PROJ-2063 and
PROJ-2085.

## Several independent reads? Batch them

`batch --file scratch/<ticket-id>-batch.json` runs a JSON array of argv arrays in parallel in one
process. Only read commands are allowed. See agent-instructions.md, "Batch independent reads".

## All commands

<!-- generated from app/cli.py build_parser(); test_command_reference.py keeps this in sync -->

**Coordination and governor**
- `presence-check --entity <entity> --owner <owner> [--force]`
- `presence-heartbeat --entity <entity> --owner <owner>`
- `presence-release --entity <entity> --owner <owner>`
- `governor-check --user <user> --backend <backend>`. Read-only: it reports and never uses quota.
- `governor-reset --backend <backend> [--confirmed]`. Ops only, never part of an investigation.
- `artifacts-purge [--confirmed]`. Ops only, never part of an investigation (refused in engagements). Deletes uploads, staged copies and published deliverables older than `UPLOAD_RETENTION_DAYS` / `SHARED_FILE_RETENTION_DAYS`; a dry run that lists them unless `--confirmed`.
- `job-trace --job-id <job_id>`

**Registry and lineage**
- `registry-resolve --repo-key <repo_key> [--environment <environment>]`
- `lineage-trace --node <node> [--direction <direction>] [--max-hops <max_hops>]`
- `lineage-coverage`
- `lineage-rebuild`. Rebuilds the local index from `docs/ai/client/lineage-graph.yaml`.
- `retrieve --query <query> [--scope <scope>] [--user <user>] [--limit <limit>] [--include-unsuccessful] [--include-overturned]`. Default scopes: `learnings` (team-wide, every caller's learnings) + `docs`, plus `past-engagements` (the caller's own full results) when the caller is known. By default it skips failed, cancelled and incomplete runs, and verdicts a human overturned. Each past-engagement hit carries `meta.review_outcome`; treat `unreviewed` or `partially_upheld` as a lead to re-verify, not a fact.
- `verdict-review-stats --user <user> [--limit <limit>]`. Read-only: review coverage, outcome counts and upheld rate (over reviewed verdicts only). No command writes a review; only a human can, through `POST /v1/engagements/{job_id}/review`.

**Diagrams**
- `diagram-lint --file <file>`. Checks every ` ```mermaid ` block in a markdown file renders (full `erDiagram` grammar check, other kinds reported as not linted) and reports problems by file line. Local, no governor, batchable. The `er-diagram` skill runs it on its own output before answering; `ok: false` means mermaid would show an error instead of the diagram.

**Mirror (Prod)**
- `mirror-list --repo-key <repo_key> [--environment <environment>] [--branch <branch>] [--path <path>]`
- `mirror-read --repo-key <repo_key> [--environment <environment>] [--branch <branch>] --path <path> [--as-text] [--lines <lines>]`
- `mirror-find --pattern <pattern> [--repo-key <repo_key>] [--environment <environment>] [--branch <branch>] [--limit <limit>]`
- `mirror-grep --pattern <pattern> [--repo-key <repo_key>] [--environment <environment>] [--branch <branch>] [--path-prefix <path_prefix>] [--glob <glob>] [--case-sensitive] [--max-matches <max_matches>]`
- `table-usage --table <table> [--repo-key <repo_key>] [--environment <environment>] [--branch <branch>] [--max-lines <max_lines>]`. Read, from the mirror, redacted. Matches the table's last name segment as a whole word in notebooks/scripts, ADF JSON and semantic models, and classifies each hit line as write / read / mention (a heuristic: where to look, not proof). Batchable.
- `report-bindings [--repo-key <repo_key>] [--environment <environment>] [--branch <branch>] [--report <report>] [--table <table>] [--model <model>] [--limit <limit>]`. Read, from the mirror, redacted. With `--report` (needs `--repo-key`): one report's summary, narrowed to visuals/filters on `--table` (repeatable, semantic-model table names) plus each page's slicers, and `tables_not_used` for any `--table` the report never touches. Without `--report`: a ranking of reports using `--table`, identical copies across trees grouped under `locations`. Field notation: `T.F` column, `T.F (measure)`, `T.F (report measure)` (defined in the report, not the model), `Sum(T.F)`. `hidden` pages are hidden from the tab bar, often still reached by a navigation button. Batchable.

**Live reads (Dev/UAT, only when explicitly named)**
- `live-list --user <user> --org <org> --project <project> --repo <repo> --branch <branch> [--path <path>]`
- `live-read --user <user> --org <org> --project <project> --repo <repo> --branch <branch> --path <path>`

**Databricks and Fabric**
- `databricks-select --user <user> --catalog <catalog> --statement <statement>`. Inside an engagement `--user` is replaced by the run's real caller (`SCOTRI_RUN_TOKEN`); outside one, use `--user "Scotri:<ticket-id>"`. Use `--catalog <prod_catalog>`, or `<dev_catalog>` only when Dev is named.
- `dax-execute --user <user> --workspace <workspace> --model <model> --dax <dax> [--role <role>]`
- `refresh-history --user <user> --workspace <workspace> --dataset <dataset> [--top <top>] [--refresh-id <refresh_id>]`

**Run history**. `--factory` names one of the factories in `services/config/adf_factories.yaml` (`docs/ai/client/client.md` lists them and the default). A factory without its subscription and resource group filled in answers with a configuration error. A pipeline run lives in one factory: query the factory that owns the pipeline. Only when ADF is enabled for this deployment.
- `pipeline-runs --user <user> [--environment <environment>] [--factory <factory>] --pipeline <pipeline...> --start <start> --end <end>`
- `activity-runs --user <user> [--environment <environment>] [--factory <factory>] --run-id <run_id> --start <start> --end <end>`
- `trigger-runs --user <user> [--environment <environment>] [--factory <factory>] --trigger <trigger...> --start <start> --end <end>`
- `job-runs --user <user> [--job-id <job_id>] --start <start> --end <end> [--limit <limit>]`
- `job-run-detail --user <user> --run-id <run_id> [--with-output]`

**Jira**
- `jira-get-issue --key <key> [--user <user>]`
- `jira-get-attachment --key <key> [--attachment-id <attachment_id>] [--user <user>]`. Read: downloads the ticket's attachments (all, or one) to `scratch/jira-attachments/<KEY>/`. Text is redacted; `.docx`/`.xlsx`/`.pptx` arrive as extracted `.txt`; PDFs/images as sent, for the `Read` tool. Other formats are reported in `skipped`, not saved. Batchable.
- `jira-lookup-user --query <query> [--user <user>]`
- `jira-comment --key <key> [--user <user>] [--body <body>] [--body-file <body_file>] [--wiki] [--confirmed]`. Write: `jira-write` rules apply.
- `jira-edit-comment --key <key> --comment-id <comment_id> --body <body> [--user <user>] [--confirmed]`. Write.
- `jira-create-bug --project <project> --summary <summary> --description <description> [--user <user>] [--confirmed]`. Write.
- `jira-attach --key <key> --file <file> [--filename <filename>] [--user <user>] [--confirmed]`. Write: attaches one file from `scratch/`, `proposals/` or `investigations/` (text redacted first, 10 MB cap). `jira-write` rules apply.

**Other**
- `batch --file <file> [--workers <workers>]`
- `sync-run-once`. The sync service's job, never this agent's. Refused while another sync (the schedule or the admin panel's Sync now) holds the shared sync lock.
