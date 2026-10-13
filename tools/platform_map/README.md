# Platform map

One self-contained HTML page that walks a new joiner through the client's Prod data platform, from
the broad picture down to single objects. The page is the same for every client: names, catalogs,
workspaces and tenants come from the profile, and the prose a person writes comes from an optional
notes file (see [Curated notes](#curated-notes)).

- **Zoom 1, Big picture:** the five stages (source apps → ADF → Databricks → Power BI/Fabric →
  consumers) and a "Start here" reading order.
- **Zoom 2:** one page per layer: sources and ingestion (with every schedule on a 24-hour clock),
  the lakehouse, Power BI/Fabric, apps fed from Databricks, environments and compute, glossary.
- **Zoom 3, Lineage explorer:** search any trigger, pipeline, notebook, table, model or report and
  see its upstream and downstream as columns of cards, one column per hop. You can set the depth
  each way (or press "Whole chain"), hide kinds (hidden hops are walked through and drawn dashed),
  hover to highlight neighbours, pan and zoom, and open "+N more" lists. Each direction gets a
  blast-radius summary (counts by kind, stopped triggers, every reached object with its hop and
  what it came through).
  A Cards / Mermaid diagram switch draws the same trace as a Mermaid flowchart (Mermaid loads on
  first use from jsDelivr, pinned to 10.9.1); "Copy Mermaid source" puts it on the clipboard for a
  ticket or a doc.
- **Zoom 2, Environments:** where everything lives: each repo and branch from the registry with its
  Azure DevOps organisation, project and link, the environment and what it deploys to; the ADF
  factories with subscription and resource group (`services/config/adf_factories.yaml`); the Entra
  tenants the profile names.
- **Zoom 3, Inventories:** pipelines, tables, models, reports and KPI definitions as sortable,
  filterable, paged lists. Click a row to trace it, or copy the filtered rows as CSV.

Each view has a plain `#anchor` (`#lineage`, `#inventory`, ...) for linking. A theme toggle overrides
the viewer's light or dark theme.

```bash
services/.venv/Scripts/python.exe tools/platform_map/gen_platform_map.py
```

- Writes `services/data/platform-map.html` (gitignored; `PLATFORM_MAP_PATH` moves it). `--out`
  writes somewhere else. `--lineage-db`, `--mirror-root`, `--registry`, `--client` and `--notes`
  override the defaults (`LINEAGE_DB_PATH`, `MIRROR_ROOT`, `services/config/repo_registry.yaml`,
  `services/config/client.yaml`, `docs/ai/client/platform-map-notes.json`).
- Prints one JSON line of counts. `stale_names` lists any pipeline or trigger the notes file names
  that the ADF Prod tree no longer has. Fix the notes when it isn't empty.
- **Runs itself after every sync.** At the end of each sync run that swapped at least one tree
  (scheduled, `sync-run-once`, or the admin panel's Sync now), `Container.after_mirror_sync` runs it
  once, after every tree's lineage is in the index, still under the sync lock. It's best-effort: a
  failure is logged and doesn't fail the sync. It takes about 8 s. That only happens when a sync
  runs: the scheduled sync service has to be running (`OPEN_ITEMS.md` #9).

## What comes from where

| Part of the page | Source |
|---|---|
| Models, reports, measures, RLS roles, tables, notebooks, and the edges between them | the lineage index, `services/data/lineage.db` |
| Which Fabric trees exist | `services/config/repo_registry.yaml` (mirrored Fabric entries) |
| Fabric workspace for each tree | `fabric.workspaces` in the profile, rendered into `services/config/client.yaml` |
| Triggers, schedules, pipelines, cluster use per Databricks linked service | the ADF Prod tree in the mirror |
| Report pages, hidden flags and visual counts | each `.Report` folder in the mirror (PBIR or legacy) |
| KPI and measure definitions | the index's `node_notes` (model descriptions, and the report documents under `docs/ai/client/reports/`) |
| Repos, branches, ADO organisation/project | `services/config/repo_registry.yaml` |
| ADF factories, subscription, resource group | `services/config/adf_factories.yaml` |
| Legacy vs new Fabric repos | `legacy: true` on a repo in the profile |
| Entra tenants | `fabric.tenant_id` and `adf.tenant_id` in the profile |
| Source systems, outbound apps, the main run, repo layout, glossary | the notes file, `docs/ai/client/platform-map-notes.json` |

The page reads only local, already-derived state, never a live backend. It contains names and ids
(workspaces, clusters, warehouses), but no data rows and no credentials.

## Publishing

**From the Scotri API (always current).** Open `http://<api-host>:8000/platform-map` in a browser.
It is a sign-in page with no data in it: paste a Scotri bearer token (the same one the admin panel
and scotri-ui take; a token already given in that browser tab is reused), and it loads the latest
build from `GET /v1/platform-map`, which any authenticated caller may read. Because the sync rebuilds
the file, people see the current map without anyone publishing it.

To keep a snapshot in the client's repo, write one with:

```bash
services/.venv/Scripts/python.exe tools/platform_map/gen_platform_map.py --out docs/platform-map.html
```

The sync keeps rebuilding the gitignored copy, not the snapshot, so a snapshot is only as fresh as
its last commit. It names internal workspaces and tenants: keep it in the client's private repo.

The generated file is a complete page. To share it as a claude.ai Artifact, publish it to the same
URL each time so the link stays the same.

## Curated notes

`docs/ai/client/platform-map-notes.json` is optional. `python -m app.setup render` creates an empty
skeleton once and never overwrites it; [`examples/acme-retail/docs/ai/client/platform-map-notes.json`](../../examples/acme-retail/docs/ai/client/platform-map-notes.json)
is a filled-in example. Every text field is plain text, where `` `backticks` `` become code. Without
the file, each section shows what the mirror and the profile give it.

| Key | Shown in |
|---|---|
| `tagline` | the page header |
| `locale`, `timezone`, `timezone_label` | number and date formatting; the label trigger times are read in |
| `sources[]` (`name`, `short`, `what`, `linked_service`, `type`, `ir`, `pipelines`, `gold_folder`) | Big picture, Sources |
| `ingestion` | the Sources introduction |
| `main_run_title`, `main_run[]` (`step`, `text`, `triggers`, `pipelines`) | Sources: the main scheduled run, step by step |
| `adf_naming[]`, `repo_layout[]`, `schemas[]`, `powerbi_patterns[]`, `glossary[]` (`term`, `meaning`) | the matching cards |
| `layers`, `powerbi_note`, `environments_note` | notes under the matching sections |
| `gold_runner` (`notebook`, `folder_pattern`, `order[]`) | a runner notebook that runs folders of notebooks in a fixed order: each notebook's place in the queue |
| `consumers[]` (`name`, `short`, `how`, `warn`, `pipelines`) | Big picture, Downstream apps |
| `platform[]` (`title`, `text`, `items[]`) | Environments: extra cards (workspaces, warehouses, storage, out of scope) |
| `start_here` (`trigger`, `report`, `extra[]`) | Start here; without it, the running trigger that reaches most and the smallest report |
