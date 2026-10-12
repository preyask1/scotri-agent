---
description: "How data reaches a Power BI report in $name's platform: gold tables -> semantic model M partitions -> DAX -> visuals, and the consumption paths. Written by people; created once by app.setup render."
---

# Power BI / Fabric architecture — $name

> **TODO (people):** fill in the sections below. Example:
> `examples/acme-retail/docs/ai/client/fabric-architecture.md`.

Reports and semantic models live in the Fabric repos $fabric_repos (see
[`repo-registry.md`](repo-registry.md)). This agent never writes to them; it reads them from the
mirror (`fabric-read`, `report-bindings`) and evaluates DAX read-only against published models
(`dax-debug`).

## What lives where

| Repo | Contents |
|---|---|
| TODO | TODO: which report families and models each repo/branch holds |

## How a semantic model pulls data

Each table's **M partition** imports from a Databricks table via `Databricks.Catalogs(...)`. Read the
catalog from the `CatalogName`/`SchemaName` parameters (`definition/expressions.tmdl`) or the literal
in the partition — never from an M variable name, which can be a leftover from a dev-era copy.

- **TODO:** parameterised vs hardcoded partitions: which models use which.
- **TODO:** whether reports' M queries point at a different Databricks workspace host than the one the
  agent queries (fine if they share one Unity Catalog metastore).

## Consumption paths

- **TODO:** Power BI Service, an embedded app, or both; how RLS differs between them.
