---
description: "Fabric Git integration, Entra tenants and the doc index for this deployment. Generated from client/profile.yaml."
---
<!-- GENERATED from client/profile.yaml by `python -m app.setup render`. Do not edit: change the profile and re-render (SETUP.md). -->

# Repo map — workspaces, tenants and the doc index

Which repos and branches exist is in [`repo-registry.md`](repo-registry.md). This file covers how they connect to the rest of the platform.

## Fabric Git integration — workspace → repo/branch

| Fabric workspace | Repo | Branch |
|---|---|---|
| `ACME-Fabric-Prod` | `fabric_core` | `main` |
| `ACME-Fabric-HO-Prod` | `fabric_core` | `hq_live` |
| `ACME-Fabric-Prod-2` | `fabric_secondary` | `regional_main` |
| `ACME-HQ-Prod` | `fabric_hq` | `main` |
| `ACME-Field-Prod` | `fabric_stores` | `main` |
| `ACME-Regional-Prod` | `fabric_regional` | `main` |

A workspace with no Git integration cannot be read from the mirror at all; reaching it would need the Fabric API directly.

## Entra tenants

The data plane and the BI plane may sit in different Entra tenants; each credential names its own.

| Plane | Used for |
|---|---|
| Azure DevOps | ADO Git REST, used only by the sync service to refresh the mirror (read-only PAT) |
| Databricks | Databricks SDK, service-principal M2M OAuth, via the query governor |
| Azure Data Factory | ARM run-history reads by the `ADF_*` app registration (`ADF_TENANT_ID`), via a federated credential |
| Power BI / Fabric | Fabric REST (`dax-debug`, `refresh-history`) by the Fabric service principal (`FABRIC_TENANT_ID`) |

🔴 **A wrong-tenant token returns HTTP 203 with an HTML sign-in page, not a 401.** Treat an HTML response as a wrong tenant, never as an empty result.

## Which doc covers what

| Topic | Doc |
|---|---|
| Client facts (catalogs, projects, repo keys) | [`client.md`](client.md) |
| Repo/branch/environment registry | [`repo-registry.md`](repo-registry.md) |
| Databricks architecture | [`databricks-architecture.md`](databricks-architecture.md) |
| Fabric architecture | [`fabric-architecture.md`](fabric-architecture.md) |
| Fabric conventions | [`fabric-conventions.md`](fabric-conventions.md) |
| Cross-repo lineage | [`lineage-map.md`](lineage-map.md) |
| Tool access (mechanics) | [`../fabric-tool-access.md`](../fabric-tool-access.md) |
| Jira access | [`../jira-access.md`](../jira-access.md) |
