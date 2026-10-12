---
description: "Databricks medallion architecture for $name: catalogs, schemas, the orchestration notebooks and this agent's read boundary. Written by people; created once by app.setup render."
---

# Databricks architecture — $name

> **TODO (people):** replace every TODO below with what is true for this platform. The agent reads
> this file at the start of Databricks work; anything left vague here it has to rediscover each time.
> See `examples/acme-retail/docs/ai/client/databricks-architecture.md` for a filled-in example.

The ETL repo (`--repo-key $databricks_repo`) builds the analytics layer the reports read. This agent
reads notebook source from the read-only mirror (`adf-databricks-read`) and verifies tables
empirically with live `SELECT`/`DESCRIBE` (`databricks-query`, through the query governor). Where the
two disagree, the disagreement is itself a finding.

## Catalogs and schemas

| Catalog | Use | This agent's access |
|---|---|---|
| `$prod_catalog` | production — the default | `SELECT`/`DESCRIBE`/`SHOW` only, through the governor |
| `$dev_catalog` | dev — only when Dev is named, after a presence check | same |

Schemas: $schemas.

- **TODO:** what each schema holds (raw / cleaned / analytics-ready), and which one reports read.
- **TODO:** history conventions (e.g. SCD Type 2 columns such as `IS_CURRENT_FLAG`, `VALID_FROM`).
- **TODO:** any schema that feeds an application directly rather than through reports.

## Sources and ingestion

- **TODO:** source systems (ERP, POS, CRM, apps…) and how each lands in bronze (ADF Copy, Auto
  Loader, a vendor connector…).

## Orchestration

- **TODO:** the orchestrator notebooks or jobs, the folder layout of table-building notebooks, and the
  naming conventions (e.g. `NB_*` orchestration, `USP_*` table builders, `VW_*` views).
- **TODO:** how build order is expressed (priority folders, job task dependencies…).

## Gotchas worth knowing up front

- **TODO:** variants that look live but aren't (`_bkp`, `_v1` copies), tables written by jobs outside
  this repo, known slow or very large tables.
