---
description: "The single source of truth for which repos/branches exist in Acme Retail's platform, per environment, and how this agent reads each one. Generated from client/profile.yaml."
---
<!-- GENERATED from client/profile.yaml by `python -m app.setup render`. Do not edit: change the profile and re-render (SETUP.md). -->

# Repo registry — Acme Retail (read this before assuming a repo name in any skill)

This is the one place repo/branch/workspace facts live. If a skill's prose names a specific repo or
branch instead of pointing here, the skill is out of date. Machine-readable twin:
`services/config/repo_registry.yaml` (both generated from `client/profile.yaml`).

## Read-access rule (applies to every row below)

- **Prod branches are persistently mirrored** at `services/mirror/mirror-active/<repo>/<branch>/`,
  refreshed by the sync service (scheduled + manual, atomic swap). Use `mirror-list` /
  `mirror-read --repo-key <key>`: they resolve the path and check the maintenance flag.
- **Dev and UAT branches are read on demand, live, never mirrored.** A developer's Dev branch is their
  active workspace, not a standing snapshot to poll. Read it only when a ticket or user explicitly
  names that environment.
- **Before any Dev/UAT read, check `presence-claim`.** If someone holds a claim on that ticket or
  branch, say so before reading their in-progress work.
- **A live Dev/UAT read still goes through the query governor**: rate-limited and logged.
- **Default scope for `impact-analysis` and every debugging skill is Prod**, unless the ticket or the
  user names Dev or UAT. With no environment named, assume Prod and say so.
- A repo not listed here → `not yet in the repo registry — hand back to a human to confirm the
  mapping`, never a guessed name.

## Databricks

| Repo (`--repo-key`) | ADO org / project | Environment | ADO repo | Branch | Mirrored | Catalog |
|---|---|---|---|---|---|---|
| `databricks` | `acme-data` / `Data-Platform` | prod | `Acme_Databricks_WS` | `release_main` | yes | `acme_prod` |
| `databricks` | `acme-data` / `Data-Platform` | dev | `Acme_Databricks_WS` | `develop_main` | no — live read only | `acme_dev` |

## Azure Data Factory

| Repo (`--repo-key`) | ADO org / project | Environment | ADO repo | Branch | Mirrored | Catalog |
|---|---|---|---|---|---|---|
| `adf` | `acme-data` / `Data-Platform` | prod | `AcmeAdfProd` | `main` | yes | — |
| `adf` | `acme-data` / `Data-Platform` | dev | `Data-Platform` | `main` | no — live read only | — |

## Power BI / Fabric

| Repo (`--repo-key`) | ADO org / project | Environment | ADO repo | Branch | Mirrored | Catalog |
|---|---|---|---|---|---|---|
| `fabric_core` | `AcmeFabricReports` / `AcmeFabricReports` | prod | `Acme-PowerBIReports` | `main` | yes | — |
| `fabric_core` | `AcmeFabricReports` / `AcmeFabricReports` | prod | `Acme-PowerBIReports` | `hq_live` | yes | — |
| `fabric_secondary` | `AcmeFabricReports` / `AcmeFabricReports` | prod | `AcmeFabricReports` | `regional_main` | yes | — |
| `fabric_secondary` | `AcmeFabricReports` / `AcmeFabricReports` | uat | `AcmeFabricReports` | `main` | no — live read only | — |
| `fabric_hq` | `acme-data` / `Data-Platform` | prod | `PowerBI_Acme_HQ` | `main` | yes | — |
| `fabric_hq` | `acme-data` / `Data-Platform` | dev | `PowerBI_Acme_HQ` | `dev` | no — live read only | — |
| `fabric_stores` | `acme-data` / `Data-Platform` | prod | `PowerBI_Acme_Field` | `main` | yes | — |
| `fabric_stores` | `acme-data` / `Data-Platform` | dev | `PowerBI_Acme_Field` | `dev` | no — live read only | — |
| `fabric_regional` | `acme-data` / `Data-Platform` | prod | `PowerBI_Acme_Regional` | `main` | yes | — |
| `fabric_regional` | `acme-data` / `Data-Platform` | dev | `PowerBI_Acme_Regional` | `dev` | no — live read only | — |

- `fabric_core`: legacy structure; hq_live is a second, independent live content set
- `fabric_secondary`: legacy structure
- `fabric_hq`: new structure (head office)
- `fabric_stores`: new structure (field teams)
- `fabric_regional`: new structure (regions)
