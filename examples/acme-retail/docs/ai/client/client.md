---
description: "Client facts for Acme Retail: display name, interpreter, components, catalogs, Jira projects, repo keys, workspaces and factories. Generated from client/profile.yaml."
---
<!-- GENERATED from client/profile.yaml by `python -m app.setup render`. Do not edit: change the profile and re-render (SETUP.md). -->

# Client facts: Acme Retail

The deployment-specific values the operating rules and skills refer to. Generated from
`client/profile.yaml`; the domain docs next to this file are written by people.

## Your name

In everything a user reads (answers, deliverables, Jira comments), call yourself **Scotri**.

## Running commands

The interpreter is `services/.venv/Scripts/python.exe`, so every command is:

```bash
cd "<this workspace root>/services" && "<this workspace root>/services/.venv/Scripts/python.exe" -m app.cli <command> [args...]
```

## Components

| Component | In this deployment |
|---|---|
| Databricks | on |
| Jira | on |
| Azure Data Factory | on |
| Power BI / Fabric | on |

## Databricks

- Prod catalog: `acme_prod`, the default for every query.
- Dev catalog: `acme_dev`, only when Dev is named (presence check first).
- No UAT catalog: a UAT question about Databricks data is a dev-catalog question.
- Schemas: `brnz`, `slvr`, `gold`, `insights`.
- Query workspace: `adb-1111111111111111.11.azuredatabricks.net`.
- In the skills and examples, `<prod_catalog>` means `acme_prod` and `<dev_catalog>` means `acme_dev`.
- Secondary workspace `adb-2222222222222222.4.azuredatabricks.net` (hosts the nightly jobs that write the Prod gold tables): use it **only** from `bug-investigation`'s pipeline-failure chain trace, when that trace lands on a job hosted there. Never from `impact-analysis`, never for Story work. Same read-only restrictions.

## Jira

- Site: https://acme.atlassian.net
- Projects: `ACME` (primary: `ACME`). Ticket keys look like `ACME-123`.

## Repos (`--repo-key` values)

| `--repo-key` | Kind | ADO repo | Mirrored (Prod) | Live read only | Note |
|---|---|---|---|---|---|
| `databricks` | databricks | Acme_Databricks_WS | `release_main` (prod) | `develop_main` (dev) |  |
| `adf` | adf | (per environment) | `main` (prod, AcmeAdfProd) | `main` (dev, Data-Platform) |  |
| `fabric_core` | fabric | Acme-PowerBIReports | `main` (prod), `hq_live` (prod) | — | legacy structure; hq_live is a second, independent live content set |
| `fabric_secondary` | fabric | AcmeFabricReports | `regional_main` (prod) | `main` (uat) | legacy structure |
| `fabric_hq` | fabric | PowerBI_Acme_HQ | `main` (prod) | `dev` (dev) | new structure (head office) |
| `fabric_stores` | fabric | PowerBI_Acme_Field | `main` (prod) | `dev` (dev) | new structure (field teams) |
| `fabric_regional` | fabric | PowerBI_Acme_Regional | `main` (prod) | `dev` (dev) | new structure (regions) |

`fabric_core` has more than one mirrored branch: `main` is the default; pass `--branch hq_live` for the other.

Repo keys use underscores. Full read rules: [`repo-registry.md`](repo-registry.md).

## Fabric workspaces

| Workspace | Synced from |
|---|---|
| `ACME-Fabric-Prod` | `fabric_core` / `main` |
| `ACME-Fabric-HO-Prod` | `fabric_core` / `hq_live` |
| `ACME-Fabric-Prod-2` | `fabric_secondary` / `regional_main` |
| `ACME-HQ-Prod` | `fabric_hq` / `main` |
| `ACME-Field-Prod` | `fabric_stores` / `main` |
| `ACME-Regional-Prod` | `fabric_regional` / `main` |

Pass these names to `dax-execute`/`refresh-history --workspace`; never a `WorkspaceName` parameter found in TMDL.

## ADF factories

`--factory` takes one of `acme-adf-prod`, `acme-ingest-adf`, `acme-analytics-adf`; `acme-adf-prod` is the default. A pipeline run lives in one factory: query the factory that owns the pipeline.

## Domain docs

Written by people, next to this file: [`databricks-architecture.md`](databricks-architecture.md), [`fabric-architecture.md`](fabric-architecture.md), [`fabric-conventions.md`](fabric-conventions.md), [`lineage-map.md`](lineage-map.md), [`lineage-graph.yaml`](lineage-graph.yaml), [`platform-map-notes.json`](platform-map-notes.json), [`reports/sources.yaml`](reports/sources.yaml).
